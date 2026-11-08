# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Easy Speed Slider"
# description = "Ползунок скорости внешней стенки; остальные параметры — множителем к ней или в мм/с, с пояснениями. Заметки и режимы сохраняются для каждого пресета. Проверка полосы избегания резонанса (VFA) и объёмного расхода. RU/EN."
# author = "you"
# version = "0.7.1"
# type = "script"
# ///
"""Easy Speed Slider — стенка ползунком, остальное множителем к ней или мм/с.

Модель:

    скорость внешней стенки = ползунок (мм/с)
    для остальных строк:
        режим «×»    : значение_i = ползунок × ratio_i
        режим «мм/с» : значение_i = заданная скорость (не едет за ползунком)

По умолчанию у каждой строки режим «×», а ratio_i снят с пресета:
ratio_i = база_i / база_внешней_стенки. Поэтому при ползунке на текущем
значении стенки и нетронутых строках применение ничего не меняет — это
исходная пропорция профиля.

Каждую строку можно переключить в «мм/с» и вписать абсолютную скорость;
она останется такой при любом положении ползунка.

Считаем всегда от базы, поэтому ползунок можно двигать туда-обратно, а
«Сбросить к базе» возвращает исходную пропорцию.

Страница двуязычная (RU/EN), выбор языка запоминается в приватной папке
плагина (orca.host.plugin.storage()) вместе с примечаниями и режимами строк.

Страница сверяет скорость внешней стенки с полосой избегания резонанса (VFA):
из printer-пресета читаются resonance_avoidance, min_resonance_avoidance_speed
и max_resonance_avoidance_speed, и если выставленная скорость попадает в полосу,
появляется предупреждение с тем, на какой скорости слайсер реально напечатает
стенку (нижняя половина полосы прижимается к минимуму, верхняя — к максимуму).

Ещё страница оценивает предел скорости по объёмному расходу филамента:
filament_max_volumetric_speed / (ширина_линии * высота_слоя * flow-ratio).
Это оценка (адаптивная высота слоя и Arachne не учитываются), но она показывает,
где слайсер молча срежет выставленную скорость.

Запись идёт через orca.host.preset.process (begin_update -> set -> commit) —
этот API есть в 2.5.0-beta/rc, в вики он пока не описан. Правки применяются к
текущему активному process-пресету через обычный путь изменения настроек и
оставляют его "изменённым" (dirty): сохраняет пользователь.

Скорости-проценты (small_perimeter_speed, overhang_*_speed) сюда не входят:
они уже привязаны к outer_wall_speed на стороне слайсера (ratio_over) и едут
за ним сами.
"""

import json
import os
import threading
import time

import orca

ANCHOR = "outer_wall_speed"
CAPABILITY_NAME = "ESS"
DEFAULT_LANG = "ru"
LANGS = ("ru", "en")

# Остальные строки: ключ настройки, подпись RU, подпись EN.
# Порядок = порядок строк в таблице; добавить/убрать параметр можно одной строкой.
ROWS = [
    ("inner_wall_speed", "Внутренняя стенка", "Inner wall"),
    ("sparse_infill_speed", "Разреженный заполнитель", "Sparse infill"),
    ("internal_solid_infill_speed", "Сплошной заполнитель", "Internal solid infill"),
    ("top_surface_speed", "Верхняя поверхность", "Top surface"),
    ("gap_infill_speed", "Заполнение зазоров", "Gap infill"),
    ("support_speed", "Поддержки", "Support"),
    ("support_interface_speed", "Интерфейс поддержек", "Support interface"),
    ("bridge_speed", "Мосты", "Bridges"),
]

MIN_SPEED = 1.0                 # def->min для скоростных опций в PrintConfig
MAX_SPEED_SETTING = 1000.0
MIN_RATIO = 0.01
MAX_RATIO = 50.0
STORE_FILE = "speed_slider.json"
WORKER_NAME = "speed-slider"

# Избегание резонанса (VFA) — опции printer-пресета: вкладка принтера,
# «Resonance Compensation» / «Борьба с вертикальными артефактами».
VFA_ENABLED = "resonance_avoidance"
VFA_MIN = "min_resonance_avoidance_speed"
VFA_MAX = "max_resonance_avoidance_speed"

# Объёмный расход: лимит филамента (мм³/с) и ширины линий по ролям.
# speed-ключ -> (ключ ширины линии, ключ ролевого flow-ratio или None).
FLOW_LIMIT = "filament_max_volumetric_speed"
FLOW_ROWS = {
    "outer_wall_speed": ("outer_wall_line_width", "outer_wall_flow_ratio"),
    "inner_wall_speed": ("inner_wall_line_width", "inner_wall_flow_ratio"),
    "sparse_infill_speed": ("sparse_infill_line_width", "sparse_infill_flow_ratio"),
    "internal_solid_infill_speed": ("internal_solid_infill_line_width",
                                    "internal_solid_infill_flow_ratio"),
    "top_surface_speed": ("top_surface_line_width", "top_solid_infill_flow_ratio"),
    "gap_infill_speed": ("line_width", "gap_fill_flow_ratio"),
    "support_speed": ("support_line_width", "support_flow_ratio"),
    "support_interface_speed": ("support_line_width", "support_interface_flow_ratio"),
    "bridge_speed": ("bridge_line_width", "bridge_flow"),
}


# --------------------------------------------------------------------------- #
#  Мелкие помощники
# --------------------------------------------------------------------------- #

def _num(value):
    """45.0 -> '45', 45.5 -> '45.5'."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _parse_list(raw):
    """'60' -> [60.0]; '60,60' -> [60.0, 60.0]; '50%'/None -> None."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        values = []
        for item in raw:
            try:
                values.append(float(item))
            except (TypeError, ValueError):
                return None
        return values or None
    text = str(raw).strip()
    if not text or "%" in text:
        return None
    values = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            return None
        try:
            values.append(float(part))
        except ValueError:
            return None
    return values or None


def _fmt_list(values):
    return ",".join(_num(v) for v in values)


def _flag(raw):
    """Сериализованное значение coBool ('1'/'0', True/False) -> bool."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _read_vfa(bundle):
    """Полоса избегания резонанса из printer-пресета: {enabled, min, max}.

    Опции resonance_avoidance / min_resonance_avoidance_speed /
    max_resonance_avoidance_speed живут в printer-пресете, поэтому читаем
    full_config: в нём printer-конфиг уже смёржен. Если сборка их не знает
    (или значения выключены/некорректны) — полоса считается выключенной.
    """
    def value(key):
        try:
            return bundle.full_config_value(key)
        except Exception:
            return None

    enabled = _flag(value(VFA_ENABLED))
    lo = _parse_list(value(VFA_MIN))
    hi = _parse_list(value(VFA_MAX))
    min_speed = lo[0] if lo else 0.0
    max_speed = hi[0] if hi else 0.0
    if min_speed <= 0 or max_speed <= min_speed:
        enabled = False
    return {"enabled": enabled, "min": min_speed, "max": max_speed}


def _first_float(raw):
    values = _parse_list(raw)
    return values[0] if values else None


def _float_or_percent_mm(raw, nozzle):
    """ConfigOptionFloatOrPercent -> мм; 0/мусор -> None (0 = «авто»)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("%"):
        try:
            percent = float(text[:-1].replace(",", "."))
        except ValueError:
            return None
        return nozzle * percent / 100.0 if nozzle and nozzle > 0 else None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def _resolve_line_width(value, key, nozzle):
    """Ширина линии в мм; 0/авто -> «Default» -> диаметр сопла."""
    width = _float_or_percent_mm(value(key), nozzle)
    if width:
        return width
    width = _float_or_percent_mm(value("line_width"), nozzle)
    return width if width else (nozzle if nozzle and nozzle > 0 else None)


def _read_flow(bundle):
    """Оценка предела скорости по объёмному расходу: {enabled, limit, layerHeight, maxSpeeds}.

    OrcaSlicer в G-code режет скорость так:
        speed = min(speed, filament_max_volumetric_speed / mm3_per_mm),
    где mm3_per_mm ≈ ширина_линии * высота_слоя * flow-ratio.
    Ширины «0/авто» и проценты резолвим по соплу; это всё равно оценка:
    адаптивная высота слоя, Arachne и переменная ширина в расчёт не входят.
    """
    def value(key):
        try:
            return bundle.full_config_value(key)
        except Exception:
            return None

    empty = {"enabled": False, "limit": 0.0, "layerHeight": 0.0, "maxSpeeds": {}}

    limits = [v for v in (_parse_list(value(FLOW_LIMIT)) or []) if v > 0]
    layer_height = _first_float(value("layer_height"))
    nozzle_values = _parse_list(value("nozzle_diameter")) or []
    nozzle = nozzle_values[0] if nozzle_values else 0.0
    if not limits or not layer_height or layer_height <= 0 or nozzle <= 0:
        return empty

    # Худший случай: самый жёсткий лимит среди филаментов (AMS может печатать любым).
    limit = min(limits)
    limit_index = limits.index(limit)
    filament_ratios = _parse_list(value("filament_flow_ratio")) or [1.0]
    filament_ratio = (filament_ratios[limit_index]
                      if limit_index < len(filament_ratios) else 1.0)
    print_flow = _first_float(value("print_flow_ratio")) or 1.0
    base_factor = print_flow * filament_ratio
    if base_factor <= 0:
        return empty

    other_flow = _flag(value("set_other_flow_ratios"))
    max_speeds = {}
    for speed_key, (width_key, role_ratio_key) in FLOW_ROWS.items():
        width = _resolve_line_width(value, width_key, nozzle)
        if not width:
            continue
        factor = base_factor
        if other_flow and role_ratio_key:
            role_ratio = _first_float(value(role_ratio_key))
            if role_ratio and role_ratio > 0:
                factor *= role_ratio
        if factor <= 0:
            continue
        max_speed = limit / (width * layer_height * factor)
        if max_speed > 0:
            max_speeds[speed_key] = round(max_speed, 1)
    if not max_speeds:
        return empty
    return {
        "enabled": True,
        "limit": round(limit, 2),
        "layerHeight": round(layer_height, 3),
        "maxSpeeds": max_speeds,
    }


class _Row:
    """Настройка одной строки: режим, множитель к стенке или абсолютная скорость."""

    __slots__ = ("mode", "ratio", "speed", "note")

    def __init__(self, mode, ratio, speed, note=""):
        self.mode = mode          # "ratio" | "speed"
        self.ratio = ratio        # множитель к внешней стенке
        self.speed = speed        # абсолютная скорость, мм/с
        self.note = note

    def effective(self, anchor_speed):
        return anchor_speed * self.ratio if self.mode == "ratio" else self.speed

    def as_json(self):
        return {
            "mode": self.mode,
            "ratio": round(self.ratio, 4),
            "speed": round(self.speed, 2),
            "note": self.note,
        }


class _State:
    """Состояние capability: воркер, очередь команд, база, строки, язык."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.worker = None

        self.pending_value = None
        self.pending_rows = None
        self.pending_notes = None
        self.pending_lang = None
        self.pending_rebind = False

        self.anchor = []                  # база outer_wall_speed
        self.value = 0.0                  # текущий ползунок (экструдер 1)
        self.base = {}                    # key -> [float]
        self.rows = {}                    # key -> _Row
        self.lang = DEFAULT_LANG
        self.vfa = {"enabled": False, "min": 0.0, "max": 0.0}
        self.flow = {"enabled": False, "limit": 0.0, "layerHeight": 0.0, "maxSpeeds": {}}

        self.storage_dir = None
        self.storage_failed = False
        self.store = None


# --------------------------------------------------------------------------- #
#  Страница
# --------------------------------------------------------------------------- #

PAGE_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  .ss-wrap { font-family: var(--orca-font, -apple-system, "Segoe UI", sans-serif);
             color: var(--orca-fg, #e6e6e6); background: var(--orca-bg, transparent);
             padding: 14px 18px; max-width: 1120px; }
  .ss-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .ss-title { font-size: 18px; font-weight: 600; letter-spacing: .2px; }
  .ss-sub { font-size: 12px; color: var(--orca-muted, #9a9a9a); }
  .ss-right { display: flex; align-items: center; gap: 14px; }
  .ss-lang { display: inline-flex; border: 1px solid var(--orca-border, #444); border-radius: 4px; overflow: hidden; }
  .ss-lang button { border: 0; background: transparent; color: inherit; cursor: pointer;
        padding: 3px 8px; font-size: 11px; }
  .ss-lang button.on { background: var(--orca-accent, #4a90d9); color: var(--orca-accent-fg, #fff); }
  .ss-slider-row { display: flex; align-items: center; gap: 12px; margin-top: 16px; }
  .ss-slider-row label { font-size: 13px; min-width: 128px; }
  .ss-slider-row input[type=range] { width: 320px; flex: 0 0 320px;
        accent-color: var(--orca-accent, #4a90d9); }
  .ss-slider-row input[type=number] { width: 84px; padding: 4px 6px; text-align: right;
        font: inherit; font-size: 13px; color: inherit; background: transparent;
        border: 1px solid var(--orca-border, #444); border-radius: 4px; }
  .ss-slider-row input.ss-warn { border-color: #d9a441; }
  .ss-slider-row input[type=range].ss-warn { accent-color: #d9a441; }
  .ss-vfa { margin-top: 8px; font-size: 12px; min-height: 16px; line-height: 1.4;
            color: var(--orca-muted, #9a9a9a); }
  .ss-vfa.warn { color: #d9a441; }
  .ss-flow { margin-top: 6px; font-size: 12px; min-height: 16px; line-height: 1.4;
             color: var(--orca-muted, #9a9a9a); }
  .ss-flow.warn { color: #d9a441; }
  table.ss-table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 13px; table-layout: fixed; }
  table.ss-table th, table.ss-table td { text-align: left; padding: 5px 8px;
                   border-bottom: 1px solid var(--orca-border, #3a3a3a); vertical-align: middle; }
  table.ss-table th { font-weight: 500; color: var(--orca-muted, #9a9a9a); }
  table.ss-table th.num, table.ss-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
  table.ss-table tr.ss-changed td.ss-name,
  table.ss-table tr.ss-changed td.ss-out { color: var(--orca-accent, #4a90d9); }
  table.ss-table td.ss-out.ss-capped,
  table.ss-table tr td.ss-out.ss-capped { color: #d9a441; }
  table.ss-table input { box-sizing: border-box; padding: 3px 6px; font: inherit;
        font-size: 13px; color: inherit; background: transparent;
        border: 1px solid var(--orca-border, #444); border-radius: 4px; }
  table.ss-table input.val { width: 78px; text-align: right; font-variant-numeric: tabular-nums; }
  table.ss-table input.note { width: 100%; }
  table.ss-table input.note::placeholder { color: var(--orca-muted, #9a9a9a); opacity: .7; }
  .ss-unit { display: inline-flex; margin-left: 6px; vertical-align: middle;
        border: 1px solid var(--orca-border, #444); border-radius: 4px; overflow: hidden; }
  .ss-unit button { border: 0; background: transparent; color: inherit; cursor: pointer;
        padding: 4px 9px; font-size: 12px; line-height: 1.2; }
  .ss-unit button.on { background: var(--orca-accent, #4a90d9); color: var(--orca-accent-fg, #fff); }
  .ss-actions { display: flex; gap: 8px; margin-top: 16px; }
  .ss-actions button { padding: 6px 14px; border-radius: 4px; cursor: pointer;
        border: 1px solid var(--orca-border, #444); background: transparent;
        color: inherit; font-size: 13px; }
  .ss-actions button.ss-primary { background: var(--orca-accent, #4a90d9);
        border-color: var(--orca-accent, #4a90d9); color: var(--orca-accent-fg, #fff); }
  .ss-status { margin-top: 12px; font-size: 12px; min-height: 16px;
               color: var(--orca-muted, #9a9a9a); }
  .ss-status.err { color: #e06c6c; }
  .ss-status.warn { color: #d9a441; }
  .ss-last { margin-top: 6px; font-size: 12px; color: var(--orca-muted, #9a9a9a); }
  .ss-readme { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--orca-border, #3a3a3a);
               font-size: 12px; line-height: 1.5; color: var(--orca-muted, #9a9a9a);
               white-space: pre-line; }
</style>
</head>
<body>
<div class="ss-wrap">
  <div class="ss-head">
    <div>
      <div class="ss-title">Easy Speed Slider</div>
      <div class="ss-sub" id="ss-preset">&nbsp;</div>
    </div>
    <div class="ss-right">
      <span class="ss-sub" id="ss-changed"></span>
      <span class="ss-lang">
        <button id="ss-lang-ru" class="on">RU</button>
        <button id="ss-lang-en">EN</button>
      </span>
    </div>
  </div>

  <div class="ss-slider-row">
    <label for="ss-range" id="ss-wall-label">Внешняя стенка</label>
    <input id="ss-range" type="range" min="1" max="200" step="1" value="60">
    <input id="ss-num" type="number" min="1" max="1000" step="0.5" value="60">
    <span class="ss-sub">mm/s</span>
  </div>

  <div class="ss-vfa" id="ss-vfa"></div>
  <div class="ss-flow" id="ss-flow"></div>

  <table class="ss-table">
    <thead>
      <tr>
        <th style="width: 25%" id="ss-th-param">Параметр</th>
        <th style="width: 24%" id="ss-th-value">Значение</th>
        <th class="num" style="width: 11%" id="ss-th-out">Станет</th>
        <th style="width: 40%" id="ss-th-why">Почему так</th>
      </tr>
    </thead>
    <tbody id="ss-body"></tbody>
  </table>

  <div class="ss-last" id="ss-last"></div>

  <div class="ss-actions">
    <button id="ss-apply" class="ss-primary">Применить</button>
    <button id="ss-reset">Сбросить к базе</button>
    <button id="ss-rebind">Переснять базу</button>
  </div>

  <div id="ss-status" class="ss-status"></div>

  <div class="ss-readme" id="ss-readme"></div>
</div>

<script>
(function () {
  'use strict';

  var STR = {
    ru: {
      wall: 'Внешняя стенка',
      unitSpeed: 'мм/с',
      param: 'Параметр',
      value: 'Значение',
      out: 'Станет',
      why: 'Почему так',
      apply: 'Применить',
      reset: 'Сбросить к базе',
      rebind: 'Переснять базу',
      dev: function (n, total) { return 'отклонений: ' + n + ' из ' + total; },
      dirty: 'Пресет изменён — не забудьте сохранить.',
      applying: 'Применяю…',
      applied: 'Применено.',
      resetting: 'Возвращена исходная пропорция (не применено).',
      rebinding: 'Переснимаю базу с текущего пресета…',
      empty: 'В пресете не нашлось ни одной абсолютной скорости — проверьте настройки.',
      vfaRange: function (min, max) { return 'Диапазон избегаемых скоростей: ' + min + '–' + max + ' мм/с.'; },
      vfaWarn: function (speed, adjusted, min, max) {
        return '⚠ ' + speed + ' мм/с попадает в диапазон избегаемых скоростей ' + min + '–' + max +
          ' мм/с — слайсер напечатает внешнюю стенку на ' + adjusted +
          ' мм/с. Безопасно: < ' + min + ' или ≥ ' + max + ' мм/с.';
      },
      vfaWarnOff: function (speed, min, max) {
        return '⚠ ' + speed + ' мм/с попадает в диапазон избегаемых скоростей ' + min + '–' + max +
          ' мм/с, но «Избегание ряби» выключено — слайсер не будет её корректировать. ' +
          'Безопасно: < ' + min + ' или ≥ ' + max + ' мм/с.';
      },
      flowOk: function (limit, wallMax) {
        var tail = (wallMax !== null && wallMax !== undefined)
          ? ' (для внешней стенки максимум ' + wallMax + ' мм/с)' : '';
        return 'Объёмный расход: лимит ' + limit + ' мм³/с — все скорости в пределах' + tail +
          '. Оценка по ширине линии и высоте слоя.';
      },
      flowWarn: function (limit, parts) {
        return '⚠ Объёмный расход ' + limit + ' мм³/с (оценка): ' + parts.join('; ') +
          '. Слайсер срежет эти скорости.';
      },
      noLink: 'Нет связи с плагином: ',
      error: 'Ошибка',
      last: 'Последнее применение',
      wallShort: 'стенка',
      whyRatio: function (r) { return 'почему ×' + r + ' к стенке'; },
      whySpeed: function (v) { return 'почему ' + v + ' мм/с'; },
      readme: 'Как это работает.\n' +
        'Ползунок сверху задаёт скорость внешней стенки — это опорная скорость.\n' +
        'Каждая строка таблицы берёт либо множитель к ней (×2 = вдвое быстрее стенки), либо собственную скорость в мм/с — такие строки от ползунка не зависят.\n' +
        'Пояснения в последней колонке сохраняются сами и запоминаются для каждого пресета.\n' +
        'Если в настройках принтера включено избегание резонанса, появится предупреждение, когда скорость стенки попадёт в избегаемый диапазон.\n' +
        'Если у филамента задан максимальный объёмный расход, плагин оценивает предел скорости по ширине линии и высоте слоя и подсвечивает строки, где слайсер срежет скорость.\n' +
        '«Применить» записывает всё в текущий пресет — он станет изменённым, сохраните его.\n' +
        '«Сбросить к базе» возвращает исходные пропорции профиля.'
    },
    en: {
      wall: 'Outer wall',
      unitSpeed: 'mm/s',
      param: 'Parameter',
      value: 'Value',
      out: 'Result',
      why: 'Why',
      apply: 'Apply',
      reset: 'Reset to base',
      rebind: 'Re-read base',
      dev: function (n, total) { return 'deviations: ' + n + ' of ' + total; },
      dirty: "Preset modified — don't forget to save.",
      applying: 'Applying…',
      applied: 'Applied.',
      resetting: 'Original proportions restored (not applied).',
      rebinding: 'Re-reading base from the current preset…',
      empty: 'No absolute speeds found in this preset — check the settings.',
      vfaRange: function (min, max) { return 'Avoided speed band: ' + min + '–' + max + ' mm/s.'; },
      vfaWarn: function (speed, adjusted, min, max) {
        return '⚠ ' + speed + ' mm/s is inside the avoided speed band ' + min + '–' + max +
          ' mm/s — the slicer will print the outer wall at ' + adjusted +
          ' mm/s. Safe: < ' + min + ' or ≥ ' + max + ' mm/s.';
      },
      vfaWarnOff: function (speed, min, max) {
        return '⚠ ' + speed + ' mm/s is inside the avoided speed band ' + min + '–' + max +
          ' mm/s, but resonance avoidance is disabled — the slicer will not correct it. ' +
          'Safe: < ' + min + ' or ≥ ' + max + ' mm/s.';
      },
      flowOk: function (limit, wallMax) {
        var tail = (wallMax !== null && wallMax !== undefined)
          ? ' (outer wall max ' + wallMax + ' mm/s)' : '';
        return 'Volumetric flow: limit ' + limit + ' mm³/s — all speeds are within it' + tail +
          '. Estimated from line width and layer height.';
      },
      flowWarn: function (limit, parts) {
        return '⚠ Volumetric flow limit ' + limit + ' mm³/s (estimated): ' + parts.join('; ') +
          '. The slicer will cap these speeds.';
      },
      noLink: 'No link to the plugin: ',
      error: 'Error',
      last: 'Last applied',
      wallShort: 'wall',
      whyRatio: function (r) { return 'why ×' + r + ' of the wall'; },
      whySpeed: function (v) { return 'why ' + v + ' mm/s'; },
      readme: 'How it works.\n' +
        'The slider above sets the outer wall speed — the reference speed.\n' +
        'Each row takes either a multiplier of it (×2 = twice as fast as the wall) or its own speed in mm/s — such rows do not follow the slider.\n' +
        'Notes in the last column are saved automatically and remembered per preset.\n' +
        'When resonance avoidance is enabled in the printer settings, a warning appears if the wall speed falls into the avoided band.\n' +
        'If the filament has a maximum volumetric speed, the plugin estimates the speed limit from line width and layer height and highlights rows the slicer will cap.\n' +
        '"Apply" writes everything into the current preset — it becomes modified, save it.\n' +
        '"Reset to base" restores the original proportions of the profile.'
    }
  };

  var S = null;
  var lang = 'ru';

  var bodyEl = document.getElementById('ss-body');
  var presetEl = document.getElementById('ss-preset');
  var changedEl = document.getElementById('ss-changed');
  var lastEl = document.getElementById('ss-last');
  var statusEl = document.getElementById('ss-status');
  var readmeEl = document.getElementById('ss-readme');
  var rangeEl = document.getElementById('ss-range');
  var numEl = document.getElementById('ss-num');
  var langRuEl = document.getElementById('ss-lang-ru');
  var langEnEl = document.getElementById('ss-lang-en');
  var vfaEl = document.getElementById('ss-vfa');
  var flowEl = document.getElementById('ss-flow');

  // Полоса избегания резонанса из printer-пресета; приходит в state.
  var vfa = { enabled: false, min: 0, max: 0 };
  // Оценка предела по объёмному расходу филамента; приходит в state.
  var flow = { enabled: false, limit: 0, layerHeight: 0, maxSpeeds: {} };

  var noteTimer = null;

  function t() { return STR[lang]; }

  function post(msg) {
    try { orca.postMessage(msg); } catch (e) { status(t().noLink + e, 'err'); }
  }

  function status(text, cls) {
    statusEl.className = 'ss-status' + (cls ? ' ' + cls : '');
    statusEl.textContent = text || '';
  }

  function fmt(v) {
    var r = Math.round(v * 10) / 10;
    return (r % 1 === 0) ? String(r) : r.toFixed(1);
  }

  function fmtRatio(v) {
    var r = Math.round(v * 1000) / 1000;
    if (r % 1 === 0) return String(r);
    return r.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function cell(text, className) {
    var td = document.createElement('td');
    if (className) td.className = className;
    td.textContent = text;
    return td;
  }

  function rowLabel(row) {
    return (lang === 'ru') ? row.label_ru : row.label_en;
  }

  function effective(row, anchorSpeed) {
    return (row.mode === 'ratio') ? (anchorSpeed * row.ratio) : row.speed;
  }

  function parseNumber(text, lo, hi) {
    var v = Number(String(text).replace(',', '.'));
    if (!isFinite(v) || v < lo || v > hi) return null;
    return v;
  }

  function notePlaceholder(row) {
    return (row.mode === 'speed') ? t().whySpeed(fmt(row.speed)) : t().whyRatio(fmtRatio(row.ratio));
  }

  function updateHeader() {
    var changed = 0;
    for (var i = 0; i < S.rows.length; i++) {
      var row = S.rows[i];
      var base = S.base[i];
      if (row.mode === 'speed' || Math.abs(row.ratio - base.ratio) > 1e-3) changed++;
    }
    changedEl.textContent = t().dev(changed, S.rows.length);
  }

  function flowMaxFor(key) {
    if (!flow.enabled || !flow.maxSpeeds) return null;
    var max = flow.maxSpeeds[key];
    return (typeof max === 'number' && max > 0) ? max : null;
  }

  function flowCapped(key, speed) {
    var max = flowMaxFor(key);
    return (max !== null && speed > max + 1e-9) ? max : null;
  }

  function updateRow(idx) {
    var row = S.rows[idx];
    var tr = bodyEl.children[idx];
    if (!tr) return;
    var speed = Math.max(1, effective(row, S.value));
    var tdOut = tr.querySelector('.ss-out');
    tdOut.textContent = fmt(speed);
    tdOut.className = 'num ss-out' + (flowCapped(row.key, speed) !== null ? ' ss-capped' : '');
    var base = S.base[idx];
    var changed = (row.mode === 'speed') || (Math.abs(row.ratio - base.ratio) > 1e-3);
    tr.className = changed ? 'ss-changed' : '';
  }

  function updateAllRows() {
    for (var i = 0; i < S.rows.length; i++) updateRow(i);
    updateHeader();
  }

  // Диапазон считается опасным всегда, когда он заполнен: если «Избегание ряби»
  // включено, слайсер скорректирует скорость, а если выключено — напечатает как
  // есть; плагин предупреждает в обоих случаях. При включённом избегании OrcaSlicer
  // для внешних периметров прижимает нижнюю половину диапазона к min, верхнюю —
  // к max; ровно min и >= max не меняются.
  function vfaAdjusted(speed) {
    if (!(vfa.min > 0 && vfa.max > vfa.min)) return null;
    if (!(speed > vfa.min && speed < vfa.max)) return null;
    var mid = vfa.min + (vfa.max - vfa.min) / 2;
    return (speed < mid) ? vfa.min : vfa.max;
  }

  function updateVfa() {
    if (!S) { vfaEl.textContent = ''; vfaEl.className = 'ss-vfa'; return; }
    var adjusted = vfaAdjusted(S.value);
    if (adjusted !== null) {
      vfaEl.className = 'ss-vfa warn';
      vfaEl.textContent = (vfa.enabled && Math.abs(adjusted - S.value) > 1e-9)
        ? t().vfaWarn(fmt(S.value), fmt(adjusted), fmt(vfa.min), fmt(vfa.max))
        : t().vfaWarnOff(fmt(S.value), fmt(vfa.min), fmt(vfa.max));
    } else if (vfa.enabled && vfa.min > 0 && vfa.max > vfa.min) {
      vfaEl.className = 'ss-vfa';
      vfaEl.textContent = t().vfaRange(fmt(vfa.min), fmt(vfa.max));
    } else {
      vfaEl.className = 'ss-vfa';
      vfaEl.textContent = '';
    }
  }

  function updateFlow() {
    if (!S) { flowEl.textContent = ''; flowEl.className = 'ss-flow'; return; }
    if (!flow.enabled) { flowEl.className = 'ss-flow'; flowEl.textContent = ''; return; }
    var units = t().unitSpeed;
    var parts = [];
    var wallSpeed = Math.max(1, S.value);
    var wallMax = flowMaxFor('outer_wall_speed');
    if (flowCapped('outer_wall_speed', wallSpeed) !== null) {
      parts.push(t().wall + ' ' + fmt(wallSpeed) + ' > ' + fmt(wallMax) + ' ' + units);
    }
    for (var i = 0; i < S.rows.length; i++) {
      var row = S.rows[i];
      var speed = Math.max(1, effective(row, S.value));
      var capped = flowCapped(row.key, speed);
      if (capped !== null) {
        parts.push(rowLabel(row) + ' ' + fmt(speed) + ' > ' + fmt(capped) + ' ' + units);
      }
    }
    if (parts.length) {
      flowEl.className = 'ss-flow warn';
      flowEl.textContent = t().flowWarn(fmt(flow.limit), parts);
    } else {
      flowEl.className = 'ss-flow';
      flowEl.textContent = t().flowOk(fmt(flow.limit),
                                      wallMax !== null ? fmt(wallMax) : null);
    }
  }

  function updateWallWarn() {
    if (!S) return;
    var vfaWarn = vfaAdjusted(S.value) !== null;
    var wallMax = flowMaxFor('outer_wall_speed');
    var flowWarn = wallMax !== null && S.value > wallMax + 1e-9;
    var warn = vfaWarn || flowWarn;
    rangeEl.className = warn ? 'ss-warn' : '';
    numEl.className = warn ? 'ss-warn' : '';
  }

  function updateFeedback() {
    updateVfa();
    updateFlow();
    updateWallWarn();
  }

  function collectRows() {
    var map = {};
    for (var i = 0; i < S.rows.length; i++) {
      var row = S.rows[i];
      map[row.key] = { mode: row.mode, ratio: row.ratio, speed: row.speed };
    }
    return map;
  }

  function collectNotes() {
    var map = {};
    for (var i = 0; i < S.rows.length; i++) map[S.rows[i].key] = S.rows[i].note || '';
    return map;
  }

  function scheduleNotesSave() {
    if (noteTimer) clearTimeout(noteTimer);
    noteTimer = setTimeout(function () {
      post({ kind: 'note', notes: collectNotes() });
    }, 600);
  }

  function syncInput(input, row) {
    input.value = (row.mode === 'ratio') ? fmtRatio(row.ratio) : fmt(row.speed);
  }

  function buildTable() {
    clear(bodyEl);

    for (var i = 0; i < S.rows.length; i++) {
      (function (idx) {
        var row = S.rows[idx];
        var tr = document.createElement('tr');

        tr.appendChild(cell(rowLabel(row), 'ss-name'));

        var tdVal = document.createElement('td');
        var input = document.createElement('input');
        input.type = 'number';
        input.className = 'val';
        input.step = '0.05';
        syncInput(input, row);
        input.addEventListener('input', function () {
          if (row.mode === 'ratio') {
            var r = parseNumber(input.value, 0.01, 50);
            if (r === null) return;
            row.ratio = Math.round(r * 1000) / 1000;
          } else {
            var s = parseNumber(input.value, 1, 1000);
            if (s === null) return;
            row.speed = Math.round(s * 10) / 10;
          }
          updateRow(idx);
          updateFeedback();
          note.placeholder = notePlaceholder(row);
        });

        var unit = document.createElement('span');
        unit.className = 'ss-unit';
        var btnRatio = document.createElement('button');
        btnRatio.textContent = '×';
        btnRatio.title = (lang === 'ru') ? 'Множитель к скорости внешней стенки' : 'Multiplier of the outer wall speed';
        var btnSpeed = document.createElement('button');
        btnSpeed.textContent = 'mm/s';
        btnSpeed.title = (lang === 'ru') ? 'Абсолютная скорость, не зависит от ползунка' : 'Absolute speed, independent of the slider';
        btnRatio.className = (row.mode === 'ratio') ? 'on' : '';
        btnSpeed.className = (row.mode === 'speed') ? 'on' : '';
        btnRatio.addEventListener('click', function () {
          if (row.mode === 'ratio') return;
          var anchor = S.value;
          row.ratio = (anchor > 0) ? Math.round((row.speed / anchor) * 1000) / 1000 : 1;
          row.mode = 'ratio';
          btnRatio.className = 'on';
          btnSpeed.className = '';
          syncInput(input, row);
          updateRow(idx);
          updateFeedback();
          note.placeholder = notePlaceholder(row);
        });
        btnSpeed.addEventListener('click', function () {
          if (row.mode === 'speed') return;
          row.speed = Math.round((S.value * row.ratio) * 10) / 10;
          row.mode = 'speed';
          btnRatio.className = '';
          btnSpeed.className = 'on';
          syncInput(input, row);
          updateRow(idx);
          updateFeedback();
          note.placeholder = notePlaceholder(row);
        });
        unit.appendChild(btnRatio);
        unit.appendChild(btnSpeed);

        tdVal.appendChild(input);
        tdVal.appendChild(unit);

        var tdOut = document.createElement('td');
        tdOut.className = 'num ss-out';
        tdOut.textContent = fmt(Math.max(1, effective(row, S.value)));

        var tdNote = document.createElement('td');
        var note = document.createElement('input');
        note.type = 'text';
        note.className = 'note';
        note.placeholder = notePlaceholder(row);
        note.value = row.note || '';
        note.addEventListener('input', function () {
          row.note = note.value;
          scheduleNotesSave();
        });
        tdNote.appendChild(note);

        tr.appendChild(tdVal);
        tr.appendChild(tdOut);
        tr.appendChild(tdNote);

        var base = S.base[idx];
        var changed = (row.mode === 'speed') || (Math.abs(row.ratio - base.ratio) > 1e-3);
        if (changed) tr.className = 'ss-changed';

        bodyEl.appendChild(tr);
      })(i);
    }

    updateAllRows();
    updateFeedback();
  }

  function labelFor(key) {
    for (var i = 0; i < S.rows.length; i++) {
      if (S.rows[i].key === key) return rowLabel(S.rows[i]);
    }
    return key;
  }

  function renderLast(last) {
    if (!last) { lastEl.textContent = ''; return; }
    var parts = [];
    if (typeof last.slider === 'number' && last.slider > 0) {
      parts.push(t().wallShort + ' ' + fmt(last.slider) + ' mm/s');
    }
    var rows = last.rows || {};
    for (var key in rows) {
      if (!Object.prototype.hasOwnProperty.call(rows, key)) continue;
      var spec = rows[key];
      if (!spec) continue;
      if (spec.mode === 'speed') {
        parts.push(labelFor(key) + ' ' + fmt(spec.speed) + ' mm/s');
      } else if (typeof spec.ratio === 'number') {
        parts.push(labelFor(key) + ' ×' + fmtRatio(spec.ratio));
      }
    }
    if (!parts.length) { lastEl.textContent = ''; return; }
    lastEl.textContent = t().last
      + (last.updated ? ' (' + last.updated + ')' : '') + ': ' + parts.join(', ');
  }

  function applyLang(next) {
    lang = (next === 'en') ? 'en' : 'ru';
    langRuEl.className = (lang === 'ru') ? 'on' : '';
    langEnEl.className = (lang === 'en') ? 'on' : '';

    document.getElementById('ss-wall-label').textContent = t().wall;
    document.getElementById('ss-th-param').textContent = t().param;
    document.getElementById('ss-th-value').textContent = t().value;
    document.getElementById('ss-th-out').textContent = t().out;
    document.getElementById('ss-th-why').textContent = t().why;
    document.getElementById('ss-apply').textContent = t().apply;
    document.getElementById('ss-reset').textContent = t().reset;
    document.getElementById('ss-rebind').textContent = t().rebind;
    readmeEl.textContent = t().readme;

    if (S) {
      buildTable();
      renderLast(S.last);
    }
    status('');
  }

  function applyState(s) {
    S = s;
    S.base = s.rows.map(function (row) {
      var ratio = (s.anchor > 0) ? (row.base[0] / s.anchor) : 1;
      return { ratio: ratio };
    });
    presetEl.textContent = s.preset ? ('· ' + s.preset) : '';

    var max = Math.max(300, Math.round(s.anchor * 3));
    rangeEl.min = 1;
    rangeEl.max = max;
    numEl.min = 1;
    numEl.max = Math.max(1000, max);
    rangeEl.value = s.value;
    numEl.value = fmt(s.value);

    vfa = s.vfa || { enabled: false, min: 0, max: 0 };
    flow = s.flow || { enabled: false, limit: 0, layerHeight: 0, maxSpeeds: {} };
    applyLang(s.lang || 'ru');

    if (!s.rows || s.rows.length === 0) {
      status(t().empty, 'err');
    } else if (s.dirty) {
      status(t().dirty, 'warn');
    }
  }

  rangeEl.addEventListener('input', function () {
    if (!S) return;
    var v = Number(rangeEl.value);
    if (!isFinite(v) || v <= 0) return;
    S.value = v;
    numEl.value = fmt(v);
    updateAllRows();
    updateFeedback();
  });

  numEl.addEventListener('input', function () {
    if (!S) return;
    var v = Number(numEl.value);
    if (!isFinite(v) || v <= 0) return;
    S.value = v;
    rangeEl.value = Math.min(Number(rangeEl.max), Math.max(1, v));
    updateAllRows();
    updateFeedback();
  });

  document.getElementById('ss-apply').addEventListener('click', function () {
    if (!S) return;
    post({ kind: 'apply', value: S.value, rows: collectRows(), notes: collectNotes() });
    status(t().applying);
  });

  document.getElementById('ss-reset').addEventListener('click', function () {
    if (!S) return;
    for (var i = 0; i < S.rows.length; i++) {
      var row = S.rows[i];
      row.mode = 'ratio';
      row.ratio = Math.round(S.base[i].ratio * 1000) / 1000;
      row.speed = Math.round((S.value * row.ratio) * 10) / 10;
    }
    buildTable();
    status(t().resetting);
  });

  document.getElementById('ss-rebind').addEventListener('click', function () {
    post({ kind: 'rebind' });
    status(t().rebinding);
  });

  langRuEl.addEventListener('click', function () {
    applyLang('ru');
    post({ kind: 'lang', lang: 'ru' });
  });
  langEnEl.addEventListener('click', function () {
    applyLang('en');
    post({ kind: 'lang', lang: 'en' });
  });

  orca.onMessage(function (msg) {
    if (!msg || typeof msg !== 'object') return;
    if (msg.kind === 'state') {
      applyState(msg);
    } else if (msg.kind === 'error') {
      status(msg.message || t().error, 'err');
    }
  });

  applyLang('ru');
  post({ kind: 'ready' });
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
#  Capability
# --------------------------------------------------------------------------- #

class EasySpeedSliderPage(orca.pages.PagesPluginCapabilityBase):
    """Вкладка ESS: ползунок стенки + строки «множитель к стенке» или «мм/с»."""

    # --- Pages API ---------------------------------------------------------

    def get_name(self):
        return CAPABILITY_NAME

    def get_icon(self):
        # Иконка ярлыка вкладки: встроенный ресурс OrcaSlicer (спидометр).
        # Если ресурса нет в сборке, вкладка просто останется без иконки.
        return "param_speed"

    def get_ui(self):
        return PAGE_HTML

    def on_message(self, data):
        if not isinstance(data, dict):
            return

        # plugin.storage() работает только внутри плагин-колбэка и смотрит
        # thread_local плагина — поэтому берём путь здесь, в UI-потоке.
        self._ensure_storage()

        kind = data.get("kind")
        try:
            self._ensure_worker()
        except Exception as exc:
            print(f"Easy Speed Slider: не удалось запустить поток: {exc}")
            self._post({"kind": "error", "message": f"Stream failed: {exc}"})
            return

        if kind in ("ready", "rebind"):
            self._schedule(rebind=True)
        elif kind == "lang":
            lang = data.get("lang")
            if lang in LANGS:
                self._schedule(lang=lang)
        elif kind == "apply":
            try:
                value = float(data.get("value"))
            except (TypeError, ValueError):
                self._post({"kind": "error", "message": "Bad wall speed"})
                return
            if not (MIN_SPEED <= value <= MAX_SPEED_SETTING):
                self._post({
                    "kind": "error",
                    "message": f"Wall speed must be {MIN_SPEED:g}…{MAX_SPEED_SETTING:g} mm/s",
                })
                return
            rows = self._parse_rows(data.get("rows"))
            if rows is None:
                self._post({
                    "kind": "error",
                    "message": f"Multiplier {MIN_RATIO}…{MAX_RATIO}, speed "
                               f"{MIN_SPEED:g}…{MAX_SPEED_SETTING:g} mm/s",
                })
                return
            notes = self._parse_notes(data.get("notes"))
            self._schedule(value=value, rows=rows, notes=notes)
        elif kind == "note":
            notes = self._parse_notes(data.get("notes"))
            if notes:
                self._schedule(notes=notes)

    # --- разбор сообщений --------------------------------------------------

    def _parse_rows(self, raw):
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            return None
        known = {key for key, _ru, _en in ROWS}
        parsed = {}
        for key, spec in raw.items():
            if key not in known or not isinstance(spec, dict):
                continue
            mode = spec.get("mode")
            if mode not in ("ratio", "speed"):
                continue
            try:
                ratio = float(spec.get("ratio", 1.0))
                speed = float(spec.get("speed", 1.0))
            except (TypeError, ValueError):
                return None
            if mode == "ratio" and not (MIN_RATIO <= ratio <= MAX_RATIO):
                return None
            if mode == "speed" and not (MIN_SPEED <= speed <= MAX_SPEED_SETTING):
                return None
            parsed[key] = {"mode": mode, "ratio": ratio, "speed": speed}
        return parsed

    def _parse_notes(self, raw):
        if not isinstance(raw, dict):
            return None
        known = {key for key, _ru, _en in ROWS}
        notes = {}
        for key, value in raw.items():
            if key in known:
                notes[key] = str(value or "")
        return notes or None

    # --- Lifecycle ---------------------------------------------------------

    def on_lifecycle_event(self, event, ctx):
        try:
            if event != orca.LifecycleEvent.PresetSelected:
                return
            kind = (getattr(ctx, "msg", "") or "").lower()
            # print/process — сменился process-пресет; printer — сменился принтер
            # (своя база/VFA); filament — сменился филамент (свой лимит объёмного расхода).
            if kind in ("print", "process", "printer", "machine", "filament"):
                self._ensure_worker()
                self._schedule(rebind=True)
        except Exception as exc:
            print(f"Easy Speed Slider: lifecycle: {exc}")

    def on_unload(self):
        state = self._state_if_any()
        if state is None:
            return
        state.stop.set()
        state.wake.set()
        worker = state.worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)

    # --- состояние ---------------------------------------------------------

    def _state(self):
        state = getattr(self, "_speed_slider_state", None)
        if state is None:
            state = _State()
            self._speed_slider_state = state
        return state

    def _state_if_any(self):
        return getattr(self, "_speed_slider_state", None)

    def _post(self, payload):
        try:
            self.post_message(payload)
        except Exception as exc:
            print(f"Easy Speed Slider: страница не ответила: {exc}")

    # --- приватная папка плагина -------------------------------------------

    def _ensure_storage(self):
        state = self._state()
        if state.storage_dir is not None or state.storage_failed:
            return
        try:
            state.storage_dir = orca.host.plugin.storage()
        except Exception as exc:
            state.storage_failed = True
            print(f"Easy Speed Slider: хранилище недоступно: {exc}")

    def _store_path(self, state):
        if not state.storage_dir:
            return None
        return os.path.join(state.storage_dir, STORE_FILE)

    def _load_store(self, state):
        if state.store is not None:
            return
        state.store = {"version": 5, "presets": {}, "ui": {"lang": DEFAULT_LANG}}
        path = self._store_path(state)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("presets"), dict):
                state.store = data
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"Easy Speed Slider: не удалось прочитать настройки: {exc}")
        ui = state.store.get("ui")
        if isinstance(ui, dict) and ui.get("lang") in LANGS:
            state.lang = ui["lang"]

    def _write_store(self, state):
        path = self._store_path(state)
        if not path:
            return
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state.store, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            print(f"Easy Speed Slider: не удалось сохранить настройки: {exc}")

    def _record(self, state, preset_label):
        presets = state.store.setdefault("presets", {})
        record = presets.get(preset_label)
        return record if isinstance(record, dict) else None

    def _remember(self, state, preset_label, slider=None, rows=None, notes=None, lang=None):
        if not preset_label:
            return
        self._load_store(state)
        if lang is not None:
            state.store.setdefault("ui", {})["lang"] = lang
        presets = state.store.setdefault("presets", {})
        record = presets.get(preset_label)
        if not isinstance(record, dict):
            record = {"rows": {}}
        if slider is not None:
            record["slider"] = round(float(slider), 2)
            record["anchor"] = round(float(state.anchor[0]), 2) if state.anchor else 0.0
        stored_rows = record.setdefault("rows", {})
        for key, spec in (rows or {}).items():
            entry = stored_rows.setdefault(key, {})
            entry["mode"] = spec["mode"]
            entry["ratio"] = round(float(spec["ratio"]), 4)
            entry["speed"] = round(float(spec["speed"]), 2)
        for key, text in (notes or {}).items():
            stored_rows.setdefault(key, {})["note"] = text
        record["updated"] = time.strftime("%d.%m %H:%M")
        presets[preset_label] = record
        self._write_store(state)

    # --- воркер ------------------------------------------------------------

    def _ensure_worker(self):
        state = self._state()
        with state.lock:
            if state.worker is not None and state.worker.is_alive():
                return
            state.stop.clear()
            state.worker = threading.Thread(
                target=self._loop, name=WORKER_NAME, daemon=True
            )
            state.worker.start()

    def _schedule(self, value=None, rows=None, notes=None, lang=None, rebind=False):
        state = self._state()
        with state.lock:
            if value is not None:
                state.pending_value = float(value)
            if rows is not None:
                state.pending_rows = dict(rows)
            if notes is not None:
                state.pending_notes = dict(notes)
            if lang is not None:
                state.pending_lang = lang
            if rebind:
                state.pending_rebind = True
        state.wake.set()

    def _loop(self):
        state = self._state()
        while True:
            state.wake.wait()
            state.wake.clear()
            if state.stop.is_set():
                return

            with state.lock:
                value = state.pending_value
                state.pending_value = None
                rows = state.pending_rows
                state.pending_rows = None
                notes = state.pending_notes
                state.pending_notes = None
                lang = state.pending_lang
                state.pending_lang = None
                rebind = state.pending_rebind
                state.pending_rebind = False

            try:
                if rebind or not state.anchor:
                    self._bind(state)
                if lang is not None:
                    state.lang = lang
                    self._remember(state, self._preset_label(), lang=lang)
                if value is not None:
                    self._apply(state, value, rows or {}, notes)
                elif notes is not None:
                    for key, text in notes.items():
                        if key in state.rows:
                            state.rows[key].note = text
                    self._remember(
                        state, self._preset_label(),
                        rows={k: v.as_json() for k, v in state.rows.items()},
                        notes=notes,
                    )
                self._post_state(state)
            except Exception as exc:                  # noqa: BLE001 — показываем всё
                print(f"Easy Speed Slider: {exc}")
                self._post({"kind": "error", "message": str(exc)})

    # --- работа с пресетом -------------------------------------------------

    def _preset_api(self):
        host = getattr(orca, "host", None)
        api = getattr(host, "preset", None) if host is not None else None
        if api is None or not hasattr(api, "process"):
            raise RuntimeError(
                "This build has no orca.host.preset — need OrcaSlicer 2.5.0-beta or newer."
            )
        return api

    def _preset(self):
        return orca.host.preset_bundle().current_process_preset()

    def _preset_label(self):
        try:
            return self._preset().label()
        except Exception:
            return ""

    def _bind(self, state):
        """Снять базу: стенку и все ROWS-скорости, подтянуть прошлые настройки строк."""
        self._preset_api()

        bundle = orca.host.preset_bundle()
        raw_anchor = bundle.full_config_value(ANCHOR)
        anchor = _parse_list(raw_anchor)
        if not anchor or anchor[0] <= 0:
            raise RuntimeError(
                f"Cannot read {ANCHOR}: {raw_anchor!r}. Check that a process preset is selected."
            )

        base = {}
        for key, _ru, _en in ROWS:
            raw = bundle.full_config_value(key)
            parsed = _parse_list(raw)
            if not parsed:
                continue
            if any(v <= 0 for v in parsed):           # 0 = «авто», такие не трогаем
                continue
            base[key] = parsed

        state.vfa = _read_vfa(bundle)
        state.flow = _read_flow(bundle)

        label = self._preset_label()
        self._load_store(state)
        record = self._record(state, label) if label else None
        record_rows = (record or {}).get("rows", {}) if isinstance(record, dict) else {}

        rows = {}
        for key, values in base.items():
            ratio = values[0] / anchor[0]
            stored = record_rows.get(key)
            stored = stored if isinstance(stored, dict) else {}
            mode = stored.get("mode")
            mode = mode if mode in ("ratio", "speed") else "ratio"
            try:
                stored_ratio = float(stored.get("ratio"))
            except (TypeError, ValueError):
                stored_ratio = None
            try:
                stored_speed = float(stored.get("speed"))
            except (TypeError, ValueError):
                stored_speed = None
            if stored_ratio is None or not (MIN_RATIO <= stored_ratio <= MAX_RATIO):
                stored_ratio = ratio
            if stored_speed is None or not (MIN_SPEED <= stored_speed <= MAX_SPEED_SETTING):
                stored_speed = values[0]
            rows[key] = _Row(
                mode=mode,
                ratio=stored_ratio,
                speed=stored_speed,
                note=str(stored.get("note") or ""),
            )

        state.anchor = anchor
        state.value = anchor[0]
        state.base = base
        state.rows = rows

        vfa_text = (f"{state.vfa['min']:g}-{state.vfa['max']:g} mm/s"
                    if state.vfa["enabled"] else "off")
        flow_text = (f"{state.flow['limit']:g} mm3/s"
                     if state.flow["enabled"] else "off")
        print(f"Easy Speed Slider: base read. wall={anchor}, rows={len(base)}, "
              f"vfa={vfa_text}, flow={flow_text}, lang={state.lang}")

    def _apply(self, state, value, specs, notes):
        """Записать внешнюю стенку и все строки одним коммитом."""
        if not state.anchor:
            self._bind(state)

        anchor0 = state.anchor[0]
        k = float(value) / anchor0
        if k <= 0:
            raise ValueError("Multiplier must be greater than zero.")

        anchor_scaled = [v * k for v in state.anchor]
        tx = orca.host.preset.process.begin_update()
        tx.set(ANCHOR, _fmt_list([max(MIN_SPEED, v) for v in anchor_scaled]))

        changed = 0
        for key, values in state.base.items():
            spec = specs.get(key)
            row = state.rows.get(key)
            if row is None:
                continue
            if spec:
                row.mode = spec["mode"]
                row.ratio = spec["ratio"]
                row.speed = spec["speed"]

            out = []
            for idx in range(len(values)):
                anchor_speed = anchor_scaled[idx] if idx < len(anchor_scaled) else anchor_scaled[0]
                out.append(max(MIN_SPEED, row.effective(anchor_speed)))
            tx.set(key, _fmt_list(out))

            if row.mode == "speed" or abs(row.ratio - values[0] / anchor0) > 1e-9:
                changed += 1

        result = tx.commit()

        state.value = float(value)
        if notes:
            for key, text in notes.items():
                if key in state.rows:
                    state.rows[key].note = text
        self._remember(
            state, self._preset_label(),
            slider=state.value,
            rows={k2: r.as_json() for k2, r in state.rows.items()},
            notes=notes or {},
        )

        target = getattr(result, "target_name", "") or "?"
        print(f"Easy Speed Slider: x{k:.3f} -> wall {value:.1f}, deviations {changed} "
              f"(preset {target!r})")

    # --- отдача состояния странице ----------------------------------------

    def _post_state(self, state):
        if not state.anchor:
            self._post({"kind": "error", "message": "Base is not read."})
            return

        preset_name = ""
        dirty = False
        try:
            preset = self._preset()
            preset_name = preset.label()
            dirty = bool(preset.is_dirty)
        except Exception as exc:
            print(f"Easy Speed Slider: cannot read preset: {exc}")

        self._load_store(state)
        record = self._record(state, preset_name) if preset_name else None
        record_rows = (record or {}).get("rows", {}) if isinstance(record, dict) else {}

        rows = []
        for key, label_ru, label_en in ROWS:
            values = state.base.get(key)
            row = state.rows.get(key)
            if not values or row is None:
                continue
            rows.append({
                "key": key,
                "label_ru": label_ru,
                "label_en": label_en,
                "base": values,
                "mode": row.mode,
                "ratio": row.ratio,
                "speed": row.speed,
                "note": row.note,
            })

        last_rows = {}
        for key, value in record_rows.items():
            if not isinstance(value, dict):
                continue
            spec = {"mode": value.get("mode", "ratio")}
            try:
                spec["ratio"] = float(value.get("ratio", 1.0))
            except (TypeError, ValueError):
                spec["ratio"] = 1.0
            try:
                spec["speed"] = float(value.get("speed", 1.0))
            except (TypeError, ValueError):
                spec["speed"] = 1.0
            last_rows[key] = spec

        self._post({
            "kind": "state",
            "preset": preset_name,
            "anchor": state.anchor[0],
            "value": state.value if state.value else state.anchor[0],
            "lang": state.lang,
            "vfa": state.vfa,
            "flow": state.flow,
            "rows": rows,
            "notesEnabled": bool(state.storage_dir),
            "dirty": dirty,
            "last": {
                "updated": (record or {}).get("updated", "") if isinstance(record, dict) else "",
                "slider": (record or {}).get("slider") if isinstance(record, dict) else None,
                "rows": last_rows,
            },
        })


@orca.plugin
class EasySpeedSliderPlugin(orca.base):
    def register_capabilities(self):
        # Воркер создаёт один поток: без разрешения коммит пришлось бы делать
        # в UI-потоке, а orca.host.preset оттуда вызывать нельзя.
        orca.request_permissions(threading=True)
        orca.register_capability(EasySpeedSliderPage)

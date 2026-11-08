"""Смоук-тест плагина без OrcaSlicer: подменяем модуль `orca` заглушкой.

Схема: ползунок задаёт скорость внешней стенки; остальные строки — множитель
к ней («×») либо абсолютная скорость («мм/с»). Проверяем базу, пересчёт,
сохранение режимов/множителей/пояснений, язык RU/EN, полосу избегания
резонанса (VFA), сброс к базе и ошибки.
"""

import importlib.util
import json
import pathlib
import sys
import tempfile
import time
import types

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "speed_slider.py"

PRESET = {
    "outer_wall_speed": "60",
    "inner_wall_speed": "150",
    "sparse_infill_speed": "200",
    "internal_solid_infill_speed": "180",
    "top_surface_speed": "120",
    "gap_infill_speed": "150",
    "support_speed": "150",
    "support_interface_speed": "100",
    "bridge_speed": "30",
    "small_perimeter_speed": "50%",   # не в ROWS и в процентах — трогать нельзя
    "travel_speed": "700",            # не в ROWS — не должен попасть в запись
    # Printer-пресет: избегание резонанса (полоса 70–120 мм/с).
    "resonance_avoidance": "1",
    "min_resonance_avoidance_speed": "70",
    "max_resonance_avoidance_speed": "120",
    # Расход: лимит филамента, высота слоя, ширины линий и flow-ratio.
    "filament_max_volumetric_speed": "12",
    "layer_height": "0.2",
    "nozzle_diameter": "0.4",
    "line_width": "0.45",
    "outer_wall_line_width": "0.45",
    "inner_wall_line_width": "0.5",
    "sparse_infill_line_width": "0.5",
    "internal_solid_infill_line_width": "0.45",
    "top_surface_line_width": "0.42",
    "support_line_width": "0.4",
    "bridge_line_width": "0.4",
    "print_flow_ratio": "1",
    "filament_flow_ratio": "1",
    "set_other_flow_ratios": "0",
}

KEYS = ["inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed",
        "top_surface_speed", "gap_infill_speed", "support_speed",
        "support_interface_speed", "bridge_speed"]

APPLIED = {}
STORAGE = tempfile.mkdtemp(prefix="ess-store-")


class Cfg:
    fail_commit = False


def build_fake_orca(anchor_present=True):
    orca = types.ModuleType("orca")
    orca.base = type("base", (), {})

    def plugin(cls):
        return cls

    orca.plugin = plugin

    class PagesBase:
        def post_message(self, payload):
            self.__dict__.setdefault("_out", []).append(payload)

    orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=PagesBase)
    orca.request_permissions = lambda **kwargs: kwargs
    orca.register_capability = lambda cls: cls
    orca.LifecycleEvent = types.SimpleNamespace(PresetSelected="PresetSelected")

    class Preset:
        is_dirty = False

        def label(self):
            return "Test Preset"

    class Bundle:
        def full_config_value(self, key):
            if key == "outer_wall_speed" and not anchor_present:
                return None
            return PRESET.get(key)

        def current_process_preset(self):
            return Preset()

    class Tx:
        def __init__(self):
            self.staged = {}

        def set(self, key, value):
            self.staged[key] = value

        def commit(self):
            if Cfg.fail_commit:
                raise ValueError("Ошибка коммита: недопустимый ключ")
            APPLIED.clear()
            APPLIED.update(self.staged)
            return types.SimpleNamespace(
                target_name="Test Preset", keys=list(self.staged), dirty=True
            )

    class Editor:
        def begin_update(self, preset_name=""):
            return Tx()

    orca.host = types.SimpleNamespace(
        preset=types.SimpleNamespace(process=Editor(), printer=Editor(), filament=Editor()),
        preset_bundle=lambda: Bundle(),
        plugin=types.SimpleNamespace(storage=lambda: STORAGE),
    )
    return orca


def load_plugin(orca):
    sys.modules["orca"] = orca
    spec = importlib.util.spec_from_file_location("ess_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_next(capability, timeout=3.0):
    """Вернуть следующее необработанное сообщение плагина (ошибки приходят сразу)."""
    out = capability.__dict__.setdefault("_out", [])
    seen = capability.__dict__.get("_seen", 0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(out) > seen:
            capability.__dict__["_seen"] = seen + 1
            return out[seen]
        time.sleep(0.02)
    raise AssertionError(f"нет ответа от плагина: {out[seen:]}")


def read_store():
    path = pathlib.Path(STORAGE) / "speed_slider.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def rows_spec(anchor, preset=None, **overrides):
    """Спека всех строк: по умолчанию множитель = база_параметра / база_стенки."""
    preset = preset or PRESET
    base_anchor = float(preset["outer_wall_speed"])
    spec = {}
    for key in KEYS:
        value = float(preset[key])
        spec[key] = {"mode": "ratio", "ratio": value / base_anchor, "speed": value}
    for key, patch in overrides.items():
        spec[key].update(patch)
    return spec


def main():
    fake = build_fake_orca()
    module = load_plugin(fake)
    cap = module.EasySpeedSliderPage()
    assert cap.get_name() == "ESS"
    assert cap.get_icon() == "param_speed"

    # 1. ready: стенка = база, все строки «×» с пропорцией пресета, язык RU
    cap.on_message({"kind": "ready"})
    state = wait_next(cap)
    assert state["kind"] == "state", state
    assert state["anchor"] == 60.0 and state["value"] == 60.0, state
    keys = [row["key"] for row in state["rows"]]
    assert keys == KEYS, keys
    by_key = {row["key"]: row for row in state["rows"]}
    assert abs(by_key["inner_wall_speed"]["ratio"] - 2.5) < 1e-9, by_key
    assert abs(by_key["bridge_speed"]["ratio"] - 0.5) < 1e-9, by_key
    assert all(row["mode"] == "ratio" for row in state["rows"]), state["rows"]
    assert state["lang"] == "ru", state
    assert state["vfa"] == {"enabled": True, "min": 70.0, "max": 120.0}, state.get("vfa")
    assert state["flow"]["enabled"] is True, state.get("flow")
    assert state["flow"]["limit"] == 12.0, state["flow"]
    assert state["flow"]["layerHeight"] == 0.2, state["flow"]
    maxs = state["flow"]["maxSpeeds"]
    assert abs(maxs["outer_wall_speed"] - 133.3) < 0.2, maxs
    assert abs(maxs["inner_wall_speed"] - 120.0) < 0.2, maxs
    assert abs(maxs["support_speed"] - 150.0) < 0.2, maxs
    assert len(maxs) == len(KEYS) + 1, maxs
    print("flow:", state["flow"])
    assert by_key["inner_wall_speed"]["label_ru"] == "Внутренняя стенка", by_key
    assert by_key["inner_wall_speed"]["label_en"] == "Inner wall", by_key
    print("state:", state["anchor"], state["value"], len(keys), "rows, lang", state["lang"],
          "vfa", state["vfa"])

    # 2. язык EN сохраняется в файл
    cap.on_message({"kind": "lang", "lang": "en"})
    st = wait_next(cap)
    assert st["kind"] == "state" and st["lang"] == "en", st
    assert read_store()["ui"]["lang"] == "en", read_store().get("ui")
    cap.on_message({"kind": "lang", "lang": "ru"})
    wait_next(cap)
    print("lang toggle ok")

    # 3. ползунок 45 + внутренняя стенка ×2.0 + мосты фиксированные 25 мм/с
    cap.on_message({
        "kind": "apply",
        "value": 45,
        "rows": rows_spec(60,
                          inner_wall_speed={"ratio": 2.0},
                          bridge_speed={"mode": "speed", "speed": 25}),
        "notes": {"inner_wall_speed": "стенка 45, инфилл ×2 даёт 90",
                  "bridge_speed": "мосты всегда 25"},
    })
    wait_next(cap)
    assert APPLIED["outer_wall_speed"] == "45", APPLIED
    assert APPLIED["inner_wall_speed"] == "90", APPLIED        # 45 * 2.0
    assert APPLIED["sparse_infill_speed"] == "150", APPLIED   # 45 * 200/60
    assert APPLIED["top_surface_speed"] == "90", APPLIED      # 45 * 2.0
    assert APPLIED["bridge_speed"] == "25", APPLIED           # режим мм/с
    assert APPLIED["support_interface_speed"] == "75", APPLIED
    assert "travel_speed" not in APPLIED, APPLIED
    print("apply:", APPLIED["outer_wall_speed"], APPLIED["inner_wall_speed"],
          APPLIED["sparse_infill_speed"], APPLIED["bridge_speed"])

    rec = read_store()["presets"]["Test Preset"]
    assert rec["slider"] == 45.0, rec
    assert rec["rows"]["inner_wall_speed"]["ratio"] == 2.0, rec
    assert rec["rows"]["bridge_speed"]["mode"] == "speed", rec
    assert rec["rows"]["bridge_speed"]["speed"] == 25.0, rec
    assert "мосты" in rec["rows"]["bridge_speed"]["note"], rec
    print("store:", rec["slider"], rec["rows"]["inner_wall_speed"],
          rec["rows"]["bridge_speed"])

    # 4. переоткрытие: база переснята, режимы/множители/пояснения восстановлены
    cap.on_message({"kind": "ready"})
    st = wait_next(cap)
    assert st["value"] == 60.0, st
    bk = {row["key"]: row for row in st["rows"]}
    assert abs(bk["inner_wall_speed"]["ratio"] - 2.0) < 1e-9, bk
    assert bk["bridge_speed"]["mode"] == "speed", bk
    assert bk["bridge_speed"]["speed"] == 25.0, bk
    assert "мосты" in bk["bridge_speed"]["note"], bk
    assert st["last"]["slider"] == 45.0, st["last"]
    assert st["lang"] == "ru", st
    print("reopen: ratio/mode/notes restored, last slider =", st["last"]["slider"])

    # 5. пояснение без применения
    cap.on_message({"kind": "note", "notes": {"gap_infill_speed": "зазоры медленно"}})
    wait_next(cap)
    rec = read_store()["presets"]["Test Preset"]
    assert rec["rows"]["gap_infill_speed"]["note"] == "зазоры медленно", rec
    print("note-only saved")

    # 6. сброс к базе
    cap.on_message({"kind": "apply", "value": 60, "rows": rows_spec(60)})
    wait_next(cap)
    assert APPLIED["outer_wall_speed"] == "60", APPLIED
    assert APPLIED["inner_wall_speed"] == "150", APPLIED
    assert APPLIED["bridge_speed"] == "30", APPLIED
    print("back to base:", APPLIED["outer_wall_speed"], APPLIED["inner_wall_speed"])

    # 7. строка «мм/с» не едет за ползунком
    cap.on_message({
        "kind": "apply",
        "value": 90,
        "rows": rows_spec(60, bridge_speed={"mode": "speed", "speed": 25}),
    })
    wait_next(cap)
    assert APPLIED["outer_wall_speed"] == "90", APPLIED
    assert APPLIED["bridge_speed"] == "25", APPLIED
    assert APPLIED["inner_wall_speed"] == "225", APPLIED     # 90 * 2.5
    print("speed row fixed:", APPLIED["bridge_speed"], "outer:", APPLIED["outer_wall_speed"])

    # 8. ошибки валидации
    cap.on_message({"kind": "apply", "value": 45,
                    "rows": rows_spec(60, inner_wall_speed={"ratio": 0})})
    assert wait_next(cap)["kind"] == "error"
    cap.on_message({"kind": "apply", "value": 45,
                    "rows": rows_spec(60, bridge_speed={"mode": "speed", "speed": 0})})
    assert wait_next(cap)["kind"] == "error"
    cap.on_message({"kind": "apply", "value": 0, "rows": rows_spec(60)})
    err = wait_next(cap)
    assert err["kind"] == "error", err
    print("validation:", err["message"][:50])

    cap.on_unload()

    # 9. нет outer_wall_speed -> внятная ошибка
    module2 = load_plugin(build_fake_orca(anchor_present=False))
    cap2 = module2.EasySpeedSliderPage()
    cap2.on_message({"kind": "ready"})
    out2 = wait_next(cap2)
    assert out2["kind"] == "error", out2
    print("no anchor:", out2["message"][:70])
    cap2.on_unload()

    # 10. commit падает -> ошибка пользователю, плагин жив
    fake3 = build_fake_orca()
    module3 = load_plugin(fake3)
    cap3 = module3.EasySpeedSliderPage()
    cap3.on_message({"kind": "ready"})
    wait_next(cap3)
    Cfg.fail_commit = True
    cap3.on_message({"kind": "apply", "value": 45, "rows": rows_spec(60)})
    err = wait_next(cap3)
    Cfg.fail_commit = False
    assert err["kind"] == "error", err
    assert "коммит" in err["message"].lower(), err
    print("commit error:", err["message"][:60])

    # 11. смена пресета -> новая база
    PRESET["outer_wall_speed"] = "80"
    PRESET["inner_wall_speed"] = "200"
    cap3.on_lifecycle_event(
        fake3.LifecycleEvent.PresetSelected,
        types.SimpleNamespace(msg="print"),
    )
    st = wait_next(cap3)
    assert st["kind"] == "state", st
    assert st["anchor"] == 80.0 and st["value"] == 80.0, st
    print("preset switch:", st["anchor"])

    # 11b. смена принтера -> переснимаем базу и полосу VFA
    PRESET["max_resonance_avoidance_speed"] = "150"
    cap3.on_lifecycle_event(
        fake3.LifecycleEvent.PresetSelected,
        types.SimpleNamespace(msg="printer"),
    )
    st = wait_next(cap3)
    assert st["kind"] == "state", st
    assert st["vfa"] == {"enabled": True, "min": 70.0, "max": 150.0}, st["vfa"]
    print("printer switch:", st["vfa"])

    # 12. полоса избегания резонанса выключена: значения остаются, enabled=False
    PRESET["resonance_avoidance"] = "0"
    PRESET["max_resonance_avoidance_speed"] = "120"
    cap3.on_message({"kind": "ready"})
    st = wait_next(cap3)
    assert st["kind"] == "state", st
    assert st["vfa"] == {"enabled": False, "min": 70.0, "max": 120.0}, st["vfa"]
    print("vfa off:", st["vfa"])

    # 12b. объёмный расход: процентная ширина и худший лимит среди филаментов
    PRESET["outer_wall_line_width"] = "105%"
    PRESET["filament_max_volumetric_speed"] = "12,8"
    cap3.on_message({"kind": "ready"})
    st = wait_next(cap3)
    assert st["flow"]["enabled"] is True, st["flow"]
    assert st["flow"]["limit"] == 8.0, st["flow"]
    # 105% от сопла 0.4 = 0.42 мм; 8 / (0.42 * 0.2) = 95.2 мм/с
    assert abs(st["flow"]["maxSpeeds"]["outer_wall_speed"] - 95.2) < 0.3, st["flow"]
    print("flow percent/min:", st["flow"])

    cap3.on_unload()

    # 13. парсер и разбор bool
    assert module3._parse_list("60,60") == [60.0, 60.0]
    assert module3._parse_list(["45", 45.0]) == [45.0, 45.0]
    assert module3._parse_list("50%") is None
    assert module3._parse_list(None) is None
    assert module3._flag("1") and module3._flag(True) and module3._flag("true")
    assert not module3._flag("0") and not module3._flag(None) and not module3._flag("")
    print("parse ok")

    # 14. старые сборки без resonance_avoidance -> полоса выключена, плагин жив
    for _key in ("resonance_avoidance", "min_resonance_avoidance_speed",
                 "max_resonance_avoidance_speed"):
        PRESET.pop(_key, None)
    PRESET.pop("filament_max_volumetric_speed", None)
    cap4 = load_plugin(build_fake_orca()).EasySpeedSliderPage()
    cap4.on_message({"kind": "ready"})
    st = wait_next(cap4)
    assert st["kind"] == "state", st
    assert st["vfa"] == {"enabled": False, "min": 0.0, "max": 0.0}, st["vfa"]
    assert st["flow"]["enabled"] is False, st["flow"]
    print("vfa missing keys ok:", st["vfa"], "| flow off:", st["flow"]["enabled"])
    cap4.on_unload()

    print("OK: все проверки прошли")


if __name__ == "__main__":
    main()

# Публикация Easy Speed Slider в OrcaCloud

Всё, что нужно для загрузки, лежит рядом:

| Файл | Что это |
|---|---|
| `easy_speed_slider_any.py` | сам плагин, имя с target-суффиксом `_any` (универсальный, чистый Python) |
| `easy_speed_slider_icon.png` | иконка листинга 512×512, PNG, 7 КБ |

Требования OrcaCloud, которым они соответствуют: `.py`/`.whl` в имени обязан заканчиваться
на поддерживаемый OS-суффикс (`win_x86_64`, `macosx_arm64`, `linux_x86_64`, `any`, …);
до 20 файлов в релизе, до 100 МБ на файл; иконка PNG/JPG/WEBP/GIF до 2 МБ; в метаданных
`.py` должны быть name, version и type (script block, header comment или `PLUGIN_TYPE`).

## Шаги

1. Войди в OrcaCloud под тем же аккаунтом, что в OrcaSlicer.
2. Открой **Plugins → Shared Plugins** (слева в навигации, в бете помечено Beta).
3. Нажми **+** в правом нижнем углу.
4. В диалоге **Create plugin**:
   - перетащи `easy_speed_slider_any.py` в **Plugin Files**;
   - добавь **Plugin Image** — `easy_speed_slider_icon.png`;
   - Name подставится из файла (`Easy Speed Slider`), можно поправить;
   - вставь описание (ниже);
   - Version: `0.7.1`;
   - Type: **Script** (в таксономии OrcaCloud сейчас только Script, Slicing-pipeline,
     Resource textures, Resource printer assets — отдельного «Pages» нет; вкладка ESS
     при этом работает как обычно, тип влияет только на фильтры в Hub);
   - Compatible OrcaSlicer Version: **2.5.0** и выше;
   - Changelog (ниже), теги `utility, speeds, calibration, quality`;
   - включи **Public Plugin**, если хочешь, чтобы плагин был в Plugin Hub.
5. **Create plugin** → запустится автоматическая проверка (AI review). Результат видно до
   публикации; если что-то помечено — можно «Publish anyway», текст уйдёт модераторам.
6. Нажми **Publish** (ничего не публикуется до этой кнопки).

После публикации: подписка в Hub → в OrcaSlicer **File → Plugins** под тем же аккаунтом →
**Refresh** → галочка **Activate** у capability `ESS`.

## Текст для формы

**Name:** `Easy Speed Slider`

**Короткое описание (RU, одна строка, если понадобится):**
`Один ползунок для скоростей печати: внешняя стенка как опора, остальные параметры — множителем к ней или в мм/с, с заметками на каждый пресет.`

**Short description (EN, one line, if needed):**
`One slider for print speeds: the outer wall is the reference, every other parameter follows it as a multiplier or is pinned in mm/s, with notes per preset.`

**Description (RU):**

```markdown
## Зачем я это сделал

Я устал вручную следить за кучей взаимозависимых настроек — особенно когда постоянно меняешь
филамент. Поэтому решил сделать небольшой «костыль», который хотя бы немного улучшит качество
жизни.

Часть этой логики в OrcaSlicer уже есть, но не для всех типов линий.

Смысл в том, что для качественной печати скорости разных типов линий не должны сильно отличаться
друг от друга. Если развить мысль дальше: чтобы держать баланс между качеством и скоростью, имеет
смысл задать зависимость между скоростями линий, взяв за базу скорость внешней стенки. Именно она
отвечает за то, как выглядит готовое изделие, и при настройке печати в первую очередь опираются на
неё.

Поэтому и появился этот мини-плагин: в его настройках можно, исходя из своего опыта и проведённых
тестов, вписать усреднённую зависимость скоростей линий от базовой скорости внешнего периметра.

## Что делает

Easy Speed Slider добавляет в OrcaSlicer вкладку **ESS**. Короткий ползунок задаёт **скорость
внешней стенки**, а каждая остальная скорость печати либо следует за ней пропорционально
(множителем к скорости стенки), либо закрепляется на своём абсолютном значении в мм/с.

## Как это работает

- У каждой строки свой множитель **×** к скорости внешней стенки: `2` — «вдвое быстрее стенки».
  Множители снимаются с пропорций текущего профиля, поэтому нетронутая страница ничего не меняет.
- Любую строку можно переключить в **мм/с** и закрепить на абсолютной скорости, не зависящей от
  ползунка.
- Колонка «Почему так» хранит заметку для каждой строки; заметки сохраняются автоматически и
  запоминаются для каждого пресета.
- **Применить** записывает весь набор в текущий process-пресет одним атомарным коммитом. Пресет
  становится изменённым — сохраните его в OrcaSlicer как обычно.
- **Сбросить к базе** возвращает исходные пропорции профиля.
- Интерфейс двуязычный: **RU / EN**.
- Если в настройках принтера задан **диапазон избегания резонанса** (VFA), вкладка предупреждает,
  когда скорость стенки попадает в него; при включённом «Избегании ряби» ещё и показывает, на какой
  скорости слайсер реально напечатает стенку (прижимает к минимуму или максимуму диапазона).
- Если у филамента задан **максимальный объёмный расход**, вкладка оценивает предел скорости по
  ширине линии и высоте слоя и подсвечивает строки, где слайсер срежет скорость (это оценка).

## Требования

- OrcaSlicer **2.5.0** или новее (плагин использует API редактирования пресетов, появившийся в
  2.5.0 beta/rc).
- Один запрос разрешения при первом включении (создание рабочего потока) — правки пресета не должны
  выполняться в UI-потоке.

## Примечания

- Плагин не трогает перемещения, первый слой и настройки филамента.
- Плагин не меняет скорость сам, чтобы обойти диапазон VFA: он только предупреждает, коррекцию
  по-прежнему делает слайсер (и только для внешних периметров).
- Оценка по объёмному расходу приблизительная: адаптивная высота слоя, Arachne и
  `filament_adaptive_volumetric_speed` не учитываются.
- Множитель всегда считается от **скорости внешней стенки**, а не от собственного значения параметра.
```

**Description (EN):**

```markdown
## Why I made it

I got tired of tracking a pile of interdependent settings by hand — especially when you change
filament all the time. So I made a small workaround that at least makes life a bit easier.

Part of this logic already exists in OrcaSlicer, but not for every line type.

The idea is that for good print quality the speeds of different line types should not differ too
much from each other. Taking it further: to keep a balance between quality and speed, it makes
sense to set a dependency between line speeds using the outer wall speed as the base. The outer
wall defines how the finished part looks, and when tuning a print people lean on it first.

That is why this mini-plugin exists: in its settings you can enter an averaged dependency of line
speeds on the base outer perimeter speed, based on your own experience and tests.

## What it does

Easy Speed Slider adds an **ESS** tab to OrcaSlicer. One short slider sets the **outer wall speed**,
and every other print speed either follows it proportionally (as a multiplier of the wall speed)
or is pinned to its own absolute value in mm/s.

## How it works

- Each row has its own multiplier **×** relative to the outer wall speed: `2` means "twice as fast
  as the wall". Multipliers start from the proportions already in your profile, so an untouched
  page changes nothing.
- Any row can be switched to **mm/s** and pinned to an absolute speed that ignores the slider.
- The **Why** column stores a note per row, saved automatically and remembered per preset.
- **Apply** writes the whole set into the current process preset in one atomic commit. The preset
  becomes modified — save it in OrcaSlicer as usual.
- **Reset to base** restores the original proportions of the profile.
- Interface is bilingual: **RU / EN**.
- If the printer settings define a **resonance avoidance** (VFA) speed band, the tab warns when the
  wall speed falls inside it; with resonance avoidance enabled it also shows the speed the slicer
  will actually print the wall at (clamped to the min/max band edge).
- If the filament has a **max volumetric speed**, the tab estimates the speed limit from line width
  and layer height and highlights the rows the slicer will cap (an estimate).

## Requirements

- OrcaSlicer **2.5.0** or newer (the plugin uses the preset editing API that appeared in 2.5.0
  beta/rc).
- One permission prompt on first load (creating a worker thread) — needed because preset edits
  must not run on the UI thread.

## Notes

- The plugin does not touch travel, first-layer or filament-level settings.
- It does not change speeds itself to dodge Resonance Avoidance / VFA bands: it only warns; the
  slicer still applies its correction to external perimeters.
- The volumetric-flow estimate is approximate: adaptive layer height, Arachne and
  `filament_adaptive_volumetric_speed` are not modelled.
- Multiplier is always relative to the **outer wall speed**, never to the parameter's own value.
```

**Changelog:**

```
0.7.1 — volumetric flow check: estimates the per-role speed limit from the filament's max
volumetric speed, line width, layer height and flow ratios; highlights rows the slicer will cap.
Also adds the speedometer icon to the ESS tab.
0.7.0 — resonance avoidance (VFA) check: shows the avoided speed band from the printer settings,
warns when the wall speed falls into it and reports the speed the slicer will actually use.
0.6.1 — first public release.
Slider for the outer wall speed; per-row multipliers relative to the wall (or absolute mm/s);
notes saved per preset; bilingual RU/EN interface.
```

## Публикация из GitHub (без токенов)

Репозиторий: `https://github.com/tarknap/easy-speed-slider`. Workflow уже лежит в
`.github/workflows/publish-orcacloud.yml`.

1. В OrcaCloud: **Edit plugin → GitHub publishing** → `tarknap/easy-speed-slider` → **Connect**.
2. В GitHub создай релиз с тегом `v0.7.1` (черновик уже подготовлен) и нажми **Publish release**.
3. Workflow проверит, что тег совпадает с версией в манифесте, что
   `publish/easy_speed_slider_any.py` синхронизирован с `speed_slider.py`, и отправит файл в
   OrcaCloud. Дальше обновление подхватится в OrcaSlicer через облако.

Если тег не совпадает с версией в `speed_slider.py` или publish-файл устарел — workflow упадёт
с понятной ошибкой, ничего не публикуя.

Актуальный workflow — `.github/workflows/publish-orcacloud.yml` (проверка версии, подготовка
`dist/` и отправка в `api.orcaslicer.com`). Тег релиза должен быть semver выше текущей
опубликованной версии, файл — с OS-суффиксом в имени (`_any`).

## Перед публикацией — что проверить самому

- Плагин ставится с нуля: скопировать `easy_speed_slider_any.py` в пустую папку
  `orca_plugins/ESS/`, перезапустить OrcaSlicer, включить, открыть вкладку — таблица заполняется.
- На чистом профиле (без `speed_slider.json`) примечания пустые, ползунок на текущей стенке,
  «Применить» без изменений не меняет пресет.
- Текст описания не содержит ссылок на сторонние загрузки — модерация такое любит помечать.

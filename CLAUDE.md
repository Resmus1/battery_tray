# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Battery Tray** (formerly Mouse Tray Charge / `mouse_tray`, renamed in 0.5.0) —
a **universal** battery indicator
for wireless mice, gamepads and Bluetooth devices in the Windows system tray. The
whole design turns on one idea: every mouse — whatever the vendor or transport —
is reduced to a single normalized [`BatteryStatus`](battery_tray/battery.py), and a
single state machine in [`ui/app.py`](battery_tray/ui/app.py) renders that into the
tray icon. Vendor code does exactly two things: **detect the device** and **parse
its battery report**. Nothing else in the app knows which mouse is connected.

Windows-only at runtime (wxPython tray, `hidapi`, Windows registry for storage).

## Commands

```sh
uv sync                                          # install deps into .venv
uv run python main.py                            # run from source (or: uv run python -m battery_tray)
uv run --extra build python tools/make_release.py   # build dist/battery_tray/ (.exe via PyInstaller)
BATTERY_TRAY_DEBUG=1 uv run python main.py          # verbose logging: raw HID reports
uvx ruff check .                                 # lint (config in pyproject.toml)
```

**Ruff is configured but there is no test suite, formatter, or CI** — no pytest,
no `ruff format`, no workflows. The lint config in `pyproject.toml` deliberately
leaves `BLE`, `S110` and `DTZ` unselected; read the comments there before
re-enabling them, they encode real invariants of this app.

"Testing" a driver means running against real hardware and reading
`%LOCALAPPDATA%\Battery_Tray\app.log` (rotating, 1 MB × 3; set `debug` / the env var
above for raw report bytes). Protocols are reverse-engineered from USB captures
kept under `examples/`.

**`examples/` is gitignored and local-only — it is not part of the repository.**
Do not reference anything under `examples/` in committed docs (README, CLAUDE.md,
code comments): those paths won't exist for anyone else. Use it as a local
scratch/reference source only.

## Architecture

The layering is deliberately strict — respect it when adding code:

- [`battery.py`](battery_tray/battery.py) — `BatteryStatus(present/percent/charging/full/asleep)`.
  The one type every driver returns and the UI consumes. `BatteryStatus.absent()`
  means "no mouse".
- [`drivers/driver.py`](battery_tray/drivers/driver.py) — the `MouseDriver` ABC
  (`detect_all()` + `read_status()`, plus the stable `key` identity), the
  `MouseModel` row (VID/PID + optional HID disambiguators), and the `@register`
  decorator + registry (`detect_all_drivers()` probes every driver in
  registration order and returns *all* connected mice, deduplicated by `key`).
- [`drivers/bus.py`](battery_tray/drivers/bus.py) — one briefly-cached snapshot of
  `hid.enumerate`, shared by both transport bases. Detection walks every model of
  every driver, so the bus is enumerated once per sweep instead of once per model.
- [`drivers/hid.py`](battery_tray/drivers/hid.py) — `HidDriver`: the common
  single-transaction base (one request → one reply, fixed offsets) over `hidapi`.
  Subclasses implement only `read_status()` and call `self._transact(...)`.
- [`drivers/hidpp.py`](battery_tray/drivers/hidpp.py) — `HidppDriver`: multi-step
  HID++ 2.0 base for Logitech (feature discovery, device-index routing, two
  report formats). Subclasses `MouseDriver` **directly**, sharing none of the
  single-transaction model — the proof that `MouseDriver`, not `HidDriver`, is the
  real abstraction.
- [`ui/about.py`](battery_tray/ui/about.py) — the About dialog. Its supported-mouse
  table is generated from `all_drivers()`, so a new driver appears there for
  free; the optional `MouseDriver.note` is the one place a driver may add prose
  about its own model list, keeping vendor knowledge out of the UI.
- [`ui/app.py`](battery_tray/ui/app.py) — the wx app, the background polling thread,
  and the single `_apply_status` state machine. This module **never imports a
  driver module**, only `detect_all_drivers()` and the `MouseDriver` contract.
  `_apply_status` also drives the fork's extras: low-battery toasts
  (`_warn_low`) and the time-left estimate (`_track_discharge` →
  [`estimate.py`](battery_tray/estimate.py) `DischargeEstimator`, whose stretches
  persist per mouse in the registry `Discharge` subkey).
- [`ui/settings.py`](battery_tray/ui/settings.py) — edits a `Config` copy only;
  `app._open_settings` persists it and refreshes the tray. Font/size/outline/icon
  changes are previewed live via `PreviewHooks`. "Start with Windows" is the
  exception: it lives only in the Windows `Run` key
  ([`autostart.py`](battery_tray/autostart.py)) and the dialog writes it itself.
- [`build_info.py`](battery_tray/build_info.py) — the version label's commit hash
  comes from `git rev-parse` in a checkout, or from `battery_tray/_commit.py`
  (gitignored) that `tools/make_release.py` writes for the frozen exe.

### Three things to know about the runtime

- **Threading:** polling runs on a daemon worker thread; it touches widgets only
  via `wx.CallAfter`. All wx/UI work stays on the main thread. Don't call UI code
  from `read_status()` or the poll loop.
- **Re-detection:** every tick re-runs detection (cheap — one cached bus
  enumeration) so a mouse plugged in later appears on its own. Driver objects are
  reused across sweeps, keyed by `key`, because they cache per-device state.
  `detect_all()`/`read_status()` **must never raise** for an absent device or I/O
  hiccup; return an empty list / `BatteryStatus.absent()`. Between ticks the
  worker sleeps the full `poll_rate`; hot-plug (cable, dongle) wakes it early
  via `WM_DEVICECHANGE` ([`ui/devnotify.py`](battery_tray/ui/devnotify.py)), as
  does a left click on the icon.
- **Which mice the tray shows:** all connected devices are detected; up to
  `Config.tray_icons` (1–4) are read, one tray icon (`_TraySlot`) each. With no
  pin the worker picks the first that report `present` and sticks to them
  (`_auto_keys`) — so a plugged dongle whose mouse is off doesn't shadow a
  working one. Pins from the tray menu are *strict*: an unplugged pinned device
  shows "-", it never falls back to another one. Pins live in the registry
  (`SelectedMice`, REG_MULTI_SZ of `key<TAB>name`; the old single
  `SelectedMouse` / `SelectedMouseName` is still written for downgrades and read
  as a fallback); the worker owns the driver objects and only ever *reads* the
  pins, which keeps driver state single-threaded.
- **Slots vs. devices:** slots are pooled and only hidden, never destroyed
  before exit (destroying a `TaskBarIcon` with its menu open is unsafe), and
  slot 0 is always visible because the notifications hang off it. Everything
  remembered about a device — full-charge timer, `DischargeEstimator`, warnings
  already shown — lives in `_MouseState`, keyed by display name like the
  registry, never in the slot: a device can land in another slot after a re-plug.

## Adding mouse support

Drivers live in two subpackages: `drivers/chipset/` for shared-silicon protocols
(named after the chipset, e.g. `nordic52`, `nordic54`, `realtek` — one protocol
spanning every brand that rebrands that chip) and `drivers/vendor/` for
brand-specific protocols. Pick by what the protocol actually keys on, not the
brand on the box. A third, `drivers/system/`, holds drivers with no protocol of
their own: [`bluetooth.py`](battery_tray/drivers/system/bluetooth.py) reads the
level Windows keeps for paired Bluetooth devices (PnP property via CfgMgr32 /
`ctypes`, no extra dependency). It has no `models`, keys devices by container
id, probes last and skips any device whose address shows up in the HID path of
a model another driver lists.

- **Same protocol, new model** — add one `MouseModel` row to that driver's
  `models` list (VID, wireless PID, wired PID, plus `usage_page`/`usage`/
  `interface` only as needed to select the right HID collection). No new code.
  Many off-brand mice ride the Compx/Nordic 52840 silicon and drop straight into
  `chipset/nordic52` this way.
- **New protocol** — add `drivers/<subpkg>/<name>.py`, subclass `HidDriver` (or
  `HidppDriver` for multi-step), declare `vendor` + `models`, implement
  `read_status()`, decorate the class with `@register`, **and add the module to
  `_DRIVER_MODULES` in [`drivers/__init__.py`](battery_tray/drivers/__init__.py)**
  (that list is both the import list and the detection probe order). Detection,
  the tray UI, and packaging then pick it up automatically.

PyInstaller can't see the dynamically-imported drivers statically, so the build
uses `--collect-submodules battery_tray.drivers` — new drivers are bundled for free,
no `.spec` edit needed.

## Storage & config

- User settings and per-mouse "last full charge" timestamps live in the Windows
  registry under `HKCU\SOFTWARE\Battery_Tray\` (see
  [`storage.py`](battery_tray/storage.py)). Settings apply immediately and persist.
- [`config.py`](battery_tray/config.py) holds `Config` (poll rates, colors, font,
  debug) and `VERSION` (the single source of the runtime version). Note the split:
  **`app_name`** is the stable storage/identity key (registry subkey, autostart
  value, log dir — do not change casually; the 0.5.0 rename from `Mouse_Tray`
  needed `LEGACY_APP_NAME` plus `storage.adopt_legacy_data` and
  `autostart.adopt_legacy`, run once at start-up) while **`display_name`** is the human-facing label
  (tooltip, toasts, settings title) and is free to change.

## Форк Resmus1/battery_tray

Это форк [Fan4Metal/mouse_tray](https://github.com/Fan4Metal/mouse_tray). Сверх
оригинала: уведомления о разряде (пороги, повтор, кнопка «Тест»), оценка
оставшегося времени ([`estimate.py`](battery_tray/estimate.py)), русский интерфейс
([`i18n.py`](battery_tray/i18n.py)), настройки по разделам, сборка релизов в
GitHub Actions, геймпады Sony
([`vendor/sony.py`](battery_tray/drivers/vendor/sony.py): каждый геймпад — свой
экземпляр с ключом по серийному номеру, т. е. BT-адресу, поэтому одинаковые
геймпады различаются), до четырёх устройств в трее одновременно и любые
Bluetooth-устройства, чей заряд знает Windows
([`system/bluetooth.py`](battery_tray/drivers/system/bluetooth.py)). Видимое имя —
**Battery Tray**: в 0.5.0 переименовано всё — пакет `battery_tray`, exe, путь
установки, `app_name` `Battery_Tray`. Первый запуск копирует данные из
`HKCU\SOFTWARE\Mouse_Tray` и переносит автозапуск; ссылки на upstream остаются
`Fan4Metal/mouse_tray`, а имена классов `MouseDriver`/`MouseModel` — как у автора,
чтобы слияния с upstream оставались простыми.
Новые строки интерфейса пишутся на английском через `tr()`, а перевод
добавляется в таблицу `_RU` в `i18n.py`.

### Пути

- Репозиторий: `Z:\home-projects\mouse_tray`, то же самое, что
  `\192.168.3.10\tank\home-projects\mouse_tray`; на сервере это
  `/tank/storage/home-projects/mouse_tray`.
- Remotes: `origin` — `Resmus1/battery_tray` (ветка `master`), `upstream` —
  `Fan4Metal/mouse_tray`. Обновления автора: `git pull upstream master`.
- Установленная программа: `%LOCALAPPDATA%\Programs\battery_tray`
  (`battery_tray.exe` + `_internal\`). Лог: `%LOCALAPPDATA%\Battery_Tray\app.log`.
  Реестр: `HKCU\SOFTWARE\Battery_Tray\` (`Settings`, `FullCharge`, `Discharge`).
- Git на сетевой папке требует `safe.directory` для этого пути: если его нет,
  передать разово через `git -c safe.directory=...`, а в глобальный конфиг
  пользователь добавляет его сам. `.venv` и сборку на сервере не держать:
  собирать и тестировать в локальном клоне во `%TEMP%`, потом удалить его.

### Сборка и релиз

1. Поднять `VERSION` в [`config.py`](battery_tray/config.py).
2. Закоммитить, `git push origin master`.
3. `git tag -a vX.Y.Z -m "battery_tray X.Y.Z"` и `git push origin vX.Y.Z`.
4. [`.github/workflows/release.yml`](.github/workflows/release.yml) собирает exe
   на `windows-latest` по `uv.lock` (Python 3.14, wxPython 4.2.5) и создаёт
   Release с `battery_tray-vX.Y.Z.zip`. Следить: `gh run watch`.

Локально: `uv sync --extra build`, затем `uv run python tools/make_release.py`.
Если uv не может поставить Python в профиль пользователя, задать
`UV_PYTHON_INSTALL_DIR` на локальную папку.

### Установка и обновление

Закрыть программу (меню в трее → Exit), распаковать zip из Release и заменить
содержимое `%LOCALAPPDATA%\Programs\battery_tray`, например
`robocopy <распакованная>\battery_tray %LOCALAPPDATA%\Programs\battery_tray /MIR`;
потом запустить exe. Путь не менять: Windows привязывает к пути exe и
автозапуск, и включение значка в «Других значках в системном трее». При смене
пути их приходится включать заново.

### Проверка перед релизом

- **Тестировать на wxPython 4.2.5**, в окружении из `uv.lock`. Свежий
  `pip install wxpython` ставит 4.3, где есть API, которых нет в 4.2.5:
  `StdDialogButtonSizer.GetAffirmativeButton()` прошёл тесты на 4.3 и уронил
  окно настроек в релизе.
- У exe нет консоли: необработанные ошибки пишутся в `app.log` через
  `sys.excepthook`. Смотреть туда, а не ждать трейсбека.
- Не импортировать модули, исключённые в `EXCLUDES` в
  [`tools/make_release.py`](tools/make_release.py): например, `statistics`
  тянет `decimal`, поэтому в `estimate.py` своя медиана.
- Вёрстку окон проверять скриншотом: показать диалог, снять его через
  `PIL.ImageGrab` и посмотреть оба языка.
- `uvx ruff check .` проходит чисто (`All checks passed!`) — так и держать.
- Окончания строк у файлов в git разные: `estimate.py`, `i18n.py`,
  `logging_setup.py`, `ui/app.py`, `ui/tray.py`, `pyproject.toml`,
  `tools/make_release.py` — CRLF, остальные — LF. Сохранять как в git
  (`core.autocrlf=false`); правка с Windows по сетевой папке может молча
  перевести файл в CRLF — такие изменения без смены содержимого не коммитить
  (проверка: `git diff --ignore-cr-at-eol --stat` пуст).

### Коммиты

- Без строк `Co-Authored-By: Claude …` и `Generated with Claude Code` в
  коммитах и PR.
- Автор задан в `.git/config` репозитория: `Re$mus
  <156493623+Resmus1@users.noreply.github.com>` (скрытая почта GitHub).

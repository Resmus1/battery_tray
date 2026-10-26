# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **universal** wireless-mouse battery indicator for the Windows system tray. The
whole design turns on one idea: every mouse — whatever the vendor or transport —
is reduced to a single normalized [`BatteryStatus`](mouse_tray/battery.py), and a
single state machine in [`ui/app.py`](mouse_tray/ui/app.py) renders that into the
tray icon. Vendor code does exactly two things: **detect the device** and **parse
its battery report**. Nothing else in the app knows which mouse is connected.

Windows-only at runtime (wxPython tray, `hidapi`, Windows registry for storage).

## Commands

```sh
uv sync                                          # install deps into .venv
uv run python main.py                            # run from source (or: uv run python -m mouse_tray)
uv run --extra build python tools/make_release.py   # build dist/mouse_tray/ (.exe via PyInstaller)
MOUSE_TRAY_DEBUG=1 uv run python main.py          # verbose logging: raw HID reports
uvx ruff check .                                 # lint (config in pyproject.toml)
```

**Ruff is configured but there is no test suite, formatter, or CI** — no pytest,
no `ruff format`, no workflows. The lint config in `pyproject.toml` deliberately
leaves `BLE`, `S110` and `DTZ` unselected; read the comments there before
re-enabling them, they encode real invariants of this app.

"Testing" a driver means running against real hardware and reading
`%LOCALAPPDATA%\Mouse_Tray\app.log` (rotating, 1 MB × 3; set `debug` / the env var
above for raw report bytes). Protocols are reverse-engineered from USB captures
kept under `examples/`.

**`examples/` is gitignored and local-only — it is not part of the repository.**
Do not reference anything under `examples/` in committed docs (README, CLAUDE.md,
code comments): those paths won't exist for anyone else. Use it as a local
scratch/reference source only.

## Architecture

The layering is deliberately strict — respect it when adding code:

- [`battery.py`](mouse_tray/battery.py) — `BatteryStatus(present/percent/charging/full/asleep)`.
  The one type every driver returns and the UI consumes. `BatteryStatus.absent()`
  means "no mouse".
- [`drivers/driver.py`](mouse_tray/drivers/driver.py) — the `MouseDriver` ABC
  (`detect_all()` + `read_status()`, plus the stable `key` identity), the
  `MouseModel` row (VID/PID + optional HID disambiguators), and the `@register`
  decorator + registry (`detect_all_drivers()` probes every driver in
  registration order and returns *all* connected mice, deduplicated by `key`).
- [`drivers/bus.py`](mouse_tray/drivers/bus.py) — one briefly-cached snapshot of
  `hid.enumerate`, shared by both transport bases. Detection walks every model of
  every driver, so the bus is enumerated once per sweep instead of once per model.
- [`drivers/hid.py`](mouse_tray/drivers/hid.py) — `HidDriver`: the common
  single-transaction base (one request → one reply, fixed offsets) over `hidapi`.
  Subclasses implement only `read_status()` and call `self._transact(...)`.
- [`drivers/hidpp.py`](mouse_tray/drivers/hidpp.py) — `HidppDriver`: multi-step
  HID++ 2.0 base for Logitech (feature discovery, device-index routing, two
  report formats). Subclasses `MouseDriver` **directly**, sharing none of the
  single-transaction model — the proof that `MouseDriver`, not `HidDriver`, is the
  real abstraction.
- [`ui/about.py`](mouse_tray/ui/about.py) — the About dialog. Its supported-mouse
  table is generated from `all_drivers()`, so a new driver appears there for
  free; the optional `MouseDriver.note` is the one place a driver may add prose
  about its own model list, keeping vendor knowledge out of the UI.
- [`ui/app.py`](mouse_tray/ui/app.py) — the wx app, the background polling thread,
  and the single `_apply_status` state machine. This module **never imports a
  driver module**, only `detect_all_drivers()` and the `MouseDriver` contract.

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
  via `WM_DEVICECHANGE` ([`ui/devnotify.py`](mouse_tray/ui/devnotify.py)), as
  does a left click on the icon.
- **Which mouse the tray shows:** all connected mice are detected, only one is
  read. With no pin the worker picks the first that reports `present` and sticks
  to it (`_auto_key`) — so a plugged dongle whose mouse is off doesn't shadow a
  working one. When the user pins a mouse from the tray menu the pin is *strict*:
  if it is unplugged the tray shows "-", it never falls back to another mouse.
  The pin lives in the registry (`SelectedMouse` / `SelectedMouseName`); the
  worker owns the driver objects and only ever *reads* the pin, which keeps
  driver state single-threaded.

## Adding mouse support

Drivers live in two subpackages: `drivers/chipset/` for shared-silicon protocols
(named after the chipset, e.g. `nordic52`, `nordic54`, `realtek` — one protocol
spanning every brand that rebrands that chip) and `drivers/vendor/` for
brand-specific protocols. Pick by what the protocol actually keys on, not the
brand on the box.

- **Same protocol, new model** — add one `MouseModel` row to that driver's
  `models` list (VID, wireless PID, wired PID, plus `usage_page`/`usage`/
  `interface` only as needed to select the right HID collection). No new code.
  Many off-brand mice ride the Compx/Nordic 52840 silicon and drop straight into
  `chipset/nordic52` this way.
- **New protocol** — add `drivers/<subpkg>/<name>.py`, subclass `HidDriver` (or
  `HidppDriver` for multi-step), declare `vendor` + `models`, implement
  `read_status()`, decorate the class with `@register`, **and add the module to
  `_DRIVER_MODULES` in [`drivers/__init__.py`](mouse_tray/drivers/__init__.py)**
  (that list is both the import list and the detection probe order). Detection,
  the tray UI, and packaging then pick it up automatically.

PyInstaller can't see the dynamically-imported drivers statically, so the build
uses `--collect-submodules mouse_tray.drivers` — new drivers are bundled for free,
no `.spec` edit needed.

## Storage & config

- User settings and per-mouse "last full charge" timestamps live in the Windows
  registry under `HKCU\SOFTWARE\Mouse_Tray\` (see
  [`storage.py`](mouse_tray/storage.py)). Settings apply immediately and persist.
- [`config.py`](mouse_tray/config.py) holds `Config` (poll rates, colors, font,
  debug) and `VERSION` (the single source of the runtime version). Note the split:
  **`app_name`** is the stable storage/identity key (registry subkey + log dir —
  do not change casually) while **`display_name`** is the human-facing label
  (tooltip, toasts, settings title) and is free to change.

## Форк Resmus1/mouse_tray

Это форк [Fan4Metal/mouse_tray](https://github.com/Fan4Metal/mouse_tray). Сверх
оригинала: уведомления о разряде (пороги, повтор, кнопка «Тест»), оценка
оставшегося времени ([`estimate.py`](mouse_tray/estimate.py)), русский интерфейс
([`i18n.py`](mouse_tray/i18n.py)), настройки по разделам, сборка релизов в
GitHub Actions. Новые строки интерфейса пишутся на английском через `tr()`, а
перевод добавляется в таблицу `_RU` в `i18n.py`.

### Пути

- Репозиторий: `Z:\home-projects\mouse_tray`, то же самое, что
  `\192.168.3.10\tank\home-projects\mouse_tray`; на сервере это
  `/tank/storage/home-projects/mouse_tray`.
- Remotes: `origin` — `Resmus1/mouse_tray` (ветка `master`), `upstream` —
  `Fan4Metal/mouse_tray`. Обновления автора: `git pull upstream master`.
- Установленная программа: `%LOCALAPPDATA%\Programs\mouse_tray`
  (`mouse_tray.exe` + `_internal\`). Лог: `%LOCALAPPDATA%\Mouse_Tray\app.log`.
  Реестр: `HKCU\SOFTWARE\Mouse_Tray\` (`Settings`, `FullCharge`, `Discharge`).
- Git на сетевой папке требует `safe.directory` для этого пути: если его нет,
  передать разово через `git -c safe.directory=...`, а в глобальный конфиг
  пользователь добавляет его сам. `.venv` и сборку на сервере не держать:
  собирать и тестировать в локальном клоне во `%TEMP%`, потом удалить его.

### Сборка и релиз

1. Поднять `VERSION` в [`config.py`](mouse_tray/config.py).
2. Закоммитить, `git push origin master`.
3. `git tag -a vX.Y.Z -m "mouse_tray X.Y.Z"` и `git push origin vX.Y.Z`.
4. [`.github/workflows/release.yml`](.github/workflows/release.yml) собирает exe
   на `windows-latest` по `uv.lock` (Python 3.14, wxPython 4.2.5) и создаёт
   Release с `mouse_tray-vX.Y.Z.zip`. Следить: `gh run watch`.

Локально: `uv sync --extra build`, затем `uv run python tools/make_release.py`.
Если uv не может поставить Python в профиль пользователя, задать
`UV_PYTHON_INSTALL_DIR` на локальную папку.

### Установка и обновление

Закрыть программу (меню в трее → Exit), распаковать zip из Release и заменить
содержимое `%LOCALAPPDATA%\Programs\mouse_tray`, например
`robocopy <распакованная>\mouse_tray %LOCALAPPDATA%\Programs\mouse_tray /MIR`;
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
- `ruff check mouse_tray`: в коде автора 28 старых замечаний, новых не
  добавлять.
- Окончания строк у файлов разные (часть в CRLF), сохранять как есть.

### Коммиты

- Без строк `Co-Authored-By: Claude …` и `Generated with Claude Code` в
  коммитах и PR.
- Автор задан в `.git/config` репозитория: `Re$mus
  <156493623+Resmus1@users.noreply.github.com>` (скрытая почта GitHub).

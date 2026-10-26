# Battery Tray

**English** | [Русский](README.ru.md)

A **universal** battery indicator for wireless mice, gamepads and Bluetooth
devices in the Windows system tray (formerly *Mouse Tray Charge*).
One app, many vendors — adding a new manufacturer or model is a single small
file, no changes to the UI or polling code.

![Screenshot](images/screenshot.png)

## Supported models

- **ATK / VXE / VGN:** ATK F1 Ultimate, ATK A9 Ultimate, ATK Zero, VXE MAD R,
  VXE MAD R Major Plus, VXE R1 Pro Max, VXE R1 SE+, VGN F1 Pro, VGN F2 Pro Max
- **Zaopin:** Z2 Mini
- **Scyrox:** V8
- **Dareu:** A950 Air, A950 Wing
- **G-Wolves:** Lycan
- **MCHOSE:** L7 Pro
- **Ninjutso:** Sora V2
- **Razer:** Viper V2 Pro, Viper V3 Pro, DeathAdder V3 Pro, DeathAdder V4 Pro,
  Basilisk V3 Pro, Basilisk V3 Pro 35K, Basilisk Ultimate, Cobra Pro, Naga Pro,
  Naga V2 Pro, Lancehead Wireless, Pro Click V2
- **Lamzu:** Maya X, Inca
- **Attack Shark:** X3
- **A4Tech Bloody:** SG5 Pro (8K dongle)
- **Logitech:** any Lightspeed/Bolt/Unifying mouse with the UnifiedBattery
  feature, via a receiver **or connected directly by USB cable / Bluetooth**
  (model name auto-detected over HID++) — verified on PRO X2 SUPERSTRIKE
  (receiver and wired)
- **Sony gamepads:** DualShock 4 (v1, v2, USB wireless adapter), DualSense,
  DualSense Edge — USB and Bluetooth. Not yet verified on hardware; a pad
  hidden by DS4Windows/HidHide exclusive mode is not visible.
- **Bluetooth:** any paired device whose battery Windows itself shows under
  Settings → Bluetooth & devices — BLE mice, keyboards and gamepads, classic
  headsets. Windows gives the level only, so there is no charging animation.
  A device another driver already reads (a Logitech mouse over Bluetooth) is
  left to that driver. Names carry the last four digits of the Bluetooth
  address, e.g. `WH-1000XM4 (E4F5)`, so two identical devices stay apart.

> The Razer driver was ported from a `pyusb` implementation to `hidapi` for
> uniformity; the report offset / HID collection may need confirmation on
> hardware (see the note in [`drivers/razer.py`](battery_tray/drivers/razer.py)).
> Only **Viper V2 Pro** is verified on real hardware — the rest of the list was
> taken from [OpenRazer](https://github.com/openrazer/openrazer)'s device
> database. These are the models whose battery query uses transaction id `0x1F`
> (what the driver hardcodes), so they need no code change; older Razer families
> that use `0x3F` / `0xFF` are not covered yet. If one of the unverified models
> reads wrong, confirm the response offset / `usage_page` on hardware.

## Install

There is no installer: the program is a folder with `battery_tray.exe` and its
`_internal\` directory (Python and the libraries). To install or update:

1. Exit the running copy (tray icon → **Exit**).
2. Download the zip from [Releases](https://github.com/Resmus1/battery_tray/releases)
   and unzip it.
3. Copy `battery_tray.exe` **and** `_internal\` into
   `%LOCALAPPDATA%\Programs\battery_tray`
   (`C:\Users\<you>\AppData\Local\Programs\battery_tray`), replacing the old files.
4. Run `battery_tray.exe`. On the very first start it adds itself to Windows
   startup.

**Coming from Mouse Tray (0.4 and older)?** Exit it and install as above into
the new `battery_tray` folder. The first start copies the old settings, timers
and discharge history from `HKCU\SOFTWARE\Mouse_Tray` and moves the
**Start with Windows** entry to the new exe (a Task Manager switch-off carries
over). Then delete `%LOCALAPPDATA%\Programs\mouse_tray`. Windows sees a new
program, so re-enable its icon once in "Other system tray icons"; old logs stay
in `%LOCALAPPDATA%\Mouse_Tray`.

Why `%LOCALAPPDATA%\Programs`: it is Windows' standard place for per-user
programs (VS Code, Discord and others install there too).

- **No administrator rights.** Files in `C:\Program Files` can only be replaced
  through a UAC prompt; here they are ordinary user files.
- **Matches the autostart.** Startup is registered for the current user only
  (`HKCU\...\Run`), so a per-user program folder fits.
- **Never synced.** Desktop and Documents are often synced to OneDrive, which
  would upload ~67 MB of libraries; `AppData\Local` is never synced.
- **Local disk.** The program starts even when a network drive or server is not
  available yet.
- **Out of the way.** The folder is hidden, so it is not deleted or moved by
  accident.

Any folder on a local disk works if you prefer another one; keep the exe and
`_internal\` together. After moving it, toggle **Start with Windows** in the
settings and re-enable the tray icon in Windows' "Other system tray icons",
because Windows ties both to the exe path. `AppData` is hidden: open the folder
by pasting `%LOCALAPPDATA%\Programs\battery_tray` into Explorer's address bar or
**Win+R**.

## Run from source

The project is managed with [uv](https://docs.astral.sh/uv/) — a fast Python
package/project manager that handles the virtualenv and dependencies for you
(no manual `pip install` or `venv`). Install it once:

```sh
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# or via winget / pipx
winget install astral-sh.uv
```

Then, from the project root:

```sh
uv sync                      # create .venv and install dependencies from uv.lock
uv run python main.py        # or:  uv run python -m battery_tray
```

`uv run` auto-syncs first, so `uv run python main.py` alone is enough after a
fresh clone. `uv.lock` pins exact versions for reproducible installs.

## Build a standalone .exe

Releases are built by GitHub Actions ([`.github/workflows/release.yml`](.github/workflows/release.yml)):
push a tag such as `v0.2.0` and the workflow builds the exe on a clean Windows
runner from `uv.lock` and attaches the zip to a release named after the tag.
To build locally:

```sh
uv run --extra build python tools/make_release.py
# -> dist/battery_tray/
```

The build is a **folder**, not a single file: `battery_tray.exe` loads Python and
its libraries (`python314.dll` and the rest) from the `_internal\` directory next
to it. Copy or move the whole `dist\battery_tray\` folder — the exe on its own
fails to start with a "python314.dll not found" error. A one-file build is
deliberately not used: it would unpack itself into `%TEMP%` on every launch,
starting slower and tripping antivirus heuristics more often.

The resulting folder is about 37 MB. Drivers are collected as a whole
(`--collect-submodules`), so a new driver ships without touching the script.
Modules the app cannot reach — Pillow's codecs, `ssl` with its libcrypto/libssl,
`wx.html` and the like — are dropped through the `EXCLUDES` list in
[`tools/make_release.py`](tools/make_release.py), which also records what must
stay. A new dependency is reason to revisit that list.

## Multiple mice

Every supported device that is plugged in is detected. By default the tray
shows one of them: the first one that actually answers — a receiver whose mouse
is switched off is skipped rather than shown as "no mouse" — and it stays the
active one until it disappears.

When two or more are connected, the tray menu grows a device list: pick **Auto**
for that behaviour, or pin a specific device. A pin is strict — if the pinned
device is unplugged the tray shows `-` and names it in the tooltip, it never
quietly switches to another one. The choice is saved to the registry and
survives restarts. Only the shown devices are polled, so the others cost nothing.

**Devices in tray** (Settings, 1–4) shows several devices at once, one icon
each — say, a mouse and a gamepad. On **Auto** the tray then shows every
device that answers, up to that many, and icons come and go with them; the
list in the menu turns into checkboxes, each pinning one device to its own
icon. Every icon has the full menu; **Reset timer** applies to the device of
the icon clicked.

The "time since last full charge" timer, the time-left estimate and the
low-battery warnings are kept per device, so switching keeps each one's own.

## Settings

Right-click the tray icon and choose **Settings…** to change the poll interval,
font, font color, charge-level coloring, the indicator style and debug logging
from a dialog. Changes apply immediately and are saved to the registry
(`HKCU\SOFTWARE\Battery_Tray\Settings`), so they survive restarts; **Reset to
defaults** restores the code defaults.

The font picker lists monospaced faces by default (the digits want to be
fixed-width); tick **Show all fonts** to pick any installed font. The **Text
size** slider scales the digits from 50% to 100% of the fitted maximum, and
**Text outline** (on by default) keeps them readable on a same-colored
taskbar. Font, size and outline changes preview on the tray immediately,
without closing the dialog.

With **Color by charge level** enabled (the default), the battery-percent indicator is colored
by charge: the font color above 50%, and two extra pickers appear next to it for
the low bands — **≤ 50%** (yellow by default) and **≤ 20%** (red by default).

With **Show battery icon** enabled, the charge level is drawn as a battery
filled to that percent instead of as digits, and the exact number moves to the
hover tooltip (which also shows the mouse name and time since the last full
charge). "No mouse", sleep and unknown-level states stay textual either way.

**Devices in tray** sets how many devices the tray shows at once (1 by
default, up to 4); see [Multiple mice](#multiple-mice).

**Low battery notification** (on by default) shows a Windows notification when
the charge drops to the first level and again at the second (**Notify at**,
20% and 10% by default). Below the lower level it repeats every **Repeat below
lower level** minutes (15 by default, 0 = once). Charging resets it. The
**Test** button shows the notification right away, to check that Windows lets
it through (Settings → System → Notifications, Do not disturb).

**Language** switches the interface between English and Russian; **Auto**
follows the Windows display language.

### Time left

The tooltip shows an estimate such as `≈ 6 h 20 min left`. It is measured, not
assumed: each time the percent drops, the time since the previous drop gives
the current drain rate, and the estimate is the median of the last six such
stretches. The drain depends on the mouse settings — polling rate (1K vs 8K),
DPI, motion sync — and on how much it is used, so:

- when the two latest stretches both differ from the older ones by more than
  1.8× (say, polling went from 1K to 8K), the older ones are dropped and the
  estimate follows the new drain after two percent;
- an idle stretch that runs long counts as one more sample, so one quiet hour
  does not swing the number;
- time without readings (PC asleep or off, mouse off, app closed) is never
  counted as discharge.

Until two drops have been seen it says `estimating time left…`. The history is
kept per mouse in the registry (`HKCU\SOFTWARE\Battery_Tray\Discharge`), so it
survives restarts.

**Start with Windows** is turned on automatically at the very first start; after
that the program never changes it again. It adds the program to the current user's sign-in startup
(`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `Battery_Tray`); no
administrator rights are needed. The checkbox reflects the actual Windows
state, including the switch on the Startup tab of Task Manager, and is not
affected by **Reset to defaults**. If the program folder is moved, the entry is
redirected to the new location on the next manual start.

For the full set of fields (including those not exposed in the dialog), edit
[`battery_tray/config.py`](battery_tray/config.py):

| Field              | Meaning                                            |
| ------------------ | -------------------------------------------------- |
| `poll_rate`        | Seconds between reads while awake & discharging    |
| `fast_poll_rate`   | Seconds between reads while charging/asleep/absent |
| `foreground_color` | RGB color of the indicator digits (and the top band) |
| `dynamic_color`    | Color the percent by charge level (on)             |
| `mid_color`        | RGB color of the ≤ 50% band (yellow)               |
| `low_color`        | RGB color of the ≤ 20% band (red)                  |
| `battery_icon`     | Draw a filled battery instead of the percent digits |
| `background_color` | RGBA icon background (transparent by default)      |
| `font`             | Font file for the digits (`consola.ttf`)           |
| `app_name`         | Storage key: registry subkey + log directory name  |
| `display_name`     | Human-facing name (tray tooltip, toasts, settings) |
| `low_battery_notify` | Low-battery notifications (on)                   |
| `notify_level_high` / `notify_level_low` | Notification levels, % (20 / 10) |
| `notify_repeat_minutes` | Repeat interval below the lower level (15, 0 = off) |
| `language`         | `auto`, `en` or `ru`                               |
| `debug`            | Verbose DEBUG logging (raw HID reports)            |

## About

**About…** in the tray menu opens a dialog with the version and build commit, a
link to the project page, and the table of every device the running build
supports — model, USB id (`VID:PID`, plus the wired PID when the cable
enumerates its own) and the protocol behind it, sortable by any column header.
The table is generated from the driver registry, so it always matches the
drivers actually bundled — a build with a new driver lists its models without
any edit here.

## Logging

Logs go to a rotating file at `%LOCALAPPDATA%\Battery_Tray\app.log` (1 MB × 3
backups), plus the console when one is available — under the windowed `.exe`
build there is no console, so the file is the place to look. The **Open log**
button in the settings dialog opens it. Enable verbose
DEBUG output (raw HID reports) with the `debug` config flag or by setting the
`BATTERY_TRAY_DEBUG=1` environment variable. Configured in
[`battery_tray/logging_setup.py`](battery_tray/logging_setup.py).

## Adding a new mouse

**Same vendor, new model** — add a row to that driver's `models` list:

```python
# drivers/chipset/nordic52.py
_model("VXE NewModel", 0x373B, 0x1234, 0x5678),
```

> The `nordic52` driver covers the shared **Compx/Nordic 52840 chipset**, not just
> ATK/VXE/VGN. Many off-brand mice ride the same silicon (the receiver enumerates
> as "Compx") and work by adding a single `_model` row with their VID/PID — no new
> driver. The Zaopin Z2 Mini, Scyrox V8, Dareu A950 Air and G-Wolves Lycan were added exactly this
> way; if a percent reads at byte 6 of the report-8 reply, it's this protocol. The newer
> Nordic 54L15 silicon (ATK Zero) speaks a different 64-byte protocol and lives in
> `nordic54`.

**New vendor** — create `drivers/vendor/<vendor>.py` (or `drivers/chipset/<chip>.py`
if the silicon is shared across brands), subclass `HidDriver`, list the models and
implement `read_status()`:

```python
from ...battery import BatteryStatus
from ..driver import MouseModel, register
from ..hid import HidDriver

@register
class AcmeDriver(HidDriver):
    vendor = "Acme"
    models = [MouseModel("Acme X1", 0xABCD, 0x0001, 0x0002, usage_page=0xFF00)]

    def read_status(self) -> BatteryStatus:
        res = self._transact([0x00, ...], read_length=32, feature=True)
        if res is None:
            return BatteryStatus.absent()
        return BatteryStatus(present=True, percent=res[5], charging=bool(res[6]))
```

Then add `"vendor.acme"` to `_DRIVER_MODULES` in
[`drivers/__init__.py`](battery_tray/drivers/__init__.py). Done — detection, the
tray UI and packaging pick it up automatically.

> Most mice fit `HidDriver` (one request, fixed offsets). Multi-step protocols
> like Logitech HID++ instead subclass `HidppDriver` — but they return the same
> `BatteryStatus`, so the UI/registry are unchanged either way. That the two
> very different transports plug into one `MouseDriver` contract is the whole
> point of the design.

## How it works

Every mouse, regardless of vendor, is reduced to one normalized
[`BatteryStatus`](battery_tray/battery.py) — `present / percent / charging / full
/ asleep`. A single state machine in [`ui/app.py`](battery_tray/ui/app.py) renders
that into the tray icon (digits, charging animation, full-charge notification,
"time since last full charge" tooltip). Vendor code only does two things:
**detect the device** and **parse its battery report**.

```
hid.enumerate (cached 0.5 s)  →  detect_all_drivers()  →  driver.read_status()
         bus.py                      driver.py                → BatteryStatus
                                                                     ↓
                                                       _apply_status() → tray icon
```

### The bus

[`drivers/bus.py`](battery_tray/drivers/bus.py) enumerates the whole HID bus once
and caches the snapshot for 0.5 s. Detection walks every model of every driver —
dozens of VID/PID pairs — so matching happens in memory instead of rescanning the
bus once per model. The TTL sits well below the fastest poll rate, so a
hot-plugged mouse still lands within one tick.

### Drivers

The [`MouseDriver`](battery_tray/drivers/driver.py) contract is two methods —
`detect_all()` and `read_status()` — plus a stable `key` (driver class + VID/PID)
that identifies the mouse across re-plugs. Classes register themselves with the
`@register` decorator; the module list in
[`drivers/__init__.py`](battery_tray/drivers/__init__.py) is both the import list
and the detection probe order. Neither method may raise for an absent device or
an I/O hiccup: an empty list and `BatteryStatus.absent()` are the answers there.

Two transport bases sit under that contract:
[`HidDriver`](battery_tray/drivers/hid.py) for the common one-request/one-reply
case (a subclass writes only `read_status()` on top of `_transact()`), and
[`HidppDriver`](battery_tray/drivers/hidpp.py) for multi-step HID++ 2.0 (feature
discovery, device-index routing). The two share no code, which is precisely why
the real abstraction is `MouseDriver` rather than either base.

A third kind needs no protocol at all:
[`system/bluetooth.py`](battery_tray/drivers/system/bluetooth.py) reads the level
Windows keeps for every paired Bluetooth device (the PnP property
`{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2`, through CfgMgr32 and `ctypes`),
counting a device only while its node lacks `DN_DEVICE_DISCONNECTED`. It probes
last, so a device a HID driver handles is never shown twice.

### Polling

A daemon thread runs the poll loop. Detection re-runs on every tick — it costs
one cached bus enumeration — so a mouse plugged in later appears on its own,
without a restart. Driver objects are reused across sweeps, keyed by `key`,
because they cache per-device state (an HID++ driver, for instance, discovers its
device index and feature indices on the first read). The interval is `poll_rate`
(60 s by default) while the mouse is awake and discharging, and `fast_poll_rate`
(1 s) in the transient states — charging, asleep, or no mouse — where a quick
reaction matters. The worker touches widgets only through `wx.CallAfter`, so all
UI work stays on the main thread.

The interval is not the only trigger: the hidden frame also listens for
`WM_DEVICECHANGE` (`ui/devnotify.py`), so a cable plugged into the mouse or a
dongle inserted or removed re-polls within about a second whatever the interval
is set to. A left click on the tray icon forces a re-poll too.

### Which mouse is shown

All connected devices are detected; only the shown ones are read, up to
`tray_icons` of them. With no pin the worker takes the first that report
`present` and sticks to them, so a connected receiver whose mouse is switched
off never shadows a working one. Pins chosen from the tray menu are strict — an
unplugged pinned device yields `-` rather than a silent switch to another one.
The pins live in the registry and are only ever *read* by the worker, which
keeps the driver objects single-threaded. Each reading gets a tray icon of its
own; icons no longer needed are hidden rather than destroyed, and what the app
remembers about a device (timer, estimate, warnings) is keyed by the device,
not by the icon.

### Rendering

[`_apply_status`](battery_tray/ui/app.py) is a flat, ordered chain of guard
clauses, one per state: no mouse → `-`; charging → the fill animation (a
main-thread timer); full → a green icon, a toast and a fresh full-charge
timestamp; asleep → `Zzz`; present but with no readable level → `?`; otherwise
the percent, as digits or as a filled battery.
[`ui/icons.py`](battery_tray/ui/icons.py) draws the digits with PIL and rasterizes
the battery from an SVG template through `wx.svg` (NanoSVG, bundled with
wxPython), both keeping a real alpha channel all the way to the icon.

### Storage

[`storage.py`](battery_tray/storage.py) keeps the settings, the pinned mouse and
the per-mouse "last full charge" timestamp under `HKCU\SOFTWARE\Battery_Tray\`.
That timestamp is what the tooltip's elapsed-time line is computed from.

One architectural rule holds the whole thing together: `ui/app.py` never imports
a driver module — only `detect_all_drivers()` and the `MouseDriver` contract.

### Layout

```
battery_tray/
  autostart.py          Start with Windows (HKCU Run value)
  battery.py            BatteryStatus — the universal status model
  config.py             settings (poll rate, colors, font)
  resources.py          PyInstaller-safe resource paths
  storage.py            "last full charge" timestamp, settings, pinned mouse
  drivers/
    driver.py           MouseModel, MouseDriver, @register + registry
    bus.py              one cached hid.enumerate snapshot per detection sweep
    hid.py              HidDriver — shared single-transaction hidapi base
    hidpp.py            HidppDriver — multi-step HID++ base (Logitech)
    __init__.py         auto-imports drivers -> registry is populated
    chipset/            shared-silicon protocols (named by chipset)
      nordic52.py       Compx/Nordic 52840 (HID write/read, report 8, 17-byte)
      nordic54.py       Compx/Nordic 54L15 (HID write/read, report 8, 64-byte)
      realtek.py        MCHOSE / RealTek  (pushed report 0x13, XOR 0xFF)
    vendor/             brand-specific protocols
      ninjutso.py       Ninjutso Sora     (HID feature report 5)
      razer.py          Razer             (HID feature report 0, OpenRazer)
      lamzu.py          Lamzu             (HID feature report, iface 2)
      logitech.py       Logitech          (HID++ 2.0 via receiver)
      attackshark.py    Attack Shark      (pushed HID input report 3)
      bloody.py         A4Tech Bloody     (HID feature report 0 + input report 6)
      sony.py           DualShock 4 / DualSense (streamed input report)
    system/             what Windows already knows
      bluetooth.py      any Bluetooth device (PnP battery property, CfgMgr32)
  ui/
    icons.py            tray icon rendering (PIL digits + SVG battery)
    tray.py             TaskBarIcon wrapper
    settings.py         settings dialog
    devnotify.py        WM_DEVICECHANGE hook: re-poll on hot-plug
    about.py            about dialog (model table from the registry)
    app.py              wx app + the single state machine
  icons/                bundled .ico assets
```

## Protocol sources & credits

Each driver's protocol was ported from (or verified against) these projects:

- **ATK / VXE / VGN** — [Fan4Metal/ATK_tray](https://github.com/Fan4Metal/ATK_tray)
- **Ninjutso** — [Fan4Metal/Sora_tray](https://github.com/Fan4Metal/Sora_tray)
- **Razer** — [Fan4Metal/razer_tray](https://github.com/Fan4Metal/razer_tray),
  based on [OpenRazer](https://github.com/openrazer/openrazer) and
  [rsmith-nl/scripts](https://github.com/rsmith-nl/scripts)
- **Lamzu** — [Sheroune/lamzu-battery-monitory](https://github.com/Sheroune/lamzu-battery-monitory)
- **Logitech (HID++ 2.0)** — [l2-/LogitechBatteryIndicator](https://github.com/l2-/LogitechBatteryIndicator),
  with protocol details from [Solaar](https://github.com/pwr-Solaar/Solaar) and
  [libratbag](https://github.com/libratbag/libratbag)
- **Attack Shark** — [HarukaYamamoto0/attack-shark-x11-driver](https://github.com/HarukaYamamoto0/attack-shark-x11-driver),
  whose notes on the shared messaging protocol were contributed in
  [issue #1](https://github.com/Fan4Metal/mouse_tray/issues/1)
- **A4Tech Bloody (8K dongle)** — protocol reverse-engineered from a USB capture
  contributed by JurecUA in
  [issue #5](https://github.com/Fan4Metal/mouse_tray/issues/5)
- **Sony DualShock 4 / DualSense** — the Linux kernel drivers
  [`hid-playstation`](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-playstation.c)
  and [`hid-sony`](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-sony.c)

"""Per-mouse persistence of the "last full charge" timestamp (Windows registry).

Each mouse keeps its own timestamp, keyed by its display name, stored as one
value under ``SOFTWARE\\<app_name>\\FullCharge``. Switching mice therefore keeps
each timer independent.
"""

from __future__ import annotations

import logging
import winreg
from datetime import datetime

from .config import TEXT_SIZE_MAX, TEXT_SIZE_MIN, TRAY_ICONS_MAX, Config
from .i18n import LANGUAGES

_DATE_FORMAT = "%d.%m.%Y %H:%M:%S"

log = logging.getLogger(__name__)


def _reg_path(app_name: str) -> str:
    return rf"SOFTWARE\{app_name}\FullCharge"


def _settings_path(app_name: str) -> str:
    return rf"SOFTWARE\{app_name}\Settings"


def _value_name(mouse: str) -> str:
    """Registry-safe value name for a mouse (backslashes break key paths)."""
    return mouse.replace("\\", "_").strip() or "Unknown"


def save_full_charge_date(app_name: str, mouse: str, when: datetime) -> None:
    """Persist the moment ``mouse`` was last fully charged."""
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _reg_path(app_name)) as key:
            winreg.SetValueEx(key, _value_name(mouse), 0, winreg.REG_SZ, when.strftime(_DATE_FORMAT))
    except OSError as exc:
        log.warning("Could not save full charge date for %s: %s", mouse, exc)


def load_full_charge_date(app_name: str, mouse: str) -> datetime | None:
    """Read ``mouse``'s stored full-charge timestamp, or ``None`` if absent/invalid."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _reg_path(app_name), 0, winreg.KEY_READ) as key:
            value = winreg.QueryValueEx(key, _value_name(mouse))[0]
        return datetime.strptime(value, _DATE_FORMAT)
    except (OSError, ValueError):
        return None


# --- discharge history (remaining-time estimate) ---------------------------


def _discharge_path(app_name: str) -> str:
    return rf"SOFTWARE\{app_name}\Discharge"


def save_discharge_stretches(app_name: str, mouse: str, stretches: list[float]) -> None:
    """Persist ``mouse``'s recent seconds-per-percent measurements."""
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _discharge_path(app_name)) as key:
            value = ",".join(f"{s:.0f}" for s in stretches)
            winreg.SetValueEx(key, _value_name(mouse), 0, winreg.REG_SZ, value)
    except OSError as exc:
        log.warning("Could not save discharge history for %s: %s", mouse, exc)


def load_discharge_stretches(app_name: str, mouse: str) -> list[float]:
    """Read ``mouse``'s stored discharge measurements (empty when none)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _discharge_path(app_name), 0, winreg.KEY_READ) as key:
            value = str(winreg.QueryValueEx(key, _value_name(mouse))[0])
        return [float(part) for part in value.split(",") if part and float(part) > 0]
    except (OSError, ValueError):
        return []


# --- user settings (poll rate, font, size, color, debug) ------------------


def _color_to_str(color: tuple[int, int, int]) -> str:
    return ",".join(str(c) for c in color)


def _str_to_color(value: str) -> tuple[int, int, int] | None:
    try:
        parts = tuple(int(p) for p in value.split(","))
    except ValueError:
        return None
    if len(parts) != 3 or not all(0 <= p <= 255 for p in parts):
        return None
    return parts


def _read_int(key: winreg.HKEYType, name: str) -> int | None:
    try:
        return int(winreg.QueryValueEx(key, name)[0])
    except (OSError, ValueError, TypeError):
        return None


def _read_str(key: winreg.HKEYType, name: str) -> str | None:
    try:
        return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return None


def save_settings(app_name: str, config: Config) -> None:
    """Persist the user-tunable settings (poll rate, font, size, color, debug)."""
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _settings_path(app_name)) as key:
            winreg.SetValueEx(key, "PollRate", 0, winreg.REG_DWORD, int(config.poll_rate))
            winreg.SetValueEx(key, "Font", 0, winreg.REG_SZ, config.font)
            winreg.SetValueEx(key, "TextSizePct", 0, winreg.REG_DWORD, int(config.text_size))
            winreg.SetValueEx(key, "TextOutline", 0, winreg.REG_DWORD, 1 if config.text_outline else 0)
            winreg.SetValueEx(key, "TrayIcons", 0, winreg.REG_DWORD, int(config.tray_icons))
            color = _color_to_str(config.foreground_color)
            winreg.SetValueEx(key, "ForegroundColor", 0, winreg.REG_SZ, color)
            winreg.SetValueEx(key, "DynamicColor", 0, winreg.REG_DWORD, 1 if config.dynamic_color else 0)
            winreg.SetValueEx(key, "MidColor", 0, winreg.REG_SZ, _color_to_str(config.mid_color))
            winreg.SetValueEx(key, "LowColor", 0, winreg.REG_SZ, _color_to_str(config.low_color))
            winreg.SetValueEx(key, "BatteryIcon", 0, winreg.REG_DWORD, 1 if config.battery_icon else 0)
            notify = 1 if config.low_battery_notify else 0
            winreg.SetValueEx(key, "LowBatteryNotify", 0, winreg.REG_DWORD, notify)
            winreg.SetValueEx(key, "NotifyHigh", 0, winreg.REG_DWORD, int(config.notify_level_high))
            winreg.SetValueEx(key, "NotifyLow", 0, winreg.REG_DWORD, int(config.notify_level_low))
            repeat = int(config.notify_repeat_minutes)
            winreg.SetValueEx(key, "NotifyRepeatMin", 0, winreg.REG_DWORD, repeat)
            winreg.SetValueEx(key, "Language", 0, winreg.REG_SZ, config.language)
            winreg.SetValueEx(key, "Debug", 0, winreg.REG_DWORD, 1 if config.debug else 0)
    except OSError as exc:
        log.warning("Could not save settings: %s", exc)


def load_settings(app_name: str, config: Config) -> None:  # noqa: PLR0912, PLR0915 -- one guard per setting
    """Apply persisted settings onto ``config`` in place.

    Missing or invalid values are skipped so each field keeps its code default.
    """
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _settings_path(app_name), 0, winreg.KEY_READ)
    except OSError:
        return
    with key:
        poll_rate = _read_int(key, "PollRate")
        if poll_rate is not None and poll_rate > 0:
            config.poll_rate = poll_rate

        font = _read_str(key, "Font")
        if font:
            config.font = font

        text_size = _read_int(key, "TextSizePct")
        if text_size is not None and TEXT_SIZE_MIN <= text_size <= TEXT_SIZE_MAX:
            config.text_size = text_size

        text_outline = _read_int(key, "TextOutline")
        if text_outline is not None:
            config.text_outline = bool(text_outline)

        tray_icons = _read_int(key, "TrayIcons")
        if tray_icons is not None and 1 <= tray_icons <= TRAY_ICONS_MAX:
            config.tray_icons = tray_icons

        color = _read_str(key, "ForegroundColor")
        if color is not None and (parsed := _str_to_color(color)) is not None:
            config.foreground_color = parsed

        dynamic_color = _read_int(key, "DynamicColor")
        if dynamic_color is not None:
            config.dynamic_color = bool(dynamic_color)

        mid_color = _read_str(key, "MidColor")
        if mid_color is not None and (parsed := _str_to_color(mid_color)) is not None:
            config.mid_color = parsed

        low_color = _read_str(key, "LowColor")
        if low_color is not None and (parsed := _str_to_color(low_color)) is not None:
            config.low_color = parsed

        battery_icon = _read_int(key, "BatteryIcon")
        if battery_icon is not None:
            config.battery_icon = bool(battery_icon)

        low_battery_notify = _read_int(key, "LowBatteryNotify")
        if low_battery_notify is not None:
            config.low_battery_notify = bool(low_battery_notify)

        notify_high = _read_int(key, "NotifyHigh")
        if notify_high is not None and 1 <= notify_high <= 99:
            config.notify_level_high = notify_high

        notify_low = _read_int(key, "NotifyLow")
        if notify_low is not None and 1 <= notify_low <= 99:
            config.notify_level_low = notify_low

        repeat = _read_int(key, "NotifyRepeatMin")
        if repeat is not None and 0 <= repeat <= 1440:
            config.notify_repeat_minutes = repeat

        language = _read_str(key, "Language")
        if language in LANGUAGES:
            config.language = language

        debug = _read_int(key, "Debug")
        if debug is not None:
            config.debug = bool(debug)


# --- first-run autostart ----------------------------------------------------
#
# Autostart defaults to on, but only once: after the first start the user's
# choice (settings dialog or Task Manager) is never overridden again.

_AUTOSTART_DEFAULTED = "AutostartDefaulted"


def autostart_defaulted(app_name: str) -> bool:
    """Whether the one-time "autostart on by default" step has already run."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _settings_path(app_name), 0, winreg.KEY_READ) as key:
            return bool(_read_int(key, _AUTOSTART_DEFAULTED))
    except OSError:
        return False


def mark_autostart_defaulted(app_name: str) -> None:
    """Record that the one-time autostart default has been applied."""
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _settings_path(app_name)) as key:
            winreg.SetValueEx(key, _AUTOSTART_DEFAULTED, 0, winreg.REG_DWORD, 1)
    except OSError as exc:
        log.warning("Could not save the autostart default flag: %s", exc)


# --- pinned mice ------------------------------------------------------------
#
# Kept out of Config on purpose: the pins are chosen from the tray menu, not
# from the settings dialog, so they must not ride along with save_settings.

_SELECTED = "SelectedMice"
# The single pin of 0.2.x. Still written (first pin) so a downgrade keeps it,
# and read when the list above has never been saved.
_SELECTED_KEY = "SelectedMouse"
_SELECTED_NAME = "SelectedMouseName"
_SEPARATOR = "\t"


def save_selected_mice(app_name: str, pins: list[tuple[str, str | None]]) -> None:
    """Persist the pinned mice as ``(key, name)`` in tray order; empty means auto.

    The display name is stored next to the key so a pinned-but-offline mouse can
    still be named in the tooltip and in the tray menu -- the key alone
    (``DriverClass/VID/PID``) is not something to show a user.
    """
    entries = [f"{key}{_SEPARATOR}{name or ''}" for key, name in pins]
    first_key, first_name = pins[0] if pins else (None, None)
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _settings_path(app_name)) as reg_key:
            winreg.SetValueEx(reg_key, _SELECTED, 0, winreg.REG_MULTI_SZ, entries)
            winreg.SetValueEx(reg_key, _SELECTED_KEY, 0, winreg.REG_SZ, first_key or "")
            winreg.SetValueEx(reg_key, _SELECTED_NAME, 0, winreg.REG_SZ, first_name or "")
    except OSError as exc:
        log.warning("Could not save the selected mice: %s", exc)


def load_selected_mice(app_name: str) -> list[tuple[str, str | None]]:
    """Read the pinned mice as ``(key, name)`` pairs; an empty list means auto."""
    path = _settings_path(app_name)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as reg_key:
            try:
                entries = winreg.QueryValueEx(reg_key, _SELECTED)[0]
            except OSError:
                key = _read_str(reg_key, _SELECTED_KEY)
                return [(key, _read_str(reg_key, _SELECTED_NAME) or None)] if key else []
    except OSError:
        return []
    pins = []
    for entry in entries or []:
        key, _, name = str(entry).partition(_SEPARATOR)
        if key:
            pins.append((key, name or None))
    return pins

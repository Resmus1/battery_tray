"""Application entry point: the wx app, the polling loop and the one state
machine that turns any :class:`BatteryStatus` into a tray icon.

All vendor differences are already gone by the time we get here -- this module
never imports a driver module, only the registry's :func:`detect_all_drivers`
and the :class:`MouseDriver` contract it returns.

The tray shows up to ``Config.tray_icons`` mice at once, one icon (a *slot*)
each. Slots are interchangeable -- a mouse may land in a different one after a
re-plug -- so everything remembered about a mouse (full-charge timer, discharge
estimate, warnings already shown) lives in a per-mouse :class:`_MouseState`,
never in the slot.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import wx
from wx.adv import NotificationMessage

from .. import autostart
from ..battery import BatteryStatus
from ..build_info import version_string
from ..config import GREEN, LEGACY_APP_NAME, Config
from ..config import config as default_config
from ..diagnostics import log_startup_diagnostics
from ..drivers import MouseDriver, detect_all_drivers
from ..estimate import DischargeEstimator
from ..i18n import set_language, tr
from ..logging_setup import setup_logging
from .devnotify import DeviceChangeWatcher
from .icons import IconRenderer, tray_icon_size
from .tray import MouseChoice, MouseMenu, TrayIcon

log = logging.getLogger(__name__)

#: Charge levels cycled through while the mouse is charging.
_ANIMATION_FRAMES = (0, 50, 100)
_ANIMATION_INTERVAL_MS = 500
_NO_DEVICE = "No device detected"
_NOT_CONNECTED = "Not connected"


def format_timedelta(delta: timedelta) -> str:
    """Format a duration like ``"1 days, 23:59:59"``."""
    days = delta.days
    seconds = int(delta.total_seconds()) - days * 86400
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return tr("{days} days, {time}", days=days, time=f"{hours:02d}:{minutes:02d}:{secs:02d}")


def format_left(seconds: float) -> str:
    """Remaining runtime, coarse on purpose: ``"1 d 4 h"``, ``"3 h 20 min"``, ``"40 min"``."""
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return tr("{d} d {h} h", d=days, h=hours)
    if hours:
        return tr("{h} h {m} min", h=hours, m=minutes // 10 * 10)
    return tr("{m} min", m=max(minutes, 1))


@dataclass(frozen=True)
class _Reading:
    """One mouse's status as read by the worker, for one tray slot.

    ``name`` is ``None`` only for the "nothing found" placeholder; a pinned
    mouse that is offline keeps its name, with an absent status.
    """

    key: str | None
    name: str | None
    status: BatteryStatus


@dataclass
class _MouseState:
    """What the app remembers about one mouse while it runs (main thread).

    Keyed by display name, like the registry values it is loaded from.
    """

    name: str
    full_charge_date: datetime | None
    estimator: DischargeEstimator
    was_full: bool = False
    #: Low-charge thresholds already warned about since the last charge.
    low_warned: set[int] = field(default_factory=set)
    last_low_notify: float = 0.0


class _TraySlot:
    """One tray icon and its charge animation (main thread).

    Slots are pooled, never destroyed before exit: a slot no longer needed is
    hidden, since destroying a ``TaskBarIcon`` while its menu is open (the
    poll can shrink the tray at any moment) would pull the menu's owner away.
    """

    def __init__(self, frame: wx.Frame, icons: IconRenderer, tray: TrayIcon):
        self.tray = tray
        self._frame = frame
        self._icons = icons
        self._timer = wx.Timer(frame)
        self._anim_index = 0
        self._anim_tooltip = ""
        frame.Bind(wx.EVT_TIMER, self._on_anim_tick, self._timer)

    def show(self, icon: wx.Icon, tooltip: str) -> None:
        self.tray.update(icon, tooltip)

    def hide(self) -> None:
        self.stop_animation()
        # Queued like update(), so it lands after any icon still pending.
        wx.CallAfter(self.tray.RemoveIcon)

    def start_animation(self, tooltip: str) -> None:
        self._anim_tooltip = tooltip
        if not self._timer.IsRunning():
            self._anim_index = 0
            self._on_anim_tick()
            self._timer.Start(_ANIMATION_INTERVAL_MS)

    def stop_animation(self) -> None:
        if self._timer.IsRunning():
            self._timer.Stop()

    def _on_anim_tick(self, _evt: wx.TimerEvent | None = None) -> None:
        level = _ANIMATION_FRAMES[self._anim_index % len(_ANIMATION_FRAMES)]
        self._anim_index += 1
        self.tray.update(self._icons.battery_icon(level), self._anim_tooltip)

    def destroy(self) -> None:
        self.stop_animation()
        self._frame.Unbind(wx.EVT_TIMER, source=self._timer)
        self.tray.RemoveIcon()
        self.tray.Destroy()


class TrayApp(wx.Frame):
    """Hidden frame that owns the tray icons, notifications and worker thread.

    A frame is needed because ``NotificationMessage`` and the wx main loop
    require a top-level window; it never becomes visible.
    """

    def __init__(self, config: Config):
        from ..storage import load_selected_mice

        super().__init__(None, title=config.display_name)
        self.config = config
        self.icons = IconRenderer(config, tray_icon_size(self))
        log.info("tray icons render at %dpx", self.icons.icon_size)

        #: Per-mouse memory, by display name (see _MouseState).
        self._states: dict[str, _MouseState] = {}

        # Which mice the tray shows. The pins (main thread, persisted) win; with
        # none the worker picks the mice that answer and sticks to them via
        # _auto_keys. _drivers belongs to the worker; _available is its read-only
        # snapshot for the menu. Both _pins and _available are rebound wholesale,
        # so the other thread always sees a consistent list.
        self._pins: tuple[tuple[str, str | None], ...] = tuple(load_selected_mice(config.app_name))
        self._drivers: dict[str, MouseDriver] = {}
        self._available: list[tuple[str, str]] = []
        self._auto_keys: list[str] = []
        self._trim_pins()

        # Last applied snapshot, so a settings live preview (or a settings
        # close) can repaint instantly without waiting for the next poll tick.
        self._last_readings: list[_Reading] = []
        #: Live settings preview for "Show battery icon"; unlike the font/size/
        #: outline overrides it lives here, not on the renderer, because the
        #: digits-vs-battery choice is made by _apply_status, not by drawing.
        self._preview_battery_icon: bool | None = None

        # Every slot ever shown, the first _shown of them visible. Slot 0 is
        # always visible: the notifications hang off its icon.
        self._slots: list[_TraySlot] = []
        self._shown = 0
        self._show_slots(1)
        self._slots[0].show(self.icons.text_icon(" "), config.display_name)

        self.notification = NotificationMessage(title=config.display_name, message="Charged 100%")
        self.notification.SetFlags(wx.ICON_INFORMATION)
        self.notification.UseTaskBarIcon(self._slots[0].tray)

        self.low_notification = NotificationMessage(title=config.display_name)
        self.low_notification.SetFlags(wx.ICON_WARNING)
        self.low_notification.UseTaskBarIcon(self._slots[0].tray)

        # Polling runs on a background thread; it only ever calls back into the
        # UI through wx.CallAfter, so all widget work stays on the main thread.
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._worker = threading.Thread(target=self._poll_loop, daemon=True)
        self._worker.start()

        # Hot-plug: a cable or dongle coming or going re-polls at once instead
        # of waiting out the poll interval. Optional -- without it the loop
        # still catches up on its next tick.
        self._devices: DeviceChangeWatcher | None = None
        try:
            self._devices = DeviceChangeWatcher(self, self._wake)
        except (OSError, AttributeError) as exc:
            log.warning("Device change notifications unavailable: %s", exc)

    # --- polling thread -----------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            readings = self._poll_once()
            wx.CallAfter(self._apply_readings, readings)

            awake = all(_awake(reading.status) for reading in readings)
            interval = self.config.poll_rate if awake else self.config.fast_poll_rate
            self._wakeup.wait(interval)
            self._wakeup.clear()

    def _poll_once(self) -> list[_Reading]:
        """Refresh the device list and read the mice to show (worker thread).

        Detection runs every tick -- it costs one cached bus enumeration -- so a
        second mouse plugged in later shows up in the menu on its own. Never
        returns an empty list: "nothing to show" is one absent reading.
        """
        self._sync_drivers()
        limit = self.config.tray_icons
        pins = self._pins[:limit]
        if not pins:
            return self._read_auto(limit)

        # Strict pins: a chosen mouse that is gone reads "no mouse" rather than
        # falling back, so a tray icon never quietly shows a different battery.
        readings = []
        for key, name in pins:
            driver = self._drivers.get(key)
            if driver is None:
                readings.append(_Reading(key, name, BatteryStatus.absent()))
            else:
                readings.append(_Reading(key, driver.name, driver.read_status()))
        return readings

    def _sync_drivers(self) -> None:
        """Re-detect, keeping the driver objects we already have.

        A driver carries per-device state (HID++ caches its device index and
        feature indices, discovered by pinging the receiver), so a fresh sweep
        must reuse the instance already talking to a mouse instead of the
        equivalent new one.
        """
        detected = detect_all_drivers()
        known = self._drivers
        self._drivers = {d.key: known.get(d.key, d) for d in detected}
        self._available = [(key, driver.name) for key, driver in self._drivers.items()]
        if self._drivers.keys() != known.keys():
            log.info("Mice detected: %s", ", ".join(d.name for d in self._drivers.values()) or "none")

    def _read_auto(self, limit: int) -> list[_Reading]:
        """Read up to ``limit`` mice that answer, preferring the ones shown last.

        Sticky, so a working mouse keeps its icon even when another is plugged
        in. Trying the others matters with a single mouse too: a plugged dongle
        whose mouse is switched off detects fine but never reports present.
        """
        rank = {key: index for index, key in enumerate(self._auto_keys)}
        drivers = sorted(self._drivers.values(), key=lambda d: rank.get(d.key, len(rank)))
        readings: list[_Reading] = []
        for driver in drivers:
            if len(readings) == limit:
                break
            status = driver.read_status()
            if status.present:
                readings.append(_Reading(driver.key, driver.name, status))
        keys = [reading.key for reading in readings if reading.key is not None]
        if keys != self._auto_keys:
            log.info("Active mice: %s", ", ".join(r.name or "?" for r in readings) or "none")
        self._auto_keys = keys
        return readings or [_Reading(None, None, BatteryStatus.absent())]

    def _wake(self) -> None:
        """Trigger an immediate re-poll (e.g. tray clicked to wake a mouse)."""
        self._wakeup.set()

    # --- mouse selection (main thread) --------------------------------------

    def _mouse_choices(self) -> MouseMenu:
        """The tray menu's mouse list: everything detected, plus the pins.

        A pinned mouse that is currently offline still gets an entry -- without
        it there would be no way back to another mouse, or to auto, once the
        pinned one is unplugged.
        """
        limit = self.config.tray_icons
        pinned = dict(self._pins)
        room = limit == 1 or len(pinned) < limit
        choices = [
            MouseChoice(key, name, key in pinned, enabled=room or key in pinned)
            for key, name in self._available
        ]
        available = {key for key, _name in self._available}
        for key, name in self._pins:
            if key not in available:
                label = f"{name or tr('Pinned device')} ({tr(_NOT_CONNECTED).lower()})"
                choices.append(MouseChoice(key, label, True))
        return MouseMenu(choices, multi=limit > 1)

    def _select_mouse(self, key: str | None) -> None:
        """Pin or unpin a mouse; ``None`` goes back to auto-select.

        With one tray icon a pick replaces the pin; with several it toggles
        that mouse's pin. Only the pins are touched here; the worker notices
        the change at the top of its next tick, which keeps the driver objects
        single-threaded.
        """
        pinned = dict(self._pins)
        if key is None:
            pins: tuple[tuple[str, str | None], ...] = ()
        elif self.config.tray_icons == 1:
            if key in pinned:
                return
            pins = ((key, self._available_name(key)),)
        elif key in pinned:
            pins = tuple(pin for pin in self._pins if pin[0] != key)
        elif len(self._pins) < self.config.tray_icons:
            pins = (*self._pins, (key, self._available_name(key)))
        else:
            return  # every slot pinned; the menu greys such entries out
        if pins == self._pins:
            return
        self._save_pins(pins)
        log.info("Selected mice: %s", ", ".join(name or key for key, name in pins) or "auto")
        self._wake()

    def _available_name(self, key: str) -> str | None:
        return next((name for k, name in self._available if k == key), None)

    def _trim_pins(self) -> None:
        """Drop pins beyond the tray icon count (after the setting was lowered)."""
        if len(self._pins) > self.config.tray_icons:
            self._save_pins(self._pins[: self.config.tray_icons])

    def _save_pins(self, pins: tuple[tuple[str, str | None], ...]) -> None:
        from ..storage import save_selected_mice

        self._pins = pins
        save_selected_mice(self.config.app_name, list(pins))

    # --- state machine (main thread) ---------------------------------------

    def _apply_readings(self, readings: list[_Reading]) -> None:
        """Give every reading its own tray icon, showing or hiding icons to fit."""
        self._last_readings = readings
        self._show_slots(len(readings))
        for slot, reading in zip(self._slots, readings, strict=False):
            self._apply_status(slot, reading)
        # A mouse that dropped out of auto gets no absent reading of its own,
        # so close its full state here, or the last moment seen full is lost.
        present = {reading.name for reading in readings if reading.status.present}
        for state in self._states.values():
            if state.name not in present:
                self._end_full(state)

    def _show_slots(self, count: int) -> None:
        """Make the first ``count`` slots (at least one) the visible ones."""
        count = max(count, 1)
        while len(self._slots) < count:
            self._slots.append(self._new_slot())
        for slot in self._slots[count : self._shown]:
            slot.hide()
        self._shown = count

    def _new_slot(self) -> _TraySlot:
        slots: list[_TraySlot] = []  # filled below: the menu needs its own slot
        tray = TrayIcon(
            on_left_click=self._wake,
            mice_provider=self._mouse_choices,
            on_select_mouse=self._select_mouse,
            on_reset_timer=lambda: self._reset_timer(slots[0]),
            on_settings=self._open_settings,
            on_about=self._open_about,
            on_exit=self._exit,
        )
        slots.append(_TraySlot(self, self.icons, tray))
        return slots[0]

    def _apply_status(self, slot: _TraySlot, reading: _Reading) -> None:  # noqa: PLR0911
        """Render one mouse into one slot. The single point that drives the tray.

        The guard-clause chain is the design (see CLAUDE.md): one flat, ordered
        list of states, each rendering and returning. Collapsing it to fit a
        return-count limit would bury the ordering that makes it readable.
        """
        status, name = reading.status, reading.name
        state = self._state(name)

        if not status.present:
            slot.stop_animation()
            if state is not None:
                self._end_full(state)
            # A name here means a pinned mouse that is simply not connected --
            # worth saying, since "-" alone reads as "the app found nothing".
            tooltip = tr(_NO_DEVICE) if name is None else f"{name}\n{tr(_NOT_CONNECTED)}"
            slot.show(self.icons.text_icon("-"), tooltip)
            return

        assert state is not None  # a present reading always comes from a named driver
        self._track_discharge(state, status)
        tooltip = self._tooltip(state, status)

        if status.charging:
            self._end_full(state)
            state.low_warned.clear()
            slot.start_animation(tooltip)
            return

        slot.stop_animation()

        if status.full:
            slot.show(self.icons.battery_icon(100, GREEN), tooltip)
            state.low_warned.clear()
            if not state.was_full:
                state.was_full = True
                self._record_full_charge(state)
                self.notification.SetMessage(f"{state.name}: {tr('Charged 100%')}")
                self.notification.Show(timeout=NotificationMessage.Timeout_Auto)
            else:
                # Still on the charger: the timer counts from the last moment
                # seen full, not from when it got there -- a night on the cable
                # is not battery time. Saved once, by _end_full.
                state.full_charge_date = datetime.now()
            return

        self._end_full(state)

        if status.asleep:
            slot.show(self.icons.text_icon("Zzz"), tooltip)
            return

        if status.percent is None:
            # Present and awake but the driver could not read a level. Neither
            # a number nor a fill height is meaningful, so say so.
            slot.show(self.icons.text_icon("?"), tooltip)
            return

        if status.percent == 100:
            # 100% but no "full" flag from the driver. The foreground color is
            # what dynamic_color would pick for this band anyway, and green
            # stays reserved for the full state above.
            slot.show(self.icons.battery_icon(100), tooltip)
            return

        self._warn_low(state, status.percent)

        color = self.config.charge_color(status.percent) if self.config.dynamic_color else None
        battery_icon = self.config.battery_icon
        if self._preview_battery_icon is not None:
            battery_icon = self._preview_battery_icon
        if battery_icon:
            # The exact number is in the tooltip built above.
            slot.show(self.icons.battery_icon(status.percent, color), tooltip)
            return
        slot.show(self.icons.text_icon(str(status.percent), color), tooltip)

    def _state(self, name: str | None) -> _MouseState | None:
        """The mouse's own memory, loaded from the registry on first sight."""
        if name is None:
            return None
        state = self._states.get(name)
        if state is None:
            from ..storage import load_discharge_stretches, load_full_charge_date

            state = _MouseState(
                name,
                load_full_charge_date(self.config.app_name, name),
                DischargeEstimator(load_discharge_stretches(self.config.app_name, name)),
            )
            self._states[name] = state
        return state

    def _test_notification(self) -> None:
        """Show the low-charge warning on demand, so Windows' notification setup can be checked."""
        percent = self._last_readings[0].status.percent if self._last_readings else None
        shown = "?" if percent is None else percent
        self.low_notification.SetMessage(tr("Test: battery {percent}%", percent=shown))
        self.low_notification.Show(timeout=NotificationMessage.Timeout_Auto)

    def _warn_low(self, state: _MouseState, percent: int) -> None:
        """Warn once per level crossed, then repeat while at or below the lower level."""
        if not self.config.low_battery_notify:
            return
        levels = {self.config.notify_level_high, self.config.notify_level_low}
        crossed = {level for level in levels if percent <= level} - state.low_warned
        now = time.time()
        repeat = self.config.notify_repeat_minutes * 60
        if crossed:
            state.low_warned |= crossed
        elif not (repeat and percent <= min(levels) and now - state.last_low_notify >= repeat):
            return
        state.last_low_notify = now
        self.low_notification.SetMessage(
            tr("{name}: battery low ({percent}%)", name=state.name, percent=percent)
        )
        self.low_notification.Show(timeout=NotificationMessage.Timeout_Auto)
        log.info("%s low battery warning at %d%%", state.name, percent)

    def _track_discharge(self, state: _MouseState, status: BatteryStatus) -> None:
        """Feed the remaining-time estimate; charging only closes the open stretch."""
        if status.asleep:
            state.estimator.touch(time.time())  # dozing on battery: keep the stretch open
            return
        if not _discharging(status) or status.percent is None:
            state.estimator.reset()
            return
        if state.estimator.add(time.time(), status.percent):
            from ..storage import save_discharge_stretches

            save_discharge_stretches(self.config.app_name, state.name, state.estimator.stretches)
            log.info(
                "%s discharge: %.0f s per percent (last %d stretches)",
                state.name,
                state.estimator.seconds_per_percent(time.time()) or 0,
                len(state.estimator.stretches),
            )

    def _tooltip(self, state: _MouseState, status: BatteryStatus) -> str:
        """Mouse name, the charge percent when known, and time since full.

        The percent line is what makes ``battery_icon`` mode usable -- the tray
        then shows a fill level and this is the only place the exact number
        appears -- but it is cheap and consistent enough to always include.
        """
        lines = [state.name]
        if status.percent is not None:
            lines.append(f"{status.percent}%")
        if _discharging(status) and status.percent is not None:
            left = state.estimator.remaining(time.time(), status.percent)
            if left is None:
                lines.append(tr("estimating time left…"))
            else:
                lines.append(tr("≈ {time} left", time=format_left(left)))
        if state.full_charge_date:
            since = format_timedelta(datetime.now() - state.full_charge_date)
            lines.append(tr("since full charge: {time}", time=since))
        return "\n".join(lines)

    # --- full-charge timer --------------------------------------------------

    def _record_full_charge(self, state: _MouseState) -> None:
        from ..storage import save_full_charge_date

        state.full_charge_date = datetime.now()
        save_full_charge_date(self.config.app_name, state.name, state.full_charge_date)
        log.info("Full charge timer for %s set to %s", state.name, state.full_charge_date)

    def _end_full(self, state: _MouseState) -> None:
        """Leaving the full state (unplugged, or charging again): persist the
        last moment the mouse was seen full."""
        if not state.was_full:
            return
        state.was_full = False
        if state.full_charge_date is not None:
            from ..storage import save_full_charge_date

            save_full_charge_date(self.config.app_name, state.name, state.full_charge_date)
            log.info("%s left full charge at %s", state.name, state.full_charge_date)

    def _reset_timer(self, slot: _TraySlot) -> None:
        """Reset the timer of the mouse shown in ``slot`` (the icon clicked)."""
        index = self._slots.index(slot)
        if index >= min(self._shown, len(self._last_readings)):
            return
        state = self._state(self._last_readings[index].name)
        if state is None:
            return  # no mouse in this slot to attribute the timer to
        self._record_full_charge(state)

    # --- settings -----------------------------------------------------------

    def _preview_font(self, font_path: str | None) -> None:
        """Live preview: repaint the tray in a candidate settings font.

        The override lives on the renderer, so poll ticks keep repainting
        through it until the dialog closes (OK applies it for real, Cancel
        clears it) -- the preview never flickers back to the old font.
        """
        self.icons.preview_font = font_path
        if not self._repaint():
            self.icons.preview_font = None  # not renderable: don't poison later ticks

    def _preview_size(self, text_size: int | None) -> None:
        """Live preview: repaint the tray at a candidate settings text size."""
        self.icons.preview_text_size = text_size
        if not self._repaint():
            self.icons.preview_text_size = None

    def _preview_outline(self, text_outline: bool | None) -> None:
        """Live preview: repaint the tray with a candidate outline setting."""
        self.icons.preview_text_outline = text_outline
        if not self._repaint():
            self.icons.preview_text_outline = None

    def _preview_battery_icon_mode(self, battery_icon: bool | None) -> None:
        """Live preview: repaint the tray as digits or a battery glyph."""
        self._preview_battery_icon = battery_icon
        if not self._repaint():
            self._preview_battery_icon = None

    def _repaint(self) -> bool:
        """Re-render the cached snapshot (preview on/off).

        Returns False when there is nothing cached yet or the font fails to
        load, in which case the old icons are simply kept.
        """
        if not self._last_readings:
            return True
        try:
            self._apply_readings(self._last_readings)
        except OSError:  # stale registry font path: keep the old icons
            return False
        return True

    def _open_settings(self) -> None:
        from ..storage import save_settings
        from .settings import PreviewHooks, open_settings

        hooks = PreviewHooks(
            font=self._preview_font,
            size=self._preview_size,
            outline=self._preview_outline,
            battery_icon=self._preview_battery_icon_mode,
        )
        ok = open_settings(self, self.config, hooks, self._test_notification)
        self.icons.preview_font = None
        self.icons.preview_text_size = None
        self.icons.preview_text_outline = None
        self._preview_battery_icon = None
        if not ok:
            self._repaint()  # drop the preview right away
            return

        # self.config is mutated in place, so IconRenderer (which holds the same
        # reference) picks up the new font/color and the worker thread reads the
        # new poll rate and icon count on its next iteration. Persist and refresh now.
        save_settings(self.config.app_name, self.config)
        set_language(self.config.language)
        setup_logging(self.config.app_name, self.config.debug)
        self._trim_pins()
        self._repaint()  # show the applied settings instantly
        self._wake()  # force an immediate re-poll so the icons repaint

    def _open_about(self) -> None:
        from .about import open_about

        open_about(self, self.config)

    # --- lifecycle ----------------------------------------------------------

    def _exit(self) -> None:
        for state in self._states.values():
            self._end_full(state)  # a mouse still on the charger: save its last full moment
        self._stop.set()
        self._wakeup.set()
        if self._devices is not None:
            self._devices.close()
        while self._slots:
            self._slots.pop().destroy()
        self.Destroy()


def _discharging(status: BatteryStatus) -> bool:
    """Present, awake and running on battery -- the only state that drains it."""
    return status.present and not status.charging and not status.full and not status.asleep


def _awake(status: BatteryStatus) -> bool:
    """Settled enough for the slow poll; anything else (absent, charging,
    asleep) is transient and polled fast."""
    return status.present and not status.charging and not status.asleep


class _App(wx.App):
    def __init__(self, config: Config):
        self._config = config
        super().__init__(False)

    def OnInit(self) -> bool:  # noqa: N802 (wx override)
        frame = TrayApp(self._config)
        self.SetTopWindow(frame)
        return True


def _default_autostart(app_name: str) -> None:
    """Turn autostart on at the very first start; later starts respect the user's choice."""
    from ..storage import autostart_defaulted, mark_autostart_defaulted

    if autostart_defaulted(app_name):
        return
    try:
        autostart.set_enabled(app_name, True)
    except OSError as exc:
        log.warning("Could not enable autostart by default: %s", exc)
        return
    mark_autostart_defaulted(app_name)
    log.info("Autostart enabled by default: %s", autostart.command())


def run(config: Config | None = None) -> None:
    """Launch the tray application."""
    from ..storage import adopt_legacy_data, load_settings

    cfg = config or default_config
    # Before anything reads the registry: the first start after the rename
    # brings the Mouse Tray settings, timers and history along.
    adopted = adopt_legacy_data(LEGACY_APP_NAME, cfg.app_name)
    load_settings(cfg.app_name, cfg)  # apply any persisted user settings
    set_language(cfg.language)
    setup_logging(cfg.app_name, cfg.debug)
    # The exe has no console: without this an exception in a menu or dialog
    # handler vanishes (or takes the process down) without a trace in app.log.
    sys.excepthook = lambda kind, value, tb: log.error("Unhandled error", exc_info=(kind, value, tb))
    log.info("Starting %s %s from %s", cfg.display_name, version_string(), sys.executable)
    log_startup_diagnostics()
    if adopted:
        log.info("Settings adopted from %s", LEGACY_APP_NAME)
    autostart.adopt_legacy(LEGACY_APP_NAME, cfg.app_name)
    autostart.repair(cfg.app_name)  # follow the program if its folder moved
    _default_autostart(cfg.app_name)
    with suppress(AttributeError, OSError):  # non-Windows or older Windows
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    app = _App(cfg)
    app.MainLoop()

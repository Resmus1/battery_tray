"""Sony gamepads: DualShock 4 and DualSense (USB and Bluetooth).

Unlike the mice there is no request: the pad streams input reports on its own
and the battery rides in a status byte of every full report. Offsets follow the
Linux ``hid-playstation`` / ``hid-sony`` drivers (``data[0]`` is the report id,
which hidapi includes for numbered reports):

* DualShock 4 -- USB report 0x01, status at byte 30; Bluetooth report 0x11,
  byte 32. Low nibble is the level 0..10 (0..11 on the cable), bit 4 the cable.
* DualSense -- USB report 0x01, status at byte 53; Bluetooth report 0x31,
  byte 54. Low nibble is the level 0..10, high nibble the state:
  0 discharging, 1 charging, 2 full, anything else an error.

Over Bluetooth both pads start in a reduced mode that sends a short 0x01 report
without the battery; reading feature report 0x05 (calibration) switches them to
the full report, so the driver does that once when it sees a short one.

The level comes in 10% steps, reported here as ``level * 10 + 5`` (capped at
100) the way the Linux drivers do. A pad hidden by DS4Windows / HidHide in
exclusive mode is invisible here, like to any other program.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import ClassVar

import hid

from ...battery import BatteryStatus
from ..bus import devices_for
from ..driver import MouseDriver, MouseModel, register

log = logging.getLogger(__name__)

_SONY = 0x054C
_USB_REPORT = 0x01
_CALIBRATION_REPORT = 0x05
_CALIBRATION_SIZE = 41
_REPORT_SIZE = 100  # larger than any report of either pad
_READ_TIMEOUT_MS = 250
#: Reports read before giving up on a full one (the first may be a stale short one).
_READ_ATTEMPTS = 5

#: Wireless adapter for the DualShock 4: enumerates with no pad paired.
_DS4_DONGLE = 0x0BA0
_DS4_DONGLE_DISCONNECTED = 0x04  # bit in byte 31 of the adapter's USB report


def _pad(name: str, pid: int) -> MouseModel:
    # USB and Bluetooth enumerate under the same PID.
    return MouseModel(name, _SONY, pid, pid)


class _SonyPad(MouseDriver):
    """One connected pad, bound to its own HID path.

    Gamepads come in identical pairs far more often than mice, so unlike the
    other drivers each pad is its own instance, told apart by the serial number
    (the Bluetooth address). Over USB the serial is usually empty and two
    identical pads still collapse into one.
    """

    vendor = "Sony"
    bt_report: ClassVar[int]
    usb_offset: ClassVar[int]
    bt_offset: ClassVar[int]

    def __init__(self, model: MouseModel, serial: str):
        super().__init__(model)
        self._serial = serial

    @property
    def name(self) -> str:
        # Stable per pad, so its full-charge timer and discharge history (keyed
        # by name) don't mix with another pad of the same model.
        return f"{self.model.name} ({self._serial[-5:].upper()})" if self._serial else self.model.name

    @property
    def key(self) -> str:
        return f"{super().key}/{self._serial}" if self._serial else super().key

    @classmethod
    def detect_all(cls) -> list[MouseDriver]:
        return [
            cls(model, device.get("serial_number") or "")
            for model in cls.models
            for device in devices_for(model)
        ]

    def _current_path(self) -> bytes | None:
        """This pad's HID path in the latest bus snapshot.

        The instance outlives a re-plug (the app reuses it by ``key``), but the
        path changes with the USB port or the Bluetooth link, so it is looked
        up again on every read rather than kept from detection.
        """
        for device in devices_for(self.model):
            if (device.get("serial_number") or "") == self._serial:
                return device["path"]
        return None

    def read_status(self) -> BatteryStatus:
        path = self._current_path()
        if path is None:
            return BatteryStatus.absent()
        device = hid.device()
        try:
            device.open_path(path)
            report = self._full_report(device)
        except OSError as exc:
            log.warning("%s HID read failed: %s", self.name, exc)
            return BatteryStatus.absent()
        finally:
            with suppress(Exception):
                device.close()
        if report is None:
            log.info("%s sent no full report", self.name)
            return BatteryStatus.absent()
        log.debug("%s received report: %s", self.name, report)
        dongle = self.model.pid_wireless == _DS4_DONGLE and report[0] == _USB_REPORT
        if dongle and len(report) > 31 and report[31] & _DS4_DONGLE_DISCONNECTED:
            return BatteryStatus.absent()  # adapter plugged in, no pad linked
        offset = self.usb_offset if report[0] == _USB_REPORT else self.bt_offset
        return self._parse(report[offset])

    def _full_report(self, device: hid.device) -> list[int] | None:
        """Read until a report carrying the battery arrives, or ``None``."""
        switched = False
        for _ in range(_READ_ATTEMPTS):
            report = device.read(_REPORT_SIZE, timeout_ms=_READ_TIMEOUT_MS)
            if not report:
                return None  # a connected pad streams nonstop: silence means gone
            if report[0] == _USB_REPORT and len(report) > self.usb_offset:
                return report
            if report[0] == self.bt_report and len(report) > self.bt_offset:
                return report
            if not switched:
                # Short Bluetooth report: ask for the full ones.
                switched = True
                with suppress(OSError):
                    device.get_feature_report(_CALIBRATION_REPORT, _CALIBRATION_SIZE)
        return None

    def _parse(self, status: int) -> BatteryStatus:
        raise NotImplementedError


def _percent(level: int) -> int:
    return min(level * 10 + 5, 100)


@register
class DualShock4Driver(_SonyPad):
    bt_report = 0x11
    usb_offset = 30
    bt_offset = 32
    models: ClassVar[list[MouseModel]] = [
        _pad("DualShock 4", 0x05C4),
        _pad("DualShock 4 v2", 0x09CC),
        _pad("DualShock 4 USB adapter", _DS4_DONGLE),
    ]

    def _parse(self, status: int) -> BatteryStatus:
        level = status & 0x0F
        cable = bool(status & 0x10)
        log.info("%s level=%s cable=%s", self.name, level, cable)
        if not cable:
            return BatteryStatus(present=True, percent=_percent(level))
        if level <= 10:
            return BatteryStatus(present=True, percent=_percent(level), charging=True)
        if level == 11:
            return BatteryStatus(present=True, percent=100, full=True)
        return BatteryStatus(present=True)  # charging error: level unknown


@register
class DualSenseDriver(_SonyPad):
    bt_report = 0x31
    usb_offset = 53
    bt_offset = 54
    models: ClassVar[list[MouseModel]] = [
        _pad("DualSense", 0x0CE6),
        _pad("DualSense Edge", 0x0DF2),
    ]

    def _parse(self, status: int) -> BatteryStatus:
        level = status & 0x0F
        state = status >> 4
        log.info("%s level=%s state=%s", self.name, level, state)
        if state == 0:
            return BatteryStatus(present=True, percent=_percent(level))
        if state == 1:
            return BatteryStatus(present=True, percent=_percent(level), charging=True)
        if state == 2:
            return BatteryStatus(present=True, percent=100, full=True)
        return BatteryStatus(present=True)  # voltage/temperature/charging error

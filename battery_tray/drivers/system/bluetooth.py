"""Any Bluetooth device whose battery Windows itself shows.

Windows reads the standard GATT Battery Service of a paired BLE device (mouse,
keyboard, gamepad) and the hands-free battery report of a classic headset, and
keeps the latest level in an undocumented device property, the one behind
Settings > Bluetooth & devices::

    {104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2    (a byte, 0..100)

It sits on the ``BTHLE\\DEV_<address>`` node for BLE and on the hands-free
service node under ``BTHENUM`` for classic devices, and it is read with
CfgMgr32 -- plain Win32 through ``ctypes``, no WinRT and no new dependency.

The level survives a disconnect (a pad switched off months ago still says
89%), so a device counts only while ``DN_DEVICE_DISCONNECTED`` is clear both on
the node with the level and on the device's own ``DEV_<address>`` node -- for a
classic headset the flag sits on the latter, not on its hands-free node.
Windows reports the level alone: no charging or full state.

A device that another driver already reads over HID (a Logitech mouse paired
over Bluetooth) is left to that driver, which knows more than the level.
"""

from __future__ import annotations

import ctypes
import logging
import re
import sys
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from typing import ClassVar

from ...battery import BatteryStatus
from ..bus import enumerate_devices
from ..driver import MouseDriver, MouseModel, all_drivers, register

log = logging.getLogger(__name__)

#: Seconds a device-tree snapshot stays valid, so detection and the reads that
#: follow it share one walk (as the HID bus snapshot does).
_SCAN_TTL = 0.5

_ENUMERATORS = ("BTHLE", "BTHENUM")

_CR_SUCCESS = 0
_CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001
_CM_GETIDLIST_FILTER_PRESENT = 0x00000100
_CM_LOCATE_DEVNODE_NORMAL = 0
#: cfg.h: "typically this means a wireless device is out of range".
_DN_DEVICE_DISCONNECTED = 0x02000000

_DEVPROP_TYPE_BYTE = 0x03
_DEVPROP_TYPE_GUID = 0x0D
_DEVPROP_TYPE_STRING = 0x12

#: The device-node part of the instance id holds the 12-digit Bluetooth address:
#: ``BTHLE\DEV_D1F2C3A4B5E6\...`` or ``BTHENUM\{...}\...&0&D1F2C3A4B5E6_C00000000``.
_ADDRESS = re.compile(r"(?:DEV_|&)([0-9A-F]{12})(?=[\\_]|$)")
#: In a HID path (``...{00001812-...}_DEV_VID&02046D_PID&B034_REV&0007_D1F2C3A4B5E6&COL01#...``)
#: the address is any free-standing run of 12 hex digits.
_HEX12 = re.compile(r"(?<![0-9A-F])([0-9A-F]{12})(?![0-9A-F])")


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    )


class _DevPropKey(ctypes.Structure):
    _fields_ = (("fmtid", _Guid), ("pid", ctypes.c_uint32))


def _key(fmtid: str, pid: int) -> _DevPropKey:
    raw = uuid.UUID(fmtid).bytes_le
    guid = _Guid.from_buffer_copy(raw)
    return _DevPropKey(guid, pid)


_BATTERY = _key("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)
_FRIENDLY_NAME = _key("A45C254E-DF1C-4EFD-8020-67D146A850E0", 14)  # DEVPKEY_Device_FriendlyName
_NAME = _key("B725F130-47EF-101A-A5F1-02608C9EEBAC", 10)  # DEVPKEY_NAME
_CONTAINER = _key("8C7ED206-3F8A-4827-B3AB-AE9E1FAEFC6C", 2)  # DEVPKEY_Device_ContainerId


@dataclass(frozen=True)
class _Device:
    """One physical Bluetooth device with a battery level, as Windows has it."""

    container: str
    name: str
    level: int
    connected: bool
    address: str | None


_cfgmgr: ctypes.WinDLL | None = None
_snapshot: tuple[float, dict[str, _Device]] | None = None


def _api() -> ctypes.WinDLL:
    global _cfgmgr  # noqa: PLW0603 -- one process-wide handle
    if _cfgmgr is None:
        dll = ctypes.WinDLL("cfgmgr32")
        ulong, pulong = wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)
        dll.CM_Get_Device_ID_List_SizeW.argtypes = (pulong, wintypes.LPCWSTR, ulong)
        dll.CM_Get_Device_ID_ListW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, ulong, ulong)
        dll.CM_Locate_DevNodeW.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, ulong)
        dll.CM_Get_DevNode_Status.argtypes = (
            ctypes.POINTER(wintypes.ULONG),
            ctypes.POINTER(wintypes.ULONG),
            wintypes.DWORD,
            wintypes.ULONG,
        )
        dll.CM_Get_DevNode_PropertyW.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(_DevPropKey),
            ctypes.POINTER(wintypes.ULONG),
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.ULONG),
            wintypes.ULONG,
        )
        _cfgmgr = dll
    return _cfgmgr


def _instance_ids(api: ctypes.WinDLL, enumerator: str) -> list[str]:
    """Instance ids of the present device nodes under one enumerator."""
    flags = _CM_GETIDLIST_FILTER_ENUMERATOR | _CM_GETIDLIST_FILTER_PRESENT
    size = wintypes.ULONG()
    if api.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), enumerator, flags) != _CR_SUCCESS:
        return []
    buffer = ctypes.create_unicode_buffer(size.value)
    if api.CM_Get_Device_ID_ListW(enumerator, buffer, size.value, flags) != _CR_SUCCESS:
        return []
    # A double-NUL-terminated list of strings.
    return [part for part in buffer[: size.value].split("\0") if part]


def _property(api: ctypes.WinDLL, node: int, key: _DevPropKey) -> tuple[int, bytes] | None:
    """``(type, raw bytes)`` of one property, or ``None`` when the node lacks it."""
    kind = wintypes.ULONG()
    buffer = (ctypes.c_ubyte * 512)()
    size = wintypes.ULONG(ctypes.sizeof(buffer))
    result = api.CM_Get_DevNode_PropertyW(
        node, ctypes.byref(key), ctypes.byref(kind), buffer, ctypes.byref(size), 0
    )
    if result != _CR_SUCCESS:
        return None
    return kind.value, bytes(buffer[: size.value])


def _string(api: ctypes.WinDLL, node: int, key: _DevPropKey) -> str | None:
    value = _property(api, node, key)
    if value is None or value[0] != _DEVPROP_TYPE_STRING:
        return None
    return value[1].decode("utf-16-le").rstrip("\0") or None


def _scan_tree() -> dict[str, _Device]:
    """Every present Bluetooth device with a battery level, by container id."""
    api = _api()
    levels: dict[str, list[tuple[int, bool, str]]] = {}
    names: dict[str, str] = {}
    gone: set[str] = set()  # containers whose own device node is disconnected
    for enumerator in _ENUMERATORS:
        for instance in _instance_ids(api, enumerator):
            node = wintypes.DWORD()
            if api.CM_Locate_DevNodeW(ctypes.byref(node), instance, _CM_LOCATE_DEVNODE_NORMAL) != _CR_SUCCESS:
                continue
            container = _property(api, node.value, _CONTAINER)
            if container is None or container[0] != _DEVPROP_TYPE_GUID:
                continue
            container_id = str(uuid.UUID(bytes_le=container[1][:16])).upper()
            status, problem = wintypes.ULONG(), wintypes.ULONG()
            api.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), node.value, 0)
            connected = not status.value & _DN_DEVICE_DISCONNECTED
            if instance.upper().startswith((r"BTHLE\DEV_", r"BTHENUM\DEV_")):
                # The device's own node carries its plain name (a service
                # node's is "<name> Hands-Free AG" and the like) and, for a
                # classic device, the only reliable connection state.
                name = _string(api, node.value, _FRIENDLY_NAME) or _string(api, node.value, _NAME)
                if name:
                    names[container_id] = name
                if not connected:
                    gone.add(container_id)
            battery = _property(api, node.value, _BATTERY)
            if battery is None or battery[0] != _DEVPROP_TYPE_BYTE or not battery[1]:
                continue
            log.debug("%s battery=%s status=0x%08X", instance, battery[1][0], status.value)
            levels.setdefault(container_id, []).append((battery[1][0], connected, instance))
            names.setdefault(
                container_id,
                _string(api, node.value, _FRIENDLY_NAME) or _string(api, node.value, _NAME) or "Bluetooth",
            )

    devices: dict[str, _Device] = {}
    for container_id, reports in levels.items():
        connected = [report for report in reports if report[1]]
        # A headset can report on its LE and its classic node; the lower level
        # is the one worth warning about.
        level, _, instance = min(connected or reports)
        match = _ADDRESS.search(instance.upper())
        address = match.group(1) if match else None
        # The app keeps per-device memory (timer, discharge history, warnings)
        # by name, so two headsets of one model need names of their own -- the
        # address tail, stable across restarts, as the Sony pads do.
        tag = address or container_id.replace("-", "")
        devices[container_id] = _Device(
            container=container_id,
            name=f"{names[container_id]} ({tag[-4:]})",
            level=min(level, 100),
            connected=bool(connected) and container_id not in gone,
            address=address,
        )
    return devices


def _scan() -> dict[str, _Device]:
    """The device tree, cached for ``_SCAN_TTL``. Never raises."""
    global _snapshot  # noqa: PLW0603 -- one process-wide cache, by definition
    now = time.monotonic()
    if _snapshot is not None and now - _snapshot[0] < _SCAN_TTL:
        return _snapshot[1]
    try:
        devices = _scan_tree()
    except Exception as exc:  # a failed walk reads as "no Bluetooth device"
        log.warning("Bluetooth device scan failed: %s", exc)
        devices = {}
    _snapshot = (now, devices)
    return devices


def _claimed_by_hid() -> set[str]:
    """Bluetooth addresses (upper-case hex) that another driver reads over HID.

    The HID path of a Bluetooth device embeds its address, and its VID/PID
    tells whether some registered driver lists it.
    """
    known = {
        (model.vid, pid)
        for driver_cls in all_drivers()
        if driver_cls is not BluetoothDriver
        for model in driver_cls.models
        for pid in (model.pid_wireless, model.pid_wired)
    }
    claimed: set[str] = set()
    for device in enumerate_devices():
        if (device["vendor_id"], device["product_id"]) not in known:
            continue
        path = device["path"]
        text = path.decode(errors="replace") if isinstance(path, bytes) else str(path)
        # Over-matches the last group of a service GUID too, which no device
        # address equals -- harmless.
        claimed.update(_HEX12.findall(text.upper()))
    return claimed


@register
class BluetoothDriver(MouseDriver):
    """One paired, connected Bluetooth device, keyed by its container id."""

    vendor = "Bluetooth"
    note = (
        "Bluetooth: any paired device whose battery Windows shows in Settings "
        "(mice, keyboards, gamepads, headphones). Windows gives the level only, "
        "without the charging state."
    )
    models: ClassVar[list[MouseModel]] = []

    def __init__(self, device: _Device):
        super().__init__(MouseModel(device.name, 0, 0, 0))
        self._container = device.container

    @property
    def key(self) -> str:
        return f"{type(self).__name__}/{self._container}"

    @classmethod
    def detect_all(cls) -> list[MouseDriver]:
        if sys.platform != "win32":
            return []
        devices = [device for device in _scan().values() if device.connected]
        if not devices:
            return []
        claimed = _claimed_by_hid()
        return [cls(device) for device in devices if device.address not in claimed]

    def read_status(self) -> BatteryStatus:
        device = _scan().get(self._container)
        if device is None or not device.connected:
            return BatteryStatus.absent()
        log.info("%s level=%s", self.name, device.level)
        return BatteryStatus(present=True, percent=device.level)

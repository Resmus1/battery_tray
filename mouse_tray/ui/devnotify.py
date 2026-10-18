"""Hot-plug notifications: wake the poll loop when a USB device comes or goes.

Between ticks the worker thread sleeps for the full poll interval, so with a
long interval a cable plugged into the mouse (a new HID device on the bus) or
a dongle inserted would go unnoticed for minutes. Windows announces every such
change with ``WM_DEVICECHANGE``; this module catches it on the hidden frame and
calls back after a short debounce -- one plug produces a burst of several
messages, and the device is not always ready to enumerate on the first one.

wxPython has no hook for raw window messages, so the frame's window procedure
is subclassed through ctypes. Everything else is forwarded to the original
procedure untouched.
"""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable
from ctypes import wintypes

import wx

log = logging.getLogger(__name__)

_WM_DEVICECHANGE = 0x0219
#: ``wParam`` values of interest. The first two come with the registered HID
#: interface filter; the last is broadcast for any devnode change and covers
#: devices that announce themselves some other way.
_DBT_DEVICEARRIVAL = 0x8000
_DBT_DEVICEREMOVECOMPLETE = 0x8004
_DBT_DEVNODES_CHANGED = 0x0007
_DEVICE_EVENTS = frozenset({_DBT_DEVICEARRIVAL, _DBT_DEVICEREMOVECOMPLETE, _DBT_DEVNODES_CHANGED})

_DBT_DEVTYP_DEVICEINTERFACE = 5
_DEVICE_NOTIFY_WINDOW_HANDLE = 0
_GWLP_WNDPROC = -4

#: Milliseconds of silence after the last message before the callback fires.
DEBOUNCE_MS = 1000


class _GUID(ctypes.Structure):
    _fields_ = (
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", wintypes.BYTE * 8),
    )


class _DevBroadcastDeviceInterface(ctypes.Structure):
    _fields_ = (
        ("dbcc_size", wintypes.DWORD),
        ("dbcc_devicetype", wintypes.DWORD),
        ("dbcc_reserved", wintypes.DWORD),
        ("dbcc_classguid", _GUID),
        ("dbcc_name", wintypes.WCHAR * 1),
    )


#: GUID_DEVINTERFACE_HID {4D1E55B2-F16F-11CF-88CB-001111000030}
_GUID_DEVINTERFACE_HID = _GUID(
    0x4D1E55B2, 0xF16F, 0x11CF, (wintypes.BYTE * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30)
)

_LONG_PTR = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LONG_PTR, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def _user32() -> ctypes.WinDLL:
    user32 = ctypes.windll.user32
    # SetWindowLongPtrW only exists on 64-bit user32; 32-bit exports SetWindowLongW.
    set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    set_long.argtypes = (wintypes.HWND, ctypes.c_int, _LONG_PTR)
    set_long.restype = _LONG_PTR
    user32.CallWindowProcW.argtypes = (
        ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    user32.CallWindowProcW.restype = _LONG_PTR
    user32.RegisterDeviceNotificationW.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    user32.RegisterDeviceNotificationW.restype = wintypes.HANDLE
    user32.UnregisterDeviceNotification.argtypes = (wintypes.HANDLE,)
    user32.UnregisterDeviceNotification.restype = wintypes.BOOL
    return user32


class DeviceChangeWatcher:
    """Call ``on_change`` (on the main thread) when a USB device comes or goes.

    ``window`` must be a top-level wx window; its message loop delivers the
    notifications. Raises ``OSError`` when the hook cannot be installed, so the
    caller can fall back to plain polling.
    """

    def __init__(self, window: wx.Window, on_change: Callable[[], None]):
        self._on_change = on_change
        self._user32 = _user32()
        user32 = self._user32
        self._set_window_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        self._hwnd = window.GetHandle()

        # Debounce timer: restarted on every message, fires once after the burst.
        self._timer = wx.Timer(window)
        window.Bind(wx.EVT_TIMER, self._fire, self._timer)

        # The callback object must outlive the hook: the C side holds a raw
        # pointer into it, so it is kept on self for the watcher's lifetime.
        self._proc = _WNDPROC(self._wndproc)
        self._orig_proc = self._set_window_long(
            self._hwnd, _GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p).value
        )
        if not self._orig_proc:
            raise OSError("SetWindowLongPtr failed")

        # Ask explicitly for HID interface arrivals/removals: a hidden window is
        # not guaranteed to be in the DBT_DEVNODES_CHANGED broadcast.
        flt = _DevBroadcastDeviceInterface()
        flt.dbcc_size = ctypes.sizeof(flt)
        flt.dbcc_devicetype = _DBT_DEVTYP_DEVICEINTERFACE
        flt.dbcc_classguid = _GUID_DEVINTERFACE_HID
        self._hnotify = self._user32.RegisterDeviceNotificationW(
            self._hwnd, ctypes.byref(flt), _DEVICE_NOTIFY_WINDOW_HANDLE
        )
        if not self._hnotify:
            self.close()
            raise OSError("RegisterDeviceNotification failed")

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == _WM_DEVICECHANGE and wparam in _DEVICE_EVENTS:
            log.debug("WM_DEVICECHANGE 0x%04x", wparam)
            self._timer.StartOnce(DEBOUNCE_MS)
        return self._user32.CallWindowProcW(self._orig_proc, hwnd, msg, wparam, lparam)

    def _fire(self, _evt: wx.TimerEvent) -> None:
        log.info("Device change detected, re-polling")
        self._on_change()

    def close(self) -> None:
        """Unhook; call before the window is destroyed."""
        self._timer.Stop()
        if getattr(self, "_hnotify", None):
            self._user32.UnregisterDeviceNotification(self._hnotify)
            self._hnotify = None
        if getattr(self, "_orig_proc", None):
            self._set_window_long(self._hwnd, _GWLP_WNDPROC, self._orig_proc)
            self._orig_proc = None

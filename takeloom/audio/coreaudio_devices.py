"""Live list of audio device names straight from macOS CoreAudio, via ctypes.

Exists for the always-running `takeloom server`'s hardware watcher (see
rig_watcher.py), which needs to notice an audio interface being powered on
or off at any time. PortAudio (sounddevice) can't answer that: it snapshots
the device list once at initialization, and the only way to refresh it —
sd._terminate()/sd._initialize() — is unsafe while any stream is open,
which is exactly when we most need to notice the device vanishing (the
ambient monitor stream is open the whole time the rig is on).

CoreAudio's own device list is safe to query at any time, from any thread,
alongside open streams. One catch for a process with no running main
CFRunLoop (a headless CLI server): the HAL only delivers device-list
updates on the run loop it's told to use, so without
kAudioHardwarePropertyRunLoop set to NULL ("use your own thread") the list
would stay frozen at whatever was present at first query. _ensure_hal_thread
sets it once.

Returns None (not an empty list) on non-macOS or any CoreAudio failure, so
callers can tell "couldn't check" apart from "nothing connected".
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import sys
import threading

_kAudioObjectSystemObject = 1
_kAudioObjectPropertyScopeGlobal = struct.unpack(">I", b"glob")[0]
_kAudioObjectPropertyElementMain = 0
_kAudioHardwarePropertyDevices = struct.unpack(">I", b"dev#")[0]
_kAudioHardwarePropertyRunLoop = struct.unpack(">I", b"rnlp")[0]
_kAudioObjectPropertyName = struct.unpack(">I", b"lnam")[0]
_kCFStringEncodingUTF8 = 0x08000100


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


_lock = threading.Lock()
_libs: tuple | None = None
_hal_thread_set = False


def _load() -> tuple | None:
    global _libs
    if _libs is not None:
        return _libs
    if sys.platform != "darwin":
        return None
    try:
        ca = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreAudio"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    except (OSError, TypeError):
        return None
    ca.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
    ]
    ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    ca.AudioObjectSetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_void_p,
    ]
    ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    _libs = (ca, cf)
    return _libs


def _ensure_hal_thread(ca) -> None:
    global _hal_thread_set
    if _hal_thread_set:
        return
    addr = _PropertyAddress(_kAudioHardwarePropertyRunLoop, _kAudioObjectPropertyScopeGlobal, _kAudioObjectPropertyElementMain)
    run_loop = ctypes.c_void_p(None)
    ca.AudioObjectSetPropertyData(
        _kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.sizeof(run_loop), ctypes.byref(run_loop),
    )
    _hal_thread_set = True


def _device_name(ca, cf, device_id: int) -> str | None:
    addr = _PropertyAddress(_kAudioObjectPropertyName, _kAudioObjectPropertyScopeGlobal, _kAudioObjectPropertyElementMain)
    cfstr = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(cfstr))
    if ca.AudioObjectGetPropertyData(device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(cfstr)) != 0:
        return None
    if not cfstr.value:
        return None
    try:
        buf = ctypes.create_string_buffer(512)
        if not cf.CFStringGetCString(cfstr, buf, len(buf), _kCFStringEncodingUTF8):
            return None
        return buf.value.decode("utf-8", "replace")
    finally:
        cf.CFRelease(cfstr)


def list_device_names() -> list[str] | None:
    """Names of every audio device CoreAudio currently knows about (the same
    names sounddevice reports), or None if this can't be checked here."""
    with _lock:
        libs = _load()
        if libs is None:
            return None
        ca, cf = libs
        try:
            _ensure_hal_thread(ca)
            addr = _PropertyAddress(_kAudioHardwarePropertyDevices, _kAudioObjectPropertyScopeGlobal, _kAudioObjectPropertyElementMain)
            size = ctypes.c_uint32(0)
            if ca.AudioObjectGetPropertyDataSize(_kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
                return None
            count = size.value // ctypes.sizeof(ctypes.c_uint32)
            ids = (ctypes.c_uint32 * max(count, 1))()
            if ca.AudioObjectGetPropertyData(_kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size), ids) != 0:
                return None
            count = size.value // ctypes.sizeof(ctypes.c_uint32)
            names = [_device_name(ca, cf, ids[i]) for i in range(count)]
        except Exception:
            return None
        return [n for n in names if n]

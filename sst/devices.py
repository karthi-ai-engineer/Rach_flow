"""The microphones Windows has right now, from its own audio system (Core Audio's MMDevice API, through ctypes).

PortAudio (sounddevice) reads the list of devices once and keeps it until it is restarted, which closes every open
stream: a headset plugged in later isn't in its list. Windows' own list is always current and reading it disturbs
nothing, so Rflow asks it what exists and which microphone is the default, and restarts PortAudio only when that
changed (sst.audio.refresh_devices). Read-only: nothing here opens a microphone.
"""
import ctypes
import logging
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

E_CAPTURE, E_CONSOLE = 1, 0  # EDataFlow, ERole
STATES = {1: "active", 2: "disabled", 4: "not present", 8: "unplugged"}  # DEVICE_STATE_*
_ACTIVE, _ALL = 0x1, 0xF
_VT_LPWSTR = 31
_CLSCTX_ALL = 0x17
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: this thread's COM was set up another way already (fine)


@dataclass(frozen=True)
class Device:
    id: str  # Windows' endpoint id: stable while the device exists
    name: str  # as Windows shows it, e.g. "Headset Microphone (Jabra Evolve2 65)"; PortAudio's WASAPI name is the same
    state: str = "active"


@dataclass(frozen=True)
class Snapshot:
    """What Rflow needs to notice a change: the microphones that can record, and the default one."""
    devices: tuple[Device, ...]
    default: Device | None

    @property
    def names(self) -> list[str]:
        return [d.name for d in self.devices]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("value", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


_CLSID_ENUMERATOR = _GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_ENUMERATOR = _GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
_FRIENDLY_NAME = _PROPERTYKEY(_GUID.of("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)  # PKEY_Device_FriendlyName


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    """Method `index` of a COM interface's vtable (0-2 are IUnknown's), called with the object first. A failed HRESULT
    raises OSError."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return lambda *args: prototype(vtable[index])(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(obj)


@contextmanager
def _enumerator():
    """IMMDeviceEnumerator, with COM set up for this thread for the time it's used."""
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.restype = ctypes.c_long
    hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED, like Qt's own thread
    enumerator = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance.restype = ctypes.HRESULT
        ole32.CoCreateInstance(ctypes.byref(_CLSID_ENUMERATOR), None, _CLSCTX_ALL, ctypes.byref(_IID_ENUMERATOR),
                               ctypes.byref(enumerator))
        yield enumerator
    finally:
        _release(enumerator)
        if hr != _RPC_E_CHANGED_MODE:
            ole32.CoUninitialize()


def _device(device: ctypes.c_void_p) -> Device:
    ole32 = ctypes.WinDLL("ole32")
    raw_id = ctypes.c_wchar_p()
    _method(device, 5, ctypes.POINTER(ctypes.c_wchar_p))(ctypes.byref(raw_id))  # GetId
    device_id = raw_id.value or ""
    ole32.CoTaskMemFree(raw_id)
    state = wintypes.DWORD()
    _method(device, 6, ctypes.POINTER(wintypes.DWORD))(ctypes.byref(state))  # GetState
    store, value, name = ctypes.c_void_p(), _PROPVARIANT(), ""
    try:
        _method(device, 4, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(0, ctypes.byref(store))  # STGM_READ
        _method(store, 5, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))(
            ctypes.byref(_FRIENDLY_NAME), ctypes.byref(value))  # IPropertyStore::GetValue
        if value.vt == _VT_LPWSTR and value.value:
            name = ctypes.wstring_at(value.value)
    finally:
        ole32.PropVariantClear(ctypes.byref(value))
        _release(store)
    return Device(device_id, name, STATES.get(state.value, "unknown"))


def _capture(enumerator, mask: int) -> list[Device]:
    collection, count, out = ctypes.c_void_p(), wintypes.UINT(), []
    _method(enumerator, 3, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(
        E_CAPTURE, mask, ctypes.byref(collection))  # EnumAudioEndpoints
    try:
        _method(collection, 3, ctypes.POINTER(wintypes.UINT))(ctypes.byref(count))  # GetCount
        for i in range(count.value):
            device = ctypes.c_void_p()
            _method(collection, 4, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(i, ctypes.byref(device))  # Item
            try:
                out.append(_device(device))
            finally:
                _release(device)
    finally:
        _release(collection)
    return out


def _default(enumerator) -> Device | None:
    device = ctypes.c_void_p()
    try:
        _method(enumerator, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            E_CAPTURE, E_CONSOLE, ctypes.byref(device))  # GetDefaultAudioEndpoint
    except OSError:  # E_NOTFOUND: no microphone at all
        return None
    try:
        return _device(device)
    finally:
        _release(device)


def snapshot() -> Snapshot | None:
    """The microphones that can record now, and Windows' default one; None if Core Audio can't be asked (then Rflow
    goes by PortAudio's list, as before)."""
    try:
        with _enumerator() as enumerator:
            devices = tuple(sorted(_capture(enumerator, _ACTIVE), key=lambda d: d.name.casefold()))
            return Snapshot(devices, _default(enumerator))
    except OSError as e:
        log.warning("Couldn't ask Windows for its microphones: %s", e)
        return None


def all_capture() -> list[Device]:
    """Every microphone Windows knows, with its state: also the unplugged and disabled ones (for messages)."""
    try:
        with _enumerator() as enumerator:
            return _capture(enumerator, _ALL)
    except OSError as e:
        log.warning("Couldn't ask Windows for its microphones: %s", e)
        return []

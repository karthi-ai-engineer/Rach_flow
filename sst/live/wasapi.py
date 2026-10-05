"""What the laptop plays (WASAPI loopback) or what its microphone hears, through Core Audio (ctypes), as the live
models want it: 16 kHz mono 16-bit frames of 100 ms.

    Capture.speakers()    the default output device (speakers or headphones), in shared loopback mode
    Capture.microphone()  the default communications microphone (the one Teams and Zoom use unless told otherwise)
    -> IAudioClient -> its mix format (usually 48 kHz float) -> mono -> 16 kHz (a box filter, then interpolation; state
    kept across packets) -> 100 ms frames

Windows sends no loopback packets at all while nothing plays, so silence is filled in to keep the stream's clock going
(the model hears a pause, not a jump). When the default device changes (headphones plugged in) or goes away, the
capture opens the new one. Read-only: nothing here plays or changes a sound. Its own small COM helpers, on purpose:
live captions share no code with dictation's microphone (sst.audio, sst.devices); shared mode lets both have it open.
"""
import ctypes
import logging
import threading
import time
import uuid
from collections.abc import Callable
from ctypes import wintypes

import numpy as np

from sst.live.contracts import FRAME_MS, RATE

FRAME = RATE * FRAME_MS // 1000  # 1,600 samples
SILENCE_AFTER = 0.15  # seconds without a packet: nothing is playing, so silence is added
CHECK_DEVICE_EVERY = 2.0  # seconds between looks at which output is the default
_BUFFER = 2_000_000  # 200 ms, in 100 ns units: WASAPI's buffer, read every 10 ms

log = logging.getLogger(__name__)


# ---- turning the device's audio into 16 kHz mono 16-bit (pure numpy: tested without Windows)

class Resampler:
    """A stream from `rate` to 16 kHz, packet by packet, with no seams: a box filter against aliasing (its window ~ the
    rate ratio), then linear interpolation. Plenty for speech recognition."""

    def __init__(self, rate: int, target: int = RATE):
        self.ratio = rate / target
        self.width = max(1, round(self.ratio))
        self._tail = np.zeros(self.width - 1, dtype=np.float32)  # the input the filter still needs
        self._buf = np.zeros(0, dtype=np.float32)  # filtered input not yet used
        self._pos = 0.0  # where the next output sample falls in _buf

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.ratio == 1:
            return x
        if self.width > 1:
            raw = np.concatenate([self._tail, x])
            filtered = np.convolve(raw, np.full(self.width, 1 / self.width, dtype=np.float32), mode="valid")
            self._tail = raw[len(raw) - (self.width - 1):]
        else:
            filtered = x
        buf = np.concatenate([self._buf, filtered])
        last = len(buf) - 1
        n = int((last - self._pos) // self.ratio) + 1 if last >= self._pos else 0
        positions = self._pos + self.ratio * np.arange(n)
        out = np.interp(positions, np.arange(len(buf)), buf).astype(np.float32) if n else np.zeros(0, np.float32)
        following = self._pos + self.ratio * n
        drop = min(int(following), len(buf))
        self._buf, self._pos = buf[drop:], following - drop
        return out


def decode(data: bytes, channels: int, bits: int, is_float: bool) -> np.ndarray:
    """A packet in the device's format as mono float in [-1, 1]."""
    if is_float and bits == 32:
        x = np.frombuffer(data, dtype="<f4")
    elif bits == 16:
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
    elif bits == 32:
        x = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648
    elif bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        x = ((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8 >> 8).astype(np.float32) / 8388608
    else:
        raise ValueError(f"unsupported audio format: {bits}-bit{' float' if is_float else ''}")
    x = x[: len(x) - len(x) % channels].reshape(-1, channels)
    return x.mean(axis=1) if channels > 1 else x[:, 0]


class Framer:
    """16 kHz samples in, 100 ms PCM16 frames out (bytes)."""

    def __init__(self):
        self._pending = np.zeros(0, dtype=np.int16)

    def __call__(self, samples: np.ndarray) -> list[bytes]:
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        self._pending = np.concatenate([self._pending, pcm])
        frames, used = [], 0
        while len(self._pending) - used >= FRAME:
            frames.append(self._pending[used:used + FRAME].tobytes())
            used += FRAME
        self._pending = self._pending[used:]
        return frames


# ---- Core Audio through ctypes

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class _WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("Format", _WAVEFORMATEX), ("wValidBitsPerSample", wintypes.WORD), ("dwChannelMask", wintypes.DWORD),
                ("SubFormat", _GUID)]


_CLSID_ENUMERATOR = _GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_ENUMERATOR = _GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_AUDIO_CLIENT = _GUID.of("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
_IID_CAPTURE_CLIENT = _GUID.of("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
_FLOAT = uuid.UUID("00000003-0000-0010-8000-00AA00389B71").bytes_le
E_RENDER, E_CAPTURE = 0, 1  # EDataFlow: what's played, what's heard
E_CONSOLE, E_COMMUNICATIONS = 0, 2  # ERole: the default device, the default for calls
_CLSCTX_ALL, _SHARED, _LOOPBACK, _SILENT = 0x17, 0, 0x00020000, 0x2


def _method(obj, index: int, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return lambda *args: ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])(obj, *args)


def _release(obj) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(obj)


class _Stream:
    """One opened stream on a default device: the output device in loopback, or a microphone (call from one thread,
    with COM set up there)."""

    def __init__(self, flow: int = E_RENDER, role: int = E_CONSOLE):
        self.flow, self.role = flow, role
        self.enumerator, self.device, self.client, self.capture = (ctypes.c_void_p() for _ in range(4))
        ctypes.WinDLL("ole32").CoCreateInstance.restype = ctypes.HRESULT
        ctypes.WinDLL("ole32").CoCreateInstance(ctypes.byref(_CLSID_ENUMERATOR), None, _CLSCTX_ALL,
                                                ctypes.byref(_IID_ENUMERATOR), ctypes.byref(self.enumerator))
        try:
            self.device_id = self._default_id()
            _method(self.enumerator, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                self.flow, self.role, ctypes.byref(self.device))  # GetDefaultAudioEndpoint
            _method(self.device, 3, ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(_IID_AUDIO_CLIENT), _CLSCTX_ALL, None,
                                                     ctypes.byref(self.client))  # IMMDevice::Activate
            mix = ctypes.POINTER(_WAVEFORMATEX)()
            _method(self.client, 8, ctypes.POINTER(ctypes.POINTER(_WAVEFORMATEX)))(ctypes.byref(mix))  # GetMixFormat
            try:
                f = mix.contents
                self.rate, self.channels, self.bits, self.block = f.nSamplesPerSec, f.nChannels, f.wBitsPerSample, f.nBlockAlign
                if f.wFormatTag == 0xFFFE:  # WAVEFORMATEXTENSIBLE: the real format is its SubFormat
                    ext = ctypes.cast(mix, ctypes.POINTER(_WAVEFORMATEXTENSIBLE)).contents
                    self.is_float = bytes(ext.SubFormat) == _FLOAT
                else:
                    self.is_float = f.wFormatTag == 3
                _method(self.client, 3, ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                        ctypes.POINTER(_WAVEFORMATEX), ctypes.c_void_p)(
                    _SHARED, _LOOPBACK if self.flow == E_RENDER else 0, _BUFFER, 0, mix, None)
            finally:
                ctypes.WinDLL("ole32").CoTaskMemFree(mix)
            _method(self.client, 14, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(_IID_CAPTURE_CLIENT), ctypes.byref(self.capture))  # GetService
            _method(self.client, 10)()  # Start
        except Exception:
            self.close()
            raise

    def _default_id(self) -> str:
        device, raw = ctypes.c_void_p(), ctypes.c_wchar_p()
        _method(self.enumerator, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            self.flow, self.role, ctypes.byref(device))
        try:
            _method(device, 5, ctypes.POINTER(ctypes.c_wchar_p))(ctypes.byref(raw))  # GetId
            value = raw.value or ""
            ctypes.WinDLL("ole32").CoTaskMemFree(raw)
            return value
        finally:
            _release(device)

    def default_changed(self) -> bool:
        try:
            return self._default_id() != self.device_id
        except OSError:  # no such device at all for a moment
            return True

    def read(self) -> list[np.ndarray]:
        """The packets waiting now, as mono float at the device's rate (silent packets as zeros)."""
        out, size = [], wintypes.UINT()
        next_size = _method(self.capture, 5, ctypes.POINTER(wintypes.UINT))
        get = _method(self.capture, 3, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p)
        release = _method(self.capture, 4, wintypes.UINT)
        next_size(ctypes.byref(size))
        while size.value:
            data, frames, flags = ctypes.c_void_p(), wintypes.UINT(), wintypes.DWORD()
            get(ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
            try:
                if flags.value & _SILENT or not data.value:
                    out.append(np.zeros(frames.value, dtype=np.float32))
                else:
                    raw = ctypes.string_at(data.value, frames.value * self.block)
                    out.append(decode(raw, self.channels, self.bits, self.is_float))
            finally:
                release(frames.value)
            next_size(ctypes.byref(size))
        return out

    def close(self) -> None:
        if self.client:
            try:
                _method(self.client, 11)()  # Stop
            except OSError:
                pass
        for obj in (self.capture, self.client, self.device, self.enumerator):
            _release(obj)
        self.capture = self.client = self.device = self.enumerator = ctypes.c_void_p()


class Capture:
    """start(on_frame) calls on_frame(bytes) with each 100 ms frame, from its own thread: of what the laptop plays
    (speakers()) or of what its microphone hears (microphone())."""

    def __init__(self, opener: Callable = _Stream, clock: Callable[[], float] = time.monotonic,
                 what: str = "output device"):
        self._opener, self._clock, self.what = opener, clock, what
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.device_rate = 0  # the device's own rate, once opened (for the log)

    @classmethod
    def speakers(cls) -> "Capture":
        return cls(lambda: _Stream(E_RENDER, E_CONSOLE), what="output device")

    @classmethod
    def microphone(cls) -> "Capture":
        return cls(lambda: _Stream(E_CAPTURE, E_COMMUNICATIONS), what="microphone")

    def start(self, on_frame: Callable[[bytes], None]) -> None:
        self._stop.clear()
        opened = threading.Event()
        problem: list[Exception] = []
        self._thread = threading.Thread(target=self._run, args=(on_frame, opened, problem),
                                        name=f"live-{self.what.split()[0]}", daemon=True)
        self._thread.start()
        opened.wait(5)
        if problem:  # the first open failed: say so to the caller rather than capturing nothing quietly
            self.stop()
            raise problem[0]

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(3)

    def _run(self, on_frame, opened: threading.Event, problem: list) -> None:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED: this thread only
        first = True
        try:
            while not self._stop.is_set():
                try:
                    stream = self._opener()
                except Exception as e:
                    if first:
                        problem.append(e)
                        opened.set()
                        return
                    log.warning("Live captions: couldn't open the %s: %s", self.what, e)
                    self._stop.wait(1.0)
                    continue
                if first:
                    first = False
                    opened.set()
                self.device_rate = stream.rate
                log.info("Live captions hear the %s (%d Hz, %d channels)", self.what, stream.rate, stream.channels)
                try:
                    self._pump(stream, on_frame)
                except OSError as e:  # unplugged, or the format changed: open whatever is the default now
                    log.info("Live captions: the %s changed (%s)", self.what, e)
                finally:
                    stream.close()
        finally:
            ole32.CoUninitialize()

    def _pump(self, stream, on_frame) -> None:
        resample, frame = Resampler(stream.rate), Framer()
        last_packet = last_fill = last_check = self._clock()
        while not self._stop.is_set():
            now = self._clock()
            packets = stream.read()
            if packets:
                last_packet = last_fill = now
                for samples in frame(resample(np.concatenate(packets))):
                    on_frame(samples)
            elif now - last_packet >= SILENCE_AFTER and now - last_fill >= FRAME_MS / 1000:
                last_fill = now  # nothing plays: Windows sends nothing, so silence keeps the clock going
                on_frame(bytes(FRAME * 2))
            if now - last_check >= CHECK_DEVICE_EVERY:
                last_check = now
                if stream.default_changed():
                    log.info("Live captions: a new default %s", self.what)
                    return
            self._stop.wait(0.01)

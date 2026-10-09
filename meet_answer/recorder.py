"""The meeting audio: what the laptop plays (sst.live.wasapi's Capture.speakers(): 16 kHz mono 16-bit frames of 100 ms,
Teams, Zoom or a browser alike, the call's Bluetooth headset included; meet_answer's own sounds left out).

With a look-back the capture stays open while meet_answer runs, and the last `lookback` seconds are kept in a ring in
memory, never written anywhere: a press that comes late still has the start of the question, and two quick presses
answer what was just said. Without one, the capture opens at the press and closes at the stop.
"""
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from sst.live.contracts import FRAME_MS, RATE
from sst.live.wasapi import Capture

log = logging.getLogger(__name__)

FRAMES_PER_SECOND = 1000 // FRAME_MS
SOUND_RMS = 100  # a frame above this (about -50 dBFS of 16-bit) has sound; silence filled in by the capture is all zeros
EDGE_KEEP = 3  # frames of quiet kept around the sound when the silent ends are cut off (300 ms)


@dataclass(frozen=True)
class Recording:
    audio: np.ndarray  # float32, -1..1, at RATE: the sound with its silent ends cut off
    seconds: float  # how long the user recorded (the look-back not counted)
    lookback: float  # seconds of look-back in front of it
    heard: bool  # any sound at all: without it there's nothing to transcribe

    @property
    def total(self) -> float:
        return len(self.audio) / RATE


def loud(frame: bytes) -> bool:
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return bool(samples.size) and float(np.sqrt(np.mean(samples * samples))) > SOUND_RMS


def to_audio(frames: list[bytes]) -> np.ndarray:
    """The frames without their silent ends (a few quiet frames kept around the sound), as float32."""
    flags = [loud(f) for f in frames]
    if not any(flags):
        return np.zeros(0, dtype=np.float32)
    first = max(flags.index(True) - EDGE_KEEP, 0)
    last = min(len(flags) - flags[::-1].index(True) + EDGE_KEEP, len(frames))
    return np.frombuffer(b"".join(frames[first:last]), dtype=np.int16).astype(np.float32) / 32768.0


class Recorder:
    """open() at the start (with a look-back), then start() and stop() for each question; close() at the end. Frames
    arrive on the capture's thread, the calls on the app's: a lock keeps the two apart. `on_full()` is called once (from
    the capture's thread) when a recording reaches `max_seconds`; `on_note(text)` passes the capture's hints on (the
    sound may be on another device, Windows' sound is muted; "" once they no longer apply)."""

    def __init__(self, lookback: float = 15.0, max_seconds: float = 180.0,
                 capture_factory: Callable[[], Capture] = Capture.speakers, clock: Callable[[], float] = time.monotonic):
        self.lookback, self.max_seconds = lookback, max_seconds
        self._factory, self._clock = capture_factory, clock
        self._ring: deque[bytes] = deque(maxlen=max(int(lookback * FRAMES_PER_SECOND), 1))
        self._frames: list[bytes] = []
        self._capture: Capture | None = None
        self._lock = threading.Lock()  # the frames: the capture's thread and the app's
        self._open_lock = threading.Lock()  # one capture, though the look-back opens it on another thread at the start
        self._started = 0.0
        self._ahead = 0  # look-back frames at the front of the recording
        self._heard = False
        self._full = False
        self.recording = False
        self.on_full: Callable[[], None] | None = None
        self.on_note: Callable[[str], None] | None = None

    # ---- the app's side

    def open(self) -> None:
        """Start listening for the look-back (nothing to do without one). Raises OSError when nothing can be captured."""
        if self.lookback > 0:
            self._ensure_open()

    def start(self) -> None:
        """Begin a recording, with the look-back in front of it. Raises OSError when nothing can be captured."""
        self._ensure_open()
        with self._lock:
            ring = list(self._ring) if self.lookback > 0 else []
            self._frames, self._ahead = ring, len(ring)
            self._heard = any(loud(f) for f in ring)
            self._full = False
            self._started = self._clock()
            self.recording = True

    def stop(self) -> Recording:
        with self._lock:
            frames, ahead, heard, seconds = self._frames, self._ahead, self._heard, self._clock() - self._started
            self._frames, self.recording = [], False
        if self.lookback <= 0:
            self._close()
        return Recording(to_audio(frames) if heard else np.zeros(0, dtype=np.float32), seconds,
                         ahead / FRAMES_PER_SECOND, heard)

    def cancel(self) -> None:
        with self._lock:
            self._frames, self.recording = [], False
        if self.lookback <= 0:
            self._close()

    def close(self) -> None:
        with self._lock:
            self._frames, self.recording = [], False
            self._ring.clear()
        self._close()

    @property
    def seconds(self) -> float:
        return self._clock() - self._started if self.recording else 0.0

    @property
    def heard(self) -> bool:
        """Sound since the recording began (or in its look-back)."""
        return self._heard

    @property
    def listening(self) -> bool:
        return self._capture is not None

    # ---- the capture's side

    def _ensure_open(self) -> None:
        with self._open_lock:
            if self._capture is None:
                self._open()

    def _open(self) -> None:
        capture = self._factory()
        capture.on_note = self._note
        capture.on_problem = self._note
        capture.start(self._on_frame)  # raises when the device can't be opened
        self._capture = capture
        log.info("Listening to the %s", capture.what)

    def _close(self) -> None:
        with self._open_lock:
            capture, self._capture = self._capture, None
        if capture is not None:
            capture.stop()

    def _on_frame(self, frame: bytes) -> None:
        full = False
        with self._lock:
            if self.lookback > 0:
                self._ring.append(frame)
            if self.recording:
                self._frames.append(frame)
                if not self._heard and loud(frame):
                    self._heard = True
                if not self._full and len(self._frames) - self._ahead >= self.max_seconds * FRAMES_PER_SECOND:
                    self._full = full = True
        if full and self.on_full:
            self.on_full()

    def _note(self, text: str) -> None:
        if self.on_note:
            self.on_note(text)

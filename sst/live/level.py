"""The computer's sound brought up to a level the live model hears well, however low it was turned.

Process loopback hears each app at the volume it plays at (a video player's slider, the volume mixer) and, on PCs whose
driver applies Windows' volume first, after the system volume too: turned down, speech arrives 30-60 dB quieter.
Gemini Live Translate itself translates clean speech down to about -70 dBFS (measured 2026-10-07: all of it at -70,
nothing at -80), so it's the 16-bit frames that lose it. The capture is 32-bit float, and this raises it before the
frames are made:

    the loudest 20 ms of the last WINDOW seconds heard -> quieter than TARGET: raised to it (by MAX_GAIN at most)

Sound at a normal volume is left exactly as it is. The gain drops at once when something loud comes (no burst), rises
by RISE dB a second at most, and a soft limit above LIMIT keeps the first loud moment after a quiet one from clipping.
Pure numpy: tested without Windows.
"""
from collections import deque

import numpy as np

from sst.live.contracts import RATE

BLOCK_MS = 20  # what's measured at a time (a shorter piece of sound, as it comes, on its own)
WINDOW = 3.0  # seconds of what was heard that the level is judged by: a pause between sentences doesn't raise it
TARGET = -20.0  # dBFS: the loudest 20 ms of speech at a normal volume is about this loud, or louder
MAX_GAIN = 60.0  # dB: down to -80 dBFS; below, it's the content's own noise
RISE = 20.0  # dB a second
LIMIT = 0.9  # above this the peaks are rounded off, never cut


def soft_limit(x: np.ndarray, limit: float = LIMIT) -> np.ndarray:
    """Samples above `limit` bent smoothly towards 1.0 (tanh), the rest untouched."""
    over = np.abs(x) > limit
    if not over.any():
        return x
    y = x.copy()
    room = 1.0 - limit
    y[over] = np.sign(x[over]) * (limit + room * np.tanh((np.abs(x[over]) - limit) / room))
    return y


class Leveler:
    """Float samples at `rate` in, the same samples raised where they're quiet out; state kept across calls."""

    def __init__(self, rate: int = RATE):
        self.rate = rate
        self.block = rate * BLOCK_MS // 1000
        self._loudest: deque[float] = deque(maxlen=round(WINDOW * 1000 / BLOCK_MS))
        self.gain_db = 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        blocks = [self._block(x[start:start + self.block]) for start in range(0, len(x), self.block)]
        return np.concatenate(blocks) if blocks else x

    def _block(self, x: np.ndarray) -> np.ndarray:
        rms = float(np.sqrt(np.mean(x * x, dtype=np.float64)))
        self._loudest.append(20 * np.log10(rms) if rms > 0 else -np.inf)
        want = min(max(TARGET - max(self._loudest), 0.0), MAX_GAIN)
        old = self.gain_db
        if want <= old:  # louder sound: down at once, for this whole block
            self.gain_db = want
            return soft_limit(x * np.float32(10 ** (want / 20))) if want else x
        self.gain_db = min(want, old + RISE * len(x) / self.rate)
        gains = 10 ** (np.linspace(old, self.gain_db, len(x), dtype=np.float32) / 20)  # up smoothly, no click
        return soft_limit(x * gains)

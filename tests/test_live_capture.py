"""What the laptop plays, as live captions get it: the device's format to 16 kHz mono frames, silence filled in, a new
output device followed. Windows is faked; no sound is played or captured."""
import threading
import time

import numpy as np
import pytest

from sst.live import wasapi
from sst.live.wasapi import FRAME, Capture, Framer, Resampler, decode


def sine(freq, seconds, rate):
    return np.sin(2 * np.pi * freq * np.arange(int(seconds * rate)) / rate).astype(np.float32)


def pitch(x, rate):
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[spectrum.argmax()]


@pytest.mark.parametrize("rate", [48_000, 44_100, 32_000, 16_000])
def test_any_device_rate_becomes_16_khz_without_seams(rate):
    audio = sine(440, 2.0, rate)
    resample, out, i = Resampler(rate), [], 0
    rng = np.random.default_rng(0)
    while i < len(audio):  # packets of uneven sizes, as WASAPI hands them over
        n = int(rng.integers(100, 2000))
        out.append(resample(audio[i:i + n]))
        i += n
    y = np.concatenate(out)
    assert abs(len(y) - 32_000) <= 2  # 2 s at 16 kHz
    assert abs(pitch(y, 16_000) - 440) < 2
    assert np.max(np.abs(np.diff(y))) < 0.2  # no jumps where packets meet (a 440 Hz sine moves ~0.17 a sample)


def test_formats_decode_to_mono():
    stereo = np.array([[0.5, -0.5], [0.25, 0.75]], dtype="<f4")
    assert np.allclose(decode(stereo.tobytes(), 2, 32, True), [0.0, 0.5])
    pcm16 = np.array([16384, -16384], dtype="<i2")
    assert np.allclose(decode(pcm16.tobytes(), 1, 16, False), [0.5, -0.5])
    pcm24 = bytes([0x00, 0x00, 0x40, 0x00, 0x00, 0xC0])  # +0.5 and -0.5 as 24-bit little-endian
    assert np.allclose(decode(pcm24, 1, 24, False), [0.5, -0.5])
    with pytest.raises(ValueError):
        decode(b"\0" * 8, 1, 8, False)


def test_frames_are_100_ms_of_pcm16():
    framer = Framer()
    assert framer(np.zeros(1000, np.float32)) == []
    frames = framer(np.full(2500, 0.5, np.float32))  # 3,500 samples so far: two frames, 300 left
    assert len(frames) == 2 and all(len(f) == FRAME * 2 for f in frames)
    assert np.frombuffer(frames[1], "<i2")[-1] == 16383
    assert len(framer(np.zeros(1300, np.float32))) == 1  # 300 + 1,300 = one more frame


class FakeStream:
    """An opened output device: hands out the packets queued in it, then nothing (silence)."""
    rate, channels = 48_000, 2

    def __init__(self, packets=(), changed_after=None):
        self.packets, self.changed_after, self.reads, self.closed = list(packets), changed_after, 0, False

    def read(self):
        self.reads += 1
        out, self.packets = self.packets[:1], self.packets[1:]
        return out

    def default_changed(self):
        return self.changed_after is not None and self.reads >= self.changed_after

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.01  # each look at the clock is 10 ms later: the capture loop runs every 10 ms
        return self.now


def run_capture(streams, seconds, until=None):
    frames, opened = [], list(streams)
    capture = Capture(opener=lambda: opened.pop(0), clock=Clock())
    capture._stop.wait = lambda s: time.sleep(0.0005)  # the loop's 10 ms pauses, much faster
    capture.start(frames.append)
    end = time.monotonic() + (5.0 if until else seconds)
    while time.monotonic() < end and not (until and until()):
        time.sleep(0.01)
    capture.stop()
    return frames, capture


def test_sound_comes_through_as_frames():
    loud = [sine(440, 0.1, 48_000)] * 10  # 1 s of sound, in 100 ms packets
    frames, capture = run_capture([FakeStream(loud)], 0.3)
    audio = np.concatenate([np.frombuffer(f, "<i2") for f in frames]).astype(np.float32)
    assert capture.device_rate == 48_000 and len(frames) >= 9
    assert abs(pitch(audio[:16_000], 16_000) - 440) < 3


def test_silence_keeps_the_clock_going_when_nothing_plays():
    frames, _ = run_capture([FakeStream()], 0.3)
    assert len(frames) > 5 and all(f == bytes(FRAME * 2) for f in frames)  # a frame of silence every 100 ms


def test_a_new_default_output_is_followed():
    first, second = FakeStream(changed_after=50), FakeStream()  # headphones become the default meanwhile
    frames, _ = run_capture([first, second], 0, until=lambda: second.reads > 0)
    assert first.closed and second.reads > 0  # noticed at the next look (every 2 s), and the new one opened


def test_an_output_that_cant_be_opened_is_reported_to_the_caller():
    def broken():
        raise OSError("no output device")
    capture = Capture(opener=broken)
    with pytest.raises(OSError):
        capture.start(lambda frame: None)
    assert not any(t.name == "live-output" and t.is_alive() for t in threading.enumerate())


def test_the_speakers_are_heard_in_loopback_and_the_microphone_for_calls(monkeypatch):
    opened = []
    monkeypatch.setattr(wasapi, "_Stream", lambda flow, role: opened.append((flow, role)))
    Capture.speakers()._opener()
    Capture.microphone()._opener()
    assert opened == [(wasapi.E_RENDER, wasapi.E_CONSOLE), (wasapi.E_CAPTURE, wasapi.E_COMMUNICATIONS)]
    assert (Capture.speakers().what, Capture.microphone().what) == ("output device", "microphone")


def test_the_real_mix_format_parts_are_the_documented_sizes():
    assert wasapi.ctypes.sizeof(wasapi._WAVEFORMATEX) == 18 and wasapi.ctypes.sizeof(wasapi._WAVEFORMATEXTENSIBLE) == 40

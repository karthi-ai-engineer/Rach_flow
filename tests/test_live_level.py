"""The computer's sound brought up to a level the live model hears (sst.live.level), and the capture that uses it."""
import numpy as np
import pytest

from sst.live import level
from sst.live.level import LIMIT, MAX_GAIN, MIC_MAX_GAIN, MIC_NOISE_CEILING, RISE, TARGET, Leveler, soft_limit

RATE = 16_000


def speech(seconds, db, seed=0):
    """Speech-like sound: noise in syllables (on 0.15 s, off 0.1 s) whose loudest 20 ms is about `db` dBFS."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(seconds * RATE)).astype(np.float32)
    on = (np.arange(len(x)) % int(0.25 * RATE)) < int(0.15 * RATE)
    x *= on
    return x * np.float32(10 ** (db / 20))  # standard normal: RMS 1.0 while on


def loudest_db(x, seconds=None):
    x = x[-int(seconds * RATE):] if seconds else x
    blocks = x[: len(x) // 320 * 320].reshape(-1, 320)
    return 20 * np.log10(np.sqrt((blocks.astype(np.float64) ** 2).mean(axis=1)).max())


def run(leveler, x, chunk=160):
    """In WASAPI-sized pieces (10 ms), as the capture hands them over."""
    return np.concatenate([leveler(x[i:i + chunk]) for i in range(0, len(x), chunk)])


def test_sound_at_a_normal_volume_is_left_exactly_as_it_is():
    x = speech(5, -12)
    assert np.array_equal(run(Leveler(), x), x)


@pytest.mark.parametrize("db", [-35, -50, -70])
def test_quiet_sound_is_raised_to_a_normal_level(db):
    out = run(Leveler(), speech(8, db))
    assert loudest_db(out, 2) == pytest.approx(TARGET, abs=1.0)


def test_it_rises_gently_and_drops_at_once_when_loud_sound_comes():
    leveler, x = Leveler(), speech(6, -60)
    quiet = run(leveler, x)
    assert leveler.gain_db == pytest.approx(TARGET - loudest_db(x, 3), abs=1.0)  # about +40 dB
    normal = speech(1, -12, seed=1)  # the volume turned up again
    assert np.array_equal(run(leveler, normal), normal) and leveler.gain_db == 0  # down at once: no burst
    gains = []
    leveler = Leveler()
    for i in range(0, 3 * RATE, 160):  # the loud part leaves the window after 3 s: from then on it may rise
        leveler(speech(0.01, -60, seed=i))
        gains.append(leveler.gain_db)
    assert max(np.diff(gains)) <= RISE * 0.01 + 1e-6
    assert len(quiet) == 6 * RATE


def test_silence_stays_silent_and_the_gain_has_a_limit():
    leveler = Leveler()
    out = run(leveler, np.zeros(5 * RATE, dtype=np.float32))
    assert not out.any() and leveler.gain_db == MAX_GAIN


def test_a_soft_limit_rounds_off_only_what_is_above_it():
    x = np.linspace(-3, 3, 601, dtype=np.float32)
    y = soft_limit(x)
    assert np.all(np.abs(y) <= 1.0) and np.all(np.diff(y) >= 0)  # never past full scale, and still in order
    assert np.all(np.diff(soft_limit(np.linspace(0.8, 1.2, 41, dtype=np.float32))) > 0)  # bent, not flattened
    inside = np.abs(x) <= LIMIT
    assert np.array_equal(y[inside], x[inside])


def noise(seconds, db, seed=2):
    return np.random.default_rng(seed).standard_normal(int(seconds * RATE)).astype(np.float32) * np.float32(10 ** (db / 20))


def rms_db(x):
    return 10 * np.log10(np.mean(np.asarray(x, np.float64) ** 2))


def test_a_microphones_quiet_room_isnt_raised_to_speech_level():
    room = noise(12, -62)  # a laptop's fan, nobody speaking
    assert rms_db(run(Leveler(), room)[-3 * RATE:]) > -25  # the computer's leveler would make it speech-loud
    mic = Leveler.for_microphone()
    out = run(mic, room)
    assert mic.noise_db() == pytest.approx(-62 - 1.3, abs=1.0)  # the quietest tenth of its 20 ms blocks
    assert rms_db(out[-3 * RATE:]) == pytest.approx(MIC_NOISE_CEILING, abs=1.5)  # raised, but kept 25 dB under speech


def test_a_quiet_voice_over_a_quiet_room_is_raised_by_the_microphones_leveler():
    voice = speech(10, -42) + noise(10, -80)
    out = run(Leveler.for_microphone(), voice)
    assert loudest_db(out, 2) == pytest.approx(TARGET, abs=1.0)
    far = speech(10, -65)  # someone across the room in digital silence: raised by MIC_MAX_GAIN at most
    mic = Leveler.for_microphone()
    run(mic, far)
    assert mic.gain_db == pytest.approx(MIC_MAX_GAIN)


def test_the_window_is_a_few_seconds_of_what_was_heard():
    assert level.WINDOW >= 2.0  # longer than a pause between sentences: a pause mustn't raise the room's hum

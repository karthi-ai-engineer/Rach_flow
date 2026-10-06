"""Live translation spoken aloud: the voice's download and preparation, the Speaker (sentences, catching up, muting),
and the Player. No sound is played, nothing is downloaded: the voice, the player and Windows are faked."""
import json
import threading
import time

import numpy as np
import pytest

from sst import downloads
from sst.live import voice as voices
from sst.live.contracts import MIC, SYSTEM, Kind, LiveConfig, LiveEvent
from sst.live.speaker import Speaker, whole_sentences
from sst.live.voice import PREPARED, TOKENS, Voice, onnx_metadata, prepare
from sst.live.wasapi import Player

# ---- the voice: downloaded, then prepared for sherpa-onnx

def read_varint(data: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[i]
        value |= (byte & 0x7F) << shift
        i, shift = i + 1, shift + 7
        if not byte & 0x80:
            return value, i


def metadata_props(data: bytes) -> dict[str, str]:
    """ModelProto's metadata_props (field 14) read back from protobuf bytes."""
    found, i = {}, 0
    while i < len(data):
        tag, i = read_varint(data, i)
        length, i = read_varint(data, i)
        payload, i = data[i:i + length], i + length
        if tag == (14 << 3 | 2):
            entry, j = {}, 0
            while j < len(payload):
                inner, j = read_varint(payload, j)
                size, j = read_varint(payload, j)
                entry[inner >> 3], j = payload[j:j + size].decode(), j + size
            found[entry[1]] = entry[2]
    return found


def test_the_metadata_is_protobuf_that_merges_into_the_model():
    data = onnx_metadata({"model_type": "vits", "sample_rate": 16000, "comment": "piper"})
    assert metadata_props(data) == {"model_type": "vits", "sample_rate": "16000", "comment": "piper"}
    long = onnx_metadata({"k": "v" * 300})  # lengths over 127 take two bytes
    assert metadata_props(long) == {"k": "v" * 300}


VOICE = Voice("test", "Test", "en", downloads.Download("voice", "https://huggingface.co/v/", (
    downloads.ModelFile("test.onnx", 4, "x"), downloads.ModelFile("test.onnx.json", 1, "x"))), "test.onnx",
    downloads.Download("data", "https://huggingface.co/d/", (downloads.ModelFile("phontab", 1, "x"),)))
CONFIG = {"language": {"name_english": "English"}, "espeak": {"voice": "en-us"}, "num_speakers": 1,
          "audio": {"sample_rate": 16000}, "phoneme_id_map": {"_": [0], "^": [1], " ": [3], "a": [14]},
          "inference": {"noise_scale": 0.667, "length_scale": 1, "noise_w": 0.8}}


def downloaded(root):
    folder = VOICE.folder(root)
    folder.mkdir(parents=True)
    model = b"\x08\x07" + b"MODEL"  # any protobuf: ir_version = 7, then the weights
    (folder / "test.onnx").write_bytes(model)
    (folder / "test.onnx.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    return folder, model


def test_preparing_appends_the_settings_writes_the_tokens_and_drops_the_original(tmp_path):
    folder, model = downloaded(tmp_path)
    prepare(VOICE, tmp_path)
    prepared = (folder / PREPARED).read_bytes()
    assert prepared.startswith(model) and metadata_props(prepared[len(model):]) == {
        "model_type": "vits", "comment": "piper", "language": "English", "voice": "en-us", "has_espeak": "1",
        "n_speakers": "1", "sample_rate": "16000"}
    assert (folder / TOKENS).read_text(encoding="utf-8") == "_ 0\n^ 1\n  3\na 14\n"
    assert not (folder / "test.onnx").exists()  # not kept twice
    assert json.loads((folder / voices.MARKER).read_text(encoding="utf-8"))["from"] == "test.onnx"


def test_installing_downloads_what_is_missing_then_prepares_once(tmp_path, monkeypatch):
    fetched = []

    def fake_download(model, progress, cancelled, root):
        fetched.append(model.folder)
        if model is VOICE.model and not (VOICE.folder(root) / "test.onnx").exists():
            downloaded(root)
        (model.path(root)).mkdir(parents=True, exist_ok=True)
        for f in model.files:
            if not (model.path(root) / f.name).exists():
                (model.path(root) / f.name).write_bytes(b"x" * f.size)
        (model.path(root) / downloads.MARKER).write_text("{}", encoding="utf-8")
        progress(model.size, model.size)
        return model.path(root)
    monkeypatch.setattr(voices, "download", fake_download)
    seen = []
    assert not VOICE.ready(tmp_path)
    voices.install(VOICE, lambda done, total: seen.append((done, total)), root=tmp_path)
    assert fetched == ["data", "voice"] and VOICE.ready(tmp_path) and seen[-1] == (VOICE.size, VOICE.size)
    voices.install(VOICE, root=tmp_path)
    assert fetched == ["data", "voice"]  # ready: nothing fetched again


def test_the_real_voice_is_pinned_on_hugging_face():
    for download in (voices.DANNY.model, voices.DANNY.data):
        assert downloads.allowed(download.base_url) and "/resolve/" in download.base_url
        assert all(len(f.sha256) == 64 for f in download.files)
    assert voices.DANNY.language == "en" and 60 * 2**20 < voices.DANNY.size < 70 * 2**20


# ---- whole sentences

@pytest.mark.parametrize(("text", "sentences"), [
    ("Hello there. How are", ["Hello there."]),
    ("It costs 3.", []),  # "3.5" may follow: not yet
    ("It costs 3.5 million. Next", ["It costs 3.5 million."]),
    ("Use e.g. this one. And", ["Use e.g. this one."]),
    ("Mr. Smith left. Then", ["Mr. Smith left."]),
    ("Wait... really? Yes! Ok", ["Wait...", "really?", "Yes!"]),
    ("He said “yes.” Then", ["He said “yes.”"]),
])
def test_a_sentence_is_whole_once_the_text_goes_on_past_it(text, sentences):
    assert whole_sentences(text)[0] == sentences


# ---- the speaker

class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeVoice:
    rate = 16000

    def __init__(self, log):
        self.log = log

    def synthesize(self, text, speed=1.0):
        self.log.append((text, round(speed, 2)))
        return np.zeros(int(0.5 * self.rate), dtype=np.float32)  # half a second of speech


class FakePlayer:
    def __init__(self, private=False, gate=None):
        self.private, self.played, self.interrupted, self.closed, self.gate = private, [], 0, False, gate

    def play(self, samples, rate):
        if self.gate is not None:
            self.gate.wait(5)
        self.played.append(len(samples) / rate)
        return 0.0

    def interrupt(self):
        self.interrupted += 1

    def close(self):
        self.closed = True


def speaker_with(lanes=(SYSTEM,), player=None, clock=None, **named):
    said = []
    speaker = Speaker(lambda: FakeVoice(said), player or FakePlayer(), lanes, clock=clock or Clock(), **named)
    return speaker, said


def wait_until(condition, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not condition():
        time.sleep(0.01)
    return condition()


def translation(text, lane=SYSTEM):
    return LiveEvent(Kind.TRANSLATION, text, lane=lane)


def test_each_sentence_is_said_once_as_soon_as_it_is_whole_and_the_rest_when_the_line_ends():
    speaker, said = speaker_with()
    speaker.start()
    for text in ("Thanks for", "Thanks for joining.", "Thanks for joining. The budget", "Thanks for joining. The budget is"):
        speaker.hear(translation(text))
    assert wait_until(lambda: said == [("Thanks for joining.", 1.0)])
    speaker.hear(LiveEvent(Kind.LINE, "Thanks for joining. The budget is ready.", source="…"))
    assert wait_until(lambda: [text for text, _ in said] == ["Thanks for joining.", "The budget is ready."])
    speaker.stop()
    assert speaker.player.closed


def test_a_last_sentence_is_said_after_a_short_quiet_without_waiting_for_the_line_to_end():
    clock = Clock()
    speaker, said = speaker_with(clock=clock)
    speaker.start()
    speaker.hear(translation("We'll decide next week."))
    time.sleep(0.3)
    assert said == []  # it may still go on ("week.5"? no; but "week. And" could come)
    clock.now += 0.7
    assert wait_until(lambda: said == [("We'll decide next week.", 1.0)])
    speaker.stop()


def test_falling_behind_it_speaks_faster_and_skips_what_is_stale():
    gate, clock = threading.Event(), Clock()
    speaker, said = speaker_with(player=FakePlayer(gate=gate), clock=clock)
    speaker.start()
    speaker.hear(translation("One. Two. Three. Four. Five. And"))
    assert wait_until(lambda: len(said) == 1)  # "One." is playing; four wait behind it
    clock.now += 11  # they've waited too long: the old ones go, the newest is said
    gate.set()
    assert wait_until(lambda: [text for text, _ in said] == ["One.", "Five."])
    assert said[0][1] == 1.6 and speaker.skipped == 3  # "One." was said faster: four were waiting
    speaker.stop()


def test_only_the_ways_it_speaks_are_said_and_untranslated_lines_are_not():
    speaker, said = speaker_with(lanes=(SYSTEM,))
    speaker.start()
    speaker.hear(LiveEvent(Kind.LINE, "はい、そうしましょう。", source="Yes, let's.", lane=MIC))  # the user's own: not said
    speaker.hear(LiveEvent(Kind.LINE, "", source="Let's start."))  # already in English: heard as it is
    speaker.hear(LiveEvent(Kind.LINE, "Good morning."))
    assert wait_until(lambda: [text for text, _ in said] == ["Good morning."])
    speaker.set_lanes([])
    speaker.hear(LiveEvent(Kind.LINE, "Not said."))
    time.sleep(0.2)
    assert [text for text, _ in said] == ["Good morning."]
    speaker.stop()


def test_muting_drops_what_waits_and_stops_what_plays():
    gate = threading.Event()
    speaker, said = speaker_with(player=FakePlayer(gate=gate))
    speaker.start()
    speaker.hear(translation("One. Two. Three"))
    assert wait_until(lambda: len(said) == 1)
    speaker.set_muted(True)
    gate.set()
    speaker.hear(translation("One. Two. Three. Four. Five"))
    time.sleep(0.2)
    assert [text for text, _ in said] == ["One."] and speaker.player.interrupted
    speaker.set_muted(False)
    speaker.hear(LiveEvent(Kind.LINE, "One. Two. Three. Four. Five. Six."))  # what was whole while muted stays unsaid
    assert wait_until(lambda: [text for text, _ in said] == ["One.", "Five. Six."])  # "Five" ended after unmuting
    speaker.stop()


def test_a_line_that_began_with_a_space_is_not_said_again_when_it_ends():
    speaker, said = speaker_with()  # the owner's report: at every pause in the video, the last line again
    speaker.start()
    speaker.hear(translation(" Tomorrow, I'm going"))  # Google's first piece of a line, with its space
    speaker.hear(translation(" Tomorrow, I'm going to see a movie with a friend. I'm looking"))
    speaker.hear(translation(" Tomorrow, I'm going to see a movie with a friend. I'm looking forward to it."))
    speaker.hear(LiveEvent(Kind.LINE, "Tomorrow, I'm going to see a movie with a friend. I'm looking forward to it."))
    assert wait_until(lambda: len(said) >= 2)
    time.sleep(0.3)  # anything said again would come now
    assert [text for text, _ in said] == ["Tomorrow, I'm going to see a movie with a friend.", "I'm looking forward to it."]
    speaker.stop()


def test_a_rewritten_line_is_not_said_twice():
    speaker, said = speaker_with()
    speaker.start()
    speaker.hear(translation("Hello there. How are"))
    assert wait_until(lambda: len(said) == 1)
    speaker.hear(translation("Hello there. How is it going? Fine"))  # the model rewrote the second sentence
    assert wait_until(lambda: [text for text, _ in said] == ["Hello there.", "How is it going?"])
    speaker.stop()


def test_speaking_lasts_while_the_voice_can_be_heard():
    clock = Clock()
    speaker, said = speaker_with(clock=clock)
    assert not speaker.speaking
    speaker.start()
    speaker.hear(LiveEvent(Kind.LINE, "Hello."))
    assert wait_until(lambda: said)
    time.sleep(0.05)
    assert speaker.speaking  # just played, and a microphone may still hear the room for a moment
    clock.now += 1
    assert not speaker.speaking
    speaker.stop()


def test_the_other_apps_are_lowered_while_it_speaks_and_put_back_when_it_stops():
    class FakeDucker:
        def __init__(self):
            self.watching, self.stopped = None, False

        def start(self, speaking):
            self.watching = speaking

        def stop(self):
            self.stopped = True
    clock, ducker = Clock(), FakeDucker()
    speaker, said = speaker_with(clock=clock, ducker=ducker)
    speaker.start()
    assert not ducker.watching()  # it watches the voice: quiet now
    speaker.hear(LiveEvent(Kind.LINE, "Hello."))
    assert wait_until(lambda: said) and wait_until(ducker.watching)
    clock.now += 1
    assert not ducker.watching()
    speaker.stop()
    assert ducker.stopped


def test_a_voice_that_cant_load_says_so_once():
    problems = []

    def broken():
        raise RuntimeError("model.onnx is damaged")
    speaker = Speaker(broken, FakePlayer(), [SYSTEM], on_problem=problems.append)
    speaker.start()
    assert wait_until(lambda: problems == ["The voice couldn't start: model.onnx is damaged"])
    speaker.stop()


def test_which_ways_a_voice_speaks():
    assert LiveConfig(target="en").spoken_lanes("en") == (SYSTEM,)
    assert LiveConfig(target="ja").spoken_lanes("en") == ()
    assert LiveConfig(source="microphone", mic_target="en").spoken_lanes("en") == (MIC,)
    both = LiveConfig(source="both", target="en", mic_target="en")
    assert both.spoken_lanes("en") == (SYSTEM,)  # with both, the user's own words are for the others


# ---- the player

class FakeRender:
    """An opened output: takes up to `room` frames per write."""

    def __init__(self, rate, room=4000, private=False, changed=False, fail_writes=0):
        self.rate, self.room, self.private, self.changed, self.fail_writes = rate, room, private, changed, fail_writes
        self.written, self.flushed, self.closed = 0, False, False

    def write(self, pcm):
        if self.fail_writes:
            self.fail_writes -= 1
            raise OSError("device invalidated")
        n = min(self.room, len(pcm) // 2)
        self.written += n
        return n

    def queued(self):
        return 1600

    def flush(self):
        self.flushed = True

    def default_changed(self):
        return self.changed

    def close(self):
        self.closed = True


def test_the_player_writes_everything_and_says_what_is_still_to_be_heard():
    opened = []
    player = Player(opener=lambda rate: opened.append(FakeRender(rate)) or opened[-1])
    assert player.play(np.zeros(16000, np.float32), 16000) == pytest.approx(0.1)
    assert opened[0].written == 16000 and not player.private


def test_the_player_follows_a_new_default_output_and_a_lost_one():
    clock, opened = Clock(), []
    renders = iter([FakeRender(16000, changed=True), FakeRender(16000, private=True, fail_writes=1),
                    FakeRender(16000, private=True)])
    player = Player(opener=lambda rate: opened.append(next(renders)) or opened[-1], clock=clock)
    player.play(np.zeros(100, np.float32), 16000)
    clock.now += 3  # time for a look at the default: headphones now
    player.play(np.zeros(100, np.float32), 16000)
    assert opened[0].closed and opened[1].closed and opened[2].written == 100 and player.private


def test_interrupting_the_player_flushes_what_was_queued():
    player = Player(opener=lambda rate: FakeRender(rate, room=0))  # a full buffer: play waits
    done = []
    thread = threading.Thread(target=lambda: done.append(player.play(np.zeros(16000, np.float32), 16000)))
    thread.start()
    time.sleep(0.05)
    player.interrupt()
    thread.join(2)
    assert done == [0.0] and player._stream.flushed

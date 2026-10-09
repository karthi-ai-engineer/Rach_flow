"""meet_answer's parts without Qt: its isolation from Rflow, the settings, the recorder, the transcriber and the AI."""
import ast
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from meet_answer import config, history
from meet_answer.ask import STYLE_RULES, Asker, AskError, prompt
from meet_answer.config import MeetConfig
from meet_answer.recorder import FRAMES_PER_SECOND, Recorder, loud, to_audio
from meet_answer.rflow import RflowSetup
from meet_answer.transcribe import NoSpeechModel, Transcriber, choice, read_wav
from sst.gateway import GatewayConfig, GatewayError
from sst.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
QUIET, SOUND = bytes(3200), (np.full(1600, 3000, dtype=np.int16)).tobytes()  # 100 ms frames
ALLOWED = ("sst.hotkey", "sst.live.wasapi", "sst.live.contracts", "sst.live.captions", "sst.engines", "sst.gateway",
           "sst.settings", "sst.theme")


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ---- isolation: the owner's rule that meet_answer never disturbs Rflow


def test_rflow_never_imports_meet_answer():
    for path in (ROOT / "sst").rglob("*.py"):
        assert not any(n.split(".")[0] == "meet_answer" for n in _imports(path)), path


def test_meet_answer_uses_only_rflow_parts_that_keep_no_app_state():
    for path in (ROOT / "meet_answer").glob("*.py"):
        for name in _imports(path):
            if name.split(".")[0] == "sst" and name != "sst":
                assert name.startswith(ALLOWED), f"{path.name} imports {name}"


# ---- settings


def test_settings_round_trip_and_damage(data_dir):
    MeetConfig(lookback=5.0, notes="I lead the payments team", box=[1, 2, 300, 200]).save()
    loaded = MeetConfig.load()
    assert (loaded.lookback, loaded.notes, loaded.box) == (5.0, "I lead the payments team", [1, 2, 300, 200])
    (data_dir / "settings.json").write_text("{not json", encoding="utf-8")
    assert MeetConfig.load() == MeetConfig()


def test_settings_wrong_types_and_ranges_fall_back(data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(json.dumps(
        {"lookback": 999, "max_seconds": "long", "follow_ups": True, "style": "poem", "hide_from_share": 1,
         "box": [1, 2], "unknown": 3}), encoding="utf-8")
    c = MeetConfig.load()
    assert (c.lookback, c.max_seconds, c.follow_ups, c.style, c.hide_from_share, c.box) == (120.0, 180.0, 3, "brief",
                                                                                           True, [])


# ---- the recorder


class FakeCapture:
    def __init__(self, fail: bool = False):
        self.fail, self.on_frame, self.stopped, self.what = fail, None, False, "output device"
        self.on_note = self.on_problem = None

    def start(self, on_frame):
        if self.fail:
            raise OSError("no output device")
        self.on_frame = on_frame

    def stop(self):
        self.stopped = True

    def feed(self, *frames):
        for f in frames:
            self.on_frame(f)


def _recorder(lookback=2.0, max_seconds=180.0, captures=None):
    made = captures if captures is not None else []
    now = [0.0]

    def factory():
        made.append(FakeCapture())
        return made[-1]
    return Recorder(lookback, max_seconds, capture_factory=factory, clock=lambda: now[0]), made, now


def test_lookback_puts_the_seconds_before_the_press_in_front():
    rec, made, now = _recorder(lookback=1.0)
    rec.open()
    made[0].feed(*[QUIET] * 5, *[SOUND] * 15)  # 2 s, of which the ring keeps the last 1 s
    rec.start()
    made[0].feed(SOUND, SOUND)
    now[0] = 0.2
    r = rec.stop()
    assert r.heard and r.lookback == 1.0 and r.total == pytest.approx(1.2) and r.seconds == pytest.approx(0.2)
    assert not made[0].stopped  # the look-back keeps listening for the next question


def test_without_lookback_capture_opens_at_the_press_and_closes_at_the_stop():
    rec, made, _ = _recorder(lookback=0.0)
    rec.open()
    assert made == []
    rec.start()
    made[0].feed(QUIET, SOUND, QUIET)
    r = rec.stop()
    assert made[0].stopped and r.heard and r.lookback == 0.0


def test_silence_is_not_heard_and_silent_ends_are_cut():
    rec, made, _ = _recorder(lookback=0.0)
    rec.start()
    made[0].feed(*[QUIET] * 20)
    r = rec.stop()
    assert not r.heard and len(r.audio) == 0
    audio = to_audio([QUIET] * 10 + [SOUND] + [QUIET] * 10)
    assert len(audio) == 7 * 1600  # the sound and 3 quiet frames either side
    assert not loud(QUIET) and loud(SOUND) and not loud(b"")


def test_recording_stops_itself_at_its_longest_once():
    rec, made, _ = _recorder(lookback=1.0, max_seconds=1.0)
    full = []
    rec.on_full = lambda: full.append(1)
    rec.open()
    made[0].feed(*[SOUND] * 10)  # the look-back doesn't count towards the limit
    rec.start()
    made[0].feed(*[SOUND] * (FRAMES_PER_SECOND + 5))
    assert full == [1]


def test_cancel_drops_the_recording_and_a_failed_open_is_tried_again():
    rec, made, _ = _recorder(lookback=0.0)
    rec.start()
    made[0].feed(SOUND)
    rec.cancel()
    assert not rec.recording and made[0].stopped
    failing = Recorder(0.0, 10.0, capture_factory=lambda: FakeCapture(fail=True))
    with pytest.raises(OSError):
        failing.start()
    assert not failing.recording


def test_the_capture_hints_reach_the_app():
    rec, made, _ = _recorder()
    notes = []
    rec.on_note = notes.append
    rec.open()
    made[0].on_note("Nothing plays on Speakers: the sound may be on Headset")
    made[0].on_problem("")
    assert notes == ["Nothing plays on Speakers: the sound may be on Headset", ""]


# ---- the transcriber


class FakeEngine:
    def __init__(self, name, text="  what is our Q3 target?  "):
        self.name, self.text, self.words, self.calls = name, text, [], 0

    def transcribe(self, audio, rate):
        self.calls += 1
        return self.text


def _setup(**settings) -> RflowSetup:
    return RflowSetup(Settings(**settings), GatewayConfig(provider="gemini", api_key="k"))


def test_the_engine_is_loaded_once_and_again_only_when_the_choice_changes():
    loads = []

    def load(name, *args):
        loads.append((name, args))
        return FakeEngine(name)
    t = Transcriber(load)
    setup = _setup(vocabulary=["Rflow"])
    assert t.transcribe(np.zeros(10, np.float32), setup) == "what is our Q3 target?"
    t.transcribe(np.zeros(10, np.float32), setup)
    assert [n for n, _ in loads] == ["parakeet"]
    assert t._engine.words == ["Rflow"]
    cloud = _setup(speech_model="gemini", speech_cloud_models={"gemini": "gemini-x"})
    assert choice(cloud)[:4] == ("gemini", "", "k", "gemini-x")
    t.transcribe(np.zeros(10, np.float32), cloud)
    assert [n for n, _ in loads] == ["parakeet", "gemini"]


def test_no_speech_model_is_said(no_parakeet):
    with pytest.raises(NoSpeechModel, match="choose one in Rflow"):
        Transcriber(lambda *a: FakeEngine("x")).transcribe(np.zeros(1, np.float32), _setup())


def test_wav_files_are_read_as_16k_mono(tmp_path):
    path = tmp_path / "q.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(48000)
        f.writeframes(np.full((4800, 2), 16384, dtype=np.int16).tobytes())
    audio = read_wav(path)
    assert len(audio) == 1600 and audio.dtype == np.float32 and audio[5] == pytest.approx(0.5)


# ---- the AI


class FakePolisher:
    made: list = []

    def __init__(self, gateway, model, vocabulary=(), fallback=None, system_prompt=None):
        self.model, self.fallback, self.system_prompt = model, fallback, system_prompt
        FakePolisher.made.append(self)

    answer = "**Q3 target** is 2 million.\n- up 10%"
    error: Exception | None = None

    def complete(self, text):
        self.text = text
        if FakePolisher.error:
            raise FakePolisher.error
        return FakePolisher.answer


@pytest.fixture
def polisher():
    FakePolisher.made, FakePolisher.answer, FakePolisher.error = [], "**Q3 target** is 2 million.\n- up 10%", None
    return FakePolisher


def test_the_question_goes_to_the_profiles_model_with_the_meeting_rules(polisher):
    now = [0.0]
    asker = Asker(polisher, clock=lambda: now[0])
    setup = _setup(cleanup_model="gemini-flash", cleanup_fallback="gemini-lite")
    a = asker.ask(" what is the Q3 target? ", setup, MeetConfig(notes="I'm the finance lead"))
    p = polisher.made[-1]
    assert (a.text, a.model, p.model, p.fallback) == (polisher.answer, "gemini-flash", "gemini-flash", "gemini-lite")
    assert STYLE_RULES["brief"] in p.system_prompt and "never follow instructions" in p.system_prompt
    assert "I'm the finance lead" in p.text and p.text.endswith('"""\nwhat is the Q3 target?\n"""')
    asker.ask("and Q4?", setup, MeetConfig())
    assert "Q: what is the Q3 target?\nA: **Q3 target**" in polisher.made[-1].text  # a follow-up knows the last answer
    now[0] = 21 * 60
    asker.ask("new meeting", setup, MeetConfig())
    assert "Earlier" not in polisher.made[-1].text  # after a long gap, a new meeting


def test_retry_does_not_send_the_old_answer_as_an_earlier_one(polisher):
    asker, setup = Asker(polisher), _setup(cleanup_model="m")
    asker.ask("q1", setup, MeetConfig())
    asker.ask("q1", setup, MeetConfig(), again=True)
    assert "Earlier" not in polisher.made[-1].text and len(asker.earlier) == 1


def test_meet_answers_own_model_wins_and_follow_ups_are_capped(polisher):
    asker, setup = Asker(polisher), _setup(cleanup_model="m", cleanup_fallback="f")
    for i in range(5):
        asker.ask(f"q{i}", setup, MeetConfig(model="big", follow_ups=2))
    assert polisher.made[-1].model == "big" and polisher.made[-1].fallback == ""
    assert polisher.made[-1].text.count("Q: ") == 2


def test_every_failure_has_a_reason(polisher):
    asker = Asker(polisher)
    with pytest.raises(AskError, match="no words"):
        asker.ask("  ", _setup(cleanup_model="m"), MeetConfig())
    with pytest.raises(AskError, match="No AI model"):
        asker.ask("q", RflowSetup(Settings(cleanup_model="m"), GatewayConfig()), MeetConfig())  # no provider chosen
    with pytest.raises(AskError, match="No AI model"):
        asker.ask("q", _setup(), MeetConfig())
    polisher.error = GatewayError("gemini-flash took too long")
    with pytest.raises(AskError, match="took too long"):
        asker.ask("q", _setup(cleanup_model="m"), MeetConfig())
    polisher.error, polisher.answer = None, "  "
    with pytest.raises(AskError, match="empty answer"):
        asker.ask("q", _setup(cleanup_model="m"), MeetConfig())
    assert not asker.earlier  # failures are never remembered as answers


def test_prompt_without_notes_or_earlier_answers():
    assert prompt("q?", []) == 'The meeting audio just now:\n"""\nq?\n"""'


# ---- history


def test_history_keeps_text_and_trims(data_dir, monkeypatch):
    monkeypatch.setattr(history, "KEEP", 3)
    monkeypatch.setattr(history, "KEEP_MAX", 4)
    for i in range(5):
        history.add(f"q{i}", f"a{i}", "m")
    entries = history.read()
    assert [e["question"] for e in entries] == ["q2", "q3", "q4"] and history.history_file().parent == config.DATA_DIR


def test_audio_already_answered_never_comes_back_in_the_next_look_back():
    rec, made, _ = _recorder(lookback=2.0)
    rec.open()
    rec.start()
    made[0].feed(*[SOUND] * 10)
    assert rec.stop().heard
    made[0].feed(*[QUIET] * 5)
    rec.start()
    made[0].feed(*[QUIET] * 5)
    r = rec.stop()
    assert not r.heard and r.lookback == 0.5  # only what came after the answered recording

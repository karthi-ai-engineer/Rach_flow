"""The live captions session (one or two ways), its transcript, and the rule that keeps live captions apart from
dictation."""
import ast
from datetime import datetime
from pathlib import Path

import pytest

import sst.live
from sst.live.contracts import MIC, SYSTEM, Kind, LiveConfig, LiveEvent, language_name
from sst.live.session import LiveSession
from sst.live.transcript import Transcript


class FakeCapture:
    def __init__(self, fail=False):
        self.on_frame, self.fail, self.stopped = None, fail, False

    def start(self, on_frame):
        if self.fail:
            raise OSError("no microphone")
        self.on_frame = on_frame

    def stop(self):
        self.stopped = True


class FakeEngine:
    def __init__(self, on_event):
        self.on_event, self.frames, self.started, self.stopped = on_event, [], False, False

    def start(self):
        self.started = True

    def feed(self, frame):
        self.frames.append(frame)

    def stop(self):
        self.stopped = True


def engine(session, lane):
    return session.lanes[lane][1]


def test_the_session_feeds_the_engine_and_passes_its_events_on(tmp_path):
    shown = []
    capture = FakeCapture()
    session = LiveSession(LiveConfig(target="en"), Transcript(tmp_path, "en"), shown.append)
    session.add(SYSTEM, capture, FakeEngine)
    capture.on_frame(b"\0" * 3200)
    assert engine(session, SYSTEM).started and engine(session, SYSTEM).frames == [b"\0" * 3200]
    engine(session, SYSTEM).on_event(LiveEvent(Kind.SOURCE, "今日は"))
    engine(session, SYSTEM).on_event(LiveEvent(Kind.LINE, "Hello today.", source="今日は", seconds=2.4))
    assert [e.kind for e in shown] == [Kind.SOURCE, Kind.LINE]
    assert session.lags == {SYSTEM: [2.4]} and session.lines == {SYSTEM: 1}
    stopped = engine(session, SYSTEM)
    session.stop()
    assert capture.stopped and stopped.stopped and not session.running
    assert session.transcript.lines == 1


def test_a_way_that_cant_start_leaves_nothing_running_and_the_other_goes_on():
    session = LiveSession(LiveConfig())
    session.add(SYSTEM, FakeCapture(), FakeEngine)
    made = []
    with pytest.raises(OSError):
        session.add(MIC, FakeCapture(fail=True), lambda on_event: made.append(FakeEngine(on_event)) or made[-1])
    assert made[0].stopped and list(session.lanes) == [SYSTEM]


def test_each_way_stops_alone():
    session = LiveSession(LiveConfig(mine=True))
    them, me = FakeCapture(), FakeCapture()
    session.add(SYSTEM, them, FakeEngine)
    session.add(MIC, me, FakeEngine)
    session.remove(MIC)
    assert me.stopped and not them.stopped and list(session.lanes) == [SYSTEM]


def test_untranslated_lines_from_the_microphone_are_echo_and_dropped(tmp_path):
    shown = []
    session = LiveSession(LiveConfig(mine=True), Transcript(tmp_path, "en", mine_target="ja"), shown.append)
    session.add(MIC, FakeCapture(), FakeEngine)
    mic = engine(session, MIC)
    mic.on_event(LiveEvent(Kind.LINE, "", source="本日はよろしくお願いします。", lane=MIC))  # the meeting, from the speakers
    mic.on_event(LiveEvent(Kind.LINE, "予算は来週決めましょう。", source="Let's set the budget next week.", lane=MIC))
    assert [e.text for e in shown] == ["予算は来週決めましょう。"] and session.lines == {MIC: 1}
    assert "本日" not in session.transcript.path.read_text(encoding="utf-8")


def test_the_settings_of_each_way():
    config = LiveConfig(target="en", mine=True, mine_target="ja")
    assert config.for_lane(SYSTEM).target == "en" and config.for_lane(MIC).target == "ja"


def test_the_transcript_is_bilingual_and_starts_with_the_first_line(tmp_path):
    now = [datetime(2026, 10, 5, 14, 3, 12)]
    transcript = Transcript(tmp_path / "live", "en", now=lambda: now[0])
    transcript.add(LiveEvent(Kind.SOURCE, "今日は"))  # only finished lines are written
    assert transcript.path is None
    transcript.add(LiveEvent(Kind.LINE, "Today we have a meeting.", source="今日は会議です"))
    now[0] = datetime(2026, 10, 5, 14, 3, 20)
    transcript.add(LiveEvent(Kind.LINE, "", source="Let's start."))  # already English: shown as heard
    text = transcript.path.read_text(encoding="utf-8")
    assert transcript.path.name == "2026-10-05 14-03-12 live captions.txt"
    assert text == ("Rflow live captions, 2026-10-05 14:03, translated into English\n\n"
                    "[14:03:12] 今日は会議です\n           Today we have a meeting.\n\n"
                    "[14:03:20] Let's start.\n\n")


def test_the_transcript_marks_the_users_own_lines(tmp_path):
    transcript = Transcript(tmp_path, "en", now=lambda: datetime(2026, 10, 5, 15, 0, 0), mine_target="ja")
    transcript.add(LiveEvent(Kind.LINE, "Thank you.", source="ありがとうございます。"))
    transcript.add(LiveEvent(Kind.LINE, "来週にしましょう。", source="Let's do it next week.", lane=MIC))
    assert transcript.path.read_text(encoding="utf-8") == (
        "Rflow live captions, 2026-10-05 15:00, translated into English; your own speech into Japanese\n\n"
        "[15:00:00] ありがとうございます。\n           Thank you.\n\n"
        "[15:00:00] You: Let's do it next week.\n           来週にしましょう。\n\n")


def test_language_names():
    assert language_name("ja") == "Japanese" and language_name("xx") == "xx"


# ---- rule number 1: live captions are a pipeline of their own

FORBIDDEN = ("sst.pipeline", "sst.dictate", "sst.audio")  # dictation's pipeline, its controller and its recorder


def test_live_captions_import_nothing_from_dictation():
    folder = Path(sst.live.__file__).parent
    for path in folder.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                assert not name.startswith(FORBIDDEN), f"{path.name} imports {name}: keep live captions apart"

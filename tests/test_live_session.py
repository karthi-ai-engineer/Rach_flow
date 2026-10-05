"""The live captions session, its transcript, and the rule that keeps live captions apart from dictation."""
import ast
from datetime import datetime
from pathlib import Path

import pytest

import sst.live
from sst.live.contracts import Kind, LiveConfig, LiveEvent, language_name
from sst.live.session import LiveSession
from sst.live.transcript import Transcript


class FakeCapture:
    def __init__(self, fail=False):
        self.on_frame, self.fail, self.stopped = None, fail, False

    def start(self, on_frame):
        if self.fail:
            raise OSError("no output device")
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


def test_the_session_feeds_the_engine_and_passes_its_events_on(tmp_path):
    shown = []
    capture = FakeCapture()
    session = LiveSession(LiveConfig(target="en"), capture, FakeEngine, Transcript(tmp_path, "en"), shown.append)
    session.start()
    capture.on_frame(b"\0" * 3200)
    assert session.engine.started and session.engine.frames == [b"\0" * 3200]
    session.engine.on_event(LiveEvent(Kind.SOURCE, "今日は"))
    session.engine.on_event(LiveEvent(Kind.LINE, "Hello today.", source="今日は", seconds=2.4))
    assert [e.kind for e in shown] == [Kind.SOURCE, Kind.LINE] and session.lags == [2.4]
    session.stop()
    assert capture.stopped and session.engine.stopped
    assert session.transcript.lines == 1


def test_a_capture_that_cant_start_stops_the_engine_too():
    session = LiveSession(LiveConfig(), FakeCapture(fail=True), FakeEngine)
    with pytest.raises(OSError):
        session.start()
    assert session.engine.stopped and not session.running


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

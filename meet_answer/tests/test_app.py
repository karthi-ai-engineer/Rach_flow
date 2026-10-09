"""MeetApp's flow with fakes: no real keys, sound, speech model, AI or window (jobs run at once, on the test's thread)."""
import queue

import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal

from meet_answer import app as meet_app
from meet_answer import history
from meet_answer.ask import Answer, AskError
from meet_answer.config import MeetConfig
from meet_answer.recorder import Recording
from meet_answer.rflow import RflowSetup
from meet_answer.transcribe import NoSpeechModel
from sst.gateway import GatewayConfig
from sst.settings import Settings


class FakeBox(QObject):
    retry, closed, moved = Signal(), Signal(), Signal(object)

    def __init__(self):
        super().__init__()
        self.visible, self.header, self.question_text, self.answer_text, self.error, self.note = False, "", "", "", "", ""
        self.placed_at = "unset"

    def place(self, geometry=None):
        self.placed_at = geometry

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def isVisible(self):
        return self.visible

    def show_recording(self, seconds, hearing):
        self.header = f"recording {seconds:.0f} {'sound' if hearing else 'silence'}"

    def show_status(self, text):
        self.header = text

    def show_note(self, text):
        self.note = text

    def show_question(self, question):
        self.question_text, self.answer_text, self.error = question, "", ""

    def show_answer(self, question, answer):
        self.question_text, self.answer_text, self.error = question, answer, ""

    def show_error(self, question, message):
        self.question_text, self.answer_text, self.error = question, "", message


class FakeListener:
    def __init__(self, hotkey):
        self.hotkey, self.events, self.recording, self.started, self.stopped = hotkey, queue.Queue(), False, False, False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def press(self, event="press"):
        self.events.put((event, 0.0))


class FakeRecorder:
    def __init__(self, heard=True, fail=None):
        self.recording, self.heard, self.seconds, self.opened, self.closed = False, heard, 0.0, False, False
        self.fail, self.on_full, self.on_note = fail, None, None

    def open(self):
        self.opened = True

    def start(self):
        if self.fail:
            raise self.fail
        self.recording = True

    def stop(self):
        self.recording = False
        audio = np.ones(16000, np.float32) if self.heard else np.zeros(0, np.float32)
        return Recording(audio, 1.0, 0.0, self.heard)

    def cancel(self):
        self.recording = False

    def close(self):
        self.closed = True


class FakeTranscriber:
    def __init__(self, text="What is the Q3 target?", error=None):
        self.text, self.error, self.prepared = text, error, 0

    def prepare(self, setup):
        self.prepared += 1
        return "parakeet"

    def transcribe(self, audio, setup):
        if self.error:
            raise self.error
        return self.text


class FakeAsker:
    def __init__(self, error=None):
        self.error, self.asked = error, []

    def ask(self, question, setup, config, again=False):
        self.asked.append((question, again))
        if self.error:
            raise self.error
        return Answer(question, f"Answer to {question}", "gemini-flash", 0.5)


def _setup():
    return RflowSetup(Settings(cleanup_model="m"), GatewayConfig(provider="gemini", api_key="k"))


def _app(qapp, recorder=None, transcriber=None, asker=None, config=None, setup=_setup):
    box = FakeBox()
    meet = meet_app.MeetApp(config or MeetConfig(), listener_factory=FakeListener, recorder=recorder or FakeRecorder(),
                            transcriber=transcriber or FakeTranscriber(), asker=asker or FakeAsker(), setup=setup,
                            box=box, tray=False, threaded=False)
    meet.start()
    return meet, box


def test_two_presses_record_transcribe_and_show_the_answer(qapp, data_dir):
    meet, box = _app(qapp)
    assert meet.listener.started and meet.recorder.opened and meet.model_name == "parakeet"
    meet.listener.press()
    meet._read_keys()
    assert meet.recorder.recording and meet.listener.recording and box.visible and box.header.startswith("recording")
    meet.listener.press()
    meet._read_keys()
    assert not meet.recorder.recording and not meet.listener.recording
    assert (box.question_text, box.answer_text, box.header) == ("What is the Q3 target?",
                                                                "Answer to What is the Q3 target?", "")
    assert history.read()[-1]["answer"] == "Answer to What is the Q3 target?"


def test_esc_cancels_and_hides_an_empty_box(qapp):
    meet, box = _app(qapp)
    meet.toggle()
    meet.listener.press("cancel")
    meet._read_keys()
    assert not meet.recorder.recording and not box.visible and not meet.listener.recording
    meet.listener.press("cancel")  # Esc with nothing recording does nothing
    meet._read_keys()


def test_closing_the_box_drops_a_recording(qapp):
    meet, box = _app(qapp)
    meet.toggle()
    box.closed.emit()
    assert not meet.recorder.recording


def test_nothing_heard_never_reaches_the_ai(qapp):
    asker = FakeAsker()
    meet, box = _app(qapp, recorder=FakeRecorder(heard=False), asker=asker)
    meet.toggle()
    meet.toggle()
    assert box.error == meet_app.NOTHING_HEARD and asker.asked == []


def test_no_words_no_model_and_ai_errors_are_said_and_keep_the_question(qapp, data_dir):
    meet, box = _app(qapp, transcriber=FakeTranscriber(text=""))
    meet.toggle()
    meet.toggle()
    assert box.error == meet_app.NO_WORDS
    meet, box = _app(qapp, transcriber=FakeTranscriber(error=NoSpeechModel("No speech model is set up")))
    meet.toggle()
    meet.toggle()
    assert box.error == "No speech model is set up"
    meet, box = _app(qapp, asker=FakeAsker(AskError("The AI model couldn't answer: timeout")))
    meet.toggle()
    meet.toggle()
    assert box.error.endswith("timeout") and box.question_text == "What is the Q3 target?" and meet._busy == 0
    assert history.read() == []  # nothing kept without an answer


def test_a_crash_in_speech_recognition_is_said_not_swallowed(qapp):
    meet, box = _app(qapp, transcriber=FakeTranscriber(error=RuntimeError("onnx failed")))
    meet.toggle()
    meet.toggle()
    assert "Speech recognition failed: onnx failed" in box.error and meet._busy == 0


def test_retry_asks_the_same_question_again(qapp):
    asker = FakeAsker()
    meet, box = _app(qapp, asker=asker)
    meet.toggle()
    meet.toggle()
    box.retry.emit()
    assert asker.asked == [("What is the Q3 target?", False), ("What is the Q3 target?", True)]
    assert box.answer_text == "Answer to What is the Q3 target?"


def test_no_audio_device_is_said_and_the_next_press_tries_again(qapp):
    recorder = FakeRecorder(fail=OSError("no output device"))
    meet, box = _app(qapp, recorder=recorder)
    meet.toggle()
    assert "no output device" in box.error and not meet.recorder.recording and not meet.listener.recording
    recorder.fail = None
    meet.toggle()
    assert meet.recorder.recording


def test_the_longest_recording_finishes_by_itself(qapp):
    meet, box = _app(qapp)
    meet.toggle()
    meet.signals.full.emit()
    assert not meet.recorder.recording and box.answer_text


def test_settings_that_cant_be_read_are_said(qapp):
    def broken():
        raise OSError("disk gone")
    meet, box = _app(qapp, setup=broken)
    meet.toggle()
    meet.toggle()
    assert "Couldn't read Rflow's settings" in box.error and meet.model_name == ""


def test_the_box_remembers_where_it_was_left(qapp, data_dir):
    meet, box = _app(qapp, config=MeetConfig(box=[10, 20, 400, 250]))
    meet.toggle()
    assert box.placed_at == [10, 20, 400, 250]
    box.moved.emit([30, 40, 420, 260])
    assert MeetConfig.load().box == [30, 40, 420, 260]


def test_a_bad_shortcut_falls_back_and_a_shortcut_rflow_uses_is_flagged(qapp):
    meet, _ = _app(qapp, config=MeetConfig(shortcut="ctrl+nonsense"))
    assert meet.hotkey.text == "ctrl+alt+j" and meet.problem == ""
    meet, _ = _app(qapp, config=MeetConfig(shortcut="ctrl+alt+l"))
    assert "live translation" in meet.problem
    assert meet_app.clash("ctrl+win", Settings()) == "dictation" and meet_app.clash("ctrl+alt+j", Settings()) == ""


@pytest.mark.parametrize("recording", [False, True])
def test_tray_icon_paints(qapp, recording):
    assert not meet_app.tray_icon(recording).isNull()


def test_quit_stops_everything(qapp, monkeypatch):
    meet, box = _app(qapp)
    monkeypatch.setattr(meet_app.QApplication, "instance", staticmethod(lambda: None))
    meet.toggle()
    meet.quit()
    assert meet.listener.stopped and meet.recorder.closed and not box.visible


def test_a_slow_answer_is_counted_so_it_never_looks_hung(qapp):
    meet, box = _app(qapp)
    meet._busy = 1
    meet._set_status("Thinking…")
    meet._refresh_header()
    assert box.header == "Thinking…" and meet._tick.isActive()
    meet._job_since -= 9
    meet._refresh_header()
    assert box.header == "Thinking… 9 s"
    meet._on_finished("q", "a", "")
    assert box.header == "" and not meet._tick.isActive()


def test_nothing_heard_leaves_the_answer_being_read(qapp):
    recorder = FakeRecorder()
    meet, box = _app(qapp, recorder=recorder)
    meet.toggle()
    meet.toggle()
    recorder.heard = False
    meet.toggle()
    meet.toggle()
    assert box.answer_text == "Answer to What is the Q3 target?" and box.note == meet_app.NOTHING_HEARD
    recorder.heard = True
    meet.toggle()  # the next press clears the message
    assert box.note == ""
    meet.transcriber.text = ""  # a tone, no speech: no words either
    meet.toggle()
    assert box.answer_text == "Answer to What is the Q3 target?" and box.note == meet_app.NO_WORDS


def test_a_question_waiting_behind_another_doesnt_reset_the_status(qapp):
    meet, box = _app(qapp)
    meet._threaded = True  # jobs queue up instead of running at once
    meet._busy = 1
    meet._set_status("Thinking…")
    since = meet._job_since
    meet.toggle()
    meet.toggle()
    assert meet._busy == 2 and meet._job_status == "Thinking…" and meet._job_since == since


def test_moving_the_box_keeps_settings_edited_by_hand(qapp, data_dir):
    meet, box = _app(qapp)
    MeetConfig(style="detailed", notes="finance lead", lookback=30.0).save()
    box.moved.emit([5, 6, 400, 300])
    saved = MeetConfig.load()
    assert (saved.style, saved.notes, saved.lookback, saved.box) == ("detailed", "finance lead", 30.0, [5, 6, 400, 300])

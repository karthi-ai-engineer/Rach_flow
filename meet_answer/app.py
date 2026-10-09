"""MeetApp: the tray icon, the shortcut and the stages wired together.

  Ctrl+Alt+J    start a recording (the look-back in front of it); again: stop it, then text, the AI, the answer
  Esc           while recording: drop the recording (Esc is kept from the app in front meanwhile)
  the box's ✕   hides the box, and drops a recording under way

What follows a recording (speech to text, then the AI) runs on one worker thread, in order: a new recording can start
while the last question is still being answered, and its answer follows. Results come back to the Qt thread by signals.
"""
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from meet_answer import config as cfg
from meet_answer import history
from meet_answer.ask import Asker, AskError
from meet_answer.config import MeetConfig
from meet_answer.overlay import AnswerBox
from meet_answer.recorder import Recorder, Recording
from meet_answer.rflow import RflowSetup
from meet_answer.transcribe import NoSpeechModel, Transcriber
from sst import theme
from sst.hotkey import HotkeyListener, parse_hotkey

log = logging.getLogger(__name__)

APP_NAME = "meet_answer"
DEFAULT_SHORTCUT = "ctrl+alt+j"
POLL_MS, TICK_MS = 30, 250  # reading the shortcut's events; the clock in the box (recording, waiting)
SHOW_WAIT_AFTER = 3.0  # seconds of transcribing or thinking after which the box counts them: a slow provider isn't a hang
NOTHING_HEARD = "Nothing was heard. Is the meeting playing on this laptop, with Windows' sound not muted?"
NO_WORDS = "No words could be made out of the sound. Press the shortcut, let the question play, press it again."


@dataclass(frozen=True)
class Job:
    kind: str  # "prepare" (load the speech model), "audio" (a recording) or "question" (Retry)
    recording: Recording | None = None
    question: str = ""


class Signals(QObject):
    status = Signal(str)  # what the worker does now: "Transcribing…", "Thinking…"
    question = Signal(str)  # the words heard
    finished = Signal(str, str, str)  # (question, answer, error): exactly once for every audio or question job
    ready = Signal(str)  # the speech model is loaded: its name ("" when none could be)
    note = Signal(str)  # a hint about the meeting audio ("" once it no longer applies)
    full = Signal()  # the recording reached its longest


def clash(shortcut: str, settings) -> str:
    """The Rflow tool already on this shortcut ("" = none): both would act on it."""
    mine = parse_hotkey(shortcut)
    for name, theirs in (("dictation", settings.hotkey), ("Text Transform", settings.transform_shortcut),
                         ("Translate", settings.translate_shortcut), ("live translation", settings.live_shortcut)):
        try:
            other = parse_hotkey(theirs) if theirs else None
        except ValueError:
            continue
        if other and (other.modifiers, other.key, other.double) == (mine.modifiers, mine.key, mine.double):
            return name
    return ""


def tray_icon(recording: bool) -> QIcon:
    """A round "?" in the accent colour; red while recording."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme.TOKENS["dark"]["live" if recording else "iris"]))
    p.drawEllipse(QRectF(4, 4, 56, 56))
    p.setPen(QColor("#0B0D13"))
    font = QFont("Segoe UI", 30)
    font.setBold(True)
    p.setFont(font)
    p.drawText(QRectF(4, 2, 56, 56), Qt.AlignmentFlag.AlignCenter, "●" if recording else "?")
    p.end()
    return QIcon(pixmap)


class MeetApp(QObject):
    def __init__(self, config: MeetConfig, *, listener_factory: Callable = HotkeyListener, recorder: Recorder | None = None,
                 transcriber: Transcriber | None = None, asker: Asker | None = None,
                 setup: Callable[[], RflowSetup] = RflowSetup.load, box: AnswerBox | None = None, tray: bool = True,
                 threaded: bool = True):
        super().__init__()
        self.config = config
        try:
            self.hotkey = parse_hotkey(config.shortcut)
        except ValueError as e:
            log.warning("Bad shortcut %r (%s); using %s", config.shortcut, e, DEFAULT_SHORTCUT)
            self.hotkey = parse_hotkey(DEFAULT_SHORTCUT)
        self.recorder = recorder or Recorder(config.lookback, config.max_seconds)
        self.transcriber = transcriber or Transcriber(speech_model=config.speech_model)
        self.asker = asker or Asker()
        self._setup = setup
        self.box = box or AnswerBox(config.hide_from_share, self.hotkey.label)
        self.signals = Signals()
        self.signals.status.connect(self._on_status)
        self.signals.question.connect(self._on_question)
        self.signals.finished.connect(self._on_finished)
        self.signals.ready.connect(self._on_ready)
        self.signals.note.connect(self._on_note)
        self.signals.full.connect(self.finish)
        self.recorder.on_full = self.signals.full.emit  # from the capture's thread: the signal brings it to Qt's
        self.recorder.on_note = self.signals.note.emit
        self.box.retry.connect(self.retry)
        self.box.closed.connect(self._box_closed)
        self.box.moved.connect(self._box_moved)
        self._listener_factory = listener_factory
        self.listener = None
        self._threaded = threaded  # False in tests: each job runs at once, on the caller's thread
        self._jobs: queue.Queue[Job | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._busy = 0  # audio and question jobs not finished yet
        self._job_status = ""
        self._job_since = 0.0  # when the worker began its current step
        self._note = ""  # the capture's hint (where the sound may be), shown under the answer
        self._placed = False
        self.model_name = ""  # the speech model loaded ("" until then, or when none could be)
        self.prepared = False  # the first try to load it is over
        self.problem = ""  # why the shortcut can't work, for the tray
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._read_keys)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh_header)
        self.tray = self._make_tray() if tray else None

    # ---- start and stop

    def start(self) -> None:
        if self._threaded:
            self._worker = threading.Thread(target=self._work, name="meet-answer-worker", daemon=True)
            self._worker.start()
        self._submit(Job("prepare"))
        try:
            self.listener = self._listener_factory(self.hotkey)
            self.listener.start()
        except OSError as e:
            self.listener = None
            self.problem = f"The shortcut doesn't work: {e}"
            log.warning("%s", self.problem)
            self._notify("The shortcut doesn't work", str(e))
        try:
            taken = clash(self.hotkey.text, self._setup().settings)
        except Exception as e:  # Rflow's settings unreadable: the shortcut still works
            log.warning("Couldn't compare the shortcut with Rflow's: %s", e)
            taken = ""
        if taken:
            self.problem = f"{self.hotkey.label} is also Rflow's {taken} shortcut: change one of them"
            self._notify("Shortcut taken", self.problem)
        self._poll.start(POLL_MS)
        self._set_tray()
        if self._threaded:  # opening the capture can take a moment: never at the cost of the tray appearing
            threading.Thread(target=self._open_audio, name="meet-answer-audio", daemon=True).start()
        else:
            self._open_audio()

    def quit(self) -> None:
        self._poll.stop()
        self._tick.stop()
        if self.listener:
            self.listener.stop()
        self.recorder.close()
        self._jobs.put(None)
        if self.tray:
            self.tray.hide()
        self.box.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _open_audio(self) -> None:
        try:
            self.recorder.open()
        except Exception as e:  # shown, and tried again at the press
            log.warning("Can't listen to the meeting audio: %s", e)
            self.signals.note.emit(f"Can't listen to the meeting audio yet: {e}")

    # ---- the shortcut

    def _read_keys(self) -> None:
        if self.listener is None:
            return
        while True:
            try:
                event, _ = self.listener.events.get_nowait()
            except queue.Empty:
                return
            if event == "press":
                self.toggle()
            elif event == "cancel":
                self.cancel()

    def toggle(self) -> None:
        self.finish() if self.recorder.recording else self.begin()

    def begin(self) -> None:
        if self.recorder.recording:
            return
        try:
            self.recorder.start()
        except Exception as e:  # no device, or Windows refused it: said in the box
            log.warning("Can't record the meeting audio: %s", e)
            self._show_box()
            self._tell(f"Can't listen to the meeting audio: {e}")
            return
        self.box.show_note(self._note)  # a message from the last press goes
        if self.listener:
            self.listener.recording = True  # Esc now cancels
        self._show_box()
        self._refresh_header()
        self._set_tray()

    def finish(self) -> None:
        if not self.recorder.recording:
            return
        recording = self.recorder.stop()
        self._stopped()
        log.info("Recorded %.1f s (+%.1f s look-back), %.1f s with sound", recording.seconds, recording.lookback,
                 recording.total)
        if not recording.heard or not len(recording.audio):
            self._tell(NOTHING_HEARD)
            self._refresh_header()
            return
        if not self._busy:  # else the worker is still on the last question, and says when it gets to this one
            self._set_status("Transcribing…")
        self._busy += 1
        self._refresh_header()
        self._submit(Job("audio", recording=recording))

    def cancel(self) -> None:
        if not self.recorder.recording:
            return
        self.recorder.cancel()
        self._stopped()
        self._refresh_header()
        if not self._busy and not self.box.question_text and not self.box.answer_text:
            self.box.hide()  # nothing to show: the box only came up for the recording

    def retry(self) -> None:
        question = self.box.question_text
        if not question:
            return
        if not self._busy:
            self._set_status("Thinking…")
        self._busy += 1
        self._refresh_header()
        self._submit(Job("question", question=question))

    def _stopped(self) -> None:
        if self.listener:
            self.listener.recording = False
        self._set_tray()

    # ---- the worker

    def _submit(self, job: Job) -> None:
        if self._threaded:
            self._jobs.put(job)
        else:
            self._run(job)

    def _work(self) -> None:
        while (job := self._jobs.get()) is not None:
            self._run(job)

    def _run(self, job: Job) -> None:
        if job.kind == "prepare":
            try:
                name = self.transcriber.prepare(self._setup())
            except Exception as e:  # said in the tray; the first question tries again
                log.warning("No speech model yet: %s", e)
                self.signals.note.emit(str(e) if isinstance(e, NoSpeechModel) else f"The speech model didn't load: {e}")
                name = ""
            self.signals.ready.emit(name)
            return
        question, answer, error = job.question, "", ""
        try:
            question, answer = self._answer(job)
        except (AskError, NoSpeechModel) as e:
            error = str(e)
        except _Failed as e:
            question, error = e.question, e.message
        except Exception as e:  # never a silent failure: the box says what went wrong
            log.exception("Answering failed")
            error = f"Something went wrong: {e}"
        self.signals.finished.emit(question, answer, error)

    def _answer(self, job: Job) -> tuple[str, str]:
        try:
            setup = self._setup()
        except Exception as e:
            raise _Failed(job.question, f"Couldn't read Rflow's settings: {e}") from e
        question = job.question
        if job.kind == "audio":
            self.signals.status.emit("Transcribing…")
            try:
                question = self.transcriber.transcribe(job.recording.audio, setup)
            except NoSpeechModel:
                raise
            except Exception as e:
                log.exception("Speech recognition failed")
                raise _Failed("", f"Speech recognition failed: {e}") from e
            if not question:
                raise _Failed("", NO_WORDS)
            self.signals.question.emit(question)
        self.signals.status.emit("Thinking…")
        try:
            answer = self.asker.ask(question, setup, self.config, again=job.kind == "question")
        except AskError as e:
            raise _Failed(question, str(e)) from e
        if self.config.save_history:
            history.add(question, answer.text, answer.model)
        return question, answer.text

    # ---- results, on the Qt thread

    def _on_status(self, text: str) -> None:
        self._set_status(text)
        self._refresh_header()

    def _on_question(self, question: str) -> None:
        self.box.show_question(question)
        self._refresh_header()

    def _on_finished(self, question: str, answer: str, error: str) -> None:
        self._busy = max(self._busy - 1, 0)
        if not self._busy:
            self._job_status = ""
        self._show_box()  # the answer was asked for, even if the box was closed meanwhile
        if error:
            self.box.show_error(question, error)
        else:
            self.box.show_answer(question, answer)
        self._refresh_header()

    def _on_ready(self, name: str) -> None:
        self.model_name, self.prepared = name, True
        if name and not self.problem:
            self._notify(f"{APP_NAME} is ready",
                         f"In a meeting, press {self.hotkey.label}, let the question play, then press it again.")
        self._set_tray()

    def _on_note(self, text: str) -> None:
        self._note = text
        self.box.show_note(text)

    def _tell(self, message: str) -> None:
        """A message about this press: under the answer being read, if there is one (it stays), else in its place."""
        if self.box.answer_text:
            self.box.show_note(message)
        else:
            self.box.show_error("", message)

    def _refresh_header(self) -> None:
        if self.recorder.recording:
            self.box.show_recording(self.recorder.seconds, self.recorder.heard)
        else:
            waited = time.monotonic() - self._job_since
            status = self._job_status if self._busy else ""
            self.box.show_status(f"{status} {waited:.0f} s" if status and waited >= SHOW_WAIT_AFTER else status)
        if self.recorder.recording or self._busy:
            if not self._tick.isActive():
                self._tick.start(TICK_MS)
        else:
            self._tick.stop()

    def _set_status(self, text: str) -> None:
        if text != self._job_status:
            self._job_status, self._job_since = text, time.monotonic()

    # ---- the box

    def _show_box(self) -> None:
        if not self._placed:
            self.box.place(self.config.box or None)
            self._placed = True
        if not self.box.isVisible():
            self.box.show()

    def _box_closed(self) -> None:
        self.cancel()

    def _box_moved(self, geometry: list) -> None:
        self.config.box = [int(v) for v in geometry]
        try:  # only the box changes: the file may have been edited by hand since the start
            saved = MeetConfig.load()
            saved.box = self.config.box
            saved.save()
        except OSError as e:
            log.warning("Couldn't remember where the box is: %s", e)

    # ---- the tray

    def _make_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(tray_icon(False))
        menu = QMenu()
        self._toggle_action = menu.addAction("", self.toggle)
        menu.addAction("Show the last answer", self._show_box)
        menu.addSeparator()
        menu.addAction("Open the history", lambda: _open(history.history_file()))
        menu.addAction("Open the settings (used after a restart)", self._open_settings)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self._show_box() if reason == QSystemTrayIcon.ActivationReason.Trigger
                               else None)
        self._menu = menu  # kept: Qt doesn't own a context menu set on a tray icon
        tray.show()
        return tray

    def _set_tray(self) -> None:
        if not self.tray:
            return
        recording = self.recorder.recording
        self.tray.setIcon(tray_icon(recording))
        self._toggle_action.setText(f"{'Stop and answer' if recording else 'Answer a question'}\t{self.hotkey.label}")
        if self.problem or recording:
            state = self.problem or "Recording…"
        elif self.model_name:
            state = f"Ready: press {self.hotkey.label}"
        else:
            state = "No speech model yet: choose one in Rflow" if self.prepared else "Loading the speech model…"
        self.tray.setToolTip(f"{APP_NAME} · {state}")

    def _open_settings(self) -> None:
        path = cfg.DATA_DIR / "settings.json"
        if not path.exists():
            self.config.save(path)
        _open(path)

    def _notify(self, title: str, text: str) -> None:
        if self.tray:
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 5000)


class _Failed(Exception):
    def __init__(self, question: str, message: str):
        super().__init__(message)
        self.question, self.message = question, message


def _open(path) -> None:
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        os.startfile(path)  # the user's own file, in the app Windows has for it
    except OSError as e:
        log.warning("Couldn't open %s: %s", path, e)

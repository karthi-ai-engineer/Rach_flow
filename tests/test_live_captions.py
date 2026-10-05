"""The caption bar and the Qt side of a live captions session, off-screen (no window shown, no focus taken)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sst.live.captions import CaptionBar, LiveCaptions  # noqa: E402
from sst.live.contracts import Kind, LiveConfig, LiveEvent  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt():
    return QApplication.instance() or QApplication([])


def test_the_bar_shows_the_words_heard_and_the_translations_finished_dimmer():
    bar = CaptionBar(lines=2)
    bar.show_event(LiveEvent(Kind.LINE, "Thank you for joining.", source="ご参加ありがとうございます。"))
    bar.show_event(LiveEvent(Kind.LINE, "Let's review last week.", source="先週を振り返りましょう。"))
    bar.show_event(LiveEvent(Kind.LINE, "First, the deployment.", source="まずデプロイです。"))  # the oldest scrolls away
    bar.show_event(LiveEvent(Kind.SOURCE, "問題が一つ"))
    bar.show_event(LiveEvent(Kind.TRANSLATION, "One issue"))
    assert bar.heard_line() == "問題が一つ"
    shown = bar.translation_html()
    assert "Thank you" not in shown and "Let&#x27;s review last week." in shown and "One issue" in shown
    assert shown.index("deployment") < shown.index("One issue")  # the line being spoken is last, at the bottom
    bar.grab()  # paints without errors


def test_a_line_already_in_the_target_language_shows_as_heard():
    bar = CaptionBar()
    bar.show_event(LiveEvent(Kind.LINE, "", source="Let's start."))
    assert "Let&#x27;s start." in bar.translation_html() and bar.heard_line() == "Let's start."


def test_before_anyone_speaks_the_bar_says_it_listens_and_a_problem_is_shown():
    bar = CaptionBar()
    bar.target = "ja"
    bar.show_event(LiveEvent(Kind.STATUS, "Listening"))
    assert "Listening: captions into Japanese appear when someone speaks." in bar.translation_html()
    bar.show_event(LiveEvent(Kind.ERROR, "Can't reach Google: check the internet connection."))
    assert "Can&#x27;t reach Google" in bar.translation_html()
    bar.show_event(LiveEvent(Kind.SOURCE, "もしもし"))  # back: the problem goes
    assert "Google" not in bar.translation_html()


class FakeSession:
    def __init__(self, config, on_event, fail=False):
        self.config, self.on_event, self.fail, self.started, self.stopped = config, on_event, fail, False, False

    def start(self):
        if self.fail:
            raise OSError("no output device")
        self.started = True

    def stop(self):
        self.stopped = True


def test_start_shows_the_bar_and_stop_removes_it():
    made, running = [], []
    live = LiveCaptions(lambda config, on_event: made.append(FakeSession(config, on_event)) or made[-1])
    live.changed.connect(running.append)
    live.start(LiveConfig(target="ja"))
    assert live.running and live.bar is not None and live.bar.isVisible() and live.bar.target == "ja"
    made[0].on_event(LiveEvent(Kind.SOURCE, "hello"))  # from the engine's thread in real life: queued to Qt's
    QApplication.processEvents()
    assert live.bar.source == "hello"
    live.stop()
    assert not live.running and live.bar is None and made[0].stopped and running == [True, False]


def test_a_session_that_cant_start_leaves_nothing_behind():
    live = LiveCaptions(lambda config, on_event: FakeSession(config, on_event, fail=True))
    with pytest.raises(OSError):
        live.start(LiveConfig())
    assert not live.running and live.bar is None


def test_when_the_engine_gives_up_the_captions_stop_and_keep_the_reason():
    made = []
    live = LiveCaptions(lambda config, on_event: made.append(FakeSession(config, on_event)) or made[-1])
    live.start(LiveConfig())
    made[0].on_event(LiveEvent(Kind.ERROR, "Google refused the key: check the Gemini key in AI & models."))
    made[0].on_event(LiveEvent(Kind.STATUS, "Stopped"))
    QApplication.processEvents()
    assert not live.running and live.last_problem.startswith("Google refused the key")

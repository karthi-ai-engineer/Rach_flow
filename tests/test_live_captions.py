"""The caption bar and the Qt side of a live captions session, off-screen (no window shown, no focus taken)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sst.live.captions import CaptionBar, LiveCaptions  # noqa: E402
from sst.live.contracts import MIC, SYSTEM, Kind, LiveConfig, LiveEvent  # noqa: E402


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


def test_the_users_own_lines_are_marked_and_the_small_line_stays_with_the_others():
    bar = CaptionBar(lines=3)
    bar.show_event(LiveEvent(Kind.LINE, "Shall we start?", source="始めましょうか。"))
    bar.show_event(LiveEvent(Kind.LINE, "はい、始めましょう。", source="Yes, let's start.", lane=MIC))
    bar.show_event(LiveEvent(Kind.TRANSLATION, "予算について", lane=MIC))
    shown = bar.translation_html()
    assert shown.count("You&nbsp;·&nbsp;") == 2 and "Shall we start?" in shown
    assert shown.index("Shall we start?") < shown.index("はい、始めましょう。") < shown.index("予算について")
    assert bar.heard_line() == "始めましょうか。"  # not the user's own words
    bar.drop_lane(MIC)  # their way stopped: the line in progress goes, the finished one stays
    assert "予算について" not in bar.translation_html() and "はい、始めましょう。" in bar.translation_html()
    bar.grab()


def test_a_problem_on_the_users_own_way_says_whose_it_is():
    bar = CaptionBar()
    bar.show_event(LiveEvent(Kind.TRANSLATION, "We'll decide next week."))
    bar.show_event(LiveEvent(Kind.ERROR, "The microphone couldn't be opened.", lane=MIC))
    assert "We&#x27;ll decide next week." in bar.translation_html()
    assert "Your speech: The microphone couldn&#x27;t be opened." in bar.translation_html()


class FakeSession:
    """A LiveSession's ways, without captures or engines."""

    def __init__(self, config, on_event, fail=()):
        self.config, self.on_event, self.fail = config, on_event, fail
        self.lanes, self.stopped, self.transcript = {}, False, None

    def add(self, lane, capture, engine_factory):
        if lane in self.fail:
            raise OSError("no microphone" if lane == MIC else "no output device")
        self.lanes[lane] = capture

    def remove(self, lane):
        self.lanes.pop(lane, None)

    def stop(self):
        self.stopped = True
        self.lanes.clear()


def live_captions(fail=()):
    made = []
    live = LiveCaptions(lambda config, on_event: made.append(FakeSession(config, on_event, fail)) or made[-1],
                        lambda lane, config: (f"{lane} capture for {config.for_lane(lane).target}", None))
    return live, made


def test_start_shows_the_bar_and_stop_removes_it():
    live, made = live_captions()
    running = []
    live.changed.connect(running.append)
    live.start(LiveConfig(target="ja"))
    assert live.running and live.bar is not None and live.bar.isVisible() and live.bar.target == "ja"
    assert made[0].lanes == {SYSTEM: "system capture for ja"}  # the user's own speech only when asked
    made[0].on_event(LiveEvent(Kind.SOURCE, "hello"))  # from the engine's thread in real life: queued to Qt's
    QApplication.processEvents()
    assert live.bar.source == "hello"
    live.stop()
    assert not live.running and live.bar is None and made[0].stopped and running == [True, False]


def test_both_ways_start_together_and_the_users_own_can_stop_alone():
    live, made = live_captions()
    live.start(LiveConfig(target="en", mine=True, mine_target="ja"))
    assert made[0].lanes == {SYSTEM: "system capture for en", MIC: "mic capture for ja"}
    assert live.set_mine(False) == "" and list(made[0].lanes) == [SYSTEM] and live.running
    assert live.set_mine(True) == "" and MIC in made[0].lanes
    live.stop()


def test_a_microphone_that_cant_open_is_said_on_the_bar_and_the_captions_go_on():
    live, made = live_captions(fail=(MIC,))
    live.start(LiveConfig(mine=True))
    QApplication.processEvents()
    assert live.running and list(made[0].lanes) == [SYSTEM]
    assert "Your speech: The microphone couldn&#x27;t be opened (no microphone)." in live.bar.translation_html()
    live.stop()


def test_a_session_that_cant_start_leaves_nothing_behind():
    live, _ = live_captions(fail=(SYSTEM,))
    with pytest.raises(OSError):
        live.start(LiveConfig())
    assert not live.running and live.bar is None


def test_showing_the_bar_in_screen_shares_applies_at_once():
    live, _ = live_captions()
    live.start(LiveConfig())
    assert live.bar.hide_from_capture
    live.set_hidden(False)
    assert not live.bar.hide_from_capture and not live.config.hide_from_capture
    live.stop()


def test_when_the_engine_gives_up_the_captions_stop_and_keep_the_reason():
    live, made = live_captions()
    live.start(LiveConfig())
    made[0].on_event(LiveEvent(Kind.ERROR, "Google refused the key: check the Gemini key in AI & models."))
    made[0].on_event(LiveEvent(Kind.STATUS, "Stopped"))
    QApplication.processEvents()
    assert not live.running and live.last_problem.startswith("Google refused the key")


def test_when_only_the_users_own_way_gives_up_the_captions_go_on():
    live, made = live_captions()
    live.start(LiveConfig(mine=True))
    made[0].on_event(LiveEvent(Kind.ERROR, "Google's quota for this key is used up for now.", lane=MIC))
    made[0].on_event(LiveEvent(Kind.STATUS, "Stopped", lane=MIC))
    QApplication.processEvents()
    assert live.running and list(made[0].lanes) == [SYSTEM] and live.last_problem.startswith("Your speech:")
    live.stop()

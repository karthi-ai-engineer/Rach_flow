"""AnswerBox off-screen: no window shown, no focus taken, no real mouse (Qt's test events) and the off-screen platform's
own clipboard (never Windows')."""
import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from meet_answer.overlay import DEFAULT_SIZE, MARGIN, QUESTION_CHARS, AnswerBox, render_answer, tail


@pytest.fixture
def box(qapp):
    b = AnswerBox()
    yield b
    b.close()
    b.deleteLater()


def test_recording_shows_the_time_how_to_stop_and_when_nothing_is_heard(qapp):
    box = AnswerBox(shortcut_label="Ctrl+Shift+Q")
    box.show_recording(7.6, hearing=True)
    assert box.title_text == "● Recording 0:07  Ctrl+Shift+Q to stop · Esc to cancel"
    box.show_recording(65, hearing=False)
    assert box.title_text.startswith("● Recording 1:05") and box.title_text.endswith(" · no sound yet")
    box.grab()


def test_the_header_shows_the_status_then_goes_quiet(box):
    assert box.title_text == "Meeting answers"
    box.show_status("Transcribing…")
    assert box.title_text == "Transcribing…"
    box.show_question("What's the deadline?")
    assert box.title_text == "Transcribing…"  # the header is the state, not the body
    box.show_status("")
    assert box.title_text == "Answer"


def test_a_long_question_keeps_its_end(box):
    question = "so as I was saying earlier about the roadmap " * 20 + "when does the beta ship?"
    box.show_question(question)
    shown = box.view.toPlainText()
    assert shown.startswith("…") and shown.endswith("when does the beta ship?") and len(shown) <= QUESTION_CHARS + 1
    assert box.question_text == question.strip() and tail("Short one?") == "Short one?"


def test_the_answer_gets_a_light_markdown():
    rich = render_answer("**Budget:** fine\n\n- one\n* two\n• three\n1. first\nRun `make **all**` now")
    assert "<b>Budget:</b>" in rich and rich.count(">•</td>") == 3 and ">1.</td>" in rich
    assert "make **all**" in rich  # nothing inside code is touched
    assert "- one" not in rich and "**Budget" not in rich


def test_html_from_the_model_is_never_rendered(box):
    rich = render_answer('<script>alert(1)</script> <b>x</b> <img src="a">')
    assert "<script>" not in rich and "<img" not in rich and "<b>x</b>" not in rich
    assert "&lt;script&gt;" in rich and "&lt;b&gt;x&lt;/b&gt;" in rich
    box.show_answer("Say it", "<b>hi</b> & <i>bye</i>")
    assert box.view.toPlainText().endswith("<b>hi</b> & <i>bye</i>")  # shown as written
    assert box.answer_text == "<b>hi</b> & <i>bye</i>"


def test_an_error_shows_under_the_question(box):
    box.show_answer("First?", "First answer")
    box.show_error("What's the budget?", "The AI model couldn't be reached.")
    assert box.view.toPlainText() == "What's the budget?\nThe AI model couldn't be reached."
    assert box.answer_text == "" and not box.copy_button.isEnabled() and box.retry_button.isEnabled()
    box.show_error("", "Nothing was heard.")
    assert box.view.toPlainText() == "Nothing was heard." and not box.retry_button.isEnabled()


def test_copy_needs_an_answer_and_puts_it_on_the_clipboard(box):
    assert QGuiApplication.platformName() == "offscreen"  # its own clipboard, not Windows'
    assert not box.copy_button.isEnabled()
    box.show_question("Who owns it?")
    assert not box.copy_button.isEnabled()
    box.show_answer("Who owns it?", "**Priya** owns it.")
    assert box.copy_button.isEnabled()
    box.copy_button.click()
    assert QGuiApplication.clipboard().text() == "**Priya** owns it." and box.copy_button.toolTip() == "Copied"
    box._copied.timeout.emit()  # 1.5 s later
    assert box.copy_button.toolTip() == "Copy the answer"
    box.show_question("Next one?")  # the answer stays readable (and copyable) until the next one comes
    assert box.copy_button.isEnabled() and box.answer_text == "**Priya** owns it."
    box.show_error("Next one?", "The AI model couldn't answer")
    assert not box.copy_button.isEnabled()


def test_the_next_question_shows_above_the_answer_still_being_read(box):
    box.show_answer("Who owns it?", "Priya owns it.")
    box.show_question("And the budget?")
    page = box.view.toPlainText()
    assert "Next: And the budget?" in page and "Priya owns it." in page and box.question_text == "Who owns it?"
    box.show_answer("And the budget?", "Two million.")
    page = box.view.toPlainText()
    assert "Next:" not in page and "Priya" not in page and box.question_text == "And the budget?"


def test_an_error_is_titled_no_answer(box):
    box.show_error("Why?", "timeout")
    box.show_status("")
    assert box.title_text == "No answer"


def test_a_remembered_size_bigger_than_the_screen_is_made_to_fit(box):
    area = QGuiApplication.primaryScreen().availableGeometry()
    box.place([area.left(), area.top(), area.width() + 3000, area.height() + 2000])
    assert box.width() <= area.width() and box.height() <= area.height()
    box.place([area.right() - 200, area.bottom() - 100, 500, area.height()])  # partly below the screen's edge
    assert area.contains(box.geometry())


def test_retry_only_when_a_question_is_shown_and_nothing_is_going_on(box):
    asked = []
    box.retry.connect(lambda: asked.append(True))
    assert not box.retry_button.isEnabled()
    box.show_question("Why?")
    box.show_status("Thinking…")
    assert not box.retry_button.isEnabled()
    box.show_answer("Why?", "Because.")
    assert box.retry_button.isEnabled() and box.title_text == "Answer"  # the answer ends the thinking
    box.show_recording(1, hearing=True)
    assert not box.retry_button.isEnabled()
    box.show_status("")
    assert box.retry_button.isEnabled()
    box.retry_button.click()
    assert asked == [True]


def test_the_cross_hides_the_box(box):
    closed = []
    box.closed.connect(lambda: closed.append(True))
    box.show()
    box.close_button.click()
    assert not box.isVisible() and closed == [True]


def test_it_opens_top_right_unless_it_was_left_on_a_screen(box):
    area = QGuiApplication.primaryScreen().availableGeometry()
    box.place()
    g = box.geometry()
    assert g.size() == DEFAULT_SIZE and g.top() == area.top() + MARGIN and area.right() - g.right() == MARGIN
    box.place([area.left() + 50, area.top() + 60, 400, 200])
    assert box.geometry() == QRect(area.left() + 50, area.top() + 60, 400, 200)
    box.place([area.right() + 5000, area.top(), 400, 200])  # on a screen that's gone
    assert box.geometry() == g


def test_dragging_moves_it_and_where_it_ends_up_is_remembered(box):
    box.setGeometry(100, 100, 460, 300)
    box.show()
    ended = []
    box.moved.connect(ended.append)
    QTest.mousePress(box, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(150, 20))  # the title
    QTest.mouseMove(box, QPoint(190, 50))
    QTest.mouseRelease(box, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(190, 50))
    assert box.geometry() == QRect(140, 130, 460, 300) and ended == [[140, 130, 460, 300]]
    viewport = box.view.viewport()  # the text moves it too
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 40))
    QTest.mouseMove(viewport, QPoint(30, 40))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(30, 40))
    assert box.geometry().topLeft() == QPoint(130, 130) and len(ended) == 2

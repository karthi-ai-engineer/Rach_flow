"""AnswerBox: the floating box that shows the answer to the question just asked in a meeting.

It floats over the meeting, at the top right of the screen until the user drags it somewhere else (an edge resizes it;
both are remembered), and never takes the keyboard (WS_EX_NOACTIVATE): typing stays in the meeting's chat while it
shows. By default it isn't in screen shares or recordings (SetWindowDisplayAffinity), so colleagues don't see it.

The header says what's happening (recording, transcribing, thinking) and is independent of the body, which keeps the
last question and its answer until the next one: a new recording can start while the last answer is still being read.
The model's answer is shown with a light markdown (bold, lists, code), never as HTML: everything it writes is escaped
first. The window follows sst.live.captions' translation bar; its private parts are copied, not imported.
"""
import ctypes
import html
import logging
import re

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QIcon, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QTextBrowser, QToolButton, QVBoxLayout, QWidget

from sst import theme
from sst.live.captions import dragged

EDGE = 8  # px along the border where a drag resizes instead of moving
MIN_SIZE, DEFAULT_SIZE = QSize(320, 150), QSize(460, 300)
MARGIN = 24  # px from the screen's edges where the box first opens
QUESTION_CHARS = 240  # about four lines of the question at the default width; its end is kept, where the question is
COPIED_MS = 1500  # how long Copy shows "Copied"
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11  # Windows 10 2004+: drawn on the screen, left out of screen capture

log = logging.getLogger(__name__)


def _no_focus(hwnd: int) -> None:
    """Never activated: the meeting app keeps the keyboard, while clicks move, resize and scroll the box. A copy of the
    translation bar's: meet_answer uses only Rflow's public parts."""
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x80
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE
                             | WS_EX_TOOLWINDOW)


_CURSORS = {(True, False): Qt.CursorShape.SizeHorCursor, (False, True): Qt.CursorShape.SizeVerCursor}


def _mix(colour: QColor, other: QColor, amount: float) -> str:
    """`amount` of `colour` over `other`, as #rrggbb: rich text and SVG icons take no alpha reliably."""
    return QColor(*(round(a * amount + b * (1 - amount)) for a, b in (
        (colour.red(), other.red()), (colour.green(), other.green()), (colour.blue(), other.blue())))).name()


def _token(name: str) -> QColor:
    return theme.tok(name, popup=True)


def _dim() -> str:
    """A button that can't be pressed now: still there (the user learns where it is), but clearly asleep."""
    return _mix(_token("text3"), _token("base"), 0.45)


# ---- the model's text: escaped, then a light markdown

_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")  # --- or ***: a paragraph gap
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*(\d{1,3}[.)])\s+(.*)$")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_P = "margin-top:0; margin-bottom:10px"


def _inline(line: str) -> str:
    """**bold**, *italic* and `code` in an already escaped line; nothing inside a code span is touched."""
    out = []
    for i, part in enumerate(_CODE.split(line)):
        if i % 2:
            mono = theme.font(14, mono=True).families()[0]
            out.append(f'<span style="font-family:\'{mono}\'; font-size:14px; '
                       f'background-color:{_mix(_token("text"), _token("base"), 0.1)}">{part}</span>')
        else:
            out.append(_ITALIC.sub(r"<i>\1</i>", _BOLD.sub(r"<b>\1</b>", part)))
    return "".join(out)


def render_answer(text: str) -> str:
    """The answer as rich text: the model's text is escaped first, so no HTML it writes is ever rendered; then lines
    starting "- ", "* " or "• " become bullets, "1. " stays numbered, blank lines part paragraphs."""
    marker = _token("text2").name()
    blocks: list[str] = []
    para: list[str] = []
    items: list[tuple[str, str]] = []  # (marker, text) of the list being read

    def end_paragraph():
        if para:
            blocks.append(f'<p style="{_P}">{"<br>".join(para)}</p>')
            para.clear()

    def end_list():
        if items:  # a table: wrapped lines hang under the text, not under the marker
            rows = "".join(f'<tr><td align="right" style="padding-right:8px; padding-bottom:5px; color:{marker}">{m}</td>'
                           f'<td style="padding-bottom:5px">{t}</td></tr>' for m, t in items)
            blocks.append(f'<table cellspacing="0" cellpadding="0" style="margin-top:4px; margin-bottom:5px">{rows}</table>')
            items.clear()

    for line in html.escape(text.strip(), quote=False).splitlines():
        if not line.strip() or _RULE.match(line):
            end_paragraph()
            end_list()
        elif m := _BULLET.match(line):
            end_paragraph()
            items.append(("•", _inline(m[1])))
        elif m := _NUMBERED.match(line):
            end_paragraph()
            items.append((m[1], _inline(m[2])))
        elif m := _HEADING.match(line):
            end_paragraph()
            end_list()
            blocks.append(f'<p style="{_P}"><b>{_inline(m[1])}</b></p>')
        else:
            end_list()
            para.append(_inline(line.strip()))
    end_paragraph()
    end_list()
    return "".join(blocks)


def _nowrap(text: str) -> str:
    """Escaped, and a narrow box wraps it only after a " · ": "no sound yet" never ends up as "no" and "sound yet"."""
    return html.escape(text).replace(" ", "&nbsp;").replace("&nbsp;·&nbsp;", "&nbsp;· ")


def tail(text: str, limit: int = QUESTION_CHARS) -> str:
    """A long question's end, from a whole word on: the question itself is at the end, after the run-up to it."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    space = cut.find(" ")
    return "…" + (cut[space + 1:] if 0 <= space < 30 else cut)


class AnswerBox(QWidget):
    retry = Signal()  # Retry: ask the AI again about the question shown
    closed = Signal()  # ✕: the box hid itself
    moved = Signal(object)  # [x, y, width, height] after a move or a resize, to remember

    def __init__(self, hide_from_capture: bool = True, shortcut_label: str = "Ctrl+Alt+J"):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("Meeting answer")
        self.setMouseTracking(True)
        self.setMinimumSize(MIN_SIZE)
        self.resize(DEFAULT_SIZE)
        self.hide_from_capture, self.shortcut_label = hide_from_capture, shortcut_label
        self._question = self._answer = self._error = ""
        self._state = "idle"  # "recording", "busy" (transcribing, thinking...) or "idle": Retry only when idle
        self._seen = False  # something was asked: the idle title is "Answer" from then on
        self._title_text = ""
        self._drag: tuple[QPoint, QRect, tuple] | None = None  # (where the press was, the geometry then, the edges)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(EDGE + 12, EDGE + 4, EDGE + 4, EDGE + 8)
        outer.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(2)
        self.title = QLabel()
        self.title.setFont(theme.font(12, 600))
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setWordWrap(True)  # a narrow box puts the hint on a second line rather than cutting it off
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # a press on it drags the box
        self.title.setStyleSheet("background: transparent;")
        self.copy_button = self._head_button("Copy the answer")
        self.copy_button.clicked.connect(self._copy)
        self.retry_button = self._head_button("Ask the AI again")
        self._set_icon(self.retry_button, next((n for n in ("refresh", "retry") if n in theme.ICONS), ""), "Retry")
        self.retry_button.clicked.connect(self.retry.emit)
        self.close_button = self._head_button("Close")
        self._set_icon(self.close_button, "close", "✕")
        self.close_button.clicked.connect(self._close)
        head.addWidget(self.title, 1)
        for b in (self.retry_button, self.copy_button, self.close_button):
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)
        self.view = QTextBrowser()
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.view.setOpenLinks(False)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setStyleSheet(
            f"QTextBrowser {{ background: transparent; border: none; color: {_token('text').name()}; }}"
            "QScrollBar:vertical { width: 8px; background: transparent; margin: 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(255, 255, 255, 46); border-radius: 4px; min-height: 28px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }")
        self.view.viewport().setAutoFillBackground(False)
        self.view.document().setDefaultFont(theme.font(16, 400))
        self.view.document().setDocumentMargin(2)
        self.view.viewport().installEventFilter(self)  # its presses move the box too; the wheel still scrolls it
        outer.addWidget(self.view, 1)
        self.footer = QLabel()  # a hint from the app (where the sound may be), hidden when there's none
        self.footer.setFont(theme.font(12, 400))
        self.footer.setStyleSheet(f"color: {_token('text3').name()}; background: transparent;")
        self.footer.setWordWrap(True)
        self.footer.setContentsMargins(2, 0, 0, 0)  # in line with the text above (the document's margin)
        self.footer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.footer.hide()
        outer.addWidget(self.footer)
        self._copied = QTimer(self)
        self._copied.setSingleShot(True)
        self._copied.setInterval(COPIED_MS)
        self._copied.timeout.connect(self._copy_done)
        self._copy_done()
        self._idle()
        self._render()

    @staticmethod
    def _head_button(tip: str) -> QToolButton:
        b = QToolButton()
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFont(theme.font(12, 500))
        b.setStyleSheet(f"QToolButton {{ border: none; border-radius: 6px; padding: 4px; background: transparent; "
                        f"color: {_token('text2').name()}; }}"
                        "QToolButton:hover { background: rgba(255, 255, 255, 28); }"
                        f"QToolButton:disabled {{ background: transparent; color: {_dim()}; }}")
        return b

    @staticmethod
    def _set_icon(button: QToolButton, name: str, text: str, colour: str = "text2") -> None:
        """The design's icon, dimmed while the button can't be pressed; `text` when theme has no such icon."""
        if name not in theme.ICONS:
            button.setIcon(QIcon())
            button.setText(text)
            return
        icon = QIcon()
        icon.addPixmap(theme.icon_pixmap(name, _token(colour).name(), 14, 2.0), QIcon.Mode.Normal)
        icon.addPixmap(theme.icon_pixmap(name, _dim(), 14, 2.0), QIcon.Mode.Disabled)
        button.setIcon(icon)
        button.setText("")

    # -- the header: what's happening now

    def show_recording(self, seconds: float, hearing: bool) -> None:
        s = max(0, int(seconds))
        self._state = "recording"
        self._set_title(f"● Recording {s // 60}:{s % 60:02d}", "live", f"{self.shortcut_label} to stop · Esc to cancel",
                        "" if hearing else "no sound yet")
        self._refresh()

    def show_status(self, text: str) -> None:
        """Transcribing, thinking...; "" when nothing is going on."""
        if text:
            self._state = "busy"
            self._set_title(text, "text2")
        else:
            self._idle()
        self._refresh()

    def show_note(self, text: str) -> None:
        self.footer.setText(text)
        self.footer.setVisible(bool(text))

    def _idle(self) -> None:
        self._state = "idle"
        self._set_title("Answer" if self._seen else "Meeting answers", "text3")

    def _set_title(self, title: str, colour: str, hint: str = "", warning: str = "") -> None:
        muted, warn = _token("text3").name(), _token("warn").name()
        rich, plain = f'<span style="color:{_token(colour).name()}">{_nowrap(title)}</span>', title
        if hint:
            rich += f'&nbsp; <span style="color:{muted}; font-weight:400">{_nowrap(hint)}</span>'
            plain += f"  {hint}"
        if warning:
            rich += f'<span style="color:{warn}; font-weight:400">&nbsp;· {_nowrap(warning)}</span>'
            plain += f" · {warning}"
        self._title_text = plain
        self.title.setText(rich)

    # -- the body: the question and its answer

    def show_question(self, question: str) -> None:
        """The words heard, while the answer is being written: the last answer goes."""
        self._show(question, "", "")

    def show_answer(self, question: str, answer: str) -> None:
        self._show(question, answer.strip(), "")

    def show_error(self, question: str, message: str) -> None:
        self._show(question, "", message.strip())

    def _show(self, question: str, answer: str, error: str) -> None:
        self._question, self._answer, self._error = question.strip(), answer, error
        self._seen = True
        if self._state == "busy" and (answer or error):  # done: the header goes quiet (a recording keeps its own)
            self._idle()
        elif self._state == "idle":
            self._idle()  # "Meeting answers" becomes "Answer"
        self._copy_done()
        self._render()

    @property
    def answer_text(self) -> str:
        """The last answer as the model wrote it ("" if none): what Copy puts on the clipboard."""
        return self._answer

    @property
    def question_text(self) -> str:
        return self._question

    @property
    def title_text(self) -> str:
        return self._title_text

    def _render(self) -> None:
        muted = _token("text3").name()
        parts = []
        if self._question:
            parts.append(f'<p style="{_P}; font-size:13px; color:{muted}">{html.escape(tail(self._question))}</p>')
        if self._answer:
            parts.append(render_answer(self._answer))
        elif self._error:
            message = html.escape(self._error).replace("\n", "<br>")
            parts.append(f'<p style="{_P}; font-size:15px; color:{_token("warn").name()}">{message}</p>')
        elif not self._question:
            hint = f"Press {self.shortcut_label} when a question starts and again when it ends: the answer appears here."
            parts.append(f'<p style="{_P}; font-size:14px; color:{muted}">{html.escape(hint)}</p>')
        self.view.setHtml("".join(parts))
        self._refresh()

    def _refresh(self) -> None:
        self.copy_button.setEnabled(bool(self._answer))
        self.retry_button.setEnabled(bool(self._question) and self._state == "idle")

    # -- the buttons

    def _copy(self) -> None:
        if not self._answer:
            return
        QGuiApplication.clipboard().setText(self._answer)
        self._set_icon(self.copy_button, "check", "Copied", "ok")
        self.copy_button.setToolTip("Copied")
        self._copied.start()

    def _copy_done(self) -> None:
        self._copied.stop()
        self._set_icon(self.copy_button, "copy", "Copy")
        self.copy_button.setToolTip("Copy the answer")

    def _close(self) -> None:
        self.hide()
        self.closed.emit()

    def set_hidden_from_capture(self, hidden: bool) -> None:
        """Left out of screen shares, or shown in them: at once if the box is showing."""
        self.hide_from_capture = hidden
        if self.isVisible() and QGuiApplication.platformName() != "offscreen":
            ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(int(self.winId())),
                                                          WDA_EXCLUDEFROMCAPTURE if hidden else WDA_NONE)

    # -- where it is

    def place(self, geometry: list | None = None) -> None:
        """Where the user left it, if enough of that is still on a screen; else at the top right of the screen the
        pointer is on, out of the way of the meeting's faces and shared content."""
        try:
            rect = QRect(*(int(v) for v in geometry)) if geometry and len(geometry) == 4 else None
        except (TypeError, ValueError):  # a hand-edited settings file
            rect = None
        if rect is not None:
            rect.setSize(rect.size().expandedTo(MIN_SIZE))
            for screen in QGuiApplication.screens():
                seen = screen.availableGeometry().intersected(rect)
                if seen.width() >= 120 and seen.height() >= 60:  # enough of it to grab and drag back
                    self.setGeometry(rect)
                    return
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        size = DEFAULT_SIZE.boundedTo(area.size() - QSize(2 * MARGIN, 2 * MARGIN)).expandedTo(MIN_SIZE)
        self.setGeometry(area.left() + area.width() - size.width() - MARGIN, area.top() + MARGIN, size.width(),
                         size.height())

    def _edges(self, pos: QPoint) -> tuple[bool, bool, bool, bool]:
        return (pos.x() < EDGE, pos.y() < EDGE, pos.x() >= self.width() - EDGE, pos.y() >= self.height() - EDGE)

    def _press(self, global_pos: QPoint, pos: QPoint) -> None:
        self._drag = (global_pos, QRect(self.geometry()), self._edges(pos))

    def _move_to(self, global_pos: QPoint, pos: QPoint) -> None:
        if self._drag is None:
            left, top, right, bottom = self._edges(pos)
            if (left and top) or (right and bottom):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif (right and top) or (left and bottom):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            else:
                self.setCursor(_CURSORS.get((left or right, top or bottom), Qt.CursorShape.ArrowCursor))
            return
        start, geometry, edges = self._drag
        self.setGeometry(dragged(geometry, edges, global_pos - start, self.minimumSize()))

    def _release(self) -> None:
        if self._drag is not None:
            self._drag = None
            g = self.geometry()
            self.moved.emit([g.x(), g.y(), g.width(), g.height()])

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press(event.globalPosition().toPoint(), event.position().toPoint())

    def mouseMoveEvent(self, event):
        self._move_to(event.globalPosition().toPoint(), event.position().toPoint())

    def mouseReleaseEvent(self, event):
        self._release()

    def eventFilter(self, watched, event):
        if watched is self.view.viewport():
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                at = event.globalPosition().toPoint()
                self._press(at, self.mapFromGlobal(at))
                return True
            if kind == QEvent.Type.MouseMove and self._drag is not None:
                at = event.globalPosition().toPoint()
                self._move_to(at, self.mapFromGlobal(at))
                return True
            if kind == QEvent.Type.MouseButtonRelease:
                self._release()
                return True
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        if QGuiApplication.platformName() == "offscreen":  # tests: no real window
            return
        hwnd = int(self.winId())
        try:
            _no_focus(hwnd)
            if self.hide_from_capture:
                ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE)
        except Exception as e:  # never lose the answer over a window style
            log.warning("Answer box window style: %s", e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = QColor(_token("base"))
        base.setAlpha(236)
        p.setPen(QColor(255, 255, 255, 34))
        p.setBrush(base)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        p.end()

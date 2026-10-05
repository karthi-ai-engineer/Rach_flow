"""The caption bar, and the Qt side of a live captions session.

The bar sits at the bottom of the screen, over every app: the words heard on a small line, the translation below, the
finished lines dimmer than the one being spoken ("scrolling lines": research on live subtitles found it the only layout
that stays readable a few seconds behind the speaker). It never takes focus, clicks pass through it, and by default it
isn't in screen shares or recordings (SetWindowDisplayAffinity), so captions are only for the person reading them.
"""
import ctypes
import html
import logging
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QTextDocument
from PySide6.QtWidgets import QWidget

from sst import theme
from sst.live.contracts import Kind, LiveConfig, LiveEvent, language_name

WIDTH, MARGIN = 1040, 18  # the bar's widest, and its padding
WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+: drawn on the screen, left out of screen capture

log = logging.getLogger(__name__)


def _no_focus_click_through(hwnd: int) -> None:
    """Never activated (the user's app keeps the keyboard) and clicks go through (to the meeting below). Live captions'
    own copy of what the dictation pill does: the two pipelines share no code."""
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x20, 0x80
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)


class CaptionBar(QWidget):
    def __init__(self, lines: int = 2, hide_from_capture: bool = True):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide_from_capture = hide_from_capture
        self.finished: deque[tuple[str, str]] = deque(maxlen=lines)  # (words heard, translation) of the last lines
        self.source = self.translation = ""  # the line being spoken
        self.status, self.problem = "Starting…", ""
        self.target = ""
        self._source_font, self._text_font = theme.font(13, 500), theme.font(19, 500)
        self.setFixedHeight(self._height())

    def _height(self) -> int:
        text_lines = self.finished.maxlen + 1
        return int(MARGIN * 2 + QFontMetricsF(self._source_font).height() + 8
                   + QFontMetricsF(self._text_font).lineSpacing() * text_lines)

    # -- what's shown

    def show_event(self, event: LiveEvent) -> None:
        if event.kind is Kind.SOURCE:
            self.source, self.problem = event.text, ""
        elif event.kind is Kind.TRANSLATION:
            self.translation, self.problem = event.text, ""
        elif event.kind is Kind.LINE:
            self.finished.append((event.source, event.text or event.source))  # already in the target language: as heard
            self.source = self.translation = ""
        elif event.kind is Kind.STATUS:
            self.status = event.text
        elif event.kind is Kind.ERROR:
            self.problem = event.text
        self.update()

    def place(self) -> None:
        """Bottom centre of the screen the pointer is on, above the taskbar."""
        screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        width = min(WIDTH, int(area.width() * 0.8))
        self.setFixedWidth(width)
        self.move(area.left() + (area.width() - width) // 2, area.bottom() - self.height() - 24)

    def showEvent(self, event):
        super().showEvent(event)
        if QGuiApplication.platformName() == "offscreen":  # tests: no real window
            return
        hwnd = int(self.winId())
        try:
            _no_focus_click_through(hwnd)
            if self.hide_from_capture:
                ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE)
        except Exception as e:  # never lose the captions over a window style
            log.warning("Caption bar window style: %s", e)

    def heard_line(self) -> str:
        """The small top line: the words being heard now, else the last line's."""
        if self.source:
            return self.source
        return self.finished[-1][0] if self.finished else ""

    def translation_html(self) -> str:
        """Finished lines dimmer, the line being spoken bright; a status or problem when there's nothing to show."""
        done = [html.escape(text) for _, text in self.finished if text]
        current = html.escape(self.translation)
        if not done and not current:
            message = self.problem or self._waiting()
            tone = "warn" if self.problem else "text3"
            return f'<span style="color:{theme.tok(tone, popup=True).name()}">{html.escape(message)}</span>'
        dim, bright = theme.tok("text2", popup=True).name(), theme.tok("text", popup=True).name()
        parts = [f'<span style="color:{dim}">{line}</span>' for line in done]
        if current:
            parts.append(f'<span style="color:{bright}">{current}</span>')
        if self.problem:
            parts.append(f'<span style="color:{theme.tok("warn", popup=True).name()}">{html.escape(self.problem)}</span>')
        return "<br>".join(parts)

    def _waiting(self) -> str:
        if self.status == "Listening":
            into = f" into {language_name(self.target)}" if self.target else ""
            return f"Listening: captions{into} appear when someone speaks."
        return self.status

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        panel = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        base = QColor(theme.tok("base", popup=True))
        base.setAlpha(232)
        p.setPen(QColor(255, 255, 255, 30))
        p.setBrush(base)
        p.drawRoundedRect(panel, 16, 16)
        inner = panel.adjusted(MARGIN + 4, MARGIN, -MARGIN - 4, -MARGIN)
        # The words heard: one line, the newest words kept when it's too long.
        p.setFont(self._source_font)
        p.setPen(theme.tok("text3", popup=True))
        heard = QFontMetricsF(self._source_font)
        p.drawText(QRectF(inner.left(), inner.top(), inner.width(), heard.height()), Qt.AlignmentFlag.AlignLeft,
                   heard.elidedText(self.heard_line(), Qt.TextElideMode.ElideLeft, inner.width()))
        # The translation: wrapped, bottom-aligned, older lines cut off at the top.
        area = QRectF(inner.left(), inner.top() + heard.height() + 8, inner.width(),
                      inner.height() - heard.height() - 8)
        doc = QTextDocument()
        doc.setDefaultFont(self._text_font)
        doc.setDocumentMargin(0)
        doc.setTextWidth(area.width())
        doc.setHtml(self.translation_html())
        p.save()
        p.setClipRect(area)
        p.translate(QPointF(area.left(), area.bottom() - doc.size().height()))
        doc.drawContents(p)
        p.restore()
        p.end()


class LiveCaptions(QObject):
    """Starts and stops a live captions session and shows it in a caption bar; thread-safe towards the engine (its
    events arrive on its own threads and are moved to Qt's). `make_session(config, on_event)` builds the LiveSession
    (the app wires the capture, the engine with the user's key, and the transcript)."""

    event = Signal(object)
    changed = Signal(bool)  # running or not

    def __init__(self, make_session: Callable):
        super().__init__()
        self._make_session = make_session
        self.event.connect(self._on_event)
        self.session = None
        self.bar: CaptionBar | None = None
        self.last_problem = ""

    @property
    def running(self) -> bool:
        return self.session is not None

    def start(self, config: LiveConfig) -> None:
        if self.session is not None:
            return
        self.bar = CaptionBar(config.caption_lines, config.hide_from_capture)
        self.bar.target = config.target
        self.last_problem = ""
        session = self._make_session(config, self.event.emit)
        try:
            session.start()
        except Exception:
            self.bar.close()
            self.bar = None
            raise
        self.session = session
        self.bar.place()
        self.bar.show()
        self.changed.emit(True)

    def stop(self) -> None:
        session, self.session = self.session, None
        if session is not None:
            session.stop()
        if self.bar is not None:
            self.bar.close()
            self.bar.deleteLater()
            self.bar = None
        if session is not None:
            self.changed.emit(False)

    def _on_event(self, event: LiveEvent) -> None:
        if event.kind is Kind.ERROR:
            self.last_problem = event.text
        if self.bar is not None:
            self.bar.show_event(event)
        if event.kind is Kind.STATUS and event.text == "Stopped" and self.session is not None:
            self.stop()  # the engine gave up (a refused key): the bar goes; the reason stays in last_problem

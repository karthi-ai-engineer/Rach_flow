"""The caption bar, and the Qt side of a live captions session.

The bar sits at the bottom of the screen, over every app: the words heard on a small line, the translation below, the
finished lines dimmer than the one being spoken ("scrolling lines": research on live subtitles found it the only layout
that stays readable a few seconds behind the speaker). It never takes focus, clicks pass through it, and by default it
isn't in screen shares or recordings (SetWindowDisplayAffinity), so captions are only for the person reading them; a
switch shows it in a share, for colleagues to read the user's own words translated.

Both ways in one bar: what the laptop plays (SYSTEM), and the user's own speech (MIC), marked "You" in the accent
colour. The small line shows only the words heard from the others: the user knows what they said.
"""
import ctypes
import dataclasses
import html
import logging
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QTextDocument
from PySide6.QtWidgets import QWidget

from sst import theme
from sst.live.contracts import MIC, SYSTEM, Kind, LiveConfig, LiveEvent, language_name

WIDTH, MARGIN = 1040, 18  # the bar's widest, and its padding
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11  # Windows 10 2004+: drawn on the screen, left out of screen capture

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
        self.finished: deque[tuple[str, str, str]] = deque(maxlen=lines)  # (way, words heard, translation): last lines
        self.current: dict[str, tuple[str, str]] = {}  # way -> (words heard, translation) of the line being spoken
        self.problems: dict[str, str] = {}  # way -> what went wrong, until words come again
        self.status = "Starting…"
        self.target = ""
        self._source_font, self._text_font = theme.font(13, 500), theme.font(19, 500)
        self.setFixedHeight(self._height())

    def _height(self) -> int:
        text_lines = self.finished.maxlen + 1
        return int(MARGIN * 2 + QFontMetricsF(self._source_font).height() + 8
                   + QFontMetricsF(self._text_font).lineSpacing() * text_lines)

    # -- what's shown

    @property
    def source(self) -> str:
        return self.current.get(SYSTEM, ("", ""))[0]

    @property
    def translation(self) -> str:
        return self.current.get(SYSTEM, ("", ""))[1]

    def show_event(self, event: LiveEvent) -> None:
        lane, (heard, said) = event.lane, self.current.get(event.lane, ("", ""))
        if event.kind is Kind.SOURCE:
            self.current[lane] = (event.text, said)
            self.problems.pop(lane, None)
        elif event.kind is Kind.TRANSLATION:
            self.current[lane] = (heard, event.text)
            self.problems.pop(lane, None)
        elif event.kind is Kind.LINE:
            self.finished.append((lane, event.source, event.text or event.source))  # in the target language: as heard
            self.current.pop(lane, None)
        elif event.kind is Kind.STATUS and lane == SYSTEM:
            self.status = event.text
        elif event.kind is Kind.ERROR:
            self.problems[lane] = event.text
        self.update()

    def drop_lane(self, lane: str) -> None:
        """A way stopped: its line in progress and its problem go (its finished lines stay)."""
        self.current.pop(lane, None)
        self.problems.pop(lane, None)
        self.update()

    def set_hidden_from_capture(self, hidden: bool) -> None:
        """Left out of screen shares, or shown in them: at once, while the captions run."""
        self.hide_from_capture = hidden
        if self.isVisible() and QGuiApplication.platformName() != "offscreen":
            ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(int(self.winId())),
                                                          WDA_EXCLUDEFROMCAPTURE if hidden else WDA_NONE)

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
        """The small top line: the others' words being heard now, else their last line's."""
        if self.source:
            return self.source
        return next((heard for lane, heard, _ in reversed(self.finished) if lane == SYSTEM), "")

    def translation_html(self) -> str:
        """Finished lines dimmer, the lines being spoken bright, the user's own marked "You" in the accent colour; a
        status or problem when there's nothing to show."""
        lines = [(lane, text, False) for lane, _, text in self.finished if text]
        lines += [(lane, self.current[lane][1], True) for lane in (SYSTEM, MIC) if self.current.get(lane, ("", ""))[1]]
        warn = theme.tok("warn", popup=True).name()
        if not lines and not self.problems:
            return f'<span style="color:{theme.tok("text3", popup=True).name()}">{html.escape(self._waiting())}</span>'
        parts = [self._line(lane, text, bright) for lane, text, bright in lines]
        for lane, message in self.problems.items():
            who = "Your speech: " if lane == MIC else ""
            parts.append(f'<span style="color:{warn}">{html.escape(who + message)}</span>')
        return "<br>".join(parts)

    @staticmethod
    def _line(lane: str, text: str, bright: bool) -> str:
        if lane == MIC:
            accent = theme.tok("primary", popup=True)
            colour = accent.name() if bright else _mix(accent, theme.tok("base", popup=True), 0.72)
            return (f'<span style="color:{accent.name()}; font-weight:600">You&nbsp;·&nbsp;</span>'
                    f'<span style="color:{colour}">{html.escape(text)}</span>')
        colour = theme.tok("text" if bright else "text2", popup=True).name()
        return f'<span style="color:{colour}">{html.escape(text)}</span>'

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
    """Starts and stops a live captions session and shows it in a caption bar; thread-safe towards the engines (their
    events arrive on their own threads and are moved to Qt's). The app wires the parts: `make_session(config, on_event)`
    builds the LiveSession (with its transcript), `make_lane(lane, config)` gives a way's (capture, engine_factory)."""

    event = Signal(object)
    changed = Signal(bool)  # running or not

    def __init__(self, make_session: Callable, make_lane: Callable):
        super().__init__()
        self._make_session, self._make_lane = make_session, make_lane
        self.event.connect(self._on_event)
        self.session = None
        self.config = LiveConfig()
        self.bar: CaptionBar | None = None
        self.last_problem = ""

    @property
    def running(self) -> bool:
        return self.session is not None

    def start(self, config: LiveConfig) -> None:
        """What the laptop plays, and the user's own speech too when config.mine. Raises if the first can't start; the
        second's problem is shown on the bar, and the captions go on without it."""
        if self.session is not None:
            return
        self.config, self.last_problem = config, ""
        self.bar = CaptionBar(config.caption_lines, config.hide_from_capture)
        self.bar.target = config.target
        session = self._make_session(config, self.event.emit)
        try:
            session.add(SYSTEM, *self._make_lane(SYSTEM, config))
        except Exception:
            self.bar.close()
            self.bar = None
            raise
        self.session = session
        if config.mine:
            self._start_mine()
        self.bar.place()
        self.bar.show()
        self.changed.emit(True)

    def set_mine(self, on: bool) -> str:
        """Start or stop translating the user's own speech while the captions run; "" or why it didn't start."""
        self.config = dataclasses.replace(self.config, mine=on)
        if self.session is None:
            return ""
        if on:
            return self._start_mine()
        self.session.remove(MIC)
        if self.bar is not None:
            self.bar.drop_lane(MIC)
        return ""

    def _start_mine(self) -> str:
        try:
            self.session.add(MIC, *self._make_lane(MIC, self.config))
        except Exception as e:  # e.g. no microphone, or it's blocked in Windows' privacy settings
            log.warning("Live captions: the microphone didn't start: %s", e)
            message = f"The microphone couldn't be opened ({e})."
            self.event.emit(LiveEvent(Kind.ERROR, message, lane=MIC))
            return message
        if self.session.transcript is not None:
            self.session.transcript.mine_target = self.config.mine_target
        return ""

    def set_hidden(self, hidden: bool) -> None:
        """Left out of screen shares or shown in them: at once if the captions run, and for the next start."""
        self.config = dataclasses.replace(self.config, hide_from_capture=hidden)
        if self.bar is not None:
            self.bar.set_hidden_from_capture(hidden)

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
            self.last_problem = ("Your speech: " if event.lane == MIC else "") + event.text
        if self.bar is not None:
            self.bar.show_event(event)
        if event.kind is Kind.STATUS and event.text == "Stopped" and self.session is not None:
            if event.lane == MIC:  # only the user's own speech gave up: its reason stays on the bar
                self.session.remove(MIC)
            else:
                self.stop()  # the engine gave up (a refused key): the bar goes; the reason stays in last_problem


def _mix(colour: QColor, other: QColor, amount: float) -> str:
    """`amount` of `colour` over `other`, as #rrggbb: a dimmer accent for the user's finished lines."""
    return QColor(*(round(a * amount + b * (1 - amount)) for a, b in (
        (colour.red(), other.red()), (colour.green(), other.green()), (colour.blue(), other.blue())))).name()

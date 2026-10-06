"""Render the demo video (docs/media/demo.mp4) and its dictation scene as a GIF (docs/media/demo.gif) from Rflow's parts.

The pill, the Translate popup, the translation bar and the window are the app's own widgets with example data, drawn off
the screen like scripts/make_site_screenshots.py: nothing appears, takes focus, presses keys or touches the clipboard.
The editor, the meeting and the captions around them are painted here, in the app's colours and fonts. The frames are
composed with QPainter (1920x1080, 30 fps) and encoded with PyAV (H.264, no sound). Writing the GIF needs Pillow, which
Rflow itself doesn't use, so it's added only for this run:

  uv run --no-sync --with pillow python scripts/make_demo_video.py
"""
import math
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]  # this checkout's Rflow, whichever one is installed
os.environ["QT_QPA_PLATFORM"] = "windows"  # the real fonts
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"  # every part drawn at twice its size, whatever the screen's scaling:
os.environ["QT_SCALE_FACTOR"] = "2"  # sharp when the video shows it at 150%
DATA = tempfile.mkdtemp(prefix="rflow-demo-")
os.environ["APPDATA"] = os.environ["LOCALAPPDATA"] = DATA  # the example data only: the user's own Rflow stays unread

import av  # noqa: E402
import make_site_screenshots as site  # noqa: E402
import numpy as np  # noqa: E402
from av.video.reformatter import Colorspace  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from sst import theme  # noqa: E402

OUT = ROOT / "docs" / "media"
W, H, FPS = 1920, 1080, 30
S = W / 1280  # the stage is 1280x720: Rflow's parts at 150%, as on a laptop screen at 150% scaling
FADE = 0.6  # seconds between scenes: the one fades out, then the next fades in
GIF_WIDTH, GIF_STEP = 800, 0.07  # the README's GIF: 800 px wide, about 14 frames a second
T, POP = theme.TOKENS["dark"], theme.POPUP  # Obsidian; the popups are always Obsidian

EDITOR = QRectF(170, 36, 940, 400)  # the plain "Notes" window the text goes into
BAR = 40  # its title bar
TEXT_AT = QPointF(EDITOR.left() + 44, EDITOR.top() + BAR + 32)
TEXT_WIDTH, TEXT_PX, LINE = EDITOR.width() - 88, 22, 36
PILL_Y, KEYS_Y, CAPTION_Y = 500, 590, 664  # centres on the stage
PILL_SCALE = 1.3  # the pill a little larger than life: it's what the video is about

DICTATED = site.EXAMPLES[0][1]  # "Can you review the pull request before lunch ...", the website's example
JAPANESE = "来週の定例会議は木曜日の午後3時からに変更になりました。資料は水曜日までに共有フォルダにアップロードしてください。"
ENGLISH = ("Next week's regular meeting has moved to Thursday at 3 PM. Please upload the materials to the shared folder by "
           "Wednesday.")  # the website's Translate example
LIVE_LINES = [  # (start, end, words heard, translation): a meeting in Japanese, in English as people speak
    (0.45, 1.85, "では、第3四半期のロードマップから始めましょう。", "Let's start with the Q3 roadmap."),
    (2.15, 3.75, "新しい画面のデザインは、来週の月曜日までに仕上がる予定です。",
     "The designs for the new screens should be finished by next Monday."),
    (4.05, 5.35, "テストが終わったら、金曜日にリリースしましょう。", "Once testing is done, let's release on Friday."),
]


# ---------------------------------------------------------------- motion and drawing helpers

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ease(x: float) -> float:
    """Ease-in-out (cubic), 0..1."""
    x = clamp(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def ramp(t: float, start: float, length: float = 0.4) -> float:
    return ease((t - start) / length)


def mix(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def blend(a: QColor, b: QColor, k: float) -> QColor:
    return QColor.fromRgbF(*(mix(x, y, k) for x, y in zip(a.getRgbF(), b.getRgbF(), strict=True)))


def alpha(colour: QColor, a: float) -> QColor:
    c = QColor(colour)
    c.setAlphaF(clamp(c.alphaF() * a))
    return c


@contextmanager
def faded(p: QPainter, amount: float):
    """Paint at `amount` of the current opacity."""
    p.save()
    p.setOpacity(p.opacity() * clamp(amount))
    try:
        yield
    finally:
        p.restore()


def write(p: QPainter, rect: QRectF, text: str, px: float, weight: int = 400, colour: QColor | None = None,
          align=Qt.AlignmentFlag.AlignCenter, mono: bool = False) -> None:
    p.setFont(theme.font(px, weight, mono=mono))
    p.setPen(T["text"] if colour is None else colour)
    p.drawText(rect, align, text)


def draw_image(p: QPainter, image: QImage, x: float, y: float) -> None:
    """A grabbed widget (drawn at twice its size) at x, y on the stage, at its own size."""
    dpr = image.devicePixelRatio()
    p.drawImage(QRectF(x, y, image.width() / dpr, image.height() / dpr), image)


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def voice(t: float, spans: list[tuple[float, float]]) -> float:
    """How loud someone speaking is at t, 0..1: syllables, words, short breaths; quiet outside the spans."""
    for start, end in spans:
        if start <= t <= end:
            edge = min(1.0, (t - start) / 0.12, (end - t) / 0.12)
            syllables = abs(math.sin(t * math.tau * 2.4)) ** 0.7
            words = 0.6 + 0.4 * math.sin(t * math.tau * 0.45 + 1.3)
            grain = 0.75 + 0.25 * math.sin(t * 41.0) * math.sin(t * 13.7 + 0.4)
            return clamp(0.15 + 0.85 * syllables * words * grain * edge)
    return 0.04 + 0.03 * abs(math.sin(t * 23.0))


# ---------------------------------------------------------------- the stage: backdrop, captions, keys

@lru_cache(maxsize=1)
def backdrop() -> QImage:
    """Obsidian with two soft glows, Iris and Violet: the desktop everything sits on."""
    image = QImage(W, H, QImage.Format.Format_RGB32)
    p = QPainter(image)
    g = QLinearGradient(0, 0, 0, H)
    g.setColorAt(0, QColor("#171920"))
    g.setColorAt(1, QColor("#0F1115"))
    p.fillRect(image.rect(), g)
    for x, y, radius, colour, a in [(0.16, 0.10, 0.62, T["iris"], 0.10), (0.88, 0.92, 0.55, T["violet"], 0.07)]:
        glow = QRadialGradient(QPointF(x * W, y * H), radius * W)
        glow.setColorAt(0, alpha(colour, a))
        glow.setColorAt(1, alpha(colour, 0))
        p.fillRect(image.rect(), glow)
    p.end()
    return image


@lru_cache(maxsize=1)
def scrim() -> QImage:
    """The backdrop's bottom band, fading in from transparent: the caption stays clear of what's above it."""
    top = round(596 * S)
    band = backdrop().copy(0, top, W, H - top).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(band)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    g = QLinearGradient(0, 0, 0, 44 * S)
    g.setColorAt(0, QColor(0, 0, 0, 0))
    g.setColorAt(1, QColor(0, 0, 0, 255))
    p.fillRect(band.rect(), g)
    p.end()
    return band


@lru_cache(maxsize=1)
def logo() -> QImage:
    return QImage(str(ROOT / "sst" / "static" / "brand" / "rflow-mark-512.png"))


def draw_logo(p: QPainter, cx: float, cy: float, size: float) -> None:
    """The mark, in a soft Iris halo."""
    halo = QRadialGradient(QPointF(cx, cy), size * 1.6)
    halo.setColorAt(0, alpha(T["iris"], 0.22))
    halo.setColorAt(1, alpha(T["iris"], 0))
    p.fillRect(QRectF(cx - size * 1.6, cy - size * 1.6, size * 3.2, size * 3.2), halo)
    p.drawImage(QRectF(cx - size / 2, cy - size / 2, size, size), logo())


def draw_caption(p: QPainter, text: str, t: float) -> None:
    if text:
        with faded(p, ramp(t, 0.15, 0.45)):
            write(p, QRectF(0, CAPTION_Y - 24 + 6 * (1 - ramp(t, 0.15, 0.45)), 1280, 48), text, 22, 500, T["text"])


def draw_keys(p: QPainter, keys: list, amount: float) -> None:
    """Keycaps in a row, shown `amount` (0..1): (label, how far it's pressed 0..1), or a separator such as "+"."""
    if amount <= 0:
        return
    caps, seps = QFontMetricsF(theme.font(17, 600)), QFontMetricsF(theme.font(19, 500))
    items = [(k, None, seps.horizontalAdvance(k) + 2) if isinstance(k, str)
             else (k[0], k[1], max(58.0, caps.horizontalAdvance(k[0]) + 36)) for k in keys]
    gap = 10
    x = 640 - (sum(w for *_, w in items) + gap * (len(items) - 1)) / 2
    with faded(p, amount):
        p.translate(0, 8 * (1 - amount))
        for label, pressed, w in items:
            if pressed is None:
                write(p, QRectF(x, KEYS_Y - 22, w, 40), label, 19, 500, T["text3"])
            else:
                keycap(p, QRectF(x, KEYS_Y - 22, w, 42), label, pressed)
            x += w + gap


def keycap(p: QPainter, rect: QRectF, label: str, pressed: float) -> None:
    """A key in the design's keycap look; pressed, it sinks a little and lights up in Iris."""
    rect = rect.translated(0, 2.5 * pressed)
    theme.paint_surface(p, "keycap", rect, 9, popup=True, dpr=S)
    if pressed > 0:
        with faded(p, pressed):
            theme.outer_shadow(p, rect, 9, [(0, 0, 18, alpha(T["iris"], 0.45))], S)
            p.fillPath(theme.rounded(rect, 9), QColor("#262B3A"))
            p.setPen(QPen(alpha(T["iris"], 0.9), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), 8.25, 8.25)
    write(p, rect.adjusted(0, -1, 0, -1), label, 17, 600, blend(T["text"], T["iris"], pressed))


# ---------------------------------------------------------------- the editor ("Notes", a plain text window)

@lru_cache(maxsize=256)
def layout(text: str) -> QTextLayout:
    lay = QTextLayout(text, theme.font(TEXT_PX))
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    lay.setTextOption(option)
    lay.beginLayout()
    y = 0.0
    while True:
        line = lay.createLine()
        if not line.isValid():
            break
        line.setLineWidth(TEXT_WIDTH)
        line.setPosition(QPointF(0, y))
        y += LINE
    lay.endLayout()
    return lay


def _x(line, position: int) -> float:
    x = line.cursorToX(position)
    return x[0] if isinstance(x, tuple) else x


def caret_at(text: str, position: int) -> QPointF:
    """The top of the caret before `position`, on the stage."""
    lay = layout(text)
    if not text or not lay.lineCount():
        return QPointF(TEXT_AT)
    line = lay.lineForTextPosition(position)
    if not line.isValid():
        line = lay.lineAt(lay.lineCount() - 1)
    return QPointF(TEXT_AT.x() + _x(line, position), TEXT_AT.y() + line.y())


def draw_editor(p: QPainter) -> None:
    r = EDITOR
    theme.outer_shadow(p, r, 12, [(0, 18, 44, theme.rgba(0, 0, 0, .55)), (0, 2, 6, theme.rgba(0, 0, 0, .30))], S)
    p.fillPath(theme.rounded(r, 12), QColor("#1C1F25"))
    p.save()
    p.setClipPath(theme.rounded(r, 12))
    p.fillRect(QRectF(r.left(), r.top(), r.width(), BAR), QColor("#16181D"))
    p.fillRect(QRectF(r.left(), r.top() + BAR - 1, r.width(), 1), theme.rgba(255, 255, 255, .06))
    p.restore()
    p.setPen(QPen(theme.rgba(255, 255, 255, .08), 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
    p.drawPixmap(QPointF(r.left() + 16, r.top() + 12), theme.icon_pixmap("reading", T["text3"].name(), 16, 3.0))
    write(p, QRectF(r.left() + 42, r.top(), 200, BAR), "Notes", 13, 500, T["text2"], Qt.AlignmentFlag.AlignVCenter)
    cy = r.top() + BAR / 2  # minimise, maximise, close: plain lines
    p.setPen(QPen(T["text3"], 1.3))
    x = r.right() - 104
    p.drawLine(QPointF(x - 6, cy), QPointF(x + 6, cy))
    p.drawRect(QRectF(x + 40 - 5.5, cy - 5.5, 11, 11))
    p.drawLine(QPointF(x + 80 - 5.5, cy - 5.5), QPointF(x + 80 + 5.5, cy + 5.5))
    p.drawLine(QPointF(x + 80 + 5.5, cy - 5.5), QPointF(x + 80 - 5.5, cy + 5.5))


def draw_text(p: QPainter, text: str, selected: int = 0, tint: float = 0.0) -> None:
    """The document's text, its first `selected` characters highlighted as a selection; `tint` (0..1) colours it Violet,
    the AI's colour, as it settles after a transform."""
    lay = layout(text)
    for i in range(lay.lineCount()):
        line = lay.lineAt(i)
        start, end = line.textStart(), min(selected, line.textStart() + line.textLength())
        if end > start:
            x0, x1 = _x(line, start), _x(line, end)
            p.fillRect(QRectF(TEXT_AT.x() + x0 - 1, TEXT_AT.y() + line.y() - 3, x1 - x0 + 2, LINE - 2), T["selection"])
    p.setPen(blend(T["text"], T["violet"], tint))
    lay.draw(p, TEXT_AT)


def draw_caret(p: QPainter, text: str, position: int, t: float, steady: bool = False) -> None:
    """A blinking Iris caret (steady while text is being typed)."""
    on = 1.0 if steady else clamp(abs(((t % 1.06) / 1.06) * 2 - 1) * 6 - 2.5)  # about 0.5 s on, 0.5 s off, soft edges
    if on > 0:
        at = caret_at(text, position)
        p.fillRect(QRectF(at.x(), at.y() - 1, 2, TEXT_PX + 8), alpha(T["iris"], on))


def words_typed(text: str, k: float) -> str:
    """The first k (0..1) of the text, whole words at a time."""
    ends = [i for i, ch in enumerate(text) if ch == " "] + [len(text)]
    return text[:ends[round(k * (len(ends) - 1))]] if k > 0 else ""


# ---------------------------------------------------------------- Rflow's own parts, grabbed off the screen

class PillTrack:
    """The real pill (sst.app.Pill) through a scene: shown, its states one after another, hidden."""

    def __init__(self, show: float, hide: float, states: list[tuple[float, str, bool, str]],
                 speech: list[tuple[float, float]]):
        from sst.app import Pill
        self.pill = Pill(level=lambda: 0.0)
        self.show, self.hide, self.states, self.speech = show, hide, states, speech

    def image(self, i: int, t: float) -> QImage:
        start, state, ai, message = self.states[i]
        pill = self.pill
        pill.state, pill.message, pill.ai, pill._phase = state, message, ai, t
        if state == "recording":  # the pill paints its timer from the clock, and the last BARS levels, one a frame
            pill._started = time.monotonic() - max(0.0, t - start)
            pill._levels.clear()
            pill._levels.extend(voice(t - (pill.BARS - 1 - k) / 30, self.speech) for k in range(pill.BARS))
        pill.resize(pill._width() + 2 * pill.MARGIN, pill.HEIGHT + 2 * pill.MARGIN)
        return pill.grab().toImage()

    def draw(self, p: QPainter, t: float) -> None:
        shown = ramp(t, self.show, 0.25) * (1 - ramp(t, self.hide, 0.35))
        if shown <= 0:
            return
        i = max(k for k, s in enumerate(self.states) if s[0] <= t) if t >= self.states[0][0] else 0
        p.save()
        p.translate(640, PILL_Y)
        scale = PILL_SCALE * mix(0.9, 1.0, ramp(t, self.show, 0.3))
        p.scale(scale, scale)
        with faded(p, shown):
            change = ramp(t, self.states[i][0], 0.2) if i > 0 else 1.0
            for k, amount in ((i - 1, 1 - change), (i, change)):
                if k >= 0 and amount > 0:
                    image = self.image(k, t)
                    dpr = image.devicePixelRatio()
                    with faded(p, amount):
                        draw_image(p, image, -image.width() / dpr / 2, -image.height() / dpr / 2)
        p.restore()


@lru_cache(maxsize=1)
def translate_popups() -> tuple[list[QImage], QImage]:
    """The real Translate popup: translating (three steps of its dots), then the translation."""
    from sst.translate import Translation
    from sst.translateui import TranslatePopup
    popup = TranslatePopup()
    popup._choices = ["English", "Spanish", "Japanese"]
    popup.route.setText("Japanese →")
    popup.original.setText(popup.original.fontMetrics().elidedText(JAPANESE, Qt.TextElideMode.ElideRight, popup.INNER))
    popup.busy("English")
    popup.dots.stop()  # its dots are set here, frame by frame
    busy = []
    for dots in (1, 2, 3):
        popup.waiting.setText("Translating into English" + "." * dots)
        settle()
        busy.append(popup.grab().toImage())
    popup.show_result(Translation(ENGLISH, "English", JAPANESE, 0.9))
    settle()
    return busy, popup.grab().toImage()


@lru_cache(maxsize=1)
def models_window() -> QImage:
    """The real window on AI & models: the setups and what each costs a month."""
    from sst import window as w
    window = w.MainWindow(site.preview())
    window.apply_theme("dark")
    window.resize(1000, 680)
    window.set_status("Ready: hold Ctrl+Win · cleanup: gemini-3.5-flash-lite", True)
    window.show_page("models")
    settle()
    return window.grab().toImage()


# ---------------------------------------------------------------- the scenes

class Scene:
    duration = 0.0
    caption = ""
    chrome = None  # what stays from one scene to the next (the editor), if anything: drawn beneath draw()

    def draw(self, p: QPainter, t: float) -> None:
        raise NotImplementedError


class Title(Scene):
    duration = 2.5

    def draw(self, p, t):
        k = ramp(t, 0.0, 0.7)
        with faded(p, k):
            draw_logo(p, 640, 228, mix(132, 144, k))
        for start, y, text, px, weight, colour in [(0.25, 372, "Rflow", 64, 600, T["text"]),
                                                   (0.5, 440, "Speak anywhere. It types.", 30, 500, T["text"]),
                                                   (0.75, 492, "Free, open-source dictation for Windows", 19, 500,
                                                    T["text2"])]:
            k = ramp(t, start, 0.55)
            with faded(p, k):
                write(p, QRectF(0, y - 40 + 10 * (1 - k), 1280, 80), text, px, weight, colour)


class Dictation(Scene):
    duration = 9.0
    caption = "Hold Ctrl+Win and speak"
    DOWN, UP, TYPED = 1.2, 5.0, 6.4  # the keys pressed, let go; the text typed
    chrome = staticmethod(draw_editor)

    def __init__(self):
        self.pill = PillTrack(1.25, 7.5, [(1.25, "recording", False, ""), (self.UP, "transcribing", True, ""),
                                          (self.TYPED, "typed", False, "")], [(1.45, 4.75)])

    def draw(self, p, t):
        typing = ramp(t, self.TYPED - 0.05, 0.7) if t >= self.TYPED - 0.05 else 0.0
        text = words_typed(DICTATED, typing)
        draw_text(p, text)
        draw_caret(p, text, len(text), t, steady=0 < typing < 1)
        pressed = ramp(t, self.DOWN, 0.12) * (1 - ramp(t, self.UP, 0.12))
        draw_keys(p, [("Ctrl", pressed), "+", ("Win", pressed)], ramp(t, 0.85, 0.3) * (1 - ramp(t, self.UP + 0.3, 0.35)))
        self.pill.draw(p, t)


class Transform(Scene):
    duration = 7.0
    caption = "Say “make it concise”"
    DOWN, UP, DONE = 1.35, 3.1, 4.35  # the keys pressed, let go; the text replaced
    chrome = staticmethod(draw_editor)

    def __init__(self):
        from sst.window import TRANSFORM_EXAMPLES
        self.before, self.after = TRANSFORM_EXAMPLES["concise"][0]  # a pair the app's own page shows, checked by its tests
        self.pill = PillTrack(1.4, 5.9, [(1.4, "recording", False, ""), (self.UP, "transforming", False, ""),
                                         (self.DONE, "transformed", False, "Transformed: Concise")], [(1.6, 2.75)])

    def draw(self, p, t):
        gone, came = ramp(t, self.DONE - 0.15, 0.2), ramp(t, self.DONE + 0.05, 0.3)  # one text out, then the other in
        if gone < 1:
            with faded(p, 1 - gone):
                draw_text(p, self.before, round(len(self.before) * ramp(t, 0.3, 0.8)))
                if t < 0.3:
                    draw_caret(p, self.before, len(self.before), t)
        if came > 0:
            with faded(p, came):
                draw_text(p, self.after, tint=1 - ramp(t, self.DONE + 0.5, 1.0))
                if t > self.DONE + 0.5:
                    draw_caret(p, self.after, len(self.after), t - self.DONE)
        pressed = ramp(t, self.DOWN, 0.12) * (1 - ramp(t, self.UP, 0.12))
        draw_keys(p, [("Ctrl", pressed), "+", ("Win", pressed)], ramp(t, 1.05, 0.3) * (1 - ramp(t, self.UP + 0.3, 0.35)))
        self.pill.draw(p, t)


class Translate(Scene):
    duration = 6.0
    caption = "Select text and press Ctrl+C twice to translate"
    OPEN, DONE = 1.95, 2.75  # the popup opens, translating; the translation is there
    chrome = staticmethod(draw_editor)

    def draw(self, p, t):
        draw_text(p, JAPANESE, round(len(JAPANESE) * ramp(t, 0.3, 0.7)))
        if t < 0.3:
            draw_caret(p, JAPANESE, len(JAPANESE), t)
        ctrl = ramp(t, 1.15, 0.1) * (1 - ramp(t, 2.25, 0.12))
        first = ramp(t, 1.38, 0.08) * (1 - ramp(t, 1.52, 0.08))
        second = ramp(t, 1.7, 0.08) * (1 - ramp(t, 1.84, 0.08))
        draw_keys(p, [("Ctrl", ctrl), "+", ("C", first), ",", ("C", second)],
                  ramp(t, 0.95, 0.3) * (1 - ramp(t, 2.55, 0.35)))
        shown = ramp(t, self.OPEN, 0.3)
        if shown > 0:
            busy, result = translate_popups()
            end = caret_at(JAPANESE, len(JAPANESE))
            x = min(end.x() + 12, 1280 - 460 - 24) - 24  # where the app puts it: by the selection, on the screen
            y = end.y() + LINE + 10 - 24 + 8 * (1 - shown)
            done = ramp(t, self.DONE, 0.18)
            with faded(p, shown):
                if done < 1:
                    with faded(p, 1 - done):
                        draw_image(p, busy[int(max(0.0, t - self.OPEN) / 0.35) % 3], x, y)
                if done > 0:
                    with faded(p, done):
                        draw_image(p, result, x, y)


class Live(Scene):
    duration = 6.0
    caption = "Live translation: meetings translated as people speak"
    CALL = QRectF(150, 24, 980, 552)
    TINTS = ("iris", "violet", "ok", "warn")

    def __init__(self):
        from sst.live.contracts import Kind, LiveEvent
        self.events = []
        for start, end, heard, said in LIVE_LINES:  # the words heard as they come, the translation close behind
            for k in range(1, 9):
                text = heard[:math.ceil(len(heard) * k / 8)]
                self.events.append((start + (end - start) * 0.7 * k / 8, LiveEvent(Kind.SOURCE, text)))
            words = said.split()
            for k in range(1, len(words) + 1):
                at = start + 0.35 + (end - start - 0.4) * k / len(words)
                self.events.append((at, LiveEvent(Kind.TRANSLATION, " ".join(words[:k]))))
            self.events.append((end, LiveEvent(Kind.LINE, said, source=heard)))
        self.events.sort(key=lambda e: e[0])
        self.bar, self.last, self.applied, self.image = None, 0.0, 0, None

    def bar_image(self, t: float) -> QImage:
        """The real translation bar (sst.live.captions.CaptionBar), fed the meeting's lines as they come."""
        from sst.live.captions import CaptionBar
        from sst.live.contracts import Kind, LiveConfig, LiveEvent
        if self.bar is None or t < self.last:
            self.bar = CaptionBar(LiveConfig(target="en", source="computer"))
            self.bar.show_event(LiveEvent(Kind.STATUS, "Listening"))
            self.applied, self.image = 0, None
        self.last = t
        while self.applied < len(self.events) and self.events[self.applied][0] <= t:
            self.bar.show_event(self.events[self.applied][1])
            self.applied, self.image = self.applied + 1, None
        if self.image is None:
            settle()
            self.image = self.bar.grab().toImage()
        return self.image

    def draw(self, p, t):
        self.draw_meeting(p, t)
        image = self.bar_image(t)
        dpr = image.devicePixelRatio()
        draw_image(p, image, 640 - image.width() / dpr / 2, 596 - image.height() / dpr)

    def draw_meeting(self, p: QPainter, t: float) -> None:
        """An online meeting, abstract: four people as soft shapes, the one speaking ringed in Mint."""
        r = self.CALL
        theme.outer_shadow(p, r, 14, [(0, 18, 44, theme.rgba(0, 0, 0, .5))], S)
        p.fillPath(theme.rounded(r, 14), QColor("#14161B"))
        p.setPen(QPen(theme.rgba(255, 255, 255, .07), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        write(p, QRectF(r.left() + 20, r.top(), 300, 40), "Weekly sync", 13, 500, T["text2"], Qt.AlignmentFlag.AlignVCenter)
        write(p, QRectF(r.right() - 220, r.top(), 200, 40), "4 people", 13, 400, T["text3"],
              Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        speaking = next((i for i, (start, end, *_) in enumerate(LIVE_LINES) if start - 0.1 <= t <= end), None)
        who = {0: 0, 1: 1, 2: 0}.get(speaking, -1)  # tiles the translation bar doesn't cover
        level = voice(t, [(s, e) for s, e, *_ in LIVE_LINES])
        gap, top = 12, r.top() + 40
        tw, th = (r.width() - 32 - gap) / 2, (r.height() - 40 - 16 - gap) / 2
        for i, tint in enumerate(self.TINTS):
            tile = QRectF(r.left() + 16 + (i % 2) * (tw + gap), top + (i // 2) * (th + gap), tw, th)
            colour = T[tint]
            g = QLinearGradient(tile.topLeft(), tile.bottomRight())
            g.setColorAt(0, blend(QColor("#1D2029"), colour, 0.10))
            g.setColorAt(1, QColor("#171920"))
            p.fillPath(theme.rounded(tile, 12), g)
            p.save()
            p.setClipPath(theme.rounded(tile, 12))
            c = tile.center()
            shape = QRadialGradient(QPointF(c.x(), c.y() - 10), 140)
            shape.setColorAt(0, alpha(blend(QColor("#2A2E3A"), colour, 0.32), 0.85))
            shape.setColorAt(1, alpha(blend(QColor("#2A2E3A"), colour, 0.18), 0.55))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(shape)
            p.drawEllipse(QPointF(c.x(), c.y() - 20), 28, 28)
            p.drawEllipse(QRectF(c.x() - 62, c.y() + 20, 124, 110))
            p.restore()
            if i == who:
                p.setPen(QPen(alpha(T["ok"], 0.55 + 0.45 * level), 2.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(tile.adjusted(1.25, 1.25, -1.25, -1.25), 11, 11)


class Setups(Scene):
    duration = 5.0
    caption = "Pick a setup. See what it costs."

    def draw(self, p, t):
        image = models_window()
        dpr = image.devicePixelRatio()
        w, h = image.width() / dpr, image.height() / dpr
        k = ramp(t, 0.4, 4.2)
        scale = mix(0.85, 1.1, k)
        fx, fy = mix(w / 2, 545, k), mix(h / 2, 290, k)  # from the whole window to the setups and their costs
        p.save()
        p.translate(640, mix(312, 331, k))
        p.scale(scale, scale)
        p.translate(-fx, -fy)
        rect = QRectF(0, 0, w, h)
        theme.outer_shadow(p, rect, 14, [(0, 18, 44, theme.rgba(0, 0, 0, .55)), (0, 2, 6, theme.rgba(0, 0, 0, .3))], S)
        p.setClipPath(theme.rounded(rect, 14))
        p.drawImage(rect, image)
        p.setClipping(False)
        p.setPen(QPen(theme.rgba(255, 255, 255, .08), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        p.restore()
        band = scrim()
        p.drawImage(QRectF(0, 720 - band.height() / S, 1280, band.height() / S), band)


class End(Scene):
    duration = 3.5

    def draw(self, p, t):
        with faded(p, ramp(t, 0.0, 0.6)):
            draw_logo(p, 640, 200, 128)
        k = ramp(t, 0.25, 0.55)
        with faded(p, k):
            label = "Download free: rflow-ai.vercel.app"
            width = QFontMetricsF(theme.font(24, 600)).horizontalAdvance(label) + 64
            button = QRectF(640 - width / 2, 330 + 8 * (1 - k), width, 60)
            theme.paint_surface(p, "primary", button, 16, popup=True, dpr=S)
            write(p, button, label, 24, 600, T["on_primary"])
        for start, y, text, px, colour, mono in [(0.45, 436, "github.com/karthi-ai-engineer/rflow-ai", 20, T["text2"], True),
                                                 (0.6, 478, "MIT License", 16, T["text3"], False)]:
            k = ramp(t, start, 0.55)
            with faded(p, k):
                write(p, QRectF(0, y - 20 + 8 * (1 - k), 1280, 40), text, px, 500, colour, mono=mono)


# ---------------------------------------------------------------- frames and files

def painter(image: QImage) -> QPainter:
    """A painter on a frame, in stage coordinates."""
    p = QPainter(image)
    for hint in (QPainter.RenderHint.Antialiasing, QPainter.RenderHint.TextAntialiasing,
                 QPainter.RenderHint.SmoothPixmapTransform):
        p.setRenderHint(hint)
    p.scale(S, S)
    return p


def paint(p: QPainter, scene: Scene, t: float, chrome: bool = True) -> None:
    if chrome and scene.chrome is not None:
        scene.chrome(p)
    scene.draw(p, t)
    draw_caption(p, scene.caption, t)


def render(scene: Scene, t: float) -> QImage:
    image = backdrop().copy()
    p = painter(image)
    paint(p, scene, t)
    p.end()
    return image


def frame(scenes: list[Scene], starts: list[float], g: float) -> QImage:
    """The video at g seconds: one scene; around the start of the next, the first fades out, then the next fades in
    (no text over text). What both show, the editor, stays."""
    for i in range(1, len(scenes)):
        a, b, at = scenes[i - 1], scenes[i], starts[i]
        if at - FADE / 2 <= g < at + FADE / 2:
            k = (g - at + FADE / 2) / FADE
            shared = a.chrome is not None and a.chrome is b.chrome
            image = backdrop().copy()
            p = painter(image)
            if shared:
                a.chrome(p)
            for scene, t, amount in ((a, g - starts[i - 1], 1 - ease(2 * k)), (b, max(0.0, g - at), ease(2 * k - 1))):
                if amount > 0:  # each scene faded as a whole, so its own layers don't show through each other
                    layer = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
                    layer.fill(Qt.GlobalColor.transparent)
                    q = painter(layer)
                    paint(q, scene, t, chrome=not shared)
                    q.end()
                    p.save()
                    p.resetTransform()
                    p.setOpacity(amount)
                    p.drawImage(0, 0, layer)
                    p.restore()
            p.end()
            return image
    i = max(k for k, start in enumerate(starts) if start <= g)
    return render(scenes[i], g - starts[i])


def pixels(image: QImage) -> np.ndarray:
    """The frame's pixels as an array (BGRA), a copy: the image may be gone once this returns."""
    rows = np.frombuffer(image.constBits(), np.uint8, count=image.sizeInBytes()).reshape(image.height(), -1)
    return rows[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def write_video(name: str, scenes: list[Scene]) -> float:
    starts = [sum(s.duration for s in scenes[:i]) for i in range(len(scenes))]
    total = starts[-1] + scenes[-1].duration
    path = OUT / name
    with av.open(str(path), "w", container_options={"movflags": "+faststart"}) as container:  # plays while it loads
        stream = container.add_stream("libx264", rate=FPS, options={
            "crf": "20", "preset": "slow", "tune": "animation",
            "x264-params": "colorprim=bt709:transfer=bt709:colormatrix=bt709"})  # HD's colours, said in the file
        stream.width, stream.height, stream.pix_fmt = W, H, "yuv420p"
        for n in range(round(total * FPS)):
            video = av.VideoFrame.from_ndarray(pixels(frame(scenes, starts, n / FPS)), format="bgra")
            container.mux(stream.encode(video.reformat(format="yuv420p", dst_colorspace=Colorspace.ITU709)))
        container.mux(stream.encode())
    print(f"Wrote {name}: {total:.1f} s, {W}x{H}, {FPS} fps, {path.stat().st_size / 1e6:.2f} MB")
    return total


def write_gif(name: str, scene: Scene) -> None:
    """One scene as a looping GIF, GIF_WIDTH wide."""
    size = (GIF_WIDTH, round(H * GIF_WIDTH / W))
    frames = []
    for n in range(round(scene.duration / GIF_STEP)):
        bgra = pixels(render(scene, n * GIF_STEP))
        frames.append(Image.fromarray(bgra[..., 2::-1].copy()).resize(size, Image.Resampling.LANCZOS))
    # One palette for every frame keeps the colours steady: the frames' own (from frames across the scene), plus the
    # lamps' colours and their edges, which are too few pixels to win a place on their own. Index 255 means "unchanged".
    sample = Image.new("RGB", (size[0], size[1] * 6))
    for i, f in enumerate(frames[:: max(1, len(frames) // 6)][:6]):
        sample.paste(f, (0, i * size[1]))
    lamps = [blend(POP["base"], T[name], k) for name in ("live", "violet", "ok", "iris", "selection") for k in (1, .6, .3)]
    colours = sample.quantize(colors=255 - len(lamps), method=Image.Quantize.MEDIANCUT).getpalette()[:3 * (255 - len(lamps))]
    colours += [v for c in lamps for v in (c.red(), c.green(), c.blue())]
    colours += [255, 0, 255] * (256 - len(colours) // 3)  # the rest, far from every colour in the frames
    palette = Image.new("P", (1, 1))
    palette.putpalette(colours)
    indexed = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    # Pixels that didn't change are left transparent: each frame stores only what moved.
    still = np.asarray(indexed[0]).copy()
    out = [indexed[0]]
    for f in indexed[1:]:
        now = np.asarray(f)
        image = Image.fromarray(np.where(now == still, 255, now).astype(np.uint8), "P")
        image.putpalette(colours)
        still = now
        out.append(image)
    out[0].save(OUT / name, save_all=True, append_images=out[1:], duration=round(GIF_STEP * 1000), loop=0,
                transparency=255, disposal=1, optimize=False)
    print(f"Wrote {name}: {len(out)} frames, {size[0]}x{size[1]}, {(OUT / name).stat().st_size / 1e6:.2f} MB")


def main() -> None:
    QApplication([])
    theme.load_fonts()
    site.parakeet.find_model = lambda: Path(".")  # as if Parakeet were downloaded: Rflow shows as ready
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        dictation = Dictation()
        write_video("demo.mp4", [Title(), dictation, Transform(), Translate(), Live(), Setups(), End()])
        write_gif("demo.gif", dictation)
    finally:
        shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    main()

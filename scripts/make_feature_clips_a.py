"""Render three short clips of Rflow's features for the website, each one feature and nothing else (no title, no end
card), seamless to loop (the last frame eases back to the first):

- site/media/clip-dictation.mp4: hold Ctrl+Win, speak, the sentence is typed into a Notes window;
- site/media/clip-formatting.mp4: what was said, then what Rflow types, run through the real formatting stage
  (sst.pipeline.formatting.Formatter, as the Formatting page does);
- site/media/clip-translate.mp4: Japanese selected, Ctrl+C twice, the real Translate popup; Copy, then Replace.

And a poster for each (site/img/clip-<name>-poster.png, 1280x720), a frame that shows the result. The pill, the popup,
the keycaps, the Notes window and the motion are scripts/make_demo_video.py's, imported (not copied): drawn off the
screen, so nothing appears, takes focus, presses keys or touches the clipboard. The clips are drawn on a smaller stage
than the tour (960x540 at 200%), so Rflow's parts stay readable where the website shows them small. Pillow (for the
posters, and imported by make_demo_video) is added only for this run:

  uv run --no-sync --with pillow python scripts/make_feature_clips_a.py [dictation formatting translate]
"""
import dataclasses
import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]  # this checkout's Rflow, whichever one is installed
import make_demo_video as demo  # noqa: E402  (sets Qt's platform and the example data folder before Qt starts)

os.environ["QT_SCALE_FACTOR"] = "3"  # Rflow's parts grabbed at 3x: still sharp when shown a little larger than life

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QFontMetricsF, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sst import theme  # noqa: E402

T, POP = demo.T, demo.POP
clamp, ramp, blend, alpha, faded, write = demo.clamp, demo.ramp, demo.blend, demo.alpha, demo.faded, demo.write
MEDIA, IMG = ROOT / "site" / "media", ROOT / "site" / "img"
POSTER = (1280, 720)
LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


def stage(editor: QRectF, text_px: float, line: float, **more) -> demo.Stage:
    """A 960x540 stage at 200%: the tour's parts, larger in the frame."""
    base = dict(pad=36, pill_y=0, pill_scale=1.0, keys_y=0, keys_scale=1.15, caption_y=0, caption_px=19, caption_weight=400)
    return demo.Stage(1920, 1080, 2.0, editor, text_px=text_px, line=line, **{**base, **more})


def blink(t: float, duration: float) -> float:
    """The caret's blink (about 1 s), timed so the clip's end meets its start."""
    period = duration / max(1, round(duration / 1.06))
    return clamp(abs(((t % period) / period) * 2 - 1) * 6 - 2.5)


def caret(p: QPainter, at: QPointF, on: float, height: float) -> None:
    if on > 0:
        p.fillRect(QRectF(at.x(), at.y() - 1, 2, height), alpha(T["iris"], on))


# ---------------------------------------------------------------- 1. dictation

class Dictation(demo.Scene):
    """Ctrl+Win held, the pill listening while the words are said (shown small below), let go, cleaned up, typed."""
    duration = 8.0
    chrome = staticmethod(demo.draw_editor)
    text = demo.DICTATED  # "Can you review the pull request before lunch and leave a comment if anything looks wrong?"
    KEYS, DOWN, UP, TYPED, HIDE, CLEAR = 0.45, 0.75, 4.15, 4.95, 6.25, 7.2
    SPEECH = (1.0, 3.95)
    STAGE = stage(QRectF(110, 40, 740, 214), 28, 44, pill_y=336, pill_scale=1.25, keys_y=420, caption_y=492)

    def __init__(self):
        super().__init__()
        self.pill = demo.PillTrack(self.DOWN + 0.05, self.HIDE, [(self.DOWN + 0.05, "recording", False, ""),
                                                                 (self.UP, "transcribing", True, ""),
                                                                 (self.TYPED, "typed", False, "")], [self.SPEECH])

    def draw(self, p, t):
        typing = ramp(t, self.TYPED - 0.05, 0.7) if t >= self.TYPED - 0.05 else 0.0
        text, gone = demo.words_typed(self.text, typing), ramp(t, self.CLEAR, 0.55)
        if gone < 1:
            with faded(p, 1 - gone):
                demo.draw_text(p, text)
        s = demo.STAGE
        if gone == 0:
            caret(p, demo.caret_at(text, len(text)), 1.0 if 0 < typing < 1 else blink(t, self.duration), s.text_px + 8)
        elif gone >= 1:
            caret(p, demo.text_at(), blink(t, self.duration), s.text_px + 8)
        pressed = ramp(t, self.DOWN, 0.12) * (1 - ramp(t, self.UP, 0.12))
        demo.draw_keys(p, [("Ctrl", pressed), "+", ("Win", pressed)],
                       ramp(t, self.KEYS, 0.3) * (1 - ramp(t, self.UP + 0.3, 0.35)))
        self.pill.draw(p, t)
        self.heard(p, t)

    def heard(self, p: QPainter, t: float) -> None:
        """What is being said, word by word, in a small line under the keys (anchored where the whole line will end up,
        so it doesn't shift as it grows)."""
        start, end = self.SPEECH
        shown = ramp(t, start - 0.1, 0.25) * (1 - ramp(t, self.TYPED + 0.25, 0.4))
        if shown <= 0:
            return
        s = demo.STAGE
        words = demo.words_typed(self.text, clamp((t - start) / (end - start - 0.15)))
        px = 18
        metrics = QFontMetricsF(theme.font(px))
        full = metrics.horizontalAdvance(f"“{self.text}”")
        x = s.sw / 2 - (full + 26) / 2
        with faded(p, shown):
            speaking = demo.voice(t, [self.SPEECH])
            demo.icon(p, "mic", blend(T["text3"], T["live"], clamp(speaking * 1.6) if t < end else 0), x, s.caption_y - 8)
            if words:
                write(p, QRectF(x + 26, s.caption_y - 16, full + 4, 32), f"“{words}" + ("”" if words == self.text else ""),
                      px, 400, T["text2"], LEFT)


# ---------------------------------------------------------------- 2. formatting

PHRASES = ["the budget is twenty five thousand dollars", "sales went up twenty five percent", "let's meet at three thirty pm",
           "the launch is on october first twenty twenty six", "send it to john dot smith at gmail dot com"]
SCREENS = [(0, 1, 2), (3, 4)]  # the phrases on each screen of the Notes window


@dataclasses.dataclass
class Typed:
    said: str
    text: str  # what the formatting stage types
    spans: list[tuple[int, int]]  # its changes in `text`
    heard: list[tuple[int, int]]  # and what they were in `said`
    start: float = 0.0


def run_formatter(said: str) -> Typed:
    """The phrase as the speech model writes it (a capital letter at the start), through Rflow's real formatting stage;
    its changes found in the result the way the Formatting page finds them."""
    from sst.pipeline.contracts import VoiceConfig
    from sst.pipeline.formatting import Formatter
    result = Formatter(VoiceConfig().formatting).format(said[0].upper() + said[1:])
    spans, heard, pos = [], [], 0
    for change in result.changes:
        at = result.text.find(change.replacement, pos)
        if at >= 0:
            spans.append((at, at + len(change.replacement)))
            pos = at + len(change.replacement)
            was = said.lower().find(change.original.lower())
            if was >= 0:
                heard.append((was, was + len(change.original)))
    assert spans, f"the formatter changed nothing in {said!r}"
    return Typed(said, result.text, spans, heard)


def pieces(text: str, spans: list[tuple[int, int]]) -> list[tuple[str, bool]]:
    out, pos = [], 0
    for a, b in spans:
        out += [(text[pos:a], False), (text[a:b], True)]
        pos = b
    return [piece for piece in out + [(text[pos:], False)] if piece[0]]


class Formatting(demo.Scene):
    """Each phrase said (the "You say" line under the window), then typed, the parts the formatter wrote highlighted."""
    chrome = staticmethod(demo.draw_editor)
    SAY, TYPE, STEP, HOLD, CLEAR = 0.95, 1.05, 1.68, 0.8, 0.38  # words said over SAY; typed at TYPE; next at STEP
    PX, GAP = 30, 58  # the typed lines' size and spacing
    CHIP = QRectF(150, 410, 660, 62)
    STAGE = stage(QRectF(100, 66, 760, 268), 30, 58, pad=40)

    def __init__(self):
        super().__init__()
        self.lines = [run_formatter(said) for said in PHRASES]
        t, self.clears = 0.3, []
        for screen in SCREENS:
            for i in screen:
                self.lines[i].start, t = t, t + self.STEP
            t += self.TYPE + 0.4 + self.HOLD - self.STEP  # the last one typed and highlighted, then read
            self.clears.append(t)
            t += self.CLEAR + 0.12
        self.duration = round(self.clears[-1] + self.CLEAR + 0.2, 2)

    def screen(self, t: float) -> int:
        return next((k for k, end in enumerate(self.clears) if t < end + self.CLEAR), len(SCREENS) - 1)

    def draw(self, p, t):
        self.draw_chip(p, t)
        k = self.screen(t)
        shown = 1 - ramp(t, self.clears[k], self.CLEAR)
        at = demo.text_at()
        last = (at, False)  # the caret: after the last line typed (steady while typing)
        with faded(p, shown):
            for row, i in enumerate(SCREENS[k]):
                line = self.lines[i]
                typing = ramp(t, line.start + self.TYPE, 0.4) if t >= line.start + self.TYPE else 0.0
                if typing > 0:
                    y = at.y() + row * self.GAP
                    n = len(demo.words_typed(line.text, typing))
                    end = self.typed_line(p, at.x(), y, line, n, ramp(t, line.start + self.TYPE + 0.4, 0.35))
                    last = (QPointF(end + 4, y), 0 < typing < 1)  # clear of the highlight
                self.you_say(p, t, line)
        height = QFontMetricsF(theme.font(self.PX)).height() + 4
        if shown >= 1:
            caret(p, last[0], 1.0 if last[1] else blink(t, self.duration), height)
        elif shown <= 0:  # cleared: back at the start
            caret(p, at, blink(t, self.duration), height)

    def typed_line(self, p: QPainter, x: float, y: float, line: Typed, n: int, lit: float) -> float:
        """The first n characters of the typed line; the formatter's parts in Iris on a soft Iris ground, `lit` 0..1."""
        h = QFontMetricsF(theme.font(self.PX)).height()
        for text, changed in pieces(line.text, line.spans):
            if n <= 0:
                break
            text, n = text[:n], n - len(text)
            weight = 500 if changed else 400
            w = QFontMetricsF(theme.font(self.PX, weight)).horizontalAdvance(text)
            if changed and lit > 0:
                p.fillPath(theme.rounded(QRectF(x - 4, y - 2, w + 8, h + 4), 8), alpha(T["iris"], 0.17 * lit))
            write(p, QRectF(x, y, w + 4, h), text, self.PX, weight, blend(T["text"], T["iris"], lit if changed else 0),
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            x += w
        return x

    def you_say(self, p: QPainter, t: float, line: Typed) -> None:
        """The words said, in the "You say" capsule under the window, word by word; when typed, the words the formatter
        rewrote turn Iris. Each phrase leaves as the next comes."""
        r = self.CHIP
        nxt = next((x.start for x in self.lines if x.start > line.start), None)
        here = ramp(t, line.start - 0.05, 0.2) * (1 - (ramp(t, nxt - 0.18, 0.16) if nxt is not None else 0.0))
        if here <= 0 or t < line.start - 0.05:
            return
        words = demo.words_typed(line.said, clamp((t - line.start) / (self.SAY - 0.2)))
        lit = ramp(t, line.start + self.TYPE + 0.4, 0.35)
        x = r.left() + 128
        with faded(p, here):
            px = 22
            for text, changed in pieces(words, [(a, min(b, len(words))) for a, b in line.heard if a < len(words)]):
                w = QFontMetricsF(theme.font(px)).horizontalAdvance(text)
                write(p, QRectF(x, r.top(), w + 4, r.height()), text, px, 400,
                      blend(POP["text"], T["iris"], 0.85 * lit if changed else 0), LEFT)
                x += w

    def draw_chip(self, p: QPainter, t: float) -> None:
        r = self.CHIP
        theme.paint_surface(p, "float", r, r.height() / 2, popup=True, dpr=demo.STAGE.s)
        speaking = 0.0
        for line in self.lines:
            if line.start <= t <= line.start + self.SAY:
                speaking = demo.voice(t, [(line.start, line.start + self.SAY - 0.1)])
        demo.icon(p, "mic", blend(POP["text3"], T["live"], clamp(speaking * 1.6)), r.left() + 22, r.center().y() - 8)
        write(p, QRectF(r.left() + 48, r.top(), 80, r.height()), "You say", 13, 600, POP["text3"], LEFT)
        p.fillRect(QRectF(r.left() + 116, r.top() + 18, 1, r.height() - 36), theme.rgba(255, 255, 255, .10))


# ---------------------------------------------------------------- 3. translate

@lru_cache(maxsize=1)
def translate_popups():
    """The real Translate popup: translating (its dots, three steps), the translation, and after Copy ("Copied")."""
    from sst.translate import Translation
    from sst.translateui import TranslatePopup
    popup = TranslatePopup()
    popup._choices = ["English", "Spanish", "Japanese"]
    popup.route.setText("Japanese →")
    popup.original.setText(popup.original.fontMetrics().elidedText(demo.JAPANESE, Qt.TextElideMode.ElideRight, popup.INNER))
    popup.busy("English")
    popup.dots.stop()  # its dots are set here, frame by frame
    busy = []
    for dots in (1, 2, 3):
        popup.waiting.setText("Translating into English" + "." * dots)
        demo.settle()
        busy.append(popup.grab().toImage())
    popup.show_result(Translation(demo.ENGLISH, "English", demo.JAPANESE, 0.9))
    demo.settle()
    result = popup.grab().toImage()
    popup.copied()  # the button only: the clipboard is the app's job, and stays untouched here
    popup.copied_timer.stop()
    demo.settle()
    return busy, result, popup.grab().toImage()


class Translate(demo.Scene):
    """Japanese selected; Ctrl+C, C; the popup by the selection, translating, then the English; C: Copied; Enter: the
    selection replaced with the English, which settles from Violet."""
    duration = 8.7
    chrome = staticmethod(demo.draw_editor)
    SELECT, KEYS, CTRL, FIRST, SECOND, KEYS_OFF = 0.35, 1.0, 1.2, 1.42, 1.72, 2.3
    OPEN, DONE = 1.95, 2.7  # the popup opens, translating; the translation is there
    COPY_KEYS, COPY, COPY_OFF = 3.45, 3.8, 4.2  # C pressed: Copied
    ENTER_KEYS, ENTER, ENTER_OFF = 4.85, 5.15, 5.65  # Enter: Replace
    BACK = 7.9  # the English out, the Japanese back, as at the start
    POPUP_SCALE = 1.1
    STAGE = stage(QRectF(70, 30, 820, 228), 24, 38, keys_y=470)

    def draw(self, p, t):
        replaced = ramp(t, self.ENTER + 0.05, 0.25)
        english = ramp(t, self.ENTER + 0.25, 0.35) * (1 - ramp(t, self.BACK, 0.3))
        back = ramp(t, self.BACK + 0.3, 0.3)
        s = demo.STAGE
        if replaced < 1:
            with faded(p, 1 - replaced):
                demo.draw_text(p, demo.JAPANESE, round(len(demo.JAPANESE) * ramp(t, self.SELECT, 0.7)))
        if english > 0:
            with faded(p, english):
                demo.draw_text(p, demo.ENGLISH, tint=1 - ramp(t, self.ENTER + 0.6, 1.0))
        if back > 0:
            with faded(p, back):
                demo.draw_text(p, demo.JAPANESE)
        on = blink(t, self.duration)
        if t < self.SELECT or back >= 1:
            caret(p, demo.caret_at(demo.JAPANESE, len(demo.JAPANESE)), on, s.text_px + 8)
        elif self.ENTER + 0.6 < t < self.BACK:
            caret(p, demo.caret_at(demo.ENGLISH, len(demo.ENGLISH)), on, s.text_px + 8)
        self.keys(p, t)
        self.popup(p, t)

    def keys(self, p: QPainter, t: float) -> None:
        def tap(at: float) -> float:
            return ramp(t, at, 0.08) * (1 - ramp(t, at + 0.16, 0.08))
        ctrl = ramp(t, self.CTRL, 0.1) * (1 - ramp(t, self.SECOND + 0.4, 0.12))
        rows = [([("Ctrl", ctrl), "+", ("C", tap(self.FIRST)), ",", ("C", tap(self.SECOND))], self.KEYS, self.KEYS_OFF),
                ([("C", tap(self.COPY))], self.COPY_KEYS, self.COPY_OFF), ([("Enter", tap(self.ENTER))], self.ENTER_KEYS,
                                                                           self.ENTER_OFF)]
        for keys, start, end in rows:
            demo.draw_keys(p, keys, ramp(t, start, 0.3) * (1 - ramp(t, end, 0.3)))

    def popup(self, p: QPainter, t: float) -> None:
        shown = ramp(t, self.OPEN, 0.3) * (1 - ramp(t, self.ENTER + 0.05, 0.25))
        if shown <= 0:
            return
        busy, result, copied = translate_popups()
        end = demo.caret_at(demo.JAPANESE, len(demo.JAPANESE))
        dpr, k = result.devicePixelRatio(), self.POPUP_SCALE
        width = result.width() / dpr * k
        x = clamp(end.x() - width / 2, 0, demo.STAGE.sw - width + 24 * k - 24)  # by the selection, on the screen
        y = end.y() + demo.STAGE.line + 8 - 24 * k + 8 * (1 - ramp(t, self.OPEN, 0.3))
        done, copy = ramp(t, self.DONE, 0.18), ramp(t, self.COPY + 0.04, 0.06)
        p.save()
        p.translate(x, y)
        p.scale(k, k)
        with faded(p, shown):
            for image, amount in ((busy[int(max(0.0, t - self.OPEN) / 0.35) % 3], 1 - done), (result, done * (1 - copy)),
                                  (copied, copy)):
                if amount > 0:
                    with faded(p, amount):
                        demo.draw_image(p, image, 0, 0)
        p.restore()


# ---------------------------------------------------------------- files

CLIPS = {"dictation": (Dictation, 5.9), "formatting": (Formatting, None), "translate": (Translate, 4.55)}


def poster(name: str, scene: demo.Scene, t: float) -> None:
    bgra = demo.pixels(demo.render(scene, t))
    image = Image.fromarray(bgra[..., 2::-1].copy()).resize(POSTER, Image.Resampling.LANCZOS)
    path = IMG / f"clip-{name}-poster.png"
    image.save(path, optimize=True)
    print(f"Wrote {path.name}: {POSTER[0]}x{POSTER[1]}, {path.stat().st_size / 1e3:.0f} kB")


def main() -> None:
    names = sys.argv[1:] or list(CLIPS)
    try:
        QApplication([])
        theme.load_fonts()
        MEDIA.mkdir(parents=True, exist_ok=True)
        demo.OUT = MEDIA  # make_demo_video's encoder (H.264, yuv420p, faststart, no sound), into the site's folder
        for name in names:
            kind, at = CLIPS[name]
            demo.use(kind.STAGE)
            scene = kind()
            demo.write_video(f"clip-{name}.mp4", [scene])
            if at is None:  # formatting: the first screen, all three lines typed and highlighted
                at = scene.clears[0] - 0.1
            poster(name, scene, at)
    finally:
        shutil.rmtree(demo.DATA, ignore_errors=True)


if __name__ == "__main__":
    main()

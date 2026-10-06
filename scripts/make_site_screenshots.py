"""Render the website's screenshots (site/img) from the real Rflow window, popups and pill, with generic example data.

Everything is drawn off the screen (nothing appears, nothing takes focus), at the screen's scaling, as if Parakeet were
downloaded. The popups and the pill are drawn without being opened, on a transparent background:

  uv run python scripts/make_site_screenshots.py
"""
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")  # the real fonts and scaling
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from sst.engines import parakeet

OUT = Path(__file__).resolve().parent.parent / "site" / "img"
EXAMPLES = [  # (minutes ago, text)
    (3, "Can you review the pull request before lunch and leave a comment if anything looks wrong?"),
    (30, "Thanks for the quick reply! I'll send you the updated draft this afternoon."),
    (54, "Let's move the stand-up to Thursday morning and share the notes in Slack."),
    (75, "Please book a room for six people on Friday at 2 o'clock."),
    (93, "Ship the release notes after the review is merged, then tag the build."),
    (107, "Remind Priya that the Q3 roadmap review moved to Monday."),
    (125, "Draft a short thank-you note to the design team for the new screens."),
    (60 * 24 + 5, "Let's move the stand-up to Thursday morning and share the notes in Slack."),
]


def preview():
    from sst import window as w
    from sst.gateway import GatewayConfig
    from sst.settings import Profiles, Settings, Stats
    now = datetime.now()
    history = [{"time": (now - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"), "text": t} for m, t in EXAMPLES]
    stats = Stats()
    for n, words in enumerate([412, 386, 520, 264, 598, 331, 190, 0, 455, 380, 610]):  # the last three weeks
        if words:
            stats.add(" ".join(["word"] * words), words / 2.3, date.today() - timedelta(days=n))
    stats.add(" ".join(["word"] * 9800), 9800 / 2.3, date.today() - timedelta(days=30))  # the month before
    settings = Settings(welcomed=True, cleanup=True, cleanup_model="gemini-3.5-flash-lite",
                        microphone="Microphone array",
                        vocabulary=["Rflow", "GitHub", "Kubernetes", "Priya", "Q3 roadmap", "Parakeet", "CodeQL",
                                    "Vercel", "Snapdragon"],
                        snippets=[{"cue": "my email", "text": "alex@example.com", "anywhere": False}])
    profiles = Profiles()
    profiles.current.name = "Alex"
    app = w.PreviewApp(settings=settings, history=history, stats=stats,
                       microphones=["Microphone array", "Headset (Bluetooth)"],
                       gateway=GatewayConfig("", "AIza-example-key-9f3c", "gemini"), profiles=profiles)
    app.add_sound_alike("post grass", "PostgreSQL")
    return app


def shot(name: str, page: str, theme_name: str = "dark", size=(1000, 680), setup=None, app=None) -> None:
    from sst import window as w
    window = w.MainWindow(app or preview())
    window.apply_theme(theme_name)
    window.resize(*size)
    window.set_status("Ready: hold Ctrl+Win · cleanup: gemini-3.5-flash-lite", True)
    window.show_page(page)
    if setup:
        setup(window)
    for _ in range(3):
        QApplication.processEvents()
    window.grab().save(str(OUT / name))


def popups(name: str) -> None:
    """The Translate popup and the Text Transform menu side by side, as they float over an app."""
    from sst.transformui import TransformMenu
    from sst.translate import Translation
    from sst.translateui import TranslatePopup
    popup = TranslatePopup()
    popup._choices = ["English", "Spanish", "Japanese"]
    popup.route.setText("Japanese →")
    popup.original.setText("来週の定例会議は木曜日の午後3時からに変更になりました。")
    popup.show_result(Translation("Next week's regular meeting has moved to Thursday at 3 PM. Please upload the materials "
                                  "to the shared folder by Wednesday.", "English", "", 0.9))
    menu = TransformMenu()
    menu.items = [("concise", "1", "Concise"), ("professional", "2", "Professional"), ("bullets", "3", "Bullet points"),
                  ("actions", "4", "Action items"), ("undo", "U", "Undo last change")]
    menu.source = "Selected text · 3 sentences"
    menu.resize(menu.WIDTH + 2 * menu.MARGIN, menu._height() + 2 * menu.MARGIN)
    left, right = popup.grab(), menu.grab()
    dpr = left.devicePixelRatio()  # sizes below in logical pixels; the image keeps the screen's scale
    width, height = (left.width() + right.width()) / dpr + 8, max(left.height(), right.height()) / dpr
    image = QImage(round(width * dpr), round(height * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.drawPixmap(0, 0, left)
    p.drawPixmap(round(left.width() / dpr + 8), 0, right)
    p.end()
    image.save(str(OUT / name))


def pills(name: str) -> None:
    """The pill as it goes: listening, cleaning up, typed."""
    from sst.app import Pill
    pill = Pill(level=lambda: 0.0)
    shots = []
    for state, ai, levels in [("recording", False, [0.2, 0.5, 0.8, 0.4, 0.9, 0.6, 0.3, 0.7, 1.0, 0.5, 0.35, 0.65, 0.85,
                                                     0.45, 0.25, 0.55]),
                              ("transcribing", True, []), ("typed", False, [])]:
        pill.state, pill.message, pill.ai, pill._started, pill._phase = state, "", ai, time.monotonic() - 7, 0.18
        pill._levels.extend(levels)
        pill.resize(pill._width() + 2 * pill.MARGIN, pill.HEIGHT + 2 * pill.MARGIN)
        shots.append(pill.grab())
    dpr = shots[0].devicePixelRatio()  # sizes below in logical pixels; the image keeps the screen's scale
    width = max(s.width() for s in shots) / dpr
    height = sum(s.height() for s in shots) / dpr - 2 * 24
    image = QImage(round(width * dpr), round(height * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    y = 0.0
    for s in shots:
        p.drawPixmap(round((width - s.width() / dpr) / 2), round(y), s)
        y += s.height() / dpr - 24
    p.end()
    image.save(str(OUT / name))


def main() -> None:
    QApplication([])
    parakeet.find_model = lambda: Path(".")  # as if Parakeet were downloaded: the screenshots show Rflow ready
    OUT.mkdir(parents=True, exist_ok=True)
    shot("app.png", "home")
    shot("app-light.png", "home", "light")
    shot("models.png", "models")
    shot("tools.png", "tools")
    shot("words.png", "dictionary")
    app = preview()
    app.settings.welcomed = False
    shot("welcome.png", "welcome", app=app)
    popups("popups.png")
    pills("pill.png")
    print("Wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))


if __name__ == "__main__":
    main()

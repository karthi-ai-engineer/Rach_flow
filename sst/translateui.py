"""Translate (the owner's idea of 2026-10-02, like DeepL): select text in any app, press Ctrl+C twice, read it translated.

  Ctrl+C+C (the shortcut)   the app copies as usual; Rflow reads that copy, presses nothing, and opens a small popup
                            at the pointer: "Japanese → English" at the top, the translation below (sst.translate)
  a language at the top     translates again into it, and remembers it; More shows every language
  Copy                      the translation on the clipboard ("✓ Copied"; the popup stays)
  Replace                   the translation in place of the selected text (its window brought back first, like Text
                            Transform; if the text isn't there any more, the translation goes on the clipboard)
  Try again                 after an error, which is said in plain words (the provider's own below, small)
  Esc, ✕, a click outside   closes the popup (Esc in the language list goes back), as does going to another app

The popup never takes the keyboard focus, so the app keeps its selection; Esc is taken from the keyboard hook while the
popup is open. Text already in the chosen language goes to the second language, or else to Windows' own language (or
English), never into the same language. Another shortcut (not a double copy) copies the selection itself, with
Ctrl+Insert, never in a terminal.
"""
import dataclasses
import logging
import os
import threading
import time

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sst import translate
from sst.hotkey import VK_ESCAPE, HotkeyListener, parse_hotkey
from sst.transformui import TERMINALS, place
from sst.translate import LANGUAGES, MAX_CHARS, Translation, choose_target, detect, fallback_second

COPY_WAIT = 0.4  # seconds the app gets to copy after the second Ctrl+C, before the clipboard is read as it is

log = logging.getLogger(__name__)


def _pointer() -> QPoint:
    return QCursor.pos()  # apart, so the tests can say where the pointer is

_STYLE = """
#popup { background: rgba(28, 28, 36, 250); border: 1px solid rgba(255, 255, 255, 36); border-radius: 12px; }
QLabel { color: #F5F5FA; background: transparent; }
QLabel#muted { color: #9A9AAD; }
QLabel#route { color: #C9C9D6; font-weight: 600; }
QLabel#warning { color: #FBBF24; }
QTextBrowser { color: #F5F5FA; background: transparent; border: none; font-size: 11pt;
               selection-background-color: #3B82F6; selection-color: #FFFFFF; }
QPushButton { color: #FFFFFF; background: rgba(255, 255, 255, 22); border: none; border-radius: 6px; padding: 5px 14px; }
QPushButton:hover { background: rgba(255, 255, 255, 40); }
QPushButton#primary { background: #3B82F6; }
QPushButton#primary:hover { background: #2563EB; }
QPushButton:disabled { color: #9A9AAD; background: rgba(255, 255, 255, 10); }
QPushButton#chip { color: #D8D8E4; background: rgba(255, 255, 255, 14); border-radius: 11px; padding: 3px 11px; }
QPushButton#chip:hover { background: rgba(255, 255, 255, 34); }
QPushButton#chip:checked { color: #FFFFFF; background: #3B82F6; }
QPushButton#language { color: #E8E8F0; background: transparent; text-align: left; padding: 5px 8px; }
QPushButton#language:hover { background: rgba(255, 255, 255, 26); }
QPushButton#language:checked { background: rgba(59, 130, 246, 170); }
QToolButton { color: #A8A8B8; background: transparent; border: none; font-size: 12pt; }
QToolButton:hover { color: #FFFFFF; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 60); border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


KEY_REFUSED = "The provider refused the API key. Check it on the AI cleanup page."
UNREACHABLE = "Rflow can't reach the provider. Check the internet connection and the AI cleanup settings, then try again."


def explain(error: str) -> str:
    """What a failed translation means for the user, from the provider's error (shown below it, smaller)."""
    e = error.casefold()
    if any(s in e for s in ("401", "403", "unauthorized", "forbidden", "api key", "api_key", "permission")):
        return KEY_REFUSED
    if any(s in e for s in ("429", "quota", "rate limit", "exhausted", "too many requests")):
        return "The provider's limit is reached (quota, or too many requests). Try again in a minute."
    if any(s in e for s in ("timed out", "timeout")):
        return "The provider took too long to answer. Try again."
    if any(s in e for s in ("getaddrinfo", "connection", "unreachable", "network", "11001", "10061", "no address")):
        return UNREACHABLE
    if "no translation" in e:
        return "The model gave no translation. Try again, or choose another model in AI cleanup."
    return "Couldn't translate this. Try again."


class TranslatePopup(QWidget):
    """The panel at the pointer. The header says from what into what ("Japanese → English"), with the languages used
    most as one-click buttons and More for all of them; then the translation, as tall as it needs (it scrolls past a
    limit); then Copy and Replace, or Retry after an error. It never takes the keyboard focus (the app keeps its
    selection for Replace), stays on the screen as it grows, and closes on Esc, ✕ or a click outside it."""

    language = Signal(str)  # a language picked in the header or the full list
    copy = Signal()
    replace = Signal()
    retry = Signal()
    setup = Signal()  # open the AI cleanup page: no model yet, or its key or connection failed
    closed = Signal()

    WIDTH = 460
    INNER = WIDTH - 34  # inside the frame's margins and border
    RESULT_MAX = 360  # at most, and at most 40% of the screen; then the translation scrolls
    COLUMNS = 3  # of the full language list

    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(_STYLE)
        self.setFixedWidth(self.WIDTH)
        self.anchor, self._above, self._view, self._back, self.target = QPoint(), False, "busy", "busy", ""
        self._choices: list[str] = []
        self._dot = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setObjectName("popup")
        outer.addWidget(frame)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)

        self.head = QHBoxLayout()
        self.head.setSpacing(6)
        self.route = QLabel("Into")
        self.route.setObjectName("route")
        self.head.addWidget(self.route)
        self.chips: list[QPushButton] = []
        self.chip_row = QHBoxLayout()
        self.chip_row.setSpacing(6)
        self.head.addLayout(self.chip_row)
        self.more = self._button("More ▾", "chip", self.toggle_languages, "All languages")
        self.more.setCheckable(True)
        self.head.addWidget(self.more)
        self.head.addStretch()
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setToolTip("Close (Esc)")
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.clicked.connect(self.close_popup)
        self.head.addWidget(self.close_button)
        layout.addLayout(self.head)

        self.original = QLabel()  # one line: the text is still selected in the app, this only says which it was
        self.original.setObjectName("muted")
        layout.addWidget(self.original)
        self.waiting = QLabel()
        self.waiting.setObjectName("muted")
        layout.addWidget(self.waiting)
        self.dots = QTimer(self)
        self.dots.setInterval(350)
        self.dots.timeout.connect(self._tick)
        self.result = QTextBrowser()
        self.result.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.result.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.result)
        self.grid_box = QWidget()
        self.grid = QGridLayout(self.grid_box)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(2)
        self.all_languages: dict[str, QPushButton] = {}
        for i, name in enumerate(LANGUAGES):
            b = self._button(name, "language", lambda _=False, n=name: self._pick(n))
            b.setCheckable(True)
            self.all_languages[name] = b
            self.grid.addWidget(b, i // self.COLUMNS, i % self.COLUMNS)
        layout.addWidget(self.grid_box)
        self.status = QLabel()  # a warning about the translation, or what went wrong
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.detail = QLabel()  # the provider's own words for an error, small
        self.detail.setObjectName("muted")
        layout.addWidget(self.detail)

        self.buttons = QWidget()
        buttons = QHBoxLayout(self.buttons)
        buttons.setContentsMargins(0, 2, 0, 0)
        buttons.addStretch()
        self.copy_button = self._button("Copy", "", self.copy.emit, "Copy the translation")
        self.replace_button = self._button("Replace", "primary", self.replace.emit,
                                           "Put the translation in place of the selected text")
        self.retry_button = self._button("Try again", "primary", self.retry.emit)
        self.setup_button = self._button("Set up AI cleanup", "", self.setup.emit, "Choose the AI model Translate uses")
        self._offer_setup = False
        for b in (self.setup_button, self.copy_button, self.replace_button, self.retry_button):
            buttons.addWidget(b)
        layout.addWidget(self.buttons)
        self.copied_timer = QTimer(self)
        self.copied_timer.setSingleShot(True)
        self.copied_timer.setInterval(1500)
        self.copied_timer.timeout.connect(self._copy_done)

    def _button(self, label: str, name: str, on_click, tip: str = "") -> QPushButton:
        b = QPushButton(label)
        if name:
            b.setObjectName(name)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(on_click)
        return b

    # -- what it shows

    def open_at(self, pos: QPoint, source: str, target: str, choices: list[str] | None = None,
                source_language: str = "") -> None:
        self.anchor = pos
        area = self._area()
        room_below = area.bottom() - pos.y() - 18
        self._above = room_below < 280 and pos.y() - area.top() > room_below  # little room below: open upwards
        shown = " ".join(source.split())
        self.original.setText(self.original.fontMetrics().elidedText(shown, Qt.TextElideMode.ElideRight, self.INNER))
        self.original.setToolTip(shown[:600])
        self.route.setText(f"{source_language} →" if source_language else "Into")
        self._choices = [c for c in (choices or [target]) if c in LANGUAGES]
        self.busy(target)
        self.show()
        try:
            from sst.app import _no_activate  # clicks never take the app's focus: its selection stays for Replace
            _no_activate(int(self.winId()))
        except Exception:  # the off-screen test platform has no real window
            pass

    def busy(self, target: str) -> None:
        self.target = target
        self._set_chips(target)
        self._dot = 0
        self.waiting.setText(f"Translating into {target}")
        self.dots.start()
        self.result.setPlainText("")
        self._show("busy")

    def show_result(self, translation: Translation, model: str = "") -> None:
        self.target = translation.target
        self._set_chips(translation.target)
        self.result.setPlainText(translation.text)
        self.result.setToolTip(f"{translation.target}, by {model} in {translation.seconds:.1f} s" if model else "")
        self._say("\n".join(translation.warnings), "warning")
        self.detail.setText("")
        self._show("result")

    def show_error(self, message: str, detail: str = "", setup: bool = False) -> None:
        """What went wrong, Try again, and with `setup` the way to the AI cleanup page (a key or connection problem)."""
        self._say(message, "warning")
        detail = " ".join(detail.split())
        self.detail.setText(self.detail.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight, self.INNER))
        self.detail.setToolTip(detail)
        self._offer_setup = setup
        self._show("error")

    def show_setup(self) -> None:
        """No AI model is connected yet: say so here, at the text, with the way to set one up (not a passing notice)."""
        self.dots.stop()
        self._say("Translate needs an AI model. Choose a provider and a model in AI cleanup; Translate uses the same.",
                  "warning")
        self.detail.setText("")
        self._show("setup")

    def toggle_languages(self) -> None:
        self._show("languages" if self._view != "languages" else self._back)

    def escape(self) -> None:
        """Esc: from the full language list back to the translation; else the popup closes."""
        if self._view == "languages":
            self._show(self._back)
        else:
            self.close_popup()

    def copied(self) -> None:
        self.copy_button.setText("✓ Copied")
        self.copied_timer.start()

    def close_popup(self) -> None:
        self.dots.stop()
        if self.isVisible():
            self.hide()
            self.closed.emit()

    # -- inside

    def _pick(self, language: str) -> None:
        self.language.emit(language)

    def _set_chips(self, current: str) -> None:
        """The languages used most, as many as fit beside "More", the current one always among them (and lit)."""
        while self.chip_row.count():
            chip = self.chip_row.takeAt(0).widget()
            chip.hide()
            chip.deleteLater()
        metrics = self.fontMetrics()
        room = self.INNER - metrics.horizontalAdvance(self.route.text()) - metrics.horizontalAdvance("More ▾") - 110
        names = list(dict.fromkeys(self._choices))
        shown: list[str] = []
        for name in names:
            width = metrics.horizontalAdvance(name) + 30
            if len(shown) == 3 or width > room:
                break
            shown.append(name)
            room -= width
        if current not in shown:
            shown = (shown[:-1] if len(shown) == 3 or not shown else shown) + [current]
        self.chips = []
        for name in shown:
            chip = self._button(name, "chip", lambda _=False, n=name: self._pick(n), f"Translate into {name}")
            chip.setCheckable(True)
            chip.setChecked(name == current)
            self.chip_row.addWidget(chip)
            self.chips.append(chip)
        for name, b in self.all_languages.items():
            b.setChecked(name == current)

    def _say(self, message: str, name: str) -> None:
        self.status.setObjectName(name)
        self.status.setStyleSheet("")  # the object name changed: the style sheet applies again
        self.status.setText(message)

    def _show(self, view: str) -> None:
        if view != "languages":
            self._back = view
        self._view = view
        if view != "busy":
            self.dots.stop()
        self.more.setChecked(view == "languages")
        self.waiting.setVisible(view == "busy")
        self.result.setVisible(view == "result")
        self.grid_box.setVisible(view == "languages")
        self.status.setVisible(view in ("result", "error", "setup") and bool(self.status.text()))
        self.detail.setVisible(view == "error" and bool(self.detail.text()))
        self.buttons.setVisible(view in ("result", "error", "setup"))
        self.copy_button.setVisible(view == "result")
        self.replace_button.setVisible(view == "result")
        self.retry_button.setVisible(view == "error")
        self.setup_button.setVisible(view == "setup" or (view == "error" and self._offer_setup))
        self.setup_button.setObjectName("primary" if view == "setup" else "")
        self.setup_button.setStyleSheet("")  # the object name may have changed
        self._fit()

    def _area(self):
        screen = QGuiApplication.screenAt(self.anchor) or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _fit(self) -> None:
        """Every part as tall as its text (so nothing overlaps), the translation at most RESULT_MAX or 40% of the
        screen; then the popup is placed again, so a grown popup stays on the screen."""
        area = self._area()
        if not self.result.isHidden():
            self.result.ensurePolished()  # the style sheet's font, which the height depends on
            document = self.result.document().clone()  # measured apart: the box lays out its own at its current width,
            document.setDefaultFont(self.result.font())  # which isn't the final one yet (it measured too few lines)
            document.setTextWidth(self.INNER - 2 * self.result.frameWidth())
            height = document.size().height() + 2 * self.result.frameWidth() + 6
            self.result.setFixedHeight(int(min(max(height, 30), self.RESULT_MAX, 0.4 * area.height())))
        if not self.status.isHidden():
            self.status.setFixedHeight(self.status.heightForWidth(self.INNER))  # wrapped lines, measured, not guessed
        self.layout().activate()
        self.adjustSize()
        x = min(max(area.left(), self.anchor.x() + 12), area.right() - self.width())
        y = self.anchor.y() - 10 - self.height() if self._above else self.anchor.y() + 18
        self.move(x, min(max(area.top(), y), area.bottom() - self.height()))

    def _tick(self) -> None:
        self._dot = self._dot % 3 + 1
        self.waiting.setText(f"Translating into {self.target}" + "." * self._dot)

    def _copy_done(self) -> None:
        self.copy_button.setText("Copy")


class TranslateController(QObject):
    """The shortcut, the copied text, the popup and the translation. `app` is the TrayApp (or a stand-in in the tests):
    settings, `translate_ready()`, `run_translation(text, target, second) -> Translation`, `transform_model()`,
    `say(state, message)` and `apply_settings(settings)`. `access` is sst.textaccess (fakes in the tests)."""

    _got = Signal(object)  # (text, window) or why there is none (str), from the reading thread
    _done = Signal(object)  # (request number, Translation or error str), from the translating thread
    _placed = Signal(str)  # what happened to Replace, from the pasting thread

    def __init__(self, app, access=None, listener_factory=HotkeyListener):
        super().__init__()
        if access is None:
            from sst import textaccess as access
        self.app, self.access, self._listener_factory = app, access, listener_factory
        self.listener: HotkeyListener | None = None
        self.double_copy = False  # the shortcut is a double copy: the app copies, Rflow reads
        self.popup = TranslatePopup()
        self.popup.language.connect(self._pick)
        self.popup.copy.connect(self._copy)
        self.popup.replace.connect(self._replace)
        self.popup.retry.connect(self._retry)
        self.popup.setup.connect(self._setup)
        self.popup.closed.connect(self._closed)
        self._held = True  # a mouse button was down at the last look: only a new click outside closes the popup
        self._asked = ("", "")  # (target, second) of the last translation, for Retry
        self._got.connect(self._show)
        self._done.connect(self._finish)
        self._placed.connect(self._after_replace)
        self.pump = QTimer(self)
        self.pump.setInterval(15)
        self.pump.timeout.connect(self._pump)
        self.reading = False  # the copied text is being read
        self.request = 0  # each translation's number: an answer to an older one is dropped
        self.source: tuple[str, int] | None = None  # (the text, its window) the popup is for
        self.result: Translation | None = None

    def start(self, shortcut: str) -> None:
        """Listen for the shortcut ("" = Translate off)."""
        self.stop()
        if not shortcut:
            return
        try:
            hotkey = parse_hotkey(shortcut)
            self.listener = self._listener_factory(hotkey)
            self.listener.start()
        except (ValueError, OSError) as e:
            log.warning("Translate's shortcut %r doesn't work: %s", shortcut, e)
            self.listener = None
            return
        self.double_copy = hotkey.double and hotkey.key is not None
        self.pump.start()
        log.info("Translate on %s", shortcut)

    def stop(self) -> None:
        self.pump.stop()
        self.popup.close_popup()
        if self.listener is not None:
            self.listener.stop()
            self.listener = None

    @property
    def busy(self) -> bool:
        return self.reading

    def _pump(self) -> None:
        listener = self.listener
        if listener is not None:
            while not listener.events.empty():
                event, _ = listener.events.get_nowait()
                if event == "release":
                    self.trigger()
                elif event == f"key:{VK_ESCAPE}":
                    self.popup.escape()
        if self.popup.isVisible() and self.source:
            front = self.access.foreground_window()
            if front not in (self.source[1], 0) and self.access.window_process(front) != os.getpid():
                self.popup.close_popup()  # the user went to another app: the popup was for the text left behind
                # (Rflow's own windows don't count)
            held = bool(getattr(self.access, "mouse_down", lambda: False)())
            if held and not self._held and not self.popup.frameGeometry().contains(_pointer()):
                self.popup.close_popup()  # a click outside it, e.g. back in the text: like any popup, it goes
            self._held = held

    def trigger(self) -> None:
        if self.reading:
            return
        self.reading = True  # without an AI model the popup still opens, at the text, and says how to set one up
        hwnd, before = self.access.foreground_window(), self.access.clipboard_sequence()
        threading.Thread(target=self._read, args=(hwnd, before), name="translate-read", daemon=True).start()

    def _read(self, hwnd: int, before: int) -> None:
        """The text: what the app copied for the double Ctrl+C (read as soon as it lands, before any clipboard tool
        rewrites it), or the selection copied by Rflow for another shortcut."""
        try:
            if self.double_copy:
                end = time.monotonic() + COPY_WAIT
                while self.access.clipboard_sequence() == before and time.monotonic() < end:
                    time.sleep(0.005)
                text = self.access.clipboard_text()
            elif self.access.window_class(hwnd) in TERMINALS:
                text = None
                self._got.emit("In a terminal, select the text and press Ctrl+C twice.")
                return
            else:
                text = self.access.copy_selection()
        except Exception as e:  # a closed window, a busy clipboard: say so, never leave Translate stuck
            log.exception("Reading the text to translate failed")
            self._got.emit(f"Couldn't read the text ({e})")
            return
        self._got.emit((text, hwnd) if text and text.strip() else "Select some text first, then try again.")

    def _show(self, payload) -> None:
        self.reading = False
        if isinstance(payload, str):
            self.app.say("warning", payload)
            return
        text, hwnd = payload
        if len(text) > MAX_CHARS:
            self.app.say("warning", f"That's {len(text):,} characters: Translate takes up to {MAX_CHARS:,} at a time.")
            return
        self.source, self.result = (text, hwnd), None
        s, second = self.app.settings, self._second()
        language = detect(text)
        self._held = True
        self.popup.open_at(_pointer(), text, choose_target(text, s.translate_to, second),
                           self._choices(language, second), language)
        if self.listener is not None:
            self.listener.capture(frozenset({VK_ESCAPE}))
        if not self.app.translate_ready():
            self.popup.show_setup()
            return
        self._translate(s.translate_to, second)

    def _second(self) -> str:
        """Where text already in the chosen language goes: the user's second language, else Windows' own language
        (or English), so English text is never "translated" into English."""
        s = self.app.settings
        return s.translate_second or fallback_second(s.translate_to, translate.system_language())

    def _choices(self, source_language: str, second: str) -> list[str]:
        """The popup's one-click languages: the chosen ones, Windows' language, English, then the rest; never the
        language the text is already in."""
        s = self.app.settings
        names = [s.translate_to, second, translate.system_language(), "English", *LANGUAGES]
        return [n for n in dict.fromkeys(names) if n in LANGUAGES and not (source_language
                                                                            and n.startswith(source_language))]

    def _translate(self, target: str, second: str) -> None:
        self.request += 1
        request, (text, _) = self.request, self.source
        self._asked = (target, second)
        self.result = None
        self.popup.busy(choose_target(text, target, second))

        def work() -> None:
            try:
                self._done.emit((request, self.app.run_translation(text, target, second)))
            except Exception as e:  # no answer, no key, a refusal: shown in the popup
                log.warning("Translate failed: %s", e)
                self._done.emit((request, str(e) or type(e).__name__))
        threading.Thread(target=work, name="translate", daemon=True).start()

    def _finish(self, payload) -> None:
        request, result = payload
        if request != self.request or not self.popup.isVisible():
            return  # a newer translation was asked for, or the popup was closed
        if isinstance(result, str):
            message = explain(result)
            self.popup.show_error(message, result, setup=message in (KEY_REFUSED, UNREACHABLE))
            return
        self.result = result
        self.popup.show_result(result, self.app.transform_model())

    def _retry(self) -> None:
        if self.source is not None:
            self._translate(*self._asked)

    def _setup(self) -> None:
        self.popup.close_popup()
        open_window = getattr(self.app, "open_window", None)
        if open_window is not None:
            open_window("cleanup")

    def _pick(self, language: str) -> None:
        """A language picked in the popup: translate into it (exactly that one), and remember it."""
        if self.source is None:
            return
        if language != self.app.settings.translate_to:
            self.app.apply_settings(dataclasses.replace(self.app.settings, translate_to=language))
        self._translate(language, "")

    def _copy(self) -> None:
        if self.result is None:
            return
        try:
            self.access.set_clipboard(self.result.text)
            self.popup.copied()  # it stays open: the button says so, and a click outside closes it
        except OSError as e:
            self.app.say("warning", f"Couldn't copy the translation ({e})")

    def _replace(self) -> None:
        if self.result is None or self.source is None:
            return
        text, hwnd = self.source
        translation = self.result.text
        self.popup.close_popup()
        self.reading = True  # the shortcut waits until the paste is done

        def work() -> None:
            try:
                if place(self.access, text, hwnd):
                    self.access.paste_rich(translation)
                    self._placed.emit("")
                else:  # never paste over something else
                    self.access.set_clipboard(translation)
                    self._placed.emit("Your text wasn't where it was any more, so the translation is on the clipboard: "
                                      "press Ctrl+V.")
            except Exception as e:
                log.exception("Pasting the translation failed")
                self._placed.emit(f"Couldn't type the translation ({e})")
        threading.Thread(target=work, name="translate-paste", daemon=True).start()

    def _after_replace(self, message: str) -> None:
        self.reading = False
        if message:
            self.app.say("warning", message)
        else:
            self.app.say("transformed", "Translated")

    def _closed(self) -> None:
        if self.listener is not None:
            self.listener.capture(None)

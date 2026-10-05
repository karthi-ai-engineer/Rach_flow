"""The Rflow window: a small app like Wispr Flow's, next to the tray icon, in the "Obsidian Signal" design (Rflow UI 2.0;
sst.theme draws it, sst.ui has its widgets, docs/design/rflow-ui.html is the design).

Five sections in the sidebar:
  Home          the voice orb and how to dictate, a stats strip, the dictations (searchable, copy and correct)
  Words         Your words (the dictionary, sound-alikes, suggestions) and Snippets
  Tools         Text Transform and Translate, with a box to try them; their details one level down
  AI & models   how Rflow hears you and the AI connection; Speech models and AI connection one level down
  Settings      the dictation key and the everyday switches; Advanced has the voice pipeline, the Reading test and
                Profiles
A first-run welcome in three steps sets up how Rflow hears you, a first dictation and an AI connection. It is native
Qt, following Windows' light or dark mode (an embedded browser would add ~150 MB for the same look).

The window keeps no state of its own. It reads and changes everything through `app`: the TrayApp (sst/app.py), which
applies a change at once (hotkey, microphone, cleanup model...), or PreviewApp below for the self-test, the tests and
the website's screenshots.
"""
import dataclasses
import html
import logging
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, QSortFilterProxyModel, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QComboBox,
    QCompleter,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from sst import RECORDINGS_DIR, __version__, bench, theme
from sst.audio import LevelMeter, Take, call_quality, save_wav
from sst.commands import DEFAULT_PHRASES, UNDO, normalize, parse_phrases, phrases_for
from sst.engines import DEFAULT_MODEL, SPEECH_MODELS, WHERE, usable
from sst.engines.cloud import CLOUD, SPEECH
from sst.engines.whisper import LANGUAGES
from sst.gateway import PROVIDERS, GatewayConfig, Polisher
from sst.hotkey import parse_hotkey
from sst.pipeline.dictionary import speech_hints
from sst.scan import Computer
from sst.settings import (
    Profiles,
    Settings,
    Stats,
    can_start_with_windows,
    set_start_with_windows,
    starts_with_windows,
)
from sst.snippets import MAX_CUE_WORDS as SNIPPET_WORDS
from sst.snippets import Snippet
from sst.snippets import alone as snippet_alone
from sst.snippets import compact as compact_cue
from sst.snippets import expand as expand_snippets
from sst.snippets import load as load_snippets
from sst.snippets import protect as protect_snippets
from sst.theme import font, surface, tok
from sst.transform import TRANSFORMS
from sst.translate import LANGUAGES as TRANSLATE_LANGUAGES
from sst.translate import fallback_second, system_language
from sst.ui import (
    Button,
    Card,
    Host,
    IconButton,
    Lamp,
    Mark,
    Meter,
    Orb,
    Segmented,
    Toast,
    Toggle,
    WaveProgress,
    button,
    card,
    divider,
    icon_button,
    keycap,
    keys,
    label,
    lamp_row,
    set_tone,
    setting_row,
)

APP_NAME = "Rflow"
ICON_FILE = Path(__file__).parent / "static" / "sst.ico"
UI_IMAGES = Path(__file__).parent / "static" / "ui"  # drawn by scripts/make_ui_images.py
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "sst" / "logs"
WEBSITE = "https://rachflow.vercel.app"
REPO = "https://github.com/karthi-ai-engineer/Rach_flow"
HOTKEY_CHOICES = [("Ctrl+Win (like Wispr Flow)", "ctrl+win"), ("Menu key", "menu"), ("Ctrl+Alt+D", "ctrl+alt+d")]
# Text Transform's menu shortcut: not Ctrl+Win+... (that starts a dictation) and not a plain Ctrl+letter (apps use those).
# A double tap of Ctrl is the easiest; PowerToys' "Find My Mouse" uses a double Ctrl too (Rflow's still works with it).
# Translate's shortcut: a double copy (the app copies; Rflow reads it), or a shortcut after which Rflow copies.
TRANSLATE_SHORTCUTS = [("Ctrl+C+C (press Ctrl+C twice)", "ctrl+c+c"), ("Ctrl+Alt+L", "ctrl+alt+l"), ("Off", "")]
TRANSFORM_HOTKEYS = [("Double-tap Ctrl", "double ctrl"), ("Ctrl+Alt+T", "ctrl+alt+t"), ("F8", "f8"), ("Off", "")]
WHERE_LABELS = {"local": "On this PC", "cloud": "In the cloud", "server": "Your own server"}
COMPACT_WIDTH = 900  # below it, the sidebar becomes a rail of icons with their names under them

log = logging.getLogger("sst.window")


def dark_mode() -> bool:
    return theme.dark_mode()


def stylesheet(name: str) -> str:
    """The style sheet of a theme ("dark" Obsidian, "light" Porcelain)."""
    theme.load_fonts()
    return theme.stylesheet(name)


def text(value: str = "", name: str | None = None, muted: bool = False, wrap: bool = True) -> QLabel:
    """A label by its old names (h1, h2, section, warning, ok, stat): kept so the pages read as before."""
    roles = {"h1": ("title", None), "h2": ("heading", None), "section": ("caps", None), "warning": (None, "warn"),
             "ok": (None, "ok"), "stat": ("number", None), "sentence": ("hero", None), "brand": ("wordmark", None)}
    role, tone = roles.get(name or "", (None, None))
    widget = label(value, role, tone or ("2" if muted else None), wrap)
    if name and name not in roles:
        widget.setObjectName(name)
    return widget


def caption(value: str = "", tone: str | None = "2", wrap: bool = True) -> QLabel:
    return label(value, "caption", tone, wrap)


def fixed_height(widget: QWidget, height: int) -> None:
    """One height, and no asking the card for more: a text box's size policy grows, so a fixed height alone still made
    its card taller, and the spare height went to the card's heading (a large gap above "Try it" and "Add a snippet")."""
    widget.setFixedHeight(height)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


def row(*items, stretch_at: int | None = None, spacing: int = 8) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    for i, item in enumerate(items):
        if i == stretch_at:
            layout.addStretch()
        if isinstance(item, QHBoxLayout | QVBoxLayout):
            layout.addLayout(item)
        else:
            layout.addWidget(item)
    if stretch_at is not None and stretch_at >= len(items):
        layout.addStretch()
    return layout


def clear(layout: QLayout) -> None:
    """Empty a layout that is filled again (history, words)."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            widget.hide()  # at once: deleteLater waits for the event loop, and until then it would still be drawn
            widget.deleteLater()
        elif item.layout():
            clear(item.layout())


def field(widget: QWidget, kind: str = "field") -> QWidget:
    """A text box or list on a well with an edge (focus: a 2 px Iris edge), painted by its card."""
    return surface(widget, kind)


def bare(edit: QLineEdit) -> QLineEdit:
    """A line edit inside a field of its own (a search box with an icon): no padding of its own."""
    edit.setProperty("bare", True)
    return edit


def key_names(label_text: str) -> str:
    """A hotkey's label as keys: "Ctrl+Win" stays, "Menu key" is the Menu key."""
    return "+".join(part.strip().removesuffix(" key") for part in label_text.split("+") if part.strip())


def back_button(title: str, go_to, page: str) -> Button:
    return button(title, lambda: go_to(page), link=True, size="sm", icon="chevron-left")


# ---------------------------------------------------------------- form controls
# A settings page must not change by accident, long lists must be searchable, and what is saved must be visible (the
# owner, 2026-10-04: scrolling over a dropdown changed the model and the language, and nothing said what was saved).

class Choice(QComboBox):
    """A dropdown the mouse wheel never changes: over a closed list the wheel scrolls the page. `search` gives a long
    list a search box at its top; an `editable` one filters its list by what is typed (any other name still goes)."""

    def __init__(self, search: bool = False, editable: bool = False, small: bool = False):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # the wheel doesn't take the focus either
        self._search = search
        self._popup: _SearchPopup | None = None
        if small:
            self.setProperty("size", "sm")
        if editable:
            self.setEditable(True)
            self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            completer = QCompleter(self.model(), self)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            self.setCompleter(completer)
            bare(self.lineEdit())
        surface(self, "field", 10 if small else 14)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def wheelEvent(self, event) -> None:
        event.ignore()  # to the page, which scrolls

    def showPopup(self) -> None:
        if not self._search:
            super().showPopup()
            return
        if self._popup is None:
            self._popup = _SearchPopup(self)
        self._popup.open()


class _SearchPopup(QFrame):
    """A long dropdown's list with a search box on top: type a few letters, then Enter (or a click) picks."""

    def __init__(self, combo: QComboBox):
        super().__init__(combo, Qt.WindowType.Popup)
        self.setObjectName("searchPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)  # else a window of its own isn't painted
        theme.make_host(self)
        self.combo = combo
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search...")
        self.search.setClearButtonEnabled(True)
        field(self.search)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(combo.model())
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.view = QListView()
        self.view.setModel(self.proxy)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.empty = caption("Nothing matches", "3")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        for widget in (self.search, self.view, self.empty):
            layout.addWidget(widget)
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)  # arrows and Enter work while typing
        self.view.clicked.connect(self.pick)
        self.view.activated.connect(self.pick)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme.paint_hosted(self, p)
        p.end()

    def open(self) -> None:
        self.search.clear()
        self._filter("")
        width, height = max(self.combo.width(), 260), 320
        below = self.combo.mapToGlobal(QPoint(0, self.combo.height() + 4))
        screen = self.combo.screen().availableGeometry()
        y = below.y() if below.y() + height <= screen.bottom() else self.combo.mapToGlobal(QPoint(0, 0)).y() - height - 4
        self.setGeometry(below.x(), y, width, height)
        self.show()
        self.search.setFocus()

    def _filter(self, value: str) -> None:
        self.proxy.setFilterFixedString(value.strip())
        if value.strip():
            current = self.proxy.index(0, 0)  # Enter picks the best match
        else:
            current = self.proxy.mapFromSource(self.combo.model().index(self.combo.currentIndex(), 0))
        self.view.setCurrentIndex(current)
        self.view.scrollTo(current, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.empty.setVisible(not self.proxy.rowCount())

    def pick(self, index=None) -> None:
        index = index if index is not None and index.isValid() else self.view.currentIndex()
        if index.isValid():
            self.combo.setCurrentIndex(self.proxy.mapToSource(index).row())
        self.hide()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                QGuiApplication.sendEvent(self.view, event)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.pick()
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, event)


def _clipboard_text() -> str:
    return QGuiApplication.clipboard().text()  # apart, so the tests never touch the real clipboard


def _masked(key: str) -> str:
    return "•" * 8 + (f"  {key[-4:]}" if len(key) >= 12 else "") if key else ""  # a short key shows nothing of it


class KeyField(QWidget):
    """An API key. Saved, it shows as dots and its last four characters, with a pen to change it; being changed, it's
    a hidden field with Show, Paste and a cross back to the saved key. It's saved with the rest of its section, by
    that section's Save. text() is the key as it would be saved; setText() types one."""

    textChanged = Signal(str)

    def __init__(self, saved: str = "", placeholder: str = ""):
        super().__init__()
        self.saved = saved
        surface(self, "field")
        self.view = bare(QLineEdit())
        self.view.setReadOnly(True)
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view.setToolTip("Saved, encrypted on this computer")
        self.view.setFont(font(13, 500, mono=True, spacing=0.06))
        self.field = bare(QLineEdit())
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText(placeholder)
        self.edit = icon_button("edit", "Change the key", lambda _=False: self.start_editing(), 28, flat=True)
        self.reveal = icon_button("eye", "Show the key", lambda _=False: self._toggle_shown(), 28, flat=True)
        self.paste = icon_button("paste", "Paste a key", lambda _=False: self.setText(_clipboard_text().strip()), 28,
                                 flat=True)
        self.undo = icon_button("close", "Keep the saved key", lambda _=False: self.show_saved(), 28, flat=True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 6, 0)
        layout.setSpacing(2)
        for widget in (self.view, self.edit, self.field, self.reveal, self.paste, self.undo):
            layout.addWidget(widget)
        layout.setStretch(0, 1)
        layout.setStretch(2, 1)
        self.setFixedHeight(40)
        self.field.textChanged.connect(lambda _="": self.textChanged.emit(self.text()))
        self.show_saved(saved)

    @property
    def editing(self) -> bool:
        return not self.field.isHidden()

    def text(self) -> str:
        return self.field.text() if self.editing else self.saved

    def setText(self, value: str) -> None:
        self.start_editing()
        self.field.setText(value)

    def setPlaceholderText(self, value: str) -> None:
        self.field.setPlaceholderText(value)

    def edited(self) -> bool:
        return self.text().strip() != self.saved

    def show_saved(self, saved: str | None = None) -> None:
        """Back to the saved key, or a new one saved meanwhile (e.g. on the other page); with none, the box to type it."""
        if saved is not None:
            self.saved = saved
        self.view.setText(_masked(self.saved))
        self._set_editing(not self.saved)
        self.field.blockSignals(True)
        self.field.setText(self.saved)
        self.field.blockSignals(False)
        self.textChanged.emit(self.text())

    def start_editing(self) -> None:
        if self.editing:
            return
        self._set_editing(True)
        self.field.setFocus()
        self.field.selectAll()  # typing or pasting replaces the saved key

    def _set_editing(self, editing: bool) -> None:
        for widget in (self.view, self.edit):
            widget.setVisible(not editing)
        for widget in (self.field, self.reveal, self.paste):
            widget.setVisible(editing)
        self.undo.setVisible(editing and bool(self.saved))
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.reveal.set_icon("eye")
        self.reveal.setToolTip("Show the key")

    def _toggle_shown(self) -> None:
        shown = self.field.echoMode() == QLineEdit.EchoMode.Password
        self.field.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.reveal.set_icon("eye-off" if shown else "eye")
        self.reveal.setToolTip("Hide the key" if shown else "Show the key")


class SaveBar(QWidget):
    """A section's Save, Cancel while there are changes, and its state in words: unsaved changes, or saved."""

    def __init__(self, on_save, on_cancel, label_text: str = "Save"):
        super().__init__()
        self.save = button(label_text, lambda _=False: on_save(), primary=True, size="sm")
        self.cancel = button("Cancel", lambda _=False: on_cancel(), kind="quiet", size="sm")
        self.cancel.hide()
        self.mark = QLabel()
        self.mark.hide()
        self.state = caption("", None, wrap=True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.save)
        layout.addWidget(self.cancel)
        layout.addSpacing(4)
        layout.addWidget(self.mark)
        layout.addWidget(self.state, 1)

    def show_state(self, edited: bool, enabled: bool | None = None) -> None:
        """After every change. Save is on when there is something to save (or `enabled` says otherwise)."""
        self.save.setEnabled(edited if enabled is None else enabled)
        self.cancel.setVisible(edited)
        if edited:
            self._say("Unsaved changes", "warn")
        elif self.state.property("tone") == "warn":
            self._say("", None)

    def saved(self, message: str) -> None:
        self.cancel.hide()
        self._say(message, "ok")

    def _say(self, message: str, tone: str | None) -> None:
        set_tone(self.state, tone)
        self.state.setText(message)
        self.mark.setVisible(tone == "ok" and bool(message))
        if tone == "ok":
            self.mark.setPixmap(theme.icon_pixmap("check", tok("ok").name(), 14, self.devicePixelRatioF()))


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class _Relay(QObject):
    done = Signal(object, object)  # (result, error)


def run_in_background(owner: QObject, work, done) -> None:
    """work() on a thread (network calls), then done(result, error) back on the UI thread."""
    relay = _Relay(owner)  # parented, so it lives until it has delivered
    relay.done.connect(lambda result, error: (done(result, error), relay.deleteLater()),
                       Qt.ConnectionType.QueuedConnection)

    def run() -> None:
        try:
            relay.done.emit(work(), None)
        except Exception as e:
            relay.done.emit(None, e)
    threading.Thread(target=run, name="window-task", daemon=True).start()


class FlowLayout(QLayout):
    """Items left to right, wrapping to the next line (Your words as chips)."""

    def __init__(self, spacing: int = 8):
        super().__init__()
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def addWidget(self, widget: QWidget) -> None:
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect: QRect, apply: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._spacing, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line = max(line, hint.height())
        return y + line - rect.y()


class Page(QScrollArea):
    """A page: its header (a title, a subtitle, a slot on the right, and a way back for a page one level down), then its
    cards; it scrolls when the window is small. The page itself paints the soft depth of the cards on it."""

    def __init__(self, title: str = "", subtitle: str = "", right: QWidget | None = None, back: Button | None = None):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = Host("base")
        body.setObjectName("page")
        self.body = QVBoxLayout(body)
        # 32 around: a card's soft light shadow fades out before the page's edge (at 24 it showed as a line in Porcelain)
        self.body.setContentsMargins(32, 32, 32, 32)
        self.body.setSpacing(24)
        self.title = text(title, "h1")
        self.subtitle = label(subtitle, "subtitle")
        self.header = QWidget()
        head = QHBoxLayout(self.header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(16)
        words = QVBoxLayout()
        words.setSpacing(4)
        if back is not None:
            words.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
            words.addSpacing(2)
        words.addWidget(self.title)
        if subtitle:
            words.addWidget(self.subtitle)
        head.addLayout(words, 1)
        if right is not None:
            head.addWidget(right, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        if title:
            self.body.addWidget(self.header)
        self.setWidget(body)

    def add(self, item) -> None:
        if isinstance(item, QLayout):
            self.body.addLayout(item)
        else:
            self.body.addWidget(item)

    def set_compact(self, compact: bool) -> None:
        margin = 24 if compact else 32
        self.body.setContentsMargins(margin, margin, margin, margin)
        self.body.setSpacing(16 if compact else 24)


def advanced_row(title: str, caption_text: str, on_click) -> QPushButton:
    """The flat line that opens a page's Advanced part: a chevron, its name, what's in it."""
    b = Button(title, "quiet", icon="chevron-right")
    b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    b.setToolTip(caption_text)
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(divider())
    inner = QHBoxLayout()
    inner.setContentsMargins(0, 6, 0, 6)
    inner.setSpacing(12)
    b.clicked.connect(on_click)
    inner.addWidget(b, 0)
    hint = caption(caption_text, "3", wrap=False)
    inner.addWidget(hint, 1)
    layout.addLayout(inner)
    holder.button = b
    return holder


def link_row(title: str, caption_text: str, on_click, icon: str = "chevron-right") -> QWidget:
    """A line that leads somewhere: its name and what's there, and a chevron."""
    w = QPushButton()
    w.setCursor(Qt.CursorShape.PointingHandCursor)
    w.setFlat(True)
    w.setMinimumHeight(56)
    layout = QHBoxLayout(w)
    layout.setContentsMargins(0, 8, 0, 8)
    layout.setSpacing(12)
    words = QVBoxLayout()
    words.setSpacing(2)
    words.addWidget(label(title, "rowtitle"))
    if caption_text:
        words.addWidget(caption(caption_text))
    layout.addLayout(words, 1)
    chevron = QLabel()
    chevron.setPixmap(theme.icon_pixmap(icon, tok("text3").name(), 16, 1.0))
    layout.addWidget(chevron)
    w.clicked.connect(on_click)
    w.setAccessibleName(title)
    return w


# ---------------------------------------------------------------- the microphone box (AI & models and the welcome)

class MicrophoneBox(QWidget):
    """A microphone choice with a live level meter, so the user sees at once that the microphone hears them. The list
    follows Windows while it's shown (a headset plugged in or out shows up within FOLLOW_MS), "Windows default" says
    which microphone that is now, and a chosen one that isn't connected says what Rflow uses meanwhile."""

    changed = Signal(str)  # the chosen device name ("" = the Windows default)
    followed = Signal()  # the list changed (a microphone plugged in or out, a new default)

    FOLLOW_MS = 2000

    def __init__(self, current: str, microphones: list[str], default: str = "", source=None):
        """`source` () -> (microphones, default name) is asked again every FOLLOW_MS while the box is shown."""
        super().__init__()
        self._source, self._listed = source, None
        self.combo = Choice()
        self.combo.currentIndexChanged.connect(self._chosen)
        self.level = Meter()
        self.level.setToolTip("Say something: the bars light up.")
        self.note = caption("", "3")
        self.note.hide()
        self.hearing = caption("", "3", wrap=False)  # the microphone the meter (and so dictation) actually opened
        self.hearing.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        line = QHBoxLayout()
        line.setSpacing(12)
        line.addWidget(self.combo, 1)
        line.addWidget(self.level, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(line)
        layout.addWidget(self.note)
        layout.addWidget(self.hearing)
        self.meter = LevelMeter(current or None)
        self._timer = QTimer(self, interval=50, timeout=self._show_level)
        self._follow_timer = QTimer(self, interval=self.FOLLOW_MS, timeout=self._follow)
        self._error = ""
        self.set_microphones(microphones, default, current)

    def device(self) -> str:
        return self.combo.currentData() or ""

    def set_microphones(self, microphones: list[str], default: str = "", current: str | None = None) -> None:
        """The list as Windows has it now; the choice is kept (one not connected stays, marked so)."""
        current = self.device() if current is None else current
        self._listed = (list(microphones), default)
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem(f"Windows default (now: {default})" if default else "Windows default", "")
        for name in microphones:
            self.combo.addItem(name, name)
        if current and self.combo.findData(current) < 0:
            self.combo.addItem(f"{current} (not connected)", current)
        self.combo.setCurrentIndex(max(0, self.combo.findData(current)))
        self.combo.blockSignals(False)
        self._show_note()

    def _show_note(self) -> None:
        microphones, default = self._listed or ([], "")
        current = self.device()
        if self._error:
            text = self._error
        elif current and current not in microphones:
            text = f"Not connected now: Rflow uses {default or 'the default microphone'} until it is."
        else:
            text = ""
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _follow(self) -> None:
        if self._source is None:
            return
        try:
            microphones, default = self._source()
        except Exception as e:  # never break the page over a list
            log.warning("Couldn't list the microphones: %s", e)
            return
        if (list(microphones), default) != self._listed:
            self.set_microphones(microphones, default)
            if self._timer.isActive():
                self._restart()  # the meter listens to the microphone Rflow would use now
            self.followed.emit()

    def _chosen(self) -> None:
        self.meter.device = self.device() or None
        if self._timer.isActive():
            self._restart()
        self._show_note()
        self.changed.emit(self.device())

    def _restart(self) -> None:
        self.meter.stop()
        try:
            self.meter.start()
            self._error = ""
            hearing = self.meter.describe().get("device", "")
            self.hearing.setText(f"Hearing: {hearing}" if hearing else "")
            self.hearing.setVisible(bool(hearing))
        except Exception as e:
            self._error = f"Could not open this microphone: {e}"
            self.hearing.hide()
        self._show_note()

    def _show_level(self) -> None:
        db = 20 * math.log10(self.meter.level + 1e-6)  # speech is roughly -45..-15 dBFS
        self.level.set_value(min(1.0, max(0.0, (db + 55) / 40)))

    def showEvent(self, event):
        super().showEvent(event)
        if QGuiApplication.platformName() != "offscreen":  # tests and the self-test don't open a microphone
            self._follow()
            self._restart()
            self._timer.start()
            self._follow_timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        self._follow_timer.stop()
        self.meter.stop()
        self.level.set_value(0)
        super().hideEvent(event)


# ---------------------------------------------------------------- Home

def _day_title(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return day.strftime("%A %d %B") if day.year == today.year else day.strftime("%d %B %Y")


def how_to_dictate(label_text: str) -> str:
    extra = f" {label_text}+Space starts hands-free as well." if label_text == "Ctrl+Win" else ""
    return f"Tap once for hands-free, tap again to finish.{extra} Esc cancels."


class ElidedLabel(QLabel):
    """One line of text, cut with … when the space is short (the full text is its tooltip)."""

    def __init__(self, value: str = ""):
        super().__init__(value)
        self.setMinimumWidth(1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))
        shown = self.fontMetrics().elidedText(" ".join(self.text().split()), Qt.TextElideMode.ElideRight, self.width())
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, shown)
        p.end()


class HistoryRow(QWidget):
    """One dictation: its time, its text on one line, and Correct and Copy when the pointer is over it."""

    def __init__(self, when: datetime, entry: dict, on_copy, on_correct):
        super().__init__()
        surface(self, "row", 14)
        self.setFixedHeight(42)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 7, 0)
        layout.setSpacing(16)
        time_label = QLabel(when.strftime("%H:%M"))
        time_label.setFont(font(12, 500, mono=True))
        time_label.setProperty("tone", "3")
        time_label.setFixedWidth(40)
        self.body = ElidedLabel(entry["text"])
        self.body.setToolTip(entry["text"] + (f"\n\nHeard: {entry['heard']}" if entry.get("heard") else ""))
        self.copy = icon_button("copy", "Copy", size=28)
        self.copy.clicked.connect(lambda: on_copy(self.copy, entry["text"]))
        self.correct = icon_button("edit", "Correct it: Rflow learns from corrections you make twice", size=28)
        self.correct.clicked.connect(lambda: on_correct(entry["text"]))
        self.actions = QWidget()
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(self.correct)
        actions.addWidget(self.copy)
        self.actions.setVisible(False)
        layout.addWidget(time_label)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.actions)

    def enterEvent(self, event):
        self.actions.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.actions.setVisible(False)
        super().leaveEvent(event)


class HomePage(Page):
    """The voice orb and how to dictate (or what Rflow still needs), the stats strip, and the dictations."""

    SHOWN = 30  # dictations listed before "Show all"

    def __init__(self, app, go_to=None):
        super().__init__()
        self.app, self.go_to = app, go_to or (lambda page: None)
        self.ready, self.show_all, self.compact = False, False, False

        # the hero: the orb, the state, how to dictate (or what's missing), and what Rflow uses
        self.hero, hero = card(24, (24, 24, 24, 24), horizontal=True)
        self.orb = Orb(128, level=lambda: 0.0)
        hero.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignVCenter)
        column = QVBoxLayout()
        column.setSpacing(8)
        self.status_row, self.status_lamp, self.status_text = lamp_row("ok", "Ready in any app")
        column.addWidget(self.status_row)
        self.hero_keys = QWidget()  # "Hold [Ctrl] + [Win] and talk. Let go, it's typed."
        keys_line = QHBoxLayout(self.hero_keys)
        keys_line.setContentsMargins(0, 0, 0, 0)
        keys_line.setSpacing(8)
        self.hold = label("Hold", "hero", wrap=False)
        self.hero_caps = QWidget()
        self.hero_caps_layout = QHBoxLayout(self.hero_caps)
        self.hero_caps_layout.setContentsMargins(0, 0, 0, 0)
        self.and_talk = label("and talk.", "hero", wrap=False)
        self.let_go = label("Let go, it's typed.", "hero", wrap=False)
        for widget in (self.hold, self.hero_caps, self.and_talk, self.let_go):
            keys_line.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        keys_line.addStretch()
        column.addWidget(self.hero_keys)
        self.hero_title = label("", "hero")  # the line when Rflow isn't ready
        column.addWidget(self.hero_title)
        self.hero_text = label("", tone="2")
        self.hero_text.setMaximumWidth(520)
        column.addWidget(self.hero_text)
        self.meta = QWidget()
        meta = QHBoxLayout(self.meta)
        meta.setContentsMargins(0, 4, 0, 0)
        meta.setSpacing(16)
        self.mic_meta = QLabel()
        self.mic_meta_text = caption("", "3", wrap=False)
        self.ai_meta = QLabel()
        self.ai_meta_text = caption("", "violet", wrap=False)
        for widget in (self.mic_meta, self.mic_meta_text):
            meta.addWidget(widget)
        meta.addSpacing(8)
        for widget in (self.ai_meta, self.ai_meta_text):
            meta.addWidget(widget)
        meta.addStretch()
        column.addWidget(self.meta)
        self.progress_row = QWidget()
        progress = QHBoxLayout(self.progress_row)
        progress.setContentsMargins(0, 8, 0, 0)
        progress.setSpacing(16)
        self.wave = WaveProgress()
        self.progress_text = QLabel()
        self.progress_text.setFont(font(12, 500, mono=True))
        self.progress_text.setProperty("tone", "3")
        progress.addWidget(self.wave)
        progress.addWidget(self.progress_text)
        progress.addStretch()
        column.addWidget(self.progress_row)
        self.actions = QWidget()
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 8, 0, 0)
        actions.setSpacing(12)
        self.get_parakeet = button("Download Parakeet", lambda _=False: app.download_speech_model(DEFAULT_MODEL),
                                   primary=True, icon="download")
        self.use_cloud = button("Use a cloud model instead", lambda _=False: self.go_to("speech"), icon="cloud")
        self.pause = button("Pause", lambda _=False: app.cancel_download(), kind="quiet")
        for widget in (self.get_parakeet, self.use_cloud, self.pause):
            actions.addWidget(widget)
        actions.addStretch()
        column.addWidget(self.actions)
        hero.addLayout(column, 1)
        self.add(self.hero)

        # the stats strip: one inset readout
        self.readout, strip = card(0, (20, 0, 20, 0), kind="well", horizontal=True)
        self.readout.setProperty("neuRadius", 14)
        self.readout.setFixedHeight(48)
        self.stat_values: dict[str, QLabel] = {}
        self.stat_captions: dict[str, QLabel] = {}
        for i, (key, words) in enumerate([("week", "this week"), ("speed", "words a minute"), ("streak", "days in a row"),
                                          ("total", "all time")]):
            if i:
                line = divider(vertical=True)
                line.setFixedHeight(24)
                strip.addSpacing(20)
                strip.addWidget(line, 0, Qt.AlignmentFlag.AlignVCenter)
                strip.addSpacing(20)
            value = QLabel("0")
            value.setFont(font(16, 600, tabular=True))
            words_label = caption(words, "3", wrap=False)
            pair = QHBoxLayout()
            pair.setSpacing(6)
            pair.addWidget(value, 0, Qt.AlignmentFlag.AlignVCenter)
            pair.addWidget(words_label, 0, Qt.AlignmentFlag.AlignVCenter)
            pair.addStretch()
            strip.addLayout(pair, 1)
            self.stat_values[key], self.stat_captions[key] = value, words_label
        self.add(self.readout)

        # the dictations
        self.list_card, column = card(2, (16, 16, 16, 8))
        head = QHBoxLayout()
        head.setContentsMargins(8, 0, 0, 8)
        head.setSpacing(12)
        self.list_title = label("Today", "heading", wrap=False)
        head.addWidget(self.list_title)
        head.addStretch()
        self.search_box = Card("field", 10)
        search = QHBoxLayout(self.search_box)
        search.setContentsMargins(10, 0, 6, 0)
        search.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(theme.icon_pixmap("search", tok("text3").name(), 16, 1.0))
        self.search = bare(QLineEdit())
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _="": self.refresh())
        search.addWidget(icon)
        search.addWidget(self.search, 1)
        self.search_keys = keys("Ctrl+K", "sm")
        search.addWidget(self.search_keys)
        self.search_box.setFixedSize(236, 32)
        head.addWidget(self.search_box)
        self.all_button = button("Show all", self._toggle_all, link=True, size="sm")
        head.addWidget(self.all_button)
        column.addLayout(head)
        self.history = QVBoxLayout()
        self.history.setSpacing(2)
        column.addLayout(self.history)
        column.addStretch()
        self.add(self.list_card)
        self.body.addStretch()
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self._find,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def _find(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.refresh()

    def set_ready(self, ready: bool) -> None:
        self.ready = ready

    def set_compact(self, compact: bool) -> None:
        super().set_compact(compact)
        self.compact = compact
        self.let_go.setVisible(not compact)
        self.search_box.setFixedWidth(180 if compact else 236)
        self.search_keys.setVisible(not compact)
        self.hero.layout().setContentsMargins(*(4 * [20 if compact else 24]))

    def _hero_keys(self, label_text: str) -> None:
        clear(self.hero_caps_layout)
        caps = keys(key_names(label_text))
        self.hero_caps_layout.addWidget(caps)
        self.hero_keys.setAccessibleName(f"Hold {label_text} and talk. Let go, it's typed.")
        self.hero_keys.setToolTip(self.hero_keys.accessibleName())

    def refresh(self) -> None:
        now = datetime.now()
        today = now.date()
        app = self.app
        stats: Stats = app.stats
        label_text = app.hotkey_label()
        self._show_state(label_text)
        wpm = stats.words_per_minute
        self.stat_values["week"].setText(f"{stats.words_this_week(today):,}")
        self.stat_values["total"].setText(f"{stats.words:,}")
        self.stat_values["speed"].setText(str(wpm) if wpm else "–")
        self.stat_values["streak"].setText(str(stats.streak(today)))
        self.stat_values["speed"].setToolTip("" if wpm else "Shown after half a minute of dictation.")
        self._fill_history(app.history_entries(), today)

    def _show_state(self, label_text: str) -> None:
        """Ready, or what Rflow is waiting for: Parakeet downloading, a model loading, or no speech model yet."""
        app = self.app
        in_use, loading, downloading = app.speech_in_use(), app.loading_speech, app.downloading
        fetching = bool(downloading) and downloading[0] == DEFAULT_MODEL
        state = "ready" if self.ready and in_use and not fetching else ("fetching" if fetching else
                                                                          "loading" if (loading or in_use) else "none")
        ready = state == "ready"
        self.hero_keys.setVisible(ready)
        self.hero_title.setVisible(not ready)
        self.meta.setVisible(ready)
        self.progress_row.setVisible(state == "fetching")
        self.actions.setVisible(state in ("fetching", "none"))
        self.get_parakeet.setVisible(state == "none" and not downloading)
        self.pause.setVisible(state == "fetching")
        self.use_cloud.setVisible(state in ("fetching", "none"))
        self.use_cloud.setText("Use a cloud model instead" if state == "fetching" else "Use a cloud model")
        parakeet = SPEECH_MODELS[DEFAULT_MODEL]
        if ready:
            self.orb.set_state("ready")
            self.status_lamp.set_state("ok")
            self.status_text.setText("Ready in any app")
            self._hero_keys(label_text)
            self.hero_text.setText(how_to_dictate(label_text))
            microphone = app.settings.microphone or "Windows default microphone"
            self.mic_meta.setPixmap(theme.icon_pixmap("mic", tok("text3").name(), 16, self.devicePixelRatioF()))
            self.mic_meta_text.setText(microphone)
            model = app.transform_model() if app.settings.cleanup else ""
            provider = PROVIDERS[app.gateway.service.key].name if model else ""
            self.ai_meta.setVisible(bool(model))
            self.ai_meta_text.setVisible(bool(model))
            if model:
                self.ai_meta.setPixmap(theme.icon_pixmap("tools", tok("violet").name(), 16, self.devicePixelRatioF()))
                self.ai_meta_text.setText(f"Cleanup by {provider.split(' (')[0]}")
        elif state == "fetching":
            done, total = downloading[1], downloading[2] or 1
            self.orb.set_state("loading", done / total)
            self.status_lamp.set_state("warn")
            self.status_text.setText("Not ready yet · downloading")
            self.hero_title.setText("Getting Parakeet, your private speech model")
            self.hero_text.setText("Rflow types as soon as it's here. Until then, the key says “Not ready” "
                                   "instead of doing nothing.")
            self.wave.set_value(done / total)
            self.progress_text.setText(f"{_size(done)} / {_size(total)}")
        elif state == "loading":
            name = SPEECH_MODELS.get(loading or in_use)
            self.orb.set_state("off")
            self.status_lamp.set_state("warn")
            self.status_text.setText("Getting ready")
            self.hero_title.setText(f"Loading {name.name if name else 'the speech model'}…")
            self.hero_text.setText("A few seconds. Dictation starts as soon as it's loaded.")
        else:
            self.orb.set_state("off")
            self.status_lamp.set_state("warn")
            self.status_text.setText("Not ready yet")
            self.hero_title.setText("Choose how Rflow hears you")
            self.hero_text.setText(f"Download NVIDIA Parakeet ({_size(parakeet.download.size)}) to dictate privately on "
                                   "this PC, or use a cloud model with your own key.")

    def _fill_history(self, entries: list[dict], today: date) -> None:
        clear(self.history)
        query = self.search.text().strip().casefold()
        found = [e for e in entries if not query or query in e.get("text", "").casefold()]
        self.all_button.setVisible(len(found) > self.SHOWN and not query)
        self.all_button.setText("Show fewer" if self.show_all else "Show all")
        self.search_box.setVisible(bool(entries))
        if not entries:
            self.list_title.setText("Your dictations")
            self.history.addWidget(self._empty())
            return
        if not found:
            self.list_title.setText("Search")
            self.history.addWidget(caption(f"No dictation has “{self.search.text().strip()}”.", "3"))
            return
        shown = found if self.show_all or query else found[:self.SHOWN]
        day = None
        for entry in shown:
            try:
                when = datetime.strptime(entry.get("time", ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if when.date() != day:
                day = when.date()
                if self.history.count() == 0:
                    self.list_title.setText(_day_title(day, today) if not query else "Found")
                else:
                    heading = caption(_day_title(day, today), "3", wrap=False)
                    heading.setContentsMargins(8, 12, 0, 6)
                    self.history.addWidget(heading)
            self.history.addWidget(HistoryRow(when, entry, self._copy, self._correct))

    def _empty(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 16, 0, 16)
        layout.setSpacing(12)
        layout.addWidget(keys(key_names(self.app.hotkey_label()), "lg"), 0, Qt.AlignmentFlag.AlignHCenter)
        title = label("Nothing dictated yet", "heading")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        words = caption(f"Click in any text box, hold {self.app.hotkey_label()} and speak. Your dictations appear "
                        "here, kept on this PC, ready to copy again.")
        words.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(words)
        return w

    def ask_correction(self, typed: str) -> str | None:
        """The corrected text, or None if cancelled (a dialog; the tests replace it)."""
        value, ok = QInputDialog.getText(self, APP_NAME, "What should it have been?", text=typed)
        return value if ok else None

    def _correct(self, typed: str) -> None:
        corrected = self.ask_correction(typed)
        if corrected is not None and corrected.strip() and corrected.strip() != typed:
            self.app.correct_dictation(typed, corrected.strip())

    def _copy(self, source: IconButton, value: str) -> None:
        QGuiApplication.clipboard().setText(value)
        source.set_icon("check")
        source.setToolTip("Copied")

        def back() -> None:
            try:
                source.set_icon("copy")
                source.setToolTip("Copy")
            except RuntimeError:  # the list was filled again meanwhile (a new dictation): that button is gone
                pass
        QTimer.singleShot(1200, back)


# ---------------------------------------------------------------- Words: Your words and Snippets

def _words_header(app, go_to, current: str) -> Segmented:
    tabs = Segmented([("dictionary", "Your words"), ("snippets", "Snippets")])
    tabs.set_current(current)
    tabs.changed.connect(lambda key: go_to(key) if key != current else None)
    tabs.app = app
    return tabs


def _count_words(app) -> int:
    entries = {w.lower() for w in app.settings.vocabulary}
    entries |= {t.preferred.lower() for t in app.dictionary_terms()}
    return len(entries)


def _show_counts(tabs: Segmented, app) -> None:
    tabs.buttons["dictionary"].suffix = str(_count_words(app))
    tabs.buttons["snippets"].suffix = str(len(load_snippets(app.settings.snippets)))
    for b in tabs.buttons.values():
        b.updateGeometry()
        b.update()


class WordChip(QWidget):
    """A word as a chip: flat at rest, raised with ✕ under the pointer."""

    def __init__(self, word: str, note: str = "", tip: str = "", on_remove=None):
        super().__init__()
        surface(self, "chip", 999)
        self.word = word
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 6, 0)
        layout.setSpacing(6)
        name = QLabel(word)
        name.setFont(font(13, 500))
        layout.addWidget(name)
        if note:
            extra = QLabel(note)
            extra.setFont(font(12, 400))
            extra.setProperty("tone", "3")
            layout.addWidget(extra)
        self.remove = icon_button("close", f"Remove {word}", lambda _=False: on_remove(word) if on_remove else None, 20,
                                  flat=True)
        self.remove.setVisible(False)
        layout.addWidget(self.remove)
        self.setFixedHeight(32)
        if tip:
            self.setToolTip(tip)
        layout.addSpacing(0)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width() + (0 if self.remove.isVisible() else 20), 32)

    def enterEvent(self, event):
        self.remove.setVisible(True)
        self.setProperty("neuDown", True)
        theme.refresh(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.remove.setVisible(False)
        self.setProperty("neuDown", False)
        theme.refresh(self)
        super().leaveEvent(event)


class DictionaryPage(Page):
    def __init__(self, app, go_to):
        tabs = _words_header(app, go_to, "dictionary")
        super().__init__("Words", "Names Rflow should always get right, and phrases it types for you.", tabs)
        self.tabs, self.app, self.go_to = tabs, app, go_to
        # one box: type to find a word among yours; Enter (or Add) adds it (several: separate them with commas)
        self.entry_box = Card("field", 14)
        entry = QHBoxLayout(self.entry_box)
        entry.setContentsMargins(12, 0, 10, 0)
        entry.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(theme.icon_pixmap("search", tok("text3").name(), 16, 1.0))
        self.entry = bare(QLineEdit())
        self.entry.setPlaceholderText("Add or find a word (several: separate them with commas)")
        self.entry.returnPressed.connect(self._add)
        self.entry.textChanged.connect(lambda _="": self._filter())
        entry.addWidget(icon)
        entry.addWidget(self.entry, 1)
        entry.addWidget(keycap("↵", "sm"))
        entry.addWidget(caption("adds it", "3", wrap=False))
        self.entry_box.setFixedHeight(44)
        self.add_button = button("Add", self._add)
        self.add_button.setFixedHeight(44)
        self.add(row(self.entry_box, self.add_button, spacing=12))

        self.cleanup_off, off = card(12, (16, 12, 12, 12), kind="well", horizontal=True)
        off_icon = QLabel()
        off_icon.setPixmap(theme.icon_pixmap("info", tok("text3").name(), 16, 1.0))
        off.addWidget(off_icon)
        off.addWidget(caption("Speech recognition uses your words. AI cleanup is off, so it doesn't.", "2"), 1)
        off.addWidget(button("Turn on AI cleanup", lambda: go_to("models"), link=True, size="sm"))
        self.add(self.cleanup_off)

        self.suggestions_card = QWidget()
        self.suggestions = QVBoxLayout(self.suggestions_card)
        self.suggestions.setContentsMargins(0, 0, 0, 0)
        self.suggestions.setSpacing(12)
        self.add(self.suggestions_card)

        self.list_card, column = card(16, (20, 20, 20, 20))
        head = QHBoxLayout()
        head.addWidget(label("Your words", "heading"))
        head.addStretch()
        self.count = caption("", "3", wrap=False)
        head.addWidget(self.count)
        column.addLayout(head)
        self.chips_box = QWidget()
        self.list = FlowLayout(8)
        self.chips_box.setLayout(self.list)
        column.addWidget(self.chips_box)
        self.none_found = caption("", "3")
        self.none_found.hide()
        column.addWidget(self.none_found)
        self.add(self.list_card)

        # sound-alikes: what the speech model writes for a term ("post grass" for PostgreSQL), fixed before anything else
        self.alikes_card, column = card(12, (20, 16, 20, 20))
        column.addWidget(label("Heard as", "heading"))
        column.addWidget(caption("When Rflow writes a word wrong the same way, tell it what you meant: it's fixed "
                                 "before anything else. Corrections you make twice are learned here too."))
        self.alikes = QVBoxLayout()
        self.alikes.setSpacing(0)
        column.addLayout(self.alikes)
        self.heard = QLineEdit()
        self.heard.setPlaceholderText("When Rflow writes… (e.g. post grass)")
        field(self.heard)
        self.meant = QLineEdit()
        self.meant.setPlaceholderText("…write instead (e.g. PostgreSQL)")
        self.meant.returnPressed.connect(self._add_sound_alike)
        field(self.meant)
        arrow = QLabel()
        arrow.setPixmap(theme.icon_pixmap("arrow-right", tok("text3").name(), 16, 1.0))
        column.addLayout(row(self.heard, arrow, self.meant, button("Add sound-alike", self._add_sound_alike), spacing=10))
        self.alike_note = caption("", "2")
        self.alike_note.hide()  # until there is something to say: an empty line would leave a gap
        column.addWidget(self.alike_note)
        self.add(self.alikes_card)
        self.body.addStretch()
        self._entries: list[tuple[str, list[str]]] = []

    def refresh(self) -> None:
        settings: Settings = self.app.settings
        _show_counts(self.tabs, self.app)
        self.cleanup_off.setVisible(not settings.cleanup)
        clear(self.suggestions)
        suggestions = self.app.correction_suggestions()
        self.suggestions_card.setVisible(bool(suggestions))
        for s in suggestions[:5]:
            banner, line = card(12, (16, 10, 10, 10), kind="well", horizontal=True)
            sparkle = QLabel()
            sparkle.setPixmap(theme.icon_pixmap("tools", tok("violet").name(), 18, 1.0))
            line.addWidget(sparkle)
            times = "twice" if s.seen_count == 2 else f"{s.seen_count} times"
            said = label(f"Heard “{html.escape(s.original_phrase)}” {times}. You meant "
                         f"<b>{html.escape(s.corrected_phrase)}</b>.")
            said.setTextFormat(Qt.TextFormat.RichText)
            line.addWidget(said, 1)
            line.addWidget(button("Not now", lambda _=False, s=s: self._suggestion(s, False), kind="quiet", size="sm"))
            line.addWidget(button("Learn it", lambda _=False, s=s: self._suggestion(s, True), primary=True, size="sm"))
            self.suggestions.addWidget(banner)
        # Your words, plus terms that only have sound-alikes; each with what it is heard as.
        entries = {w.lower(): (w, []) for w in settings.vocabulary}
        sources: dict[str, str] = {}
        for term in self.app.dictionary_terms():
            word, aliases = entries.get(term.preferred.lower(), (term.preferred, []))
            entries[term.preferred.lower()] = (word, aliases + [a for a in term.aliases if a not in aliases])
            sources[term.preferred.lower()] = term.source
        self._entries = sorted(entries.values(), key=lambda e: e[0].lower())
        self._sources = sources
        self.count.setText(f"{len(entries)} word{'s' if len(entries) != 1 else ''}")
        self.list_card.setVisible(bool(entries))
        self._filter()
        clear(self.alikes)
        for word, aliases in self._entries:
            for alias in aliases:
                line = QWidget()
                layout = QHBoxLayout(line)
                layout.setContentsMargins(0, 10, 0, 10)
                layout.setSpacing(12)
                heard = label(f"<s>{html.escape(alias)}</s>", tone="3", wrap=False)
                heard.setTextFormat(Qt.TextFormat.RichText)
                heard.setMinimumWidth(140)
                arrow = QLabel()
                arrow.setPixmap(theme.icon_pixmap("arrow-right", tok("text3").name(), 14, 1.0))
                layout.addWidget(heard)
                layout.addWidget(arrow)
                layout.addWidget(label(word, "rowtitle", wrap=False), 1)
                learned = sources.get(word.lower()) == "learned"
                layout.addWidget(caption("Learned from your fixes" if learned else "You added it", "3", wrap=False))
                if self.alikes.count():
                    self.alikes.addWidget(divider())
                self.alikes.addWidget(line)

    def _filter(self) -> None:
        """The chips, those matching what's typed in the box first (typing finds a word among yours)."""
        clear(self.list)
        typed = self.entry.text().strip().casefold()
        self.add_button.setEnabled(bool(typed))
        shown = [(w, a) for w, a in self._entries if not typed or typed.split(",")[-1].strip() in w.casefold()
                 or "," in typed]
        for word, aliases in shown:
            note, tip = "", ""
            if not speech_hints([word]):
                note = "everyday word"
                tip = ("Speech models spell everyday words right by themselves; listed as hints, they get heard where "
                       "you didn't say them. Keep names and terms here.")
            elif aliases:
                note = "heard as " + ", ".join(f"“{a}”" for a in aliases[:2])
            self.list.addWidget(WordChip(word, note, tip, self._remove))
        self.none_found.setVisible(bool(typed) and not shown and "," not in typed)
        self.none_found.setText(f"“{self.entry.text().strip()}” isn't in your words yet: press Enter to "
                                "add it.")
        self.chips_box.updateGeometry()

    def _suggestion(self, suggestion, accept: bool) -> None:
        if accept:
            self.app.accept_suggestion(suggestion)
        else:
            self.app.reject_suggestion(suggestion)
        self.refresh()

    def _say(self, message: str) -> None:
        self.alike_note.setText(message)
        self.alike_note.setVisible(bool(message))

    def _add_sound_alike(self) -> None:
        heard, meant = self.heard.text().strip(), self.meant.text().strip()
        if not heard or not meant:
            self._say("Fill in both: what Rflow writes, and what it should write.")
            return
        try:
            self.app.add_sound_alike(heard, meant)
        except ValueError as e:  # e.g. the same sound-alike already stands for another word
            self._say(str(e))
            return
        self._say(f"From now on “{heard}” is written as {meant}.")
        self.heard.clear()
        self.meant.clear()
        self.refresh()

    def _add(self) -> None:
        new = [w.strip() for w in re.split(r"[,\n]", self.entry.text()) if w.strip()]
        if new:
            self.app.add_words(new)
            self.entry.clear()
            self.refresh()

    def _remove(self, word: str) -> None:
        aliases = next((a for w, a in self._entries if w == word), [])
        self.app.remove_word(word)
        self.refresh()
        toast = getattr(self.window(), "toast", None)
        if toast is not None:
            toast.show_message(f"Removed “{word}”", "close", "err", "Undo", lambda: self._undo_remove(word, aliases))

    def _undo_remove(self, word: str, aliases: list[str]) -> None:
        self.app.add_words([word])
        for alias in aliases:
            try:
                self.app.add_sound_alike(alias, word)
            except ValueError:
                pass
        self.refresh()


class SnippetsPage(Page):
    """Snippets (sst.snippets): say a short phrase, get your own text typed, exactly as written here."""

    def __init__(self, app, go_to=None):
        go_to = go_to or (lambda page: None)
        tabs = _words_header(app, go_to, "snippets")
        super().__init__("Words", "Say a short phrase, get your own text: “my email” types your email address, "
                                  "“my signature” your signature, line breaks and all. Typed exactly as "
                                  "written here, and never sent to the AI.", tabs)
        self.tabs, self.app = tabs, app
        self.editing: str | None = None  # the cue of the snippet being edited
        form, layout = card(12, (20, 20, 20, 20))
        self.form_title = label("Add a snippet", "heading")
        layout.addWidget(self.form_title)
        self.cue = QLineEdit()
        self.cue.setPlaceholderText("When I say… (e.g. my email)")
        field(self.cue)
        layout.addWidget(self.cue)
        self.snippet_text = QPlainTextEdit()
        self.snippet_text.setPlaceholderText("…type this (e.g. xyz@gmail.com). Several lines are fine.")
        field(self.snippet_text)
        fixed_height(self.snippet_text, 88)
        layout.addWidget(self.snippet_text)
        self.anywhere = Toggle("Also inside a sentence")
        anywhere, _, _ = setting_row("Also inside a sentence", "“send it to my email”. Off: only when you say "
                                     "the phrase on its own, so “I checked my email” stays as you said it.",
                                     self.anywhere)
        anywhere.layout().setContentsMargins(0, 4, 0, 4)
        layout.addWidget(anywhere)
        self.note = caption("", "2")
        self.note.hide()
        layout.addWidget(self.note)
        self.save_button = button("Add", self._save, primary=True, size="sm")
        self.cancel_button = button("Cancel", self._cancel, kind="quiet", size="sm")
        self.cancel_button.hide()
        layout.addLayout(row(self.save_button, self.cancel_button, stretch_at=2))
        self.add(form)

        trial, layout = card(10, (20, 18, 20, 20))
        layout.addWidget(label("Try it", "heading"))
        self.trial = QLineEdit()
        self.trial.setPlaceholderText("Type what you would say, e.g. send it to my email")
        field(self.trial)
        self.trial.textChanged.connect(self._try)
        layout.addWidget(self.trial)
        self.trial_result = caption("", "2")
        layout.addWidget(self.trial_result)
        self.add(trial)

        self.list_card, self.list = card(0, (20, 12, 12, 12))
        head = QHBoxLayout()
        head.setContentsMargins(0, 4, 8, 8)
        head.addWidget(label("Your snippets", "heading"))
        head.addStretch()
        self.count = caption("", "3", wrap=False)
        head.addWidget(self.count)
        self.list.addLayout(head)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        self.list.addLayout(self.rows)
        self.add(self.list_card)
        self.body.addStretch()

    def refresh(self) -> None:
        mine = load_snippets(self.app.settings.snippets)
        _show_counts(self.tabs, self.app)
        clear(self.rows)
        self.count.setText(f"{len(mine)} snippet{'s' if len(mine) != 1 else ''}")
        self.list_card.setVisible(bool(mine))
        for snippet in mine:
            line = QWidget()
            layout = QHBoxLayout(line)
            layout.setContentsMargins(0, 8, 0, 8)
            layout.setSpacing(12)
            layout.addWidget(label(f"“{snippet.cue}”", "rowtitle", wrap=False))
            arrow = QLabel()
            arrow.setPixmap(theme.icon_pixmap("arrow-right", tok("text3").name(), 14, 1.0))
            layout.addWidget(arrow)
            lines = snippet.text.strip().splitlines()
            shown = ElidedLabel(lines[0] + (" …" if len(lines) > 1 else ""))
            shown.setProperty("tone", "2")
            shown.setToolTip(snippet.text)
            layout.addWidget(shown, 1)
            if snippet.anywhere:
                layout.addWidget(caption("also inside sentences", "3", wrap=False))
            layout.addWidget(icon_button("edit", f"Edit “{snippet.cue}”", lambda _=False, s=snippet: self._edit(s),
                                         28))
            layout.addWidget(icon_button("trash", f"Remove “{snippet.cue}”",
                                         lambda _=False, s=snippet: self._remove(s), 28))
            if self.rows.count():
                self.rows.addWidget(divider())
            self.rows.addWidget(line)
        self._try()

    def _say(self, message: str) -> None:
        self.note.setText(message)
        self.note.setVisible(bool(message))

    def _save(self) -> None:
        cue, body = " ".join(self.cue.text().split()), self.snippet_text.toPlainText().strip()
        if not cue or not body:
            self._say("Fill in both: what you say, and the text to type.")
            return
        new = Snippet.from_dict({"cue": cue, "text": body, "anywhere": self.anywhere.isChecked()})
        if new is None:
            self._say(f"A phrase of at most {SNIPPET_WORDS} words, please.")
            return
        mine = [s for s in load_snippets(self.app.settings.snippets) if s.cue != self.editing]
        if any(compact_cue(s.cue) == compact_cue(cue) for s in mine):
            self._say(f"There is already a snippet for “{cue}”.")
            return
        self.app.apply_settings(dataclasses.replace(self.app.settings, snippets=[s.to_dict() for s in [*mine, new]]))
        warning = ""
        if new.anywhere and len(cue.split()) == 1:
            warning = f"Saved. Note: “{cue}” will be replaced every time you say it in a sentence."
        self._cancel()
        self._say(warning or f"Saved: say “{cue}” to type it.")

    def _edit(self, snippet: Snippet) -> None:
        self.editing = snippet.cue
        self.cue.setText(snippet.cue)
        self.snippet_text.setPlainText(snippet.text)
        self.anywhere.setChecked(snippet.anywhere)
        self.form_title.setText("Edit the snippet")
        self.save_button.setText("Save")
        self.cancel_button.show()
        self._say("")
        self.cue.setFocus()

    def _cancel(self) -> None:
        self.editing = None
        self.cue.clear()
        self.snippet_text.clear()
        self.anywhere.setChecked(False)
        self.form_title.setText("Add a snippet")
        self.save_button.setText("Add")
        self.cancel_button.hide()
        self._say("")
        self.refresh()

    def _remove(self, snippet: Snippet) -> None:
        mine = [s.to_dict() for s in load_snippets(self.app.settings.snippets) if s.cue != snippet.cue]
        self.app.apply_settings(dataclasses.replace(self.app.settings, snippets=mine))
        if self.editing == snippet.cue:
            self._cancel()
        self.refresh()

    def _try(self, *_) -> None:
        said, mine = self.trial.text().strip(), load_snippets(self.app.settings.snippets)
        if not said:
            self.trial_result.setText("What Rflow would type appears here.")
            return
        if (snippet := snippet_alone(said, mine)) is not None:
            self.trial_result.setText(f"Types: {snippet.text}")
            return
        protected, slots = protect_snippets(said, mine)
        typed = expand_snippets(protected, slots) or said
        self.trial_result.setText(f"Types: {typed}" if slots else "No snippet here: typed as you said it.")


# ---------------------------------------------------------------- Reading test

class ReadingTest(QWidget):
    """The reading test: read a set of sentences aloud, then see how many words each setup gets wrong (sst/evaluate.py)."""

    scored = Signal(object)  # from the scoring thread
    progressed = Signal(str)
    failed = Signal(str)
    restart = Signal()  # "New test"

    def __init__(self, recorder, score, add_words, microphone: str, folder: Path | None = None, block: str = "A"):
        super().__init__()
        self.recorder, self._score, self._add_words, self.microphone = recorder, score, add_words, microphone
        self.folder = folder or bench.BENCH_DIR / time.strftime("%Y-%m-%d_%H%M%S")
        self.report_folder = self.folder  # where the shown results were saved: this test, or 'summary' for all
        # A test already started keeps its set; a new one reads the set it was given.
        started = (self.folder / bench.SESSION_FILE).exists() or bool(self.recorded())
        self.block = bench.read_session(self.folder)["block"] if started else block
        self.sentences = bench.BLOCKS[self.block]
        self.index, self.recording = 0, False
        self.scored.connect(self._show_results)
        self.progressed.connect(lambda message: self.status.setText(message))
        self.failed.connect(self._score_failed)

        # page 1: reading
        purpose = "a practice set" if self.block in bench.TUNING else "a test set"
        intro = caption(f"Read each sentence aloud the way you normally dictate. Set {self.block} of {len(bench.BLOCKS)} "
                        f"({purpose}), microphone: {microphone}. Rflow then counts the words it gets wrong, with and "
                        "without AI cleanup. About 6 minutes; you can stop and continue later.", "2")
        self.counter = caption("", "3", wrap=False)
        self.sentence = text("", "sentence")
        self.sentence.setMinimumHeight(110)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setFixedHeight(6)
        self.status = label("Press Record (or Space), read the sentence, then Stop.", tone="2")
        self.record_button = button("Record", self.toggle_recording, primary=True)
        self.redo_button = button("Redo", self.toggle_recording)
        self.back_button = button("Back", lambda: self.go(self.index - 1), kind="quiet")
        self.next_button = button("Next", lambda: self.go(self.index + 1), kind="quiet")
        self.score_button = button("Score", self.start_scoring)
        reading, reading_layout = card(14, (24, 24, 24, 24))
        for widget in (intro, self.counter, self.sentence, self.level, self.status):
            reading_layout.addWidget(widget)
        reading_layout.addStretch()
        reading_layout.addLayout(row(self.record_button, self.redo_button, self.back_button, self.next_button,
                                     self.score_button, stretch_at=4))

        # page 2: results
        self.report = QTextBrowser()
        self.report.setOpenExternalLinks(False)
        self.report.setMinimumHeight(200)
        field(self.report, "well")
        self.suggestions = QListWidget()
        self.suggestions.setMaximumHeight(130)
        field(self.suggestions, "well")
        self.results_status = caption("", "2")
        results, results_layout = card(12, (24, 24, 24, 24))
        results_layout.addWidget(self.report, 1)
        results_layout.addWidget(label("Worth adding to Your words (untick any you don't want):"))
        results_layout.addWidget(self.suggestions)
        add_row = row(button("Add to Your words", self._add_selected, primary=True, size="sm"), self.results_status)
        add_row.setStretch(1, 1)  # the note takes the rest of the line
        results_layout.addLayout(add_row)
        # Two rows: one would make the page wider than the window's smallest size.
        results_layout.addLayout(row(button("Score again", self.start_scoring, size="sm"),
                                     button("Score all tests", lambda: self.start_scoring(every=True), size="sm"),
                                     button("Open folder", lambda: open_folder(self.report_folder), size="sm"),
                                     button("New test", self.restart.emit, size="sm"), stretch_at=4))

        self.pages = QStackedWidget()
        self.pages.addWidget(reading)
        self.pages.addWidget(results)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pages)
        # Only while the test has the focus: Space must still type spaces on the other pages.
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._space,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.meter = QTimer(self)
        self.meter.setInterval(50)
        self.meter.timeout.connect(lambda: self.level.setValue(min(100, int(self.recorder.level * 400))))
        self.go(next(iter(self._to_read()), 0))  # a resumed test opens at the first sentence not read yet
        if self.recorded():
            self.status.setText(f"Continuing your unfinished test ({self.recorded()} of {len(self.sentences)} read).")

    def recorded(self) -> int:
        return len(bench.recordings(self.folder)) if self.folder.exists() else 0

    def _to_read(self) -> list[int]:
        return [i for i in range(len(self.sentences)) if not (self.folder / f"{i + 1:02}.wav").exists()]

    def go(self, index: int) -> None:
        if self.recording:
            return
        self.index = max(0, min(index, len(self.sentences) - 1))
        done = (self.folder / f"{self.index + 1:02}.wav").exists()
        self.counter.setText(f"Set {self.block}  ·  sentence {self.index + 1} of {len(self.sentences)}"
                             + ("  ·  recorded" if done else ""))
        self.sentence.setText(self.sentences[self.index])
        self.record_button.setEnabled(not done)
        self.redo_button.setEnabled(done)
        self.back_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index < len(self.sentences) - 1)
        count = self.recorded()
        self.score_button.setEnabled(count > 0)
        self.score_button.setText(f"Score ({count} recorded)" if count else "Score")

    def _space(self) -> None:
        if self.pages.currentIndex() == 0:
            self.toggle_recording()

    def toggle_recording(self) -> None:
        if not self.recording:
            try:
                self.recorder.start()
            except Exception as e:
                self.status.setText(f"Could not open the microphone: {e}")
                return
            self.recording = True
            self.meter.start()
            for b in (self.redo_button, self.back_button, self.next_button, self.score_button):
                b.setEnabled(False)
            self.record_button.setEnabled(True)
            self.record_button.setText("Stop")
            self.status.setText("Recording... read the sentence, then press Stop (or Space).")
            return
        self.recording = False
        self.meter.stop()
        self.level.setValue(0)
        self.record_button.setText("Record")
        stop_later = getattr(self.recorder, "stop_later", None)
        take = stop_later() if stop_later else Take.ready(self.recorder.stop(), self.recorder.rate)
        if take.seconds < 0.5:
            self.status.setText("That was too short; press Record and read the sentence again.")
            self.go(self.index)
            return
        if take.done.is_set():
            self._save(take)
        else:  # the moment after Stop is still being recorded, as in dictation; don't hold up the window for it
            self.record_button.setEnabled(False)
            QTimer.singleShot(int(self.recorder.tail * 1000) + 50, lambda: self._save(take))

    def _save(self, take: Take) -> None:
        audio = take.audio()
        if not (self.folder / bench.SESSION_FILE).exists():  # which set, microphone and rate: for the report
            describe = getattr(self.recorder, "describe", None)
            bench.write_session(self.folder, self.block, self.microphone,
                                describe() if describe else {"rate": self.recorder.rate})
        stem = self.folder / f"{self.index + 1:02}"
        save_wav(stem.with_suffix(".wav"), audio, take.rate)
        stem.with_suffix(".txt").write_text(self.sentences[self.index], encoding="utf-8")
        remaining = self._to_read()
        if remaining:
            self.status.setText("Saved. Next sentence:")
            later = [i for i in remaining if i > self.index]
            self.go(later[0] if later else remaining[0])
        else:
            self.status.setText("All sentences recorded. Press Score.")
            self.go(self.index)

    def stop(self) -> None:
        """Leaving the page: don't leave the microphone open (not even warm)."""
        if self.recording:
            self.recording = False
            self.meter.stop()
            self.recorder.stop()
            self.record_button.setText("Record")
            self.status.setText("Recording stopped. Press Record to read the sentence again.")
            self.go(self.index)
        close = getattr(self.recorder, "close", None)
        if close:
            close()

    def start_scoring(self, every: bool = False) -> None:
        """This test, or every test of the profile together (their folders sit next to this one)."""
        if self.recording or not self.recorded():
            return
        folders = bench.sessions(self.folder.parent) if every else [self.folder]
        self.score_button.setEnabled(False)
        self.results_status.setText("Scoring...")
        self.status.setText("Scoring...")

        def work() -> None:
            try:
                self.scored.emit(self._score(folders, self.progressed.emit))
            except Exception as e:
                log.exception("Scoring the reading test failed")
                self.failed.emit(str(e))
        threading.Thread(target=work, name="reading-test-score", daemon=True).start()

    def _show_results(self, results) -> None:
        best = min(results.scores, key=lambda s: s.error_rate)
        rows = "".join(
            f"<tr><td>{'<b>' if s is best else ''}{s.name}{'</b>' if s is best else ''}</td>"
            f"<td align=right>{s.error_rate:.1%}</td><td align=right>{_interval(s.interval)}</td>"
            f"<td>{_verdict(s.difference)}</td><td align=right>{s.term_error_rate:.1%}</td>"
            f"<td align=right>{s.percentile(50):.2f} s</td></tr>" for s in results.scores)
        misheard = "".join(f"<li>{said or '<i>(extra word)</i>'} → {heard or '<i>(missed)</i>'} ({times}×)</li>"
                           for said, heard, times in results.misheard[:10])
        sessions = len({r.session for r in results.recordings})
        self.report.setHtml(
            f"<h3>{len(results.recordings)} sentences scored" + (f" from {sessions} tests" if sessions > 1 else "") + "</h3>"
            "<table cellpadding=5><tr><th align=left>Setup</th><th>Word errors</th><th>95% range</th>"
            f"<th align=left>Against the first</th><th>Names and terms</th><th>Time</th></tr>{rows}</table>"
            f"<p>Lowest error rate: <b>{best.name}</b>."
            + (" One set gives a wide range: read more sets and score all tests for a surer answer." if sessions == 1
               else "") + "</p>"
            + "".join(f"<p>⚠ {warning}</p>" for warning in microphone_warnings(results))
            + (f"<h4>Most misheard (by speech recognition)</h4><ul>{misheard}</ul>" if misheard else ""))
        self.suggestions.clear()
        for word in results.suggestions:
            item = QListWidgetItem(word)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.suggestions.addItem(item)
        # The folder name only: a full path would make the window wider than the screen allows for it.
        self.report_folder = self.folder.parent / "summary" if sessions > 1 else self.folder
        self.results_status.setText(f"Saved as report.md in the {self.report_folder.name} folder.")
        self.results_status.setToolTip(str(self.report_folder))
        self.pages.setCurrentIndex(1)
        self.go(self.index)

    def _score_failed(self, message: str) -> None:
        self.status.setText(f"Scoring failed: {message}")
        self.results_status.setText(f"Scoring failed: {message}")
        self.go(self.index)

    def _add_selected(self) -> None:
        chosen = [self.suggestions.item(i).text() for i in range(self.suggestions.count())
                  if self.suggestions.item(i).checkState() == Qt.CheckState.Checked]
        added = self._add_words(chosen)
        self.results_status.setText(f"Added {added} word(s) to Your words. Press Score again to see the difference."
                                    if added else "Those words are already in Your words.")


def _interval(interval) -> str:
    return f"{interval[0]:.0%}–{interval[1]:.0%}" if interval else ""


def _verdict(difference) -> str:
    """A setup against the first one: better or worse only when the whole 95% range says so."""
    if difference is None:
        return "the baseline"
    mean, low, high = (100 * x for x in difference)
    return f"{mean:+.1f} points, " + ("better" if high < 0 else "worse" if low > 0 else "no clear difference")


def microphone_warnings(results) -> list[str]:
    """Plain advice from the measured audio: phone-quality Bluetooth, clipping, a very quiet microphone."""
    out = []
    for microphone, indexes in results.microphones().items():
        flags = [flag for k in indexes for flag in results.recordings[k].stats.flags]
        if flags.count("narrowband") * 2 > len(indexes):
            out.append(f"<b>{microphone}</b> sounds like a phone call (nothing above 4 kHz), as a Bluetooth headset "
                       "does while its microphone is on. Speech recognition loses consonants there; the laptop's own "
                       "microphone is usually better.")
        if "clipped" in flags:
            out.append(f"<b>{microphone}</b> was too loud in {flags.count('clipped')} recording(s): lower its level in "
                       "Windows' sound settings.")
        if flags.count("quiet") * 2 > len(indexes):
            out.append(f"<b>{microphone}</b> is very quiet: speak closer to it or raise its level.")
    return out


class ReadingTestPage(Page):
    def __init__(self, app, go_to=None):
        go_to = go_to or (lambda page: None)
        super().__init__("Reading test", "How well does Rflow understand your voice, microphone and words? Read a set of "
                                         "30 short sentences, then compare speech recognition alone and with AI "
                                         "cleanup. There are 5 sets; the more you read, the surer the numbers.",
                         back=back_button("Settings", go_to, "settings"))
        self.app = app
        self.test: ReadingTest | None = None
        self.holder = QVBoxLayout()
        self.add(self.holder)
        self.body.addStretch()

    def ensure_test(self, folder: Path | None = None, new: bool = False) -> ReadingTest:
        if self.test is None or new:
            if self.test is not None:
                self.test.stop()
                self.test.deleteLater()
            microphone, root = self.app.settings.microphone, self.app.bench_dir()  # each profile has its own tests
            folder = None if new else folder or bench.unfinished(root)
            self.test = ReadingTest(self.app.new_recorder(), self.app.score_reading, self.app.add_words,
                                    microphone or "Windows default", folder=folder or root / time.strftime("%Y-%m-%d_%H%M%S"),
                                    block=bench.next_block(root))
            self.test.restart.connect(lambda: self.ensure_test(new=True))
            self.holder.addWidget(self.test)
        return self.test

    def showEvent(self, event):
        super().showEvent(event)
        self.ensure_test()

    def hideEvent(self, event):
        if self.test:
            self.test.stop()
        super().hideEvent(event)


# ---------------------------------------------------------------- How Rflow hears you (speech models)

def _size(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _parakeet_takes_over(provider: str) -> str:
    """What happens when a cloud model or own server can't be reached: Parakeet types it, if it is downloaded."""
    if SPEECH_MODELS[DEFAULT_MODEL].installed():
        return f"If {provider} can't be reached, Parakeet types it on this computer."
    return f"Download Parakeet too (On this PC) to have it type when {provider} can't be reached."


def _status_line() -> tuple[QWidget, Lamp, QLabel]:
    widget, lamp, words = lamp_row("ok", "", stretch=False)
    lamp.hide()
    return widget, lamp, words


def _set_status(lamp: Lamp, words: QLabel, message: str, state: str = "") -> None:
    words.setText(message)
    lamp.setVisible(bool(state and message))
    if state:
        lamp.set_state(state)


class _ModelCard:
    """One speech model on this computer: what it is, its state, and the buttons that change it."""

    def __init__(self, app, model):
        self.app, self.model = app, model
        self.frame, layout = card(8, (20, 18, 20, 20))
        self.status_row, self.lamp, self.status = _status_line()
        layout.addLayout(row(label(model.name, "heading", wrap=False), self.status_row, stretch_at=1))
        layout.addWidget(label(model.summary, tone="2"))
        layout.addWidget(caption(f"Languages: {model.languages}  ·  Size: {model.size}", "3"))
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        layout.addWidget(self.progress)
        size = _size(model.download.size) if model.download else ""
        self.download = button(f"Download and use ({size})", lambda _=False: app.download_speech_model(model.key),
                               primary=True, size="sm", icon="download")
        self.cancel = button("Cancel", lambda _=False: app.cancel_download(), kind="quiet", size="sm")
        self.choose = button("Use this model", lambda _=False: app.choose_speech_model(model.key), primary=True, size="sm")
        self.remove = button("Remove download", lambda _=False: app.remove_speech_model(model.key), kind="danger",
                             size="sm")
        layout.addSpacing(4)
        layout.addLayout(row(self.download, self.cancel, self.choose, self.remove, stretch_at=3))

    def refresh(self) -> None:
        app, model, key = self.app, self.model, self.model.key
        chosen, in_use, loading, downloading = (app.settings.speech_model, app.speech_in_use(), app.loading_speech,
                                                app.downloading)
        installed = model.installed()
        here = bool(downloading) and downloading[0] == key
        state = ""
        if not model.ready:
            status = "Coming soon"
        elif here:
            done, total = downloading[1], downloading[2] or 1
            status, state = f"Downloading {done * 100 // total}%  ({_size(done)} of {_size(total)})", "warn"
            self.progress.setValue(done * 1000 // total)
        elif key == loading:
            status, state = "Loading...", "warn"
        elif key == in_use:
            status, state = "In use", "ok"
        else:
            status = "Downloaded" if installed and model.download else ""
        _set_status(self.lamp, self.status, status, state)
        self.progress.setVisible(here)
        self.download.setVisible(model.ready and not installed and not here)
        self.download.setEnabled(not downloading)  # one download at a time
        self.cancel.setVisible(here)
        self.choose.setVisible(model.ready and installed and key != chosen)
        self.choose.setEnabled(not loading)
        # Only a download can be removed (not Parakeet next to an older Rflow's program).
        self.remove.setVisible(bool(model.download) and model.download.installed() and key not in (chosen, in_use))


class _CloudCard:
    """A cloud speech model: the provider's key (the same one the AI connection uses), its model, a Test, and "Use this
    model" after a question, since the voice goes to the provider."""

    def __init__(self, page, app, model):
        self.page, self.app, self.model = page, app, model
        provider = CLOUD[model.key]
        self.frame, layout = card(10, (20, 18, 20, 20))
        self.status_row, self.lamp, self.status = _status_line()
        layout.addLayout(row(label(model.name, "heading", wrap=False), self.status_row, stretch_at=1))
        layout.addWidget(label(model.summary, tone="2"))
        layout.addWidget(caption(f"Languages: {model.languages}  ·  {model.size}", "3"))
        self.privacy = caption("", "warn")
        layout.addWidget(self.privacy)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.key = KeyField(app.gateway.key_for(model.key), "Paste your key: encrypted on this computer, shared with "
                                                            "the AI connection")
        key_link = button("Get a key", lambda _=False: QDesktopServices.openUrl(QUrl(provider.key_page)), link=True,
                          size="sm", icon="external", icon_after=True)
        key_row = row(self.key, key_link)
        key_row.setStretch(0, 1)
        form.addRow(caption("API key", "2", wrap=False), key_row)
        self.model_box = Choice(editable=True)  # one of the usual models, or any other name the provider knows
        self.model_box.addItems(provider.models)
        self.model_box.setCurrentText(self._saved_model())
        self.model_box.lineEdit().setPlaceholderText("Type to search, or any model name the provider knows")
        self.test = button("Test", self._test, size="sm")
        model_row = row(self.model_box, self.test)
        model_row.setStretch(0, 1)
        form.addRow(caption("Model", "2", wrap=False), model_row)
        layout.addLayout(form)
        self.result = caption("", "2")
        self.result.hide()  # until there is something to say: an empty line would leave a gap
        layout.addWidget(self.result)
        self.bar = SaveBar(self._use, self.discard)
        self.choose = self.bar.save  # "Use this model", or "Save" once it is the one in use
        layout.addWidget(self.bar)
        self.key.textChanged.connect(lambda _="": self._show_buttons())
        self.model_box.currentTextChanged.connect(lambda _="": self._show_buttons())

    def _saved_model(self) -> str:
        return self.app.settings.speech_cloud_models.get(self.model.key) or CLOUD[self.model.key].models[0]

    def edited(self) -> bool:
        return self.key.edited() or self.model_box.currentText().strip() != self._saved_model()

    def discard(self) -> None:
        self.key.show_saved()
        self.model_box.setCurrentText(self._saved_model())
        self._say("")
        self._show_buttons()

    def refresh(self) -> None:
        app, key = self.app, self.model.key
        name = CLOUD[key].name
        self.privacy.setText(f"Your voice is sent to {name} each time you dictate. " + _parakeet_takes_over(name))
        saved = app.gateway.key_for(key)
        if not self.key.edited() and saved != self.key.saved:
            self.key.show_saved(saved)  # changed in the AI connection; a key being typed here is left alone
        if key == app.loading_speech:
            _set_status(self.lamp, self.status, "Loading...", "warn")
        elif key == app.speech_in_use():
            _set_status(self.lamp, self.status, "In use", "ok")
        else:
            _set_status(self.lamp, self.status, "")
        self._show_buttons()

    def _show_buttons(self) -> None:
        chosen, edited = self.model.key == self.app.settings.speech_model, self.edited()
        self.choose.setText("Save" if chosen else "Use this model")
        self.bar.show_state(edited, (edited or not chosen) and not self.app.loading_speech)

    def _say(self, message: str) -> None:
        self.result.setText(message)
        self.result.setVisible(bool(message))

    def _use(self) -> None:
        name, api_key, model = CLOUD[self.model.key].name, self.key.text().strip(), self.model_box.currentText().strip()
        if not api_key:
            self._say(f"Enter your {name} API key first (Get a key).")
            return
        if self.model.key != self.app.settings.speech_model and not self.page.confirm(
                f"Use {name} for speech recognition?\n\nEach time you dictate, the recording of your voice is sent to "
                f"{name}, which turns it into text. " + _parakeet_takes_over(name)):
            return
        self.app.use_cloud_speech(self.model.key, api_key, model)
        self.key.show_saved(api_key)
        self._say("")
        self.page.refresh()  # the model in use, at the top of the page
        self.bar.saved("Saved. Active from the next dictation.")

    def _test(self) -> None:
        name, api_key, model = CLOUD[self.model.key].name, self.key.text().strip(), self.model_box.currentText().strip()
        if not api_key:
            self._say(f"Enter your {name} API key first (Get a key).")
            return
        self.test.setEnabled(False)
        self._say(f"Sending a sample sentence to {name}...")

        def done(answer, error) -> None:
            self.test.setEnabled(True)
            self._say(f"Failed: {error}" if error else f"OK: {answer}")
        run_in_background(self.frame, lambda: self.app.test_cloud_speech(self.model.key, api_key, model), done)


class _ServerCard:
    """The speech model on the user's own server: its address, a key if it needs one, the model (Load models lists the
    server's speech models first), a Test, and "Use this model". The address and key are kept apart from the AI
    connection's; a new card starts from the AI connection's own server (e.g. a company gateway)."""

    def __init__(self, page, app, model):
        self.page, self.app, self.model = page, app, model
        self.frame, layout = card(10, (20, 18, 20, 20))
        self.status_row, self.lamp, self.status = _status_line()
        layout.addLayout(row(label(model.name, "heading", wrap=False), self.status_row, stretch_at=1))
        layout.addWidget(label(model.summary, tone="2"))
        layout.addWidget(caption(f"Languages: {model.languages}  ·  {model.size}", "3"))
        self.note = caption("", "2")
        layout.addWidget(self.note)
        self._saved = app.gateway.speech_server()  # (address, key) as last saved
        address, key = self._saved if self._saved[0] else app.gateway.entries().get("vllm", ("", ""))
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.address = QLineEdit(address)
        self.address.setPlaceholderText("e.g. http://localhost:8000/v1, or your company's AI gateway")
        field(self.address)
        form.addRow(caption("Address", "2", wrap=False), self.address)
        self.key = KeyField(key, "Only if your server needs one (encrypted on this computer)")
        self.load = button("Load models", self._load_models, size="sm")
        key_row = row(self.key, self.load)
        key_row.setStretch(0, 1)
        form.addRow(caption("API key", "2", wrap=False), key_row)
        self.model_box = Choice(editable=True)  # one of the loaded models, or any name the server knows
        self.model_box.lineEdit().setPlaceholderText("e.g. whisper-1: Load models, then type to search")
        if app.settings.speech_server_model:
            self.model_box.addItem(app.settings.speech_server_model)
        self.model_box.setCurrentText(app.settings.speech_server_model)
        self.test = button("Test", self._test, size="sm")
        model_row = row(self.model_box, self.test)
        model_row.setStretch(0, 1)
        form.addRow(caption("Model", "2", wrap=False), model_row)
        layout.addLayout(form)
        self.result = caption("", "2")
        self.result.hide()  # until there is something to say: an empty line would leave a gap
        layout.addWidget(self.result)
        if not self._saved[0] and address:
            self._say("Filled in from AI cleanup's server. Load models to see what it offers.")
        self._shown = self._fields()  # what the card showed when it was last in step: changes are measured from it
        self.bar = SaveBar(self._use, self.discard)
        self.choose = self.bar.save  # "Use this model", or "Save" once it is the one in use
        layout.addWidget(self.bar)
        for box in (self.address, self.key):
            box.textChanged.connect(lambda _="": self._show_buttons())
        self.model_box.currentTextChanged.connect(lambda _="": self._show_buttons())

    def _fields(self) -> tuple[str, str, str]:
        return self.address.text().strip(), self.key.text().strip(), self.model_box.currentText().strip()

    def edited(self) -> bool:
        return self._fields() != self._shown

    def discard(self) -> None:
        address, key, model = self._shown
        self.address.setText(address)
        self.key.show_saved(key)
        self.model_box.setCurrentText(model)
        self._show_buttons()

    def refresh(self) -> None:
        app, key = self.app, self.model.key
        self.note.setText("Your voice goes to this server each time you dictate. " + _parakeet_takes_over("the server"))
        saved = app.gateway.speech_server()
        if saved != self._saved and saved[0] and not self.edited():
            self.address.setText(saved[0])  # changed elsewhere; what is being typed here is left alone
            self.key.show_saved(saved[1])
            self._shown = self._fields()
        self._saved = saved
        if key == app.loading_speech:
            _set_status(self.lamp, self.status, "Loading...", "warn")
        elif key == app.speech_in_use():
            _set_status(self.lamp, self.status, "In use", "ok")
        else:
            _set_status(self.lamp, self.status, "")
        self._show_buttons()

    def _show_buttons(self) -> None:
        chosen, edited = self.model.key == self.app.settings.speech_model, self.edited()
        self.choose.setText("Save" if chosen else "Use this model")
        self.bar.show_state(edited, (edited or not chosen) and not self.app.loading_speech)

    def _say(self, message: str) -> None:
        self.result.setText(message)
        self.result.setVisible(bool(message))

    def _ready(self, need_model: bool = True) -> bool:
        address, _, model = self._fields()
        if not address:
            self._say("Enter your server's address first.")
        elif need_model and not model:
            self._say("Choose a model first (Load models).")
        return bool(address and (model or not need_model))

    def _use(self) -> None:
        if self._ready():
            address, api_key, model = self._fields()
            self.app.use_server_speech(address, api_key, model)
            self._saved = self.app.gateway.speech_server()
            self.key.show_saved(api_key)
            self._shown = self._fields()
            self._say("")
            self.page.refresh()  # the model in use, at the top of the page
            self.bar.saved("Saved. Active from the next dictation.")

    def _busy(self, busy: bool, message: str = "") -> None:
        self.load.setEnabled(not busy)
        self.test.setEnabled(not busy)
        if message:
            self._say(message)

    def _load_models(self) -> None:
        if not self._ready(need_model=False):
            return
        address, api_key, _ = self._fields()
        self._busy(True, "Loading models...")

        def done(models, error) -> None:
            self._busy(False)
            if error:
                self._say(f"Failed: {error}")
                return
            current = self.model_box.currentText()
            self.model_box.clear()
            self.model_box.addItems(models)
            speech = [m for m in models if SPEECH.search(m)]
            self.model_box.setCurrentText(current or (speech[0] if speech else ""))
            self._say(f"Loaded {len(models)} models, {len(speech)} of them for speech (listed first): choose one, then "
                      "Test." if models else "The server lists no models; type the model's name.")
        run_in_background(self.frame, lambda: self.app.server_models(address, api_key), done)

    def _test(self) -> None:
        if not self._ready():
            return
        address, api_key, model = self._fields()
        self._busy(True, "Sending a sample sentence to the server...")

        def done(answer, error) -> None:
            self._busy(False)
            self._say(f"Failed: {error}" if error else f"OK: {answer}")
        run_in_background(self.frame, lambda: self.app.test_server_speech(address, api_key, model), done)


# What a scan verdict looks like: (lamp, words).
SCAN_LEVELS = {"recommended": ("ok", "Recommended"), "fast": ("ok", "Fast here"),
               "usable": ("off", "Works, with a short wait"), "slow": ("warn", "Slow on this computer"),
               "no": ("err", "Won't run well here")}
SCAN_HINTS = {"whisper-turbo": "Choose it for other languages, or on a computer with an NVIDIA card."}


class _ScanCard:
    """Scan my computer: the button, its progress, then this computer and a verdict for each model."""

    def __init__(self, app):
        self.app = app
        self.frame, layout = card(8, (20, 18, 20, 20))
        layout.addWidget(label("Not sure which model suits this PC?", "heading"))
        layout.addWidget(label("Scan this PC checks the memory, disk space, processor and graphics card, and tries each "
                               "downloaded model on a short sentence (about half a minute with Whisper). Models not "
                               "downloaded yet are estimated.", tone="2"))
        self.button = button("Scan this PC", lambda _=False: app.scan_computer(), size="sm")
        self.status = caption("", "3", wrap=False)
        layout.addLayout(row(self.button, self.status, stretch_at=2))
        self.result = QWidget()
        result = QVBoxLayout(self.result)
        result.setContentsMargins(0, 8, 0, 0)
        result.setSpacing(8)
        self.computer = caption("", "2")
        result.addWidget(self.computer)
        self.lines = QVBoxLayout()
        self.lines.setSpacing(6)
        result.addLayout(self.lines)
        layout.addWidget(self.result)

    def refresh(self) -> None:
        scanning, data = self.app.scanning, self.app.last_scan
        self.button.setEnabled(not scanning)
        self.button.setText("Scan again" if data else "Scan this PC")
        self.status.setText(scanning or (f"Last scan: {data['time']}" if data else ""))
        self.result.setVisible(bool(data))
        if not data:
            return
        self.computer.setText("This PC: " + Computer(**data["computer"]).summary())
        clear(self.lines)
        for verdict in data["verdicts"]:
            model = SPEECH_MODELS.get(verdict["key"])
            if not model:
                continue
            state, words = SCAN_LEVELS.get(verdict["level"], ("off", verdict["level"]))
            reason = verdict["reason"][:1].upper() + verdict["reason"][1:]
            hint = SCAN_HINTS.get(model.key, "") if verdict["level"] in ("usable", "slow") else ""
            line = QWidget()
            layout = QHBoxLayout(line)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
            layout.addWidget(Lamp(state), 0, Qt.AlignmentFlag.AlignTop)
            layout.addWidget(label(f"{model.name}: {words}. {reason}." + (f" {hint}" if hint else ""), tone="2"), 1)
            self.lines.addWidget(line)


class _InUseCard:
    """The top of How Rflow hears you: the model in use, and the language you speak. It's one setting for every model
    that can choose (Whisper, the cloud, your own server), so it's set here, once, not on each model's card."""

    def __init__(self, page, app):
        self.page, self.app = page, app
        self.frame, layout = card(10, (20, 18, 20, 20))
        self.lamp = Lamp("ok")
        self.title = label("", "heading", wrap=False)
        layout.addLayout(row(self.lamp, self.title, stretch_at=2, spacing=10))
        self.detail = label("", tone="2")
        layout.addWidget(self.detail)
        self.language = Choice(search=True)
        self.language.setMinimumWidth(260)
        for code, name in LANGUAGES.items():
            self.language.addItem(name, code)
        self.language.currentIndexChanged.connect(lambda _=0: self._show_state())
        layout.addLayout(row(caption("Language you speak", "2", wrap=False), self.language, stretch_at=2, spacing=16))
        self.note = caption("", "3")
        layout.addWidget(self.note)
        self.bar = SaveBar(self._save, self.discard)
        layout.addWidget(self.bar)
        self._select(app.settings.speech_language)

    def edited(self) -> bool:
        return self.language.currentData() != self.app.settings.speech_language

    def discard(self) -> None:
        self._select(self.app.settings.speech_language)
        self._show_state()

    def _select(self, code: str) -> None:
        self.language.blockSignals(True)  # showing the setting isn't changing it
        self.language.setCurrentIndex(max(0, self.language.findData(code)))
        self.language.blockSignals(False)
        self._shown = self.language.currentData()  # what it showed when last in step with the setting

    def refresh(self) -> None:
        app = self.app
        model = SPEECH_MODELS.get(app.speech_in_use())
        if app.loading_speech in SPEECH_MODELS:
            title, detail, state = (f"Switching to {SPEECH_MODELS[app.loading_speech].name}...",
                                    "Dictation goes on meanwhile.", "warn")
        elif model is None:
            title, detail, state = ("No speech model yet", "Download NVIDIA Parakeet below, or choose a cloud model or "
                                    "your server.", "warn")
        else:
            title, state = f"In use: {model.name}", "ok"
            if model.where == "cloud":
                detail = f"In the cloud · {app.settings.speech_cloud_models.get(model.key) or CLOUD[model.key].models[0]}"
            elif model.where == "server":
                detail = f"Your own server · {app.settings.speech_server_model}"
            else:
                detail = "On this PC: your voice stays here"
        self.title.setText(title)
        self.detail.setText(detail)
        self.lamp.set_state(state)
        self.note.setText(f"{model.name} hears English; the language is for Whisper, the cloud models and your own "
                          "server." if model is not None and not model.language_choice else "")
        self.note.setVisible(bool(self.note.text()))
        if self.language.currentData() == self._shown:
            self._select(app.settings.speech_language)  # a language saved meanwhile shows; one being chosen stays
        self._show_state()

    def _show_state(self) -> None:
        self.bar.show_state(self.edited())

    def _save(self) -> None:
        self.app.set_speech_language(self.language.currentData())
        self._shown = self.language.currentData()
        self._show_state()
        self.bar.saved("Saved. Active from the next dictation.")


class SpeechPage(Page):
    """Which model turns the voice into text: a building block of its own, chosen apart from the AI connection."""

    def __init__(self, app, go_to=None):
        go_to = go_to or (lambda page: None)
        super().__init__("How Rflow hears you", "The model that turns your voice into text. Any speech model works with "
                                                "any AI connection, and your words help every model.",
                         back=back_button("AI & models", go_to, "models"))
        self.app = app
        self.in_use = _InUseCard(self, app)
        self.add(self.in_use.frame)
        self.tabs = Segmented([(key, WHERE_LABELS.get(key, label_text)) for key, label_text in WHERE.items()])
        self.where: dict[str, QPushButton] = self.tabs.buttons
        self.tabs.changed.connect(self.show_where)
        self.add(self.tabs)
        self.groups = QStackedWidget()
        self.models: dict[str, _ModelCard | _CloudCard | _ServerCard] = {}
        for key in WHERE:
            self.groups.addWidget(self._group(key))
        self.add(self.groups)
        self.body.addStretch()
        chosen = SPEECH_MODELS.get(app.settings.speech_model, SPEECH_MODELS[DEFAULT_MODEL])
        self.show_where(chosen.where)

    def _group(self, where: str) -> QWidget:
        group = Host()
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        models = [m for m in SPEECH_MODELS.values() if m.where == where]
        if where == "cloud":
            layout.addWidget(label("The provider recognises your speech on its servers, with your API key: nothing to "
                                   "download, little memory, quick on any PC. Keys are shared with the AI connection.",
                                   tone="2"))
        cards = {"cloud": lambda model: _CloudCard(self, self.app, model),
                 "server": lambda model: _ServerCard(self, self.app, model)}
        for model in models:
            self.models[model.key] = cards.get(where, lambda model: _ModelCard(self.app, model))(model)
            layout.addWidget(self.models[model.key].frame)
        if where == "local":
            self.scan = _ScanCard(self.app)
            layout.addWidget(self.scan.frame)
        layout.addStretch()  # cards keep their own height when another group is taller
        return group

    def show_where(self, where: str) -> None:
        self.where[where].setChecked(True)
        self.groups.setCurrentIndex(list(WHERE).index(where))
        self.refresh()

    def refresh(self) -> None:
        self.in_use.refresh()
        for model_card in self.models.values():
            model_card.refresh()
        self.scan.refresh()

    def _editable(self) -> list:
        return [self.in_use, *(c for c in self.models.values() if hasattr(c, "edited"))]

    def unsaved(self) -> bool:
        return any(part.edited() for part in self._editable())

    def discard_changes(self) -> None:
        for part in self._editable():
            if part.edited():
                part.discard()

    def confirm(self, question: str) -> bool:
        return QMessageBox.question(self, APP_NAME, question) == QMessageBox.StandardButton.Yes


# ---------------------------------------------------------------- AI connection (the provider, key and models)

def _model_box(hint: str) -> Choice:
    box = Choice(editable=True)  # pick from the loaded list (typing filters it), or type any model name
    box.lineEdit().setPlaceholderText(hint)
    return box


class CleanupPage(Page):
    """The AI connection: the provider, its key, the model and a backup model. The AI cleanup uses it, and so do Text
    Transform and Translate."""

    def __init__(self, app, go_to=None):
        go_to = go_to or (lambda page: None)
        super().__init__("AI connection", "An AI model adds punctuation, removes filler words and spells your words "
                                          "right. Your voice stays on this PC; only the finished text goes to the "
                                          "provider.", back=back_button("AI & models", go_to, "models"))
        self.app = app
        frame, layout = card(14, (20, 18, 20, 20))
        self.cleanup_on = Toggle("Clean up dictation")
        self.cleanup_on.setToolTip("If the model fails, the backup model is used; if the provider can't help in time, "
                                   "the text is typed as heard.")
        switch, _, _ = setting_row("Clean up dictation", "Punctuation, no filler words, your names spelled right.",
                                   self.cleanup_on)
        switch.layout().setContentsMargins(0, 0, 0, 4)
        layout.addWidget(switch)
        layout.addWidget(divider())
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(16)
        self.form.setVerticalSpacing(12)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.provider = Choice()
        for provider in PROVIDERS.values():
            self.provider.addItem(provider.name, provider.key)
        self.form.addRow(caption("Provider", "2", wrap=False), self.provider)
        self.gateway_url = QLineEdit()
        field(self.gateway_url)
        self.form.addRow(caption("Address", "2", wrap=False), self.gateway_url)
        self.api_key = KeyField()
        self.load_button = button("Load models", self._load_models, size="sm")
        key_row = row(self.api_key, self.load_button)
        key_row.setStretch(0, 1)
        self.form.addRow(caption("API key", "2", wrap=False), key_row)
        self.key_link = button("Get a key", self._open_key_page, link=True, size="sm", icon="external", icon_after=True)
        self.key_note = caption("", "3", wrap=False)
        self.key_row = row(self.key_link, self.key_note, stretch_at=2)
        self.form.addRow("", self.key_row)
        self.model = _model_box("")
        self.test_button = button("Test", self._test, size="sm")
        model_row = row(self.model, self.test_button)
        model_row.setStretch(0, 1)
        self.form.addRow(caption("Model", "2", wrap=False), model_row)
        self.fallback = _model_box("optional: used if the model fails")
        self.form.addRow(caption("Backup model", "2", wrap=False), self.fallback)
        layout.addLayout(self.form)
        self.test_result = caption("", "2")
        layout.addWidget(self.test_result)
        self.bar = SaveBar(self._save, self.discard_changes)
        layout.addWidget(self.bar)
        self.add(frame)
        self.add(row(label("This connection also powers Text Transform and Translate.", tone="2", wrap=False),
                     button("Tools", lambda: go_to("tools"), link=True, size="sm", icon="chevron-right", icon_after=True),
                     stretch_at=2))
        self.body.addStretch()
        self.discard_changes()  # the fields as saved
        self.provider.currentIndexChanged.connect(self._provider_changed)
        for changed in (self.cleanup_on.toggled, self.gateway_url.textChanged, self.api_key.textChanged,
                        self.model.currentTextChanged, self.fallback.currentTextChanged):
            changed.connect(lambda *_: self._show_state())

    def discard_changes(self) -> None:
        """The fields as saved: when the page is built, and Cancel."""
        settings, gateway = self.app.settings, self.app.gateway
        # The (address, key, model, backup model) of each provider left on this page, so switching back loses nothing.
        # The others come from app.gateway, where the speech page may also have saved a key.
        self._memory: dict[str, tuple[str, str, str, str]] = {}
        # Nothing chosen yet: start with the first provider in the list rather than an empty custom server.
        self._provider = gateway.service.key if gateway.provider or gateway.base_url else next(iter(PROVIDERS))
        self.provider.blockSignals(True)
        self.provider.setCurrentIndex(self.provider.findData(self._provider))
        self.provider.blockSignals(False)
        self.cleanup_on.blockSignals(True)
        self.cleanup_on.setChecked(settings.cleanup)
        self.cleanup_on.blockSignals(False)
        self.gateway_url.setText(gateway.base_url)
        self.api_key.show_saved(gateway.key_for(self._provider))
        for box, value in ((self.model, settings.cleanup_model), (self.fallback, settings.cleanup_fallback)):
            box.clear()
            if value:
                box.addItem(value)
            box.setCurrentText(value)
        self.test_result.setText("")
        self._show_provider()
        self._saved = self._fields()
        self._show_state()

    def _fresh(self, key: str) -> tuple[str, str, str, str]:
        """A provider's fields before any change here: its saved address and key."""
        url, secret = self.app.gateway.entries().get(key, ("", ""))
        return url or (PROVIDERS[key].url if PROVIDERS[key].own_server else ""), secret, "", ""

    def _fields(self) -> tuple:
        """What Save would change: the switch, the provider and its fields, and the address and key typed for another
        provider (their models aren't kept)."""
        others = tuple(sorted((key, value[:2]) for key, value in self._memory.items()
                              if key != self._provider and value[:2] != self._fresh(key)[:2]))
        return (self.cleanup_on.isChecked(), self._provider, self.gateway_url.text().strip(), self.api_key.text().strip(),
                self.model.currentText().strip(), self.fallback.currentText().strip(), others)

    def unsaved(self) -> bool:
        return self._fields() != self._saved

    def _show_state(self) -> None:
        self.bar.show_state(self.unsaved())

    def _show_provider(self) -> None:
        """What the chosen provider needs: a key (cloud), an address (own server), or both."""
        p = PROVIDERS[self._provider]
        self.form.setRowVisible(self.gateway_url, p.own_server)
        self.gateway_url.setPlaceholderText(p.url or "e.g. http://localhost:8000/v1, or your company's AI gateway")
        self.api_key.setPlaceholderText("Encrypted on this computer" if p.needs_key
                                        else "Only if your server needs one (encrypted on this computer)")
        self.form.setRowVisible(self.key_row, bool(p.key_page))
        self.key_link.setVisible(bool(p.key_page))
        self.key_note.setText(f"from {p.name}" if p.key_page else "")
        self.model.lineEdit().setPlaceholderText(p.hint or "choose after Load models, or type a model name")

    def _provider_changed(self) -> None:
        new = self.provider.currentData()
        self._memory[self._provider] = (self.gateway_url.text().strip(), self.api_key.text().strip(),
                                        self.model.currentText().strip(), self.fallback.currentText().strip())
        url, key, model, fallback = self._memory.get(new, self._fresh(new))
        self._provider = new
        self.gateway_url.setText(url)
        saved_key = self._fresh(new)[1]
        self.api_key.show_saved(saved_key)
        if key != saved_key:
            self.api_key.setText(key)  # typed here before switching away: still a change, not the saved key
        for box, value in ((self.model, model), (self.fallback, fallback)):
            box.clear()
            box.setCurrentText(value)
        self.test_result.setText("")
        self._show_provider()
        self._show_state()

    def _open_key_page(self) -> None:
        QDesktopServices.openUrl(QUrl(PROVIDERS[self._provider].key_page))

    def refresh(self) -> None:
        clean = not self.unsaved()
        if clean and (self.app.settings.cleanup != self.cleanup_on.isChecked()
                      or self.app.settings.cleanup_model != self.model.currentText().strip()):
            self.discard_changes()  # changed elsewhere (the switch on AI & models, the welcome): show what's saved
            return
        saved = self.app.gateway.key_for(self._provider)
        if not self.api_key.edited() and saved != self.api_key.saved:
            self.api_key.show_saved(saved)  # changed on the speech page; a key being typed is left alone
        if clean:
            self._saved = self._fields()  # a key saved elsewhere isn't a change made here
        self._show_state()

    def result(self) -> tuple[bool, str, str, GatewayConfig]:
        p = PROVIDERS[self._provider]
        entries = self.app.gateway.entries()
        entries.update({key: (url, secret) for key, (url, secret, _, _) in self._memory.items()})
        others = {key: (url, secret) for key, (url, secret) in entries.items() if key != p.key and (url or secret)}
        gateway = GatewayConfig(self.gateway_url.text().strip() if p.own_server else "", self.api_key.text().strip(),
                                p.key, others)
        return self.cleanup_on.isChecked(), self.model.currentText().strip(), self.fallback.currentText().strip(), gateway

    def _save(self) -> None:
        on, model, fallback, gateway = self.result()
        self.app.save_cleanup(on, model, fallback, gateway)
        self.api_key.show_saved(gateway.api_key)
        self._saved = self._fields()
        self._show_state()
        self.bar.saved("Saved. Active from the next dictation." if on and model else "Saved. AI cleanup is off.")

    def _busy(self, busy: bool, message: str = "") -> None:
        self.load_button.setEnabled(not busy)
        self.test_button.setEnabled(not busy)
        if message:
            self.test_result.setText(message)

    def _load_models(self) -> None:
        gateway = self.result()[3]
        if not gateway.address:
            self.test_result.setText("Enter your server's address first.")
            return
        self._busy(True, "Loading models...")

        def done(models, error) -> None:
            self._busy(False)
            if error:
                self.test_result.setText(f"Failed: {error}")
                return
            for box in (self.model, self.fallback):
                current = box.currentText()
                box.clear()
                if box is self.fallback:
                    box.addItem("")  # no backup model
                box.addItems(models)
                box.setCurrentText(current)
            self.test_result.setText(f"Loaded {len(models)} models: choose one, then Test." if models
                                     else "The provider lists no models; type a model name.")
        run_in_background(self, lambda: Polisher(gateway, "").models(), done)

    def _test(self) -> None:
        on, model, _, gateway = self.result()
        if not model:
            self.test_result.setText("Choose a model first (Load models).")
            return
        self._busy(True, "Testing...")
        words = self.app.settings.vocabulary

        def done(answer, error) -> None:
            self._busy(False)
            self.test_result.setText(f"Failed: {error}" if error else f"OK: {answer}")
        run_in_background(self, lambda: Polisher(gateway, model, words).check(), done)


# ---------------------------------------------------------------- AI & models (the overview)

def provider_name(key: str) -> str:
    return PROVIDERS[key].name.split(" (")[0] if key in PROVIDERS else key


class ModelsPage(Page):
    """How Rflow hears you and the AI connection side by side, the microphone, and the cleanup switch. Change leads
    one level down (SpeechPage, CleanupPage)."""

    def __init__(self, app, go_to):
        super().__init__("AI & models", "How Rflow hears you, and the AI that polishes what you say.")
        self.app, self.go_to = app, go_to
        pair = QHBoxLayout()
        pair.setSpacing(24)

        self.speech_card, column = card(14, (20, 20, 20, 20))
        self.speech_status, self.speech_lamp, self.speech_state = lamp_row("ok", "In use", stretch=False)
        head = QHBoxLayout()
        head.addWidget(label("How Rflow hears you", "heading", wrap=False))
        head.addStretch()
        head.addWidget(self.speech_status)
        column.addLayout(head)
        self.speech_name = label("", "rowtitle", wrap=False)
        self.speech_detail = caption("", "2", wrap=False)
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addWidget(self.speech_name)
        names.addWidget(self.speech_detail)
        column.addLayout(row(names, button("Change", lambda: go_to("speech"), size="sm"), stretch_at=1, spacing=12))
        column.addWidget(divider())
        column.addWidget(caption("Microphone", "2", wrap=False))
        self.microphone = MicrophoneBox(app.settings.microphone, app.microphones(), app.default_microphone(),
                                        source=lambda: (app.microphones(), app.default_microphone()))
        self.microphone.changed.connect(self._microphone_chosen)
        self.microphone.followed.connect(self._show_call_warning)
        column.addWidget(self.microphone)
        self.call_warning = caption("This is a Bluetooth headset's microphone. It records in call quality (like a "
                                    "phone), so Rflow gets more words wrong, and the headset plays sound in call "
                                    "quality while it's open. The laptop's own microphone is usually clearer.", "warn")
        column.addWidget(self.call_warning)
        column.addStretch()
        pair.addWidget(self.speech_card, 1)

        self.ai_card, column = card(14, (20, 20, 20, 20))
        self.ai_status, self.ai_lamp, self.ai_state = lamp_row("ok", "Connected", stretch=False)
        head = QHBoxLayout()
        head.addWidget(label("AI connection", "heading", wrap=False))
        head.addStretch()
        head.addWidget(self.ai_status)
        column.addLayout(head)
        self.ai_name = label("", "rowtitle", wrap=False)
        self.ai_model = QLabel()
        self.ai_model.setFont(font(12, 500, mono=True))
        self.ai_model.setProperty("tone", "2")
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addWidget(self.ai_name)
        names.addWidget(self.ai_model)
        self.ai_change = button("Change", lambda: go_to("cleanup"), size="sm")
        column.addLayout(row(names, self.ai_change, stretch_at=1, spacing=12))
        column.addWidget(divider())
        self.key_caption = caption("API key", "2", wrap=False)
        column.addWidget(self.key_caption)
        self.key_view = bare(QLineEdit())
        self.key_view.setReadOnly(True)
        self.key_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.key_view.setFont(font(13, 500, mono=True, spacing=0.06))
        self.key_box = Card("field", 14)
        box = QHBoxLayout(self.key_box)
        box.setContentsMargins(12, 0, 12, 0)
        box.addWidget(self.key_view)
        self.key_box.setFixedHeight(40)
        self.test_button = button("Test", self._test, size="sm")
        column.addLayout(row(self.key_box, self.test_button, spacing=12))
        self.test_note = caption("", "3")
        self.test_note.hide()
        column.addWidget(self.test_note)
        column.addStretch()
        pair.addWidget(self.ai_card, 1)
        self.add(pair)

        self.cleanup_card, column = card(0, (20, 4, 20, 4))
        self.cleanup = Toggle("Clean up dictation")
        self.cleanup.toggled.connect(self._cleanup_toggled)
        switch, _, self.cleanup_caption = setting_row(
            "Clean up dictation", "Punctuation, no filler words, your names spelled right. Only text is sent, never your "
            "voice.", self.cleanup)
        column.addWidget(switch)
        column.addWidget(divider())
        powers = QHBoxLayout()
        powers.setContentsMargins(0, 12, 0, 12)
        powers.setSpacing(12)
        sparkle = QLabel()
        sparkle.setPixmap(theme.icon_pixmap("tools", tok("violet").name(), 16, 1.0))
        powers.addWidget(sparkle)
        powers.addWidget(caption("This connection also powers Text Transform and Translate.", "2"), 1)
        powers.addWidget(button("Tools", lambda: go_to("tools"), link=True, size="sm", icon="chevron-right",
                                icon_after=True))
        column.addLayout(powers)
        self.add(self.cleanup_card)

        self.advanced = advanced_row("Advanced", "Every speech model, your own server, Scan this PC, the backup AI "
                                                 "model, the Reading test", self._toggle_advanced)
        self.add(self.advanced)
        self.advanced_card, column = card(0, (20, 4, 20, 4))
        for i, (title, words, page) in enumerate([
                ("Every speech model", "Parakeet and Whisper on this PC, the cloud models, your own server, Scan this PC",
                 "speech"),
                ("AI provider and backup model", "OpenAI, Anthropic, Gemini, Groq, Ollama or your own server", "cleanup"),
                ("Reading test", "Read 30 sentences: how many words does Rflow get wrong?", "reading")]):
            if i:
                column.addWidget(divider())
            column.addWidget(link_row(title, words, lambda _=False, p=page: go_to(p)))
        self.advanced_card.hide()
        self.add(self.advanced_card)
        self.body.addStretch()
        self._show_call_warning()

    def _toggle_advanced(self) -> None:
        open_ = self.advanced_card.isHidden()
        self.advanced_card.setVisible(open_)
        self.advanced.button.icon_name = "chevron-down" if open_ else "chevron-right"
        self.advanced.button.update()

    def _microphone_chosen(self, device: str) -> None:
        self.app.apply_settings(dataclasses.replace(self.app.settings, microphone=device))
        self._show_call_warning()

    def _show_call_warning(self) -> None:
        self.call_warning.setVisible(call_quality(self.microphone.device() or None))

    def _cleanup_toggled(self, on: bool) -> None:
        s = self.app.settings
        if on != s.cleanup:
            self.app.save_cleanup(on, s.cleanup_model, s.cleanup_fallback, self.app.gateway)

    def refresh(self) -> None:
        app = self.app
        s = app.settings
        model = SPEECH_MODELS.get(app.speech_in_use())
        loading, downloading = app.loading_speech, app.downloading
        fetching = bool(downloading) and downloading[0] == DEFAULT_MODEL and model is None
        if fetching:
            done, total = downloading[1], downloading[2] or 1
            self.speech_lamp.set_state("warn")
            self.speech_state.setText(f"Downloading {done * 100 // total}%")
            self.speech_name.setText("NVIDIA Parakeet")
            self.speech_detail.setText("On this PC · English · arriving")
        elif loading in SPEECH_MODELS:
            self.speech_lamp.set_state("warn")
            self.speech_state.setText("Loading")
            self.speech_name.setText(SPEECH_MODELS[loading].name)
            self.speech_detail.setText("Dictation goes on meanwhile")
        elif model is None:
            self.speech_lamp.set_state("warn")
            self.speech_state.setText("Not ready")
            self.speech_name.setText("No speech model yet")
            self.speech_detail.setText("Choose one: Parakeet on this PC, or a cloud model")
        else:
            self.speech_lamp.set_state("ok")
            self.speech_state.setText("In use")
            self.speech_name.setText(model.name)
            if model.where == "cloud":
                self.speech_detail.setText(f"In the cloud · {s.speech_cloud_models.get(model.key) or CLOUD[model.key].models[0]}")
            elif model.where == "server":
                self.speech_detail.setText(f"Your own server · {s.speech_server_model or 'no model chosen'}")
            else:
                verdict = self._verdict(model.key)
                self.speech_detail.setText(" · ".join(x for x in ("On this PC", model.languages, verdict) if x))
        self._show_call_warning()

        gateway = app.gateway
        connected = bool(gateway.address and s.cleanup_model)
        key = gateway.service.key
        self.ai_name.setText(provider_name(key) if connected or gateway.provider else "Not connected")
        self.ai_model.setText(s.cleanup_model if connected else "Add punctuation and drop filler words")
        self.ai_model.setFont(font(12, 500, mono=connected))
        self.ai_change.setText("Change" if connected else "Connect")
        self.ai_change.set_kind("key" if connected else "primary")
        if connected:
            self.ai_lamp.set_state("ok" if s.cleanup else "off")
            self.ai_state.setText("Connected" if s.cleanup else "Connected · cleanup off")
        else:
            self.ai_lamp.set_state("off")
            self.ai_state.setText("Not connected")
        secret = gateway.key_for(key)
        self.key_view.setText(_masked(secret) if secret else ("No key needed" if not PROVIDERS[key].needs_key
                                                               else "No key yet"))
        for widget in (self.key_caption, self.key_box, self.test_button):
            widget.setVisible(connected or bool(secret))
        self.test_button.setEnabled(connected)
        self.cleanup.blockSignals(True)
        self.cleanup.setChecked(s.cleanup and connected)
        self.cleanup.blockSignals(False)
        self.cleanup.setEnabled(connected)
        self.cleanup_caption.setText("Punctuation, no filler words, your names spelled right. Only text is sent, never "
                                     "your voice." if connected else "Connect an AI first (AI connection).")

    def _verdict(self, key: str) -> str:
        scan = self.app.last_scan
        for verdict in (scan or {}).get("verdicts", []):
            if verdict["key"] == key:
                return {"recommended": "fast here", "fast": "fast here", "usable": "a short wait",
                        "slow": "slow here"}.get(verdict["level"], "")
        return ""

    def _test(self) -> None:
        s, gateway = self.app.settings, self.app.gateway
        self.test_button.setEnabled(False)
        self.ai_state.setText("Testing…")
        self.test_note.hide()
        started = time.monotonic()

        def done(answer, error) -> None:
            self.test_button.setEnabled(True)
            if error:
                self.ai_lamp.set_state("err")
                self.ai_state.setText("Failed")
                self.test_note.setText(str(error))
                self.test_note.show()
            else:
                self.ai_lamp.set_state("ok")
                self.ai_state.setText(f"Connected · {time.monotonic() - started:.1f} s")
        run_in_background(self, lambda: self.app.check_ai(gateway, s.cleanup_model), done)


# ---------------------------------------------------------------- Tools (Text Transform and Translate)

def _shortcut_caps(shortcut: str, double_label: str = "") -> QWidget:
    """A Tools shortcut as keys: "double ctrl" → double-tap [Ctrl], "ctrl+c+c" → [Ctrl] [C] [C]."""
    if shortcut == "double ctrl":
        return keys("Ctrl", "sm")
    if shortcut == "ctrl+c+c":
        return keys("Ctrl C C", "sm")
    try:
        return keys(key_names(parse_hotkey(shortcut).label), "sm")
    except ValueError:
        return keys(shortcut, "sm")


class ToolsPage(Page):
    """Text Transform and Translate side by side, each with its switch, and a box to try them in. Their details (the
    voice phrases, the menu, the shortcuts) are one level down."""

    def __init__(self, app, go_to):
        super().__init__("Tools", "Rewrite or translate any text, in any app.")
        self.app, self.go_to = app, go_to
        self.connect_card, connect = card(12, (16, 12, 12, 12), kind="well", horizontal=True)
        info = QLabel()
        info.setPixmap(theme.icon_pixmap("info", tok("warn").name(), 16, 1.0))
        connect.addWidget(info)
        connect.addWidget(caption("Text Transform and Translate need an AI connection. Connect one: it takes a minute.",
                                  "2"), 1)
        connect.addWidget(button("Connect an AI", lambda: go_to("cleanup"), primary=True, size="sm"))
        self.add(self.connect_card)
        pair = QHBoxLayout()
        pair.setSpacing(24)

        self.transform_card, column = card(14, (20, 20, 20, 20))
        self.transform_on = Toggle("Text Transform")
        self.transform_on.toggled.connect(self._transform_toggled)
        head = QVBoxLayout()
        head.setSpacing(4)
        head.addWidget(label("Text Transform", "heading"))
        self.transform_how = QHBoxLayout()
        self.transform_how.setSpacing(6)
        head.addLayout(self.transform_how)
        column.addLayout(row(head, self.transform_on, stretch_at=1))
        self.menu_well, self.menu_rows = card(2, (6, 6, 6, 6), kind="well")
        column.addWidget(self.menu_well)
        column.addWidget(button("Customise phrases and menu", lambda: go_to("transform"), link=True, size="sm",
                                icon="chevron-right", icon_after=True), 0, Qt.AlignmentFlag.AlignLeft)
        column.addStretch()
        pair.addWidget(self.transform_card, 1, Qt.AlignmentFlag.AlignTop)

        self.translate_card, column = card(12, (20, 20, 20, 20))
        self.translate_on = Toggle("Translate")
        self.translate_on.toggled.connect(self._translate_toggled)
        head = QVBoxLayout()
        head.setSpacing(4)
        head.addWidget(label("Translate", "heading"))
        self.translate_how = QHBoxLayout()
        self.translate_how.setSpacing(6)
        head.addLayout(self.translate_how)
        column.addLayout(row(head, self.translate_on, stretch_at=1))
        column.addWidget(caption("Translate into", "2", wrap=False))
        self.target = Choice(search=True)
        self.target.addItems(list(TRANSLATE_LANGUAGES))
        self.target.currentIndexChanged.connect(self._apply_languages)
        column.addWidget(self.target)
        self.second_caption = caption("", "2", wrap=False)
        column.addWidget(self.second_caption)
        self.second = Choice(search=True)
        self.second.addItem("Automatic: Windows' language, or English", "")
        for name in TRANSLATE_LANGUAGES:
            self.second.addItem(name, name)
        self.second.currentIndexChanged.connect(self._apply_languages)
        column.addWidget(self.second)
        column.addWidget(button("More options", lambda: go_to("translate"), link=True, size="sm", icon="chevron-right",
                                icon_after=True), 0, Qt.AlignmentFlag.AlignLeft)
        pair.addWidget(self.translate_card, 1, Qt.AlignmentFlag.AlignTop)
        self.add(pair)

        self.trial_card, column = card(14, (20, 18, 20, 20))
        self.trial_tabs = Segmented([("concise", "Concise"), ("professional", "Professional"), ("translate", "Translate")])
        self.trial_tabs.set_current("concise")
        self.trial_tabs.changed.connect(self._try)
        column.addLayout(row(label("Try it", "heading"), self.trial_tabs, stretch_at=1))
        self.sample = QPlainTextEdit()
        self.sample.setPlainText("I checked the deployment and everything looks good, but we still have one issue with the "
                                 "database migration, and I think we should fix that before production.")
        field(self.sample, "well")
        fixed_height(self.sample, 112)
        self.result = QTextBrowser()
        field(self.result, "well")
        fixed_height(self.result, 112)
        self.result.setPlaceholderText("The result appears here.")
        column.addLayout(row(self.sample, self.result, spacing=16))
        self.note = caption("", "3")
        self.note.hide()
        column.addWidget(self.note)
        self.add(self.trial_card)
        self.body.addStretch()

    def refresh(self) -> None:
        s, model = self.app.settings, self.app.transform_model()
        provider = provider_name(self.app.gateway.service.key)
        self.subtitle.setText(f"Rewrite or translate any text, in any app. Uses your {provider} connection." if model
                              else "Rewrite or translate any text, in any app.")
        self.connect_card.setVisible(not model)
        for box, on in ((self.transform_on, bool(s.transform_shortcut or s.voice_commands)),
                        (self.translate_on, bool(s.translate_shortcut))):
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)
        clear(self.transform_how)
        if s.transform_shortcut:
            self.transform_how.addWidget(caption("Select text," if s.transform_shortcut != "double ctrl" else
                                                 "Select text, double-tap", "2", wrap=False))
            self.transform_how.addWidget(_shortcut_caps(s.transform_shortcut))
        else:
            self.transform_how.addWidget(caption("Say “make it concise” while dictating" if s.voice_commands
                                                 else "Off", "2", wrap=False))
        self.transform_how.addStretch()
        clear(self.menu_rows)
        phrases = phrases_for(s.command_phrases)
        for i, key in enumerate([k for k in s.transforms if k in TRANSFORMS][:9]):
            line = QWidget()
            layout = QHBoxLayout(line)
            layout.setContentsMargins(10, 0, 10, 0)
            layout.setSpacing(12)
            line.setFixedHeight(36)
            layout.addWidget(keycap(str(i + 1), "sm"))
            layout.addWidget(label(TRANSFORMS[key].name, wrap=False), 1)
            if i == 0 and phrases.get(key):
                layout.addWidget(caption(f"“{phrases[key][0]}”", "3", wrap=False))
            self.menu_rows.addWidget(line)
        self.menu_well.setVisible(bool(s.transforms))
        clear(self.translate_how)
        if s.translate_shortcut:
            self.translate_how.addWidget(caption("Select text, press", "2", wrap=False))
            self.translate_how.addWidget(_shortcut_caps(s.translate_shortcut))
        else:
            self.translate_how.addWidget(caption("Off", "2", wrap=False))
        self.translate_how.addStretch()
        for box, value in ((self.target, s.translate_to), (self.second, s.translate_second)):
            box.blockSignals(True)
            if box is self.target:
                box.setCurrentText(value)
            else:
                box.setCurrentIndex(max(0, box.findData(value)))
            box.blockSignals(False)
        self.second_caption.setText(f"When it's already in {s.translate_to}, into")
        for key in ("concise", "professional"):
            self.trial_tabs.buttons[key].setText(TRANSFORMS[key].name)
        for b in self.trial_tabs.buttons.values():
            b.setEnabled(bool(model))

    def _transform_toggled(self, on: bool) -> None:
        s = self.app.settings
        shortcut = (s.transform_shortcut or "double ctrl") if on else ""
        self.app.apply_settings(dataclasses.replace(s, transform_shortcut=shortcut, voice_commands=on))
        self.refresh()

    def _translate_toggled(self, on: bool) -> None:
        s = self.app.settings
        self.app.apply_settings(dataclasses.replace(s, translate_shortcut=(s.translate_shortcut or "ctrl+c+c") if on
                                                    else ""))
        self.refresh()

    def _apply_languages(self, *_) -> None:
        self.app.apply_settings(dataclasses.replace(self.app.settings, translate_to=self.target.currentText(),
                                                    translate_second=self.second.currentData()))
        self.refresh()

    def _say(self, message: str) -> None:
        self.note.setText(message)
        self.note.setVisible(bool(message))

    def _try(self, key: str) -> None:
        sample = self.sample.toPlainText().strip()
        if not sample:
            self._say("Type or paste some text first.")
            return
        s = self.app.settings
        self.result.setPlainText("")
        self._say(f"Translating into {s.translate_to}…" if key == "translate" else f"{TRANSFORMS[key].name}…")
        for b in self.trial_tabs.buttons.values():
            b.setEnabled(False)

        def done(result, error) -> None:
            self.refresh()
            if error:
                self._say(f"Didn't work: {error}")
            elif key == "translate":
                self.result.setPlainText(result.text)
                self._say(f"{result.target} in {result.seconds:.1f} s")
            elif not result.accepted:
                self._say("Kept the text: " + "; ".join(result.reasons))
            else:
                self.result.setHtml(result.html or html.escape(result.plain))
                self._say(f"{TRANSFORMS[key].name} in {result.seconds:.1f} s")
        if key == "translate":
            second = s.translate_second or fallback_second(s.translate_to, system_language())
            run_in_background(self, lambda: self.app.run_translation(sample, s.translate_to, second), done)
        else:
            run_in_background(self, lambda: self.app.run_transform(sample, key), done)


class TransformPage(Page):
    """Text Transform (sst.transformui): the voice commands and their phrases (sst.commands), the menu's shortcut and
    transforms, and a box to try them in."""

    def __init__(self, app, go_to):
        super().__init__("Text Transform", "Speak normally first; transform the text afterwards, only when you want to: "
                                           "say “make it concise”, or double-tap Ctrl for a menu. It works on "
                                           "the text selected in any app, or else on your last dictation. Numbers, "
                                           "names, dates and your “maybe” stay.",
                         back=back_button("Tools", go_to, "tools"))
        self.app = app
        s = app.settings
        voice, layout = card(10, (20, 18, 20, 20))
        self.voice = Toggle("Voice commands")
        self.voice.setChecked(s.voice_commands)
        self.voice.toggled.connect(self._apply)
        switch, _, _ = setting_row("Voice commands", "Hold the dictation key and say one. Say only the command; anything "
                                                     "longer is typed as dictated.", self.voice)
        switch.layout().setContentsMargins(0, 0, 0, 4)
        layout.addWidget(switch)
        layout.addWidget(caption("Your own phrases: separate them with commas.", "3"))
        names = {key: transform.name for key, transform in TRANSFORMS.items()} | {UNDO: "Undo"}
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        self.phrases: dict[str, QLineEdit] = {}
        for i, key in enumerate(DEFAULT_PHRASES):
            edit = QLineEdit()
            edit.setPlaceholderText("no phrase: this command is off")
            edit.setToolTip(f"What you say for {names[key]}. The defaults: {', '.join(DEFAULT_PHRASES[key])}.")
            edit.editingFinished.connect(self._save_phrases)
            field(edit)
            self.phrases[key] = edit
            grid.addWidget(label(names[key], "rowtitle", wrap=False), i, 0)
            grid.addWidget(edit, i, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.phrase_note = text("", "warning")
        layout.addWidget(self.phrase_note)
        self.reset = button("Use the default phrases", self._reset_phrases, link=True, size="sm")
        layout.addLayout(row(self.reset, stretch_at=1))
        self.add(voice)

        frame, layout = card(4, (20, 18, 20, 16))
        layout.addWidget(label("The menu", "heading"))
        self.hotkey = Choice(small=True)
        for label_text, value in TRANSFORM_HOTKEYS:
            self.hotkey.addItem(label_text, value)
        if self.hotkey.findData(s.transform_shortcut) < 0:
            self.hotkey.insertItem(self.hotkey.count() - 1, s.transform_shortcut, s.transform_shortcut)  # set by hand
        self.hotkey.setCurrentIndex(self.hotkey.findData(s.transform_shortcut))
        self.hotkey.currentIndexChanged.connect(self._apply)
        self.hotkey.setMinimumWidth(200)
        shortcut, _, self.how = setting_row("Shortcut", "", self.hotkey)
        layout.addWidget(shortcut)
        layout.addWidget(divider())
        layout.addWidget(caption("In the menu", "2"))
        self.choices: dict[str, Toggle] = {}
        for key, transform in TRANSFORMS.items():
            box = Toggle(transform.name)
            box.setChecked(key in app.settings.transforms)
            box.toggled.connect(self._apply)
            self.choices[key] = box
            choice, _, _ = setting_row(transform.name, transform.description, box)
            choice.layout().setContentsMargins(0, 8, 0, 8)
            layout.addWidget(choice)
        self.add(frame)

        model, layout = card(8, (20, 14, 20, 14), horizontal=True)
        self.model = label("", tone="2")
        self.setup = button("Connect an AI", lambda: go_to("cleanup"), link=True, size="sm")
        layout.addWidget(self.model, 1)  # the line takes the width: a model's name never breaks in the middle
        layout.addWidget(self.setup)
        self.add(model)

        trial, layout = card(10, (20, 18, 20, 20))
        layout.addWidget(label("Try it", "heading"))
        self.sample = QPlainTextEdit()
        self.sample.setPlainText("I checked the deployment and everything looks good, but we still have one issue with the "
                                 "database migration, and I think we should fix that before production.")
        field(self.sample, "well")
        fixed_height(self.sample, 88)
        layout.addWidget(self.sample)
        self.try_buttons: dict[str, QPushButton] = {}
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        for key, transform in TRANSFORMS.items():
            b = button(transform.name, lambda _=False, k=key: self._try(k), size="sm")
            self.try_buttons[key] = b
            buttons.addWidget(b)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.result = QTextBrowser()
        field(self.result, "well")
        self.result.setFixedHeight(120)
        self.result.hide()
        layout.addWidget(self.result)
        self.note = caption("", "3")
        self.note.hide()
        layout.addWidget(self.note)
        self.add(trial)
        self.body.addStretch()

    def refresh(self) -> None:
        s, model = self.app.settings, self.app.transform_model()
        shortcut = s.transform_shortcut
        press = "double-tap Ctrl" if shortcut == "double ctrl" else f"press {self.hotkey.currentText()}"
        self.how.setText("The menu is off: voice commands still work." if not shortcut else
                         f"Select text, or don't (then your last dictation is used), {press}, then 1-"
                         f"{max(1, len(s.transforms))} or a click. U undoes the last transform; Esc closes the menu.")
        self.how.show()
        phrases = phrases_for(s.command_phrases)
        for key, edit in self.phrases.items():
            if not edit.hasFocus():  # never rewrite what the user is typing
                edit.setText(", ".join(phrases[key]))
                edit.setCursorPosition(0)  # a long list shows its first phrases, not its last
            edit.setEnabled(s.voice_commands)
        self.voice.blockSignals(True)
        self.voice.setChecked(s.voice_commands)
        self.voice.blockSignals(False)
        self.hotkey.blockSignals(True)
        self.hotkey.setCurrentIndex(max(0, self.hotkey.findData(shortcut)))
        self.hotkey.blockSignals(False)
        self.reset.setVisible(bool(s.command_phrases))
        self._check_phrases(phrases)
        self.model.setText(f"Uses your AI connection: {model}" if model else
                           "Text Transform needs an AI model: connect one on AI & models.")
        self.setup.setVisible(not model)
        for b in self.try_buttons.values():
            b.setEnabled(bool(model))

    def _apply(self, *_) -> None:
        chosen = [key for key, box in self.choices.items() if box.isChecked()]
        self.app.apply_settings(dataclasses.replace(self.app.settings, transform_shortcut=self.hotkey.currentData(),
                                                    transforms=chosen, voice_commands=self.voice.isChecked()))
        self.refresh()

    def _save_phrases(self) -> None:
        """A phrase box was left: keep the user's phrases where they differ from the defaults (the defaults can then
        improve in an update)."""
        custom = {}
        for key, edit in self.phrases.items():
            said = parse_phrases(edit.text())
            if said != list(DEFAULT_PHRASES[key]):
                custom[key] = ", ".join(said)
        if custom != self.app.settings.command_phrases:
            self.app.apply_settings(dataclasses.replace(self.app.settings, command_phrases=custom))
        self.refresh()

    def _reset_phrases(self) -> None:
        self.app.apply_settings(dataclasses.replace(self.app.settings, command_phrases={}))
        for edit in self.phrases.values():
            edit.clearFocus()
        self.refresh()

    def _check_phrases(self, phrases: dict[str, list[str]]) -> None:
        """A phrase given to two commands: the first one gets it; say so, rather than surprise the user."""
        seen: dict[str, str] = {}
        twice = []
        names = {key: transform.name for key, transform in TRANSFORMS.items()} | {UNDO: "Undo"}
        for key, options in phrases.items():
            for phrase in options:
                said = normalize(phrase)
                if said in seen and seen[said] != key:
                    twice.append(f"\"{phrase}\" is in {names[seen[said]]} and {names[key]}: {names[seen[said]]} is used.")
                seen.setdefault(said, key)
        self.phrase_note.setText(" ".join(twice))
        self.phrase_note.setVisible(bool(twice))

    def _say(self, message: str) -> None:
        self.note.setText(message)
        self.note.setVisible(bool(message))

    def _try(self, key: str) -> None:
        sample = self.sample.toPlainText().strip()
        if not sample:
            self._say("Type or paste some text first.")
            return
        for b in self.try_buttons.values():
            b.setEnabled(False)
        self._say(f"{TRANSFORMS[key].name}...")

        def done(result, error) -> None:
            self.refresh()
            if error:
                self._say(f"Didn't work: {error}")
            elif not result.accepted:
                self.result.hide()
                self._say("Kept the text: " + "; ".join(result.reasons))
            else:
                self.result.setHtml(result.html or html.escape(result.plain))
                self.result.show()
                self._say(f"{TRANSFORMS[key].name} in {result.seconds:.1f} s" +
                          (f" (checked and fixed once: {result.attempts} answers)" if result.attempts > 1 else ""))
        run_in_background(self, lambda: self.app.run_transform(sample, key), done)


class TranslatePage(Page):
    """Translate (sst.translateui): the shortcut, the languages, the model, and a box to try it in."""

    def __init__(self, app, go_to):
        super().__init__("Translate", "Select text in any app and press Ctrl+C twice: a small window at the pointer shows "
                                      "it translated. Copy the translation, or Replace the text with it. Names, "
                                      "numbers, dates and links stay as written.",
                         back=back_button("Tools", go_to, "tools"))
        self.app = app
        s = app.settings
        frame, layout = card(4, (20, 10, 20, 14))
        self.shortcut = Choice(small=True)
        for label_text, value in TRANSLATE_SHORTCUTS:
            self.shortcut.addItem(label_text, value)
        if self.shortcut.findData(s.translate_shortcut) < 0:
            self.shortcut.insertItem(self.shortcut.count() - 1, s.translate_shortcut, s.translate_shortcut)  # by hand
        self.shortcut.setCurrentIndex(self.shortcut.findData(s.translate_shortcut))
        self.shortcut.currentIndexChanged.connect(self._apply)
        self.shortcut.setMinimumWidth(240)
        layout.addWidget(setting_row("Shortcut", "", self.shortcut)[0])
        layout.addWidget(divider())
        self.target = Choice(search=True, small=True)
        self.target.addItems(list(TRANSLATE_LANGUAGES))
        self.target.setCurrentText(s.translate_to)
        self.target.currentIndexChanged.connect(self._apply)
        self.target.setMinimumWidth(240)
        layout.addWidget(setting_row("Translate into", "", self.target)[0])
        layout.addWidget(divider())
        self.second = Choice(search=True, small=True)
        self.second.addItem("Automatic: Windows' language, or English", "")
        for name in TRANSLATE_LANGUAGES:
            self.second.addItem(name, name)
        self.second.setCurrentIndex(max(0, self.second.findData(s.translate_second)))
        self.second.currentIndexChanged.connect(self._apply)
        self.second.setMinimumWidth(240)
        layout.addWidget(setting_row("Text already in that language goes into", "", self.second)[0])
        self.how = caption("", "2")
        layout.addWidget(self.how)
        self.add(frame)

        model, layout = card(8, (20, 14, 20, 14), horizontal=True)
        self.model = label("", tone="2")
        self.setup = button("Connect an AI", lambda: go_to("cleanup"), link=True, size="sm")
        layout.addWidget(self.model, 1)
        layout.addWidget(self.setup)
        self.add(model)

        trial, layout = card(10, (20, 18, 20, 20))
        layout.addWidget(label("Try it", "heading"))
        self.sample = QPlainTextEdit()
        self.sample.setPlainText("Could you send me the updated report by Friday? The budget is $25,000.")
        field(self.sample, "well")
        fixed_height(self.sample, 72)
        layout.addWidget(self.sample)
        self.try_button = button("Translate", self._try, primary=True, size="sm")
        layout.addLayout(row(self.try_button, stretch_at=1))
        self.result = QTextBrowser()
        field(self.result, "well")
        self.result.setFixedHeight(90)
        self.result.hide()
        layout.addWidget(self.result)
        self.note = caption("", "3")
        self.note.hide()
        layout.addWidget(self.note)
        self.add(trial)
        self.body.addStretch()

    def _second(self) -> str:
        s = self.app.settings
        return s.translate_second or fallback_second(s.translate_to, system_language())

    def refresh(self) -> None:
        s, model, second = self.app.settings, self.app.transform_model(), self._second()
        for box, value in ((self.shortcut, s.translate_shortcut), (self.second, s.translate_second)):
            box.blockSignals(True)
            box.setCurrentIndex(max(0, box.findData(value)))
            box.blockSignals(False)
        self.target.blockSignals(True)
        self.target.setCurrentText(s.translate_to)
        self.target.blockSignals(False)
        already = f" (text already in {s.translate_to}: in {second})" if second else ""
        self.how.setText("Translate is off." if not s.translate_shortcut else
                         f"Select text, press {self.shortcut.currentText().split(' (')[0]}: the window shows it in "
                         f"{s.translate_to}{already}. A language at its top translates again; Esc or a click outside "
                         "closes it.")
        self.model.setText(f"Uses your AI connection: {model}" if model else
                           "Translate needs an AI model: connect one on AI & models.")
        self.setup.setVisible(not model)
        self.try_button.setEnabled(bool(model))

    def _apply(self, *_) -> None:
        self.app.apply_settings(dataclasses.replace(self.app.settings, translate_shortcut=self.shortcut.currentData(),
                                                    translate_to=self.target.currentText(),
                                                    translate_second=self.second.currentData()))
        self.refresh()

    def _say(self, message: str) -> None:
        self.note.setText(message)
        self.note.setVisible(bool(message))

    def _try(self) -> None:
        sample = self.sample.toPlainText().strip()
        if not sample:
            self._say("Type or paste some text first.")
            return
        s = self.app.settings
        self.try_button.setEnabled(False)
        self._say(f"Translating into {s.translate_to}…")

        def done(result, error) -> None:
            self.refresh()
            if error:
                self.result.hide()
                self._say(f"Didn't work: {error}")
                return
            self.result.setPlainText(result.text)
            self.result.show()
            self._say(f"{result.target} in {result.seconds:.1f} s" + (": " + "; ".join(result.warnings)
                                                                        if result.warnings else ""))
        second = self._second()
        run_in_background(self, lambda: self.app.run_translation(sample, s.translate_to, second), done)


# ---------------------------------------------------------------- Settings

MIC_READY = [("While Rflow runs", "always"), ("5 minutes after dictating", "warm"), ("Only while dictating", "off")]


class SettingsPage(Page):
    def __init__(self, app, go_to=None):
        go_to = go_to or (lambda page: None)
        super().__init__("Settings", "Changes save as you make them.")
        self.app = app
        s: Settings = app.settings

        everyday, layout = card(0, (20, 4, 20, 4))
        self.hotkey = Choice(small=True)
        for label_text, value in HOTKEY_CHOICES:
            self.hotkey.addItem(label_text, value)
        if self.hotkey.findData(s.hotkey) < 0:
            self.hotkey.addItem(s.hotkey, s.hotkey)  # a custom one set with --hotkey or by hand
        self.hotkey.setCurrentIndex(self.hotkey.findData(s.hotkey))
        self.hotkey.setMinimumWidth(220)
        self.hotkey_caps = QWidget()
        self.hotkey_caps_layout = QHBoxLayout(self.hotkey_caps)
        self.hotkey_caps_layout.setContentsMargins(0, 0, 0, 0)
        key_row, _, _ = setting_row("Dictation key", "Hold to talk, tap for hands-free. With Ctrl+Win, Ctrl+Win+Space is "
                                                     "hands-free as well.", self.hotkey_caps, self.hotkey)
        layout.addWidget(key_row)
        layout.addWidget(divider())
        self.sounds = Toggle("Sounds")
        self.sounds.setChecked(s.sounds)
        layout.addWidget(setting_row("Sounds", "A soft click when recording starts and stops", self.sounds)[0])
        layout.addWidget(divider())
        self.start_with_windows = Toggle("Start when I sign in")
        self.start_with_windows.setChecked(starts_with_windows())
        self.start_with_windows.setEnabled(can_start_with_windows())
        layout.addWidget(setting_row("Start when I sign in", "Rflow waits quietly in the tray" if can_start_with_windows()
                                     else "Available in the installed app", self.start_with_windows)[0])
        layout.addWidget(divider())
        self.format_text = Toggle("Write numbers as numbers")
        self.format_text.setChecked(s.format_text)
        self.format_text.setToolTip("Spoken forms are written the usual way. Ordinary words stay as said: \"two "
                                    "options\" isn't changed, \"twenty five percent\" becomes 25%.")
        layout.addWidget(setting_row("Write numbers as numbers", "25%, October 1, 3:30 PM, $5", self.format_text)[0])
        self.add(everyday)

        privacy, layout = card(0, (20, 4, 20, 4))
        self.save_recordings = Toggle("Keep recordings on this PC")
        self.save_recordings.setChecked(s.save_recordings)
        layout.addWidget(setting_row("Keep recordings on this PC", "Audio and text stay in your folder, never uploaded",
                                     button("Open folder", lambda: open_folder(RECORDINGS_DIR), link=True, size="sm"),
                                     self.save_recordings)[0])
        layout.addWidget(divider())
        self.mic_ready = Choice(small=True)
        for label_text, value in MIC_READY:
            self.mic_ready.addItem(label_text, value)
        self.mic_ready.setCurrentIndex(self.mic_ready.findData("always" if s.always_on_mic else "warm" if s.warm_mic
                                                               else "off"))
        self.mic_ready.setMinimumWidth(220)
        self.mic_ready.setToolTip("Dictation starts at once and keeps the moment before you pressed the key, so a word "
                                  "you began early isn't cut off. Those seconds stay in memory and are replaced all the "
                                  "time: nothing is kept or sent until you press the key. Never done for Bluetooth "
                                  "headsets.")
        layout.addWidget(setting_row("Microphone stays ready", "Your first word is never lost. Windows shows its "
                                     "microphone icon meanwhile.", self.mic_ready)[0])
        layout.addWidget(divider())
        self.update_lamp = Lamp("ok")
        self.update_status = caption("Rflow checks for updates by itself and tells you when one is ready.", "2")
        version = QVBoxLayout()
        version.setSpacing(2)
        version.addWidget(label(f"{APP_NAME} {__version__}", "rowtitle"))
        version.addLayout(row(self.update_lamp, self.update_status, spacing=8))
        about = QWidget()
        about_layout = QHBoxLayout(about)
        about_layout.setContentsMargins(0, 14, 0, 14)
        about_layout.setSpacing(16)
        about_layout.addLayout(version, 1)
        about_layout.addWidget(button("Check for updates", lambda: app.check_for_updates(manual=True), size="sm"))
        layout.addWidget(about)
        self.add(privacy)

        self.advanced = advanced_row("Advanced", "Voice pipeline, troubleshooting, Reading test, profiles, logs",
                                     self._toggle_advanced)
        self.add(self.advanced)
        self.advanced_card, layout = card(0, (20, 4, 20, 4))
        self.voice_pipeline = Toggle("Voice pipeline")
        self.voice_pipeline.setChecked(s.voice_pipeline)
        self.voice_pipeline.setToolTip("Long dictations are cut at your pauses and transcribed while you are still "
                                       "speaking, so the text is ready sooner. Then your dictionary, the formatting "
                                       "and the AI cleanup run, and a check keeps the AI from changing numbers, names "
                                       "or meaning. Off: the whole recording is transcribed at once, as before.")
        layout.addWidget(setting_row("Voice pipeline", "Transcribe in parts while you speak, then fix, format and check "
                                     "the text", self.voice_pipeline)[0])
        layout.addWidget(divider())
        self.debug_pipeline = Toggle("Keep each dictation's steps")
        self.debug_pipeline.setChecked(s.debug_pipeline)
        self.debug_pipeline.setToolTip("Saves the text after each step, and the audio parts, in a folder on this "
                                       "laptop. Turn it off when done: it keeps your voice.")
        layout.addWidget(setting_row("Keep each dictation's steps", "For troubleshooting. It keeps your voice: turn it "
                                     "off when done.", button("Open folder", lambda: open_folder(LOG_DIR.parent / "debug"),
                                                              link=True, size="sm"), self.debug_pipeline)[0])
        layout.addWidget(divider())
        self.raw_audio = Toggle("Turn off Windows' voice effects")
        self.raw_audio.setChecked(s.raw_audio)
        self.raw_audio.setToolTip("Records the microphone as it is, without Windows' or the driver's noise suppression "
                                  "and gain. Try it with the Reading test: it may help or hurt, depending on the "
                                  "microphone and the room.")
        layout.addWidget(setting_row("Turn off Windows' voice effects", "The microphone as it is: no noise suppression "
                                     "or gain. Check it with the Reading test.", self.raw_audio)[0])
        for title, words, on_click in [
                ("Reading test", "Read 30 sentences: how many words does Rflow get wrong?", lambda: go_to("reading")),
                ("Profiles", "One setup per person sharing this PC", lambda: go_to("profiles")),
                ("Logs", "Open the folder with Rflow's logs", lambda: open_folder(LOG_DIR)),
                ("Website", WEBSITE.removeprefix("https://"), lambda: QDesktopServices.openUrl(QUrl(WEBSITE))),
                ("Report a problem", "On GitHub", lambda: QDesktopServices.openUrl(QUrl(REPO + "/issues")))]:
            layout.addWidget(divider())
            layout.addWidget(link_row(title, words, lambda _=False, f=on_click: f()))
        self.advanced_card.hide()
        self.add(self.advanced_card)
        self.body.addStretch()

        self.hotkey.currentIndexChanged.connect(self._apply)
        self.mic_ready.currentIndexChanged.connect(self._apply)
        for box in (self.format_text, self.voice_pipeline, self.debug_pipeline, self.raw_audio, self.sounds,
                    self.save_recordings):
            box.toggled.connect(self._apply)
        self.start_with_windows.toggled.connect(lambda on: set_start_with_windows(on) if can_start_with_windows() else None)
        self._show_keys()

    def _toggle_advanced(self) -> None:
        open_ = self.advanced_card.isHidden()
        self.advanced_card.setVisible(open_)
        self.advanced.button.icon_name = "chevron-down" if open_ else "chevron-right"
        self.advanced.button.update()

    def _show_keys(self) -> None:
        clear(self.hotkey_caps_layout)
        try:
            label_text = parse_hotkey(self.hotkey.currentData()).label
        except ValueError:
            label_text = self.hotkey.currentText()
        self.hotkey_caps_layout.addWidget(keys(key_names(label_text)))

    def result(self, current: Settings) -> Settings:
        """The current settings with this page's choices (the other pages own the rest)."""
        ready = self.mic_ready.currentData()
        warm = ready == "warm" or (ready == "always" and current.warm_mic)  # always on keeps the warm choice
        return dataclasses.replace(current, hotkey=self.hotkey.currentData(), sounds=self.sounds.isChecked(),
                                   save_recordings=self.save_recordings.isChecked(),
                                   always_on_mic=ready == "always", warm_mic=warm,
                                   raw_audio=self.raw_audio.isChecked(), format_text=self.format_text.isChecked(),
                                   voice_pipeline=self.voice_pipeline.isChecked(),
                                   debug_pipeline=self.debug_pipeline.isChecked())

    def refresh(self) -> None:
        s = self.app.settings
        for box, on in ((self.sounds, s.sounds), (self.format_text, s.format_text), (self.voice_pipeline, s.voice_pipeline),
                        (self.debug_pipeline, s.debug_pipeline), (self.raw_audio, s.raw_audio),
                        (self.save_recordings, s.save_recordings)):
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)

    def _apply(self, *_) -> None:
        self._show_keys()
        self.app.apply_settings(self.result(self.app.settings))  # changes apply at once, like a phone's settings


# ---------------------------------------------------------------- the first-run welcome

PROVIDER_TILES = [("gemini", "Gemini", "Free key"), ("openai", "OpenAI", "Pay as you go"),
                  ("anthropic", "Anthropic", "Pay as you go"), ("groq", "Groq", "Fastest answers"),
                  ("ollama", "Ollama", "Runs on this PC")]
# The fast model the welcome picks for each provider, from the provider's own list (newest first).
FAST_MODELS = {"gemini": [r"flash-lite", r"flash"], "openai": [r"gpt-4\.1-mini", r"gpt-4o-mini", r"mini"],
               "anthropic": [r"haiku"], "groq": [r"llama-3\.1-8b-instant", r"instant", r"llama"], "ollama": [r"."]}
_NOT_CHAT = re.compile(r"embed|tts|audio|image|vision|live|transcribe|whisper|guard|moderation|search|realtime|preview|exp",
                       re.IGNORECASE)


def fast_model(provider: str, models: list[str]) -> str:
    """The provider's fast chat model for cleanup (a Flash-Lite, a mini, a Haiku...), the newest version first."""
    chat = [m for m in models if not _NOT_CHAT.search(m)] or list(models)

    def version(name: str) -> tuple:
        return tuple(int(n) for n in re.findall(r"\d+", name)[:3])
    for pattern in FAST_MODELS.get(provider, [r"."]):
        found = sorted((m for m in chat if re.search(pattern, m, re.IGNORECASE)), key=version, reverse=True)
        if found:
            return found[0]
    return chat[0] if chat else ""


class OptionCard(Card):
    """A choice as a card: raised, or pressed in with an Iris edge when chosen. clicked()."""

    clicked = Signal()

    def __init__(self, kind: str = "card"):
        super().__init__(kind)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chosen = False

    def set_chosen(self, chosen: bool) -> None:
        self.chosen = chosen
        self.set_kind("chosen" if chosen else "card")

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


class Stepper(QWidget):
    """The three steps of the welcome: done ones with a check, the current one lit."""

    def __init__(self, steps: list[str]):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.dots: list[QLabel] = []
        self.names: list[QLabel] = []
        for i, name in enumerate(steps):
            if i:
                line = divider()
                line.setFixedWidth(32)
                layout.addWidget(line, 0, Qt.AlignmentFlag.AlignVCenter)
            dot = QLabel(str(i + 1))
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setFixedSize(24, 24)
            dot.setFont(font(12, 600, mono=True))
            words = caption(name, "3", wrap=False)
            layout.addWidget(dot)
            layout.addWidget(words)
            self.dots.append(dot)
            self.names.append(words)

    def set_step(self, step: int) -> None:
        for i, (dot, words) in enumerate(zip(self.dots, self.names, strict=True)):
            if i < step:
                dot.setText("✓")
                dot.setStyleSheet(f"background: {theme.css(tok('well'))}; color: {theme.css(tok('ok'))}; "
                                  "border-radius: 12px;")
                set_tone(words, "2")
            elif i == step:
                dot.setText(str(i + 1))
                dot.setStyleSheet(f"background: {theme.css(tok('primary'))}; color: {theme.css(tok('on_primary'))}; "
                                  "border-radius: 12px;")
                set_tone(words, None)
                words.setFont(font(13, 600))
            else:
                dot.setText(str(i + 1))
                dot.setStyleSheet(f"background: {theme.css(tok('well'))}; color: {theme.css(tok('text3'))}; "
                                  "border-radius: 12px;")
                set_tone(words, "3")


class WelcomePage(QWidget):
    """The first run, in three steps: how Rflow hears you (Parakeet on this PC, or a cloud model), a first dictation, and
    an optional AI connection (the model is chosen for the user)."""

    def __init__(self, app, go_to):
        super().__init__()
        self.app, self.go_to = app, go_to
        self.step, self.choice, self.provider = 0, "local", "gemini"
        root = Host("base")
        root.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        head = QGridLayout()
        head.setContentsMargins(40, 24, 40, 16)
        brand = QHBoxLayout()
        brand.setSpacing(12)
        self.mark = Mark()
        brand.addWidget(self.mark)
        brand.addWidget(label(APP_NAME, "wordmark", wrap=False))
        brand.addStretch()
        head.addLayout(brand, 0, 0)
        self.stepper = Stepper(["Hear you", "Try it", "AI"])
        head.addWidget(self.stepper, 0, 1, Qt.AlignmentFlag.AlignCenter)
        self.skip = button("Skip setup", self.finish, kind="quiet", size="sm")
        head.addWidget(self.skip, 0, 2, Qt.AlignmentFlag.AlignRight)
        for column in (0, 2):
            head.setColumnStretch(column, 1)
        layout.addLayout(head)

        self.steps = QStackedWidget()
        self.steps.addWidget(self._hear())
        self.steps.addWidget(self._try())
        self.steps.addWidget(self._connect())
        layout.addWidget(self.steps, 1)

        layout.addWidget(divider())
        foot = QHBoxLayout()
        foot.setContentsMargins(40, 14, 40, 14)
        foot.setSpacing(12)
        self.back = button("Back", lambda: self.show_step(self.step - 1), kind="quiet")
        self.foot_note = caption("", "3", wrap=False)
        self.secondary = button("", self._secondary)
        self.primary = button("", self._primary, primary=True, size="lg")
        for widget in (self.back, self.foot_note):
            foot.addWidget(widget)
        foot.addStretch()
        foot.addWidget(self.secondary)
        foot.addWidget(self.primary)
        footer = QWidget()
        footer.setLayout(foot)
        footer.setFixedHeight(72)
        layout.addWidget(footer)
        self.title = label("Welcome to Rflow")  # for the window's "unsaved changes" question (never asked here)
        self.ready = False
        self.show_step(0)

    # -- step 1: how Rflow hears you

    def _hear(self) -> QWidget:
        page = Host()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 0, 40, 0)
        layout.addStretch()
        title = label("How should Rflow hear you?", "display")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        sub = label("Pick one to start. You can switch any time on AI & models.", tone="2")
        sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(sub)
        layout.addSpacing(32)
        options = QHBoxLayout()
        options.setSpacing(24)
        options.addStretch()
        parakeet = SPEECH_MODELS[DEFAULT_MODEL]
        self.local_option = self._option("laptop", "On this PC", "NVIDIA Parakeet · English", "Recommended", [
            ("check", "ok", "Private: your voice stays on this PC"),
            ("check", "ok", "Works offline, free, no account"),
            ("check", "ok", "Fast here: about 0.4 s a sentence")],
            ("download", f"One download of {_size(parakeet.download.size)}"))
        self.cloud_option = self._option("cloud", "In the cloud", "Gemini, OpenAI or Groq", "", [
            ("check", "ok", "99 languages, Tamil and Japanese too"),
            ("check", "ok", "Nothing to download"),
            ("info", "warn", "Your voice goes to the provider")],
            ("key", "Needs an API key from the provider"))
        for option, key in ((self.local_option, "local"), (self.cloud_option, "cloud")):
            option.clicked.connect(lambda k=key: self._choose(k))
            option.setFixedWidth(368)
            options.addWidget(option)
        options.addStretch()
        layout.addLayout(options)
        self.speech_status = caption("", "2")
        self.speech_status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(16)
        layout.addWidget(self.speech_status)
        layout.addStretch()
        return page

    def _option(self, icon: str, title: str, subtitle: str, badge: str, ticks: list, foot: tuple) -> OptionCard:
        option = OptionCard()
        layout = QVBoxLayout(option)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        top = QHBoxLayout()
        picture = QLabel()
        picture.setPixmap(theme.icon_pixmap(icon, tok("iris" if icon == "laptop" else "text2").name(), 24, 1.0))
        top.addWidget(picture)
        top.addStretch()
        if badge:
            top.addWidget(caption(badge, "2", wrap=False))
        layout.addLayout(top)
        words = QVBoxLayout()
        words.setSpacing(2)
        words.addWidget(label(title, "hero"))
        words.addWidget(caption(subtitle, "3", wrap=False))
        layout.addLayout(words)
        for tick, tone, line in ticks:
            mark = QLabel()
            mark.setPixmap(theme.icon_pixmap(tick, tok(tone).name(), 16, 1.0))
            ticked = row(mark, label(line, tone="2", wrap=False), spacing=10)
            ticked.setStretch(1, 1)
            layout.addLayout(ticked)
        layout.addWidget(divider())
        mark = QLabel()
        mark.setPixmap(theme.icon_pixmap(foot[0], tok("text3").name(), 16, 1.0))
        layout.addLayout(row(mark, caption(foot[1], "3", wrap=False), stretch_at=2, spacing=8))
        return option

    def _choose(self, key: str) -> None:
        self.choice = key
        self.local_option.set_chosen(key == "local")
        self.cloud_option.set_chosen(key == "cloud")
        self._show_footer()

    # -- step 2: try it

    def _try(self) -> QWidget:
        page = Host()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(64, 0, 64, 0)
        layout.setSpacing(56)
        left = QVBoxLayout()
        left.addStretch()
        self.orb = Orb(200)
        left.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignHCenter)
        left.addSpacing(20)
        self.orb_row, self.orb_lamp, self.orb_text = lamp_row("ok", "", stretch=False)
        left.addWidget(self.orb_row, 0, Qt.AlignmentFlag.AlignHCenter)
        left.addStretch()
        holder = QWidget()
        holder.setLayout(left)
        holder.setFixedWidth(260)
        layout.addWidget(holder)
        right = QVBoxLayout()
        right.setSpacing(20)
        right.addStretch()
        right.addWidget(label("Say something", "display"))
        self.try_line = QHBoxLayout()
        self.try_line.setSpacing(6)
        right.addLayout(self.try_line)
        self.try_text = label("", tone="2")
        right.addWidget(self.try_text)
        right.addWidget(caption("Microphone", "2", wrap=False))
        self.microphone = MicrophoneBox(self.app.settings.microphone, self.app.microphones(), self.app.default_microphone(),
                                        source=lambda: (self.app.microphones(), self.app.default_microphone()))
        self.microphone.changed.connect(self._microphone_chosen)
        right.addWidget(self.microphone)
        self.typed_well, typed = card(6, (20, 16, 20, 16), kind="well")
        typed.addWidget(caption("Typed here", "3", wrap=False))
        self.try_box = QPlainTextEdit()
        self.try_box.setPlaceholderText("Click here first. Your words will appear here.")
        self.try_box.setFont(font(16, 400))
        self.try_box.setStyleSheet("QPlainTextEdit { padding: 0; }")
        fixed_height(self.try_box, 64)
        self.try_box.textChanged.connect(self._typed)
        typed.addWidget(self.try_box)
        right.addWidget(self.typed_well)
        self.status = label("", tone="2")
        right.addWidget(self.status)
        right.addStretch()
        layout.addLayout(right, 1)
        return page

    def _microphone_chosen(self, device: str) -> None:
        self.app.apply_settings(dataclasses.replace(self.app.settings, microphone=device))

    def _typed(self) -> None:
        if self.step == 1:
            self.refresh(self.ready)

    # -- step 3: connect an AI

    def _connect(self) -> QWidget:
        page = Host()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 0, 40, 0)
        layout.setSpacing(8)
        layout.addStretch()
        for widget in (caption("Optional", "2", wrap=False), label("Make it write better", "display"),
                       label("Connect an AI to add punctuation, drop filler words and unlock Text Transform and "
                             "Translate. Only text is sent, never your voice.", tone="2")):
            widget.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(widget)
        layout.addSpacing(24)
        tiles = QHBoxLayout()
        tiles.setSpacing(16)
        tiles.addStretch()
        self.tiles: dict[str, OptionCard] = {}
        for key, name, note in PROVIDER_TILES:
            tile = OptionCard("tile")
            tile.setFixedSize(152, 64)
            box = QVBoxLayout(tile)
            box.setContentsMargins(16, 10, 16, 10)
            box.setSpacing(2)
            box.addWidget(label(name, "heading", wrap=False))
            box.addWidget(caption(note, "3", wrap=False))
            tile.clicked.connect(lambda k=key: self._pick_provider(k))
            self.tiles[key] = tile
            tiles.addWidget(tile)
        tiles.addStretch()
        layout.addLayout(tiles)
        layout.addSpacing(16)
        self.key_card, column = card(14, (24, 20, 24, 20))
        self.key_card.setFixedWidth(620)
        self.key_title = caption("", "2", wrap=False)
        self.key_link = button("Get a free key", self._open_key_page, link=True, size="sm", icon="external",
                               icon_after=True)
        column.addLayout(row(self.key_title, self.key_link, stretch_at=1))
        self.ai_key = QLineEdit()
        self.ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_key.setPlaceholderText("Paste your key")
        field(self.ai_key)
        self.ai_key.textChanged.connect(lambda _="": self._show_footer())
        self.paste_key = button("Paste", lambda: self.ai_key.setText(_clipboard_text().strip()))
        column.addLayout(row(self.ai_key, self.paste_key, spacing=12))
        self.model_line = caption("", "3")
        column.addWidget(self.model_line)
        layout.addWidget(self.key_card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return page

    def _pick_provider(self, key: str) -> None:
        self.provider = key
        for name, tile in self.tiles.items():
            tile.set_chosen(name == key)
        p = PROVIDERS[key]
        self.key_title.setText(f"{provider_name(key)} API key" if p.needs_key else "Ollama runs on this PC: no key "
                                                                                   "needed")
        self.key_link.setText("Get a free key" if key == "gemini" else "Get a key")
        self.key_link.setVisible(bool(p.key_page))
        self.ai_key.setVisible(p.needs_key)
        self.paste_key.setVisible(p.needs_key)
        self.ai_key.setText(self.app.gateway.key_for(key))
        self.model_line.setText("Rflow picks the provider's fast model for you (you can change it later).")
        self._show_footer()

    def _open_key_page(self) -> None:
        QDesktopServices.openUrl(QUrl(PROVIDERS[self.provider].key_page))

    # -- the steps and the footer

    def show_step(self, step: int) -> None:
        self.step = max(0, min(2, step))
        self.steps.setCurrentIndex(self.step)
        self.stepper.set_step(self.step)
        if self.step == 0:
            self._choose(self.choice)
        if self.step == 2 and not any(tile.chosen for tile in self.tiles.values()):
            self._pick_provider(self.provider)
        self.refresh(self.ready)
        if self.step == 1:
            self.try_box.setFocus()

    def _show_footer(self) -> None:
        step, app = self.step, self.app
        installed = SPEECH_MODELS[DEFAULT_MODEL].installed()
        fetching = bool(app.downloading) and app.downloading[0] == DEFAULT_MODEL
        self.back.setVisible(step > 0)
        self.skip.setVisible(step < 2)
        self.foot_note.setVisible(step == 0)
        self.secondary.setVisible(step > 0)
        if step == 0:
            chosen = app.speech_in_use() or app.loading_speech
            if self.choice == "cloud":
                self.primary.setText("Set up a cloud model")
                self.primary.icon_name = "arrow-right"
                self.foot_note.setText("Choose a provider and paste its key on the next page.")
            elif installed or chosen or fetching:
                self.primary.setText("Continue")
                self.primary.icon_name = None
                self.foot_note.setText("Parakeet is here." if installed else "The download keeps going while you try the "
                                                                             "next step.")
            else:
                self.primary.setText("Download Parakeet and continue")
                self.primary.icon_name = "download"
                self.foot_note.setText("The download keeps going while you try the next step.")
        elif step == 1:
            self.secondary.setText("Try again")
            self.primary.setText("Continue")
            self.primary.icon_name = "arrow-right"
            self.primary.icon_after = True
        else:
            self.secondary.setText("Skip for now")
            self.primary.setText("Connect and finish")
            self.primary.icon_name = None
            p = PROVIDERS[self.provider]
            self.primary.setEnabled(bool(self.ai_key.text().strip()) or not p.needs_key)
        if step != 2:
            self.primary.setEnabled(True)
        if step != 1:
            self.primary.icon_after = False
        self.primary.updateGeometry()
        self.primary.update()

    @property
    def get_parakeet(self) -> QPushButton:
        """The footer's main button on the first step (it downloads Parakeet when Parakeet isn't here)."""
        return self.primary

    def _primary(self) -> None:
        if self.step == 0:
            if self.choice == "cloud":
                self._to_speech()
                return
            app = self.app
            fetching = bool(app.downloading) and app.downloading[0] == DEFAULT_MODEL
            if not SPEECH_MODELS[DEFAULT_MODEL].installed() and not fetching and not app.speech_in_use():
                app.download_speech_model(DEFAULT_MODEL)
            self.show_step(1)
        elif self.step == 1:
            self.show_step(2)
        else:
            self._connect_ai()

    def _secondary(self) -> None:
        if self.step == 1:
            self.try_box.clear()
            self.try_box.setFocus()
        elif self.step == 2:
            self.finish()

    def _connect_ai(self) -> None:
        """Ask the provider for its models, pick its fast one, check it answers, then save and finish."""
        p = PROVIDERS[self.provider]
        api_key = self.ai_key.text().strip()
        gateway = GatewayConfig(p.url if p.own_server else "", api_key, p.key,
                                {k: v for k, v in self.app.gateway.entries().items() if k != p.key})
        self.primary.setEnabled(False)
        self.model_line.setText(f"Connecting to {provider_name(p.key)}…")
        set_tone(self.model_line, "2")

        def work():
            models = self.app.ai_models(gateway)
            model = fast_model(p.key, models)
            if not model:
                raise RuntimeError("the provider lists no models")
            self.app.check_ai(gateway, model)
            return model

        def done(model, error) -> None:
            self.primary.setEnabled(True)
            if error:
                set_tone(self.model_line, "err")
                self.model_line.setText(f"Couldn't connect: {error}")
                return
            set_tone(self.model_line, "ok")
            self.model_line.setText(f"Connected. Model chosen for you: {model}")
            self.app.save_cleanup(True, model, "", gateway)
            self.finish()
        run_in_background(self, work, done)

    def refresh(self, ready: bool) -> None:
        self.ready = ready
        app = self.app
        label_text = app.hotkey_label()
        in_use, loading, downloading = app.speech_in_use(), app.loading_speech, app.downloading
        fetching = bool(downloading) and downloading[0] == DEFAULT_MODEL
        chosen = in_use or loading
        self.mark.set_state("ok" if ready else "warn")
        if fetching:
            done, total = downloading[1], downloading[2] or 1
            self.speech_status.setText(f"Downloading Parakeet: {done * 100 // total}%  ({_size(done)} of {_size(total)}).")
        else:
            self.speech_status.setText(f"In use: {SPEECH_MODELS[chosen].name}" if chosen else "")
        self.speech_status.setVisible(bool(self.speech_status.text()))
        clear(self.try_line)
        self.try_line.addWidget(label("Hold", tone="2", wrap=False))
        self.try_line.addWidget(keys(key_names(label_text), "sm"))
        self.try_line.addWidget(label("and say “Hello Rflow, this is my first dictation”, then let go.",
                                      tone="2"), 1)
        self.try_text.setText(f"Click in the box below first. {how_to_dictate(label_text)}")
        typed = bool(self.try_box.toPlainText().strip())
        if typed:
            self.orb.set_state("done")
            self.orb_lamp.set_state("ok")
            self.orb_text.setText("Typed, on this PC" if (in_use and SPEECH_MODELS[in_use].where == "local") else "Typed")
            self.status.setText("It works. Now try it in any app.")
        elif ready:
            self.orb.set_state("ready")
            self.orb_lamp.set_state("ok")
            self.orb_text.setText("Ready: go ahead")
            self.status.setText("Ready: go ahead.")
        elif fetching:
            done, total = downloading[1], downloading[2] or 1
            self.orb.set_state("loading", done / total)
            self.orb_lamp.set_state("warn")
            self.orb_text.setText(f"Downloading Parakeet, {done * 100 // total}%")
            self.status.setText("Waiting for Parakeet's download... Meanwhile, choose your microphone.")
        elif chosen:
            self.orb.set_state("off")
            self.orb_lamp.set_state("warn")
            self.orb_text.setText("Loading the speech model")
            self.status.setText("Loading the speech model (a few seconds)...")
        else:
            self.orb.set_state("off")
            self.orb_lamp.set_state("warn")
            self.orb_text.setText("No speech model yet")
            self.status.setText("Choose how Rflow recognises your speech first (step 1).")
        self._show_footer()

    def _to_speech(self) -> None:
        self.finish()
        self.go_to("speech")
        page = getattr(self.window(), "pages", {}).get("speech")
        if page is not None:
            page.show_where("cloud")

    def finish(self) -> None:
        self.app.finish_welcome("")
        self.go_to("home")


# ---------------------------------------------------------------- profiles

class ProfilesPage(Page):
    """People sharing this computer: each profile has its own setup."""

    def __init__(self, app, go_to=None):
        go_to = go_to or (lambda page: None)
        super().__init__("Profiles", "Each profile has its own dictation key and microphone, words, AI provider and keys, "
                                     "dictations, stats and reading tests. Useful when several people share this PC, "
                                     "or to keep a work and a private setup apart. Click a name to change it.",
                         back=back_button("Settings", go_to, "settings"))
        self.app = app
        self.list = QVBoxLayout()
        self.list.setSpacing(16)
        self.add(self.list)
        new, layout = card(12, (20, 18, 20, 20))
        layout.addWidget(label("New profile", "heading"))
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("Name, e.g. Rahul")
        field(self.new_name)
        self.new_name.returnPressed.connect(self._create)
        layout.addLayout(row(self.new_name, button("Create and switch to it", self._create, primary=True), spacing=12))
        layout.addWidget(caption("A new profile starts with the welcome: how Rflow hears you, a first dictation, an AI "
                                 "connection.", "3"))
        self.add(new)
        self.body.addStretch()

    def refresh(self) -> None:
        clear(self.list)
        current = self.app.profiles.current
        for profile in self.app.profiles.items:
            frame, layout = card(12, (20, 14, 20, 14), horizontal=True)
            name = QLineEdit(profile.name)
            name.setPlaceholderText(profile.label)
            name.setToolTip("Type to rename, then press Enter")
            field(name)
            name.editingFinished.connect(lambda p=profile, box=name: self.app.rename_profile(p.id, box.text()))
            layout.addWidget(name, 1)
            if profile.id == current.id:
                in_use, _, _ = lamp_row("ok", "In use", stretch=False)
                layout.addWidget(in_use)
            else:
                layout.addWidget(button("Switch to this profile", lambda _=False, p=profile: self.app.switch_profile(p.id),
                                        size="sm"))
                if profile.id != "default":  # the first profile's files are the settings folder itself
                    layout.addWidget(button("Delete", lambda _=False, p=profile: self._delete(p), kind="danger",
                                            size="sm"))
            self.list.addWidget(frame)

    def _create(self) -> None:
        name = self.new_name.text().strip()
        if name:
            self.new_name.clear()
            self.app.create_profile(name)

    def _delete(self, profile) -> None:
        if self.confirm(f"Delete the profile {profile.label}, with its words, keys, dictations, stats and reading tests?"):
            self.app.delete_profile(profile.id)
            self.refresh()

    def confirm(self, question: str) -> bool:
        return QMessageBox.question(self, APP_NAME, question) == QMessageBox.StandardButton.Yes


# ---------------------------------------------------------------- the window

NAV = [("home", "Home"), ("words", "Words"), ("tools", "Tools"), ("models", "AI & models"), ("settings", "Settings")]
NAV_ICONS = {"home": "home", "words": "words", "tools": "tools", "models": "models", "settings": "settings"}
RAIL_NAMES = {"models": "AI"}  # shorter names under the icons of the narrow rail
# The section each page belongs to (its sidebar button), and the page a section opens on.
SECTION = {"home": "home", "dictionary": "words", "snippets": "words", "tools": "tools", "transform": "tools",
           "translate": "tools", "models": "models", "speech": "models", "cleanup": "models", "settings": "settings",
           "reading": "settings", "profiles": "settings"}
OPENS = {"words": "dictionary"}
REFRESHED = ("home", "dictionary", "snippets", "profiles", "speech", "cleanup", "transform", "translate", "tools",
             "models", "settings")


class MainWindow(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        theme.load_fonts()
        theme.set_theme("dark" if dark_mode() else "light")
        theme.make_host(self)
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(ICON_FILE)))
        self.setFont(font(14))
        self.resize(1000, 700)
        self.setMinimumSize(780, 540)
        self.ready = False
        self.status_message = ""
        self.compact = None

        self.sidebar = Host("base")
        self.sidebar.setObjectName("sidebar")
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(20, 24, 0, 20)
        side.setSpacing(4)
        brand = QHBoxLayout()
        brand.setContentsMargins(4, 0, 0, 0)
        brand.setSpacing(12)
        self.mark = Mark()
        self.wordmark = label(APP_NAME, "wordmark", wrap=False)
        brand.addWidget(self.mark)
        brand.addWidget(self.wordmark)
        brand.addStretch()
        side.addLayout(brand)
        side.addSpacing(24)
        self.nav: dict[str, Button] = {}
        for key, label_text in NAV:
            b = Button(label_text, "nav", icon=NAV_ICONS[key])
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.nav[key] = b
            side.addWidget(b)
        side.addStretch()
        self.update_link = button("", lambda: app.start_update(), link=True, size="sm", icon="update")
        self.update_link.hide()
        side.addWidget(self.update_link)
        self.status_card = Card("well", 14)
        status = QHBoxLayout(self.status_card)
        status.setContentsMargins(12, 12, 12, 12)
        status.setSpacing(10)
        self.status_lamp = Lamp("warn")
        words = QVBoxLayout()
        words.setSpacing(0)
        self.status_title = label("Getting ready", wrap=False)
        self.status_title.setFont(font(12, 600))
        self.status_label = caption("", "2", wrap=False)
        words.addWidget(self.status_title)
        words.addWidget(self.status_label)
        status.addWidget(self.status_lamp, 0, Qt.AlignmentFlag.AlignVCenter)
        status.addLayout(words, 1)
        side.addSpacing(8)
        side.addWidget(self.status_card)
        self.profile_button = button("", self._profile_menu, kind="quiet", size="sm", icon="profile")
        self.profile_button.setToolTip("Switch profile")
        side.addWidget(self.profile_button)

        self.banner = Card("tile", 14)
        banner = QHBoxLayout(self.banner)
        banner.setContentsMargins(16, 10, 10, 10)
        banner.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(theme.icon_pixmap("update", tok("iris").name(), 18, 1.0))
        self.banner_text = label("")
        self.notes_button = button("What's new", lambda: app.open_release_notes(), link=True, size="sm")
        self.update_button = button("Update now", lambda: app.start_update(), primary=True, size="sm")
        banner.addWidget(mark)
        banner.addWidget(self.banner_text, 1)
        banner.addWidget(self.notes_button)
        banner.addWidget(self.update_button)
        self.banner.hide()

        self.pages = {"home": HomePage(app, self.show_page), "dictionary": DictionaryPage(app, self.show_page),
                      "snippets": SnippetsPage(app, self.show_page), "tools": ToolsPage(app, self.show_page),
                      "transform": TransformPage(app, self.show_page), "translate": TranslatePage(app, self.show_page),
                      "models": ModelsPage(app, self.show_page), "speech": SpeechPage(app, self.show_page),
                      "cleanup": CleanupPage(app, self.show_page), "settings": SettingsPage(app, self.show_page),
                      "reading": ReadingTestPage(app, self.show_page), "profiles": ProfilesPage(app, self.show_page),
                      "welcome": WelcomePage(app, self.show_page)}
        self.stack = QStackedWidget()
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.content = Host("base")
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.banner_holder = QWidget()
        banner_holder = QVBoxLayout(self.banner_holder)
        banner_holder.setContentsMargins(24, 20, 24, 0)
        banner_holder.addWidget(self.banner)
        self.banner_holder.hide()
        content.addWidget(self.banner_holder)
        content.addWidget(self.stack, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.content, 1)
        self.toast = Toast(self)

        self.apply_theme()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.apply_theme)
        self._set_compact(False)
        self.show_page("home" if app.settings.welcomed else "welcome")

    @property
    def toast_offset(self) -> int:
        return 0 if self.sidebar.isHidden() else self.sidebar.width() // 2  # the toast centres on the page, not the window

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), tok("base"))
        p.end()

    def apply_theme(self, name: str | None = None) -> None:
        """Obsidian or Porcelain, as Windows is set (or `name`, e.g. "light" for the website's screenshots)."""
        theme.set_theme(name if isinstance(name, str) else "dark" if dark_mode() else "light")
        self.setStyleSheet(theme.stylesheet())
        for widget in self.findChildren(QWidget):
            widget.update()
        self.update()
        if hasattr(self, "pages"):
            self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._set_compact(self.width() < COMPACT_WIDTH)
        if self.toast.isVisible():
            self.toast.move((self.width() - self.toast.width()) // 2 + self.toast_offset, self.height() - self.toast.height())

    def _set_compact(self, compact: bool) -> None:
        if compact == self.compact:
            return
        self.compact = compact
        self.sidebar.setFixedWidth(84 if compact else 216)
        self.sidebar.layout().setContentsMargins(*((10, 20, 10, 16) if compact else (20, 24, 0, 20)))
        self.wordmark.setVisible(not compact)
        for key, b in self.nav.items():
            b.compact = compact
            b.setText(RAIL_NAMES.get(key, dict(NAV)[key]) if compact else dict(NAV)[key])
            b.setSizePolicy(QSizePolicy.Policy.Fixed if compact else QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setFixedWidth(64) if compact else b.setMinimumWidth(0)
            if not compact:
                b.setMaximumWidth(16777215)
            b.updateGeometry()
        self.status_label.setVisible(not compact)
        status = self.status_card.layout()  # the rail: the lamp over its word
        status.setDirection(QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight)
        status.setContentsMargins(*((6, 10, 6, 10) if compact else (12, 12, 12, 12)))
        status.setAlignment(self.status_lamp, Qt.AlignmentFlag.AlignHCenter if compact else Qt.AlignmentFlag.AlignVCenter)
        self.status_title.setFont(font(11 if compact else 12, 600))
        self.status_title.setAlignment(Qt.AlignmentFlag.AlignHCenter if compact else Qt.AlignmentFlag.AlignLeft)
        self.profile_button.setVisible(not compact and len(self.app.profiles.items) > 1)
        for page in self.pages.values():
            if isinstance(page, Page):
                page.set_compact(compact)
        self.layout().invalidate()

    def show_page(self, key: str) -> None:
        key = OPENS.get(key, key)
        page, current = self.pages[key], self.stack.currentWidget()
        if current is not page and getattr(current, "unsaved", lambda: False)():
            if not self.leave_unsaved(current.title.text()):
                section = SECTION.get(self.current_page())
                if section in self.nav:
                    self.nav[section].setChecked(True)  # the sidebar button just clicked lets go again
                return
            current.discard_changes()
        self._show_profile()
        if key in REFRESHED:
            page.refresh()
        elif key == "welcome":
            page.refresh(self.ready)
        self.stack.setCurrentWidget(page)
        self.sidebar.setVisible(key != "welcome")  # the welcome has the whole window
        section = SECTION.get(key)
        if section in self.nav:
            self.nav[section].setChecked(True)
        else:
            for b in self.nav.values():
                b.setAutoExclusive(False)
                b.setChecked(False)
                b.setAutoExclusive(True)

    def current_section(self) -> str:
        return SECTION.get(self.current_page(), "")

    def leave_unsaved(self, page: str) -> bool:
        """Leaving a page whose changes weren't saved: True to discard them, False to stay on it."""
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"{page} has changes that aren't saved.")
        box.setInformativeText("Stay to save them with Save, or discard them.")
        stay = box.addButton("Stay", QMessageBox.ButtonRole.RejectRole)
        discard = box.addButton("Discard changes", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(stay)
        box.exec()
        return box.clickedButton() is discard

    def _show_profile(self) -> None:
        self.profile_button.setText(self.app.profiles.current.label)
        self.profile_button.setVisible(not self.compact and len(self.app.profiles.items) > 1)
        counts = len(self.app.correction_suggestions()) if hasattr(self.app, "correction_suggestions") else 0
        self.nav["words"].badge = str(counts) if counts and not self.compact else ""
        self.nav["words"].update()

    def _profile_menu(self) -> None:
        menu = QMenu(self)
        current = self.app.profiles.current
        for profile in self.app.profiles.items:
            action = menu.addAction(profile.label, lambda p=profile: self.app.switch_profile(p.id))
            action.setCheckable(True)
            action.setChecked(profile.id == current.id)
        menu.addSeparator()
        menu.addAction("New profile...", self._new_profile)
        menu.addAction("Manage profiles", lambda: self.show_page("profiles"))
        menu.exec(self.profile_button.mapToGlobal(self.profile_button.rect().bottomLeft()))

    def _new_profile(self) -> None:
        self.show_page("profiles")
        self.pages["profiles"].new_name.setFocus()

    def current_page(self) -> str:
        return next(key for key, page in self.pages.items() if page is self.stack.currentWidget())

    def open(self, page: str | None = None) -> None:
        if not self.app.settings.welcomed:
            page = "welcome"
        self.show_page(page or self.current_page())
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def refresh(self) -> None:
        """New dictation, words, settings or profile name: update what is on screen."""
        self._show_profile()
        self._show_status()
        current = self.current_page()
        if current in REFRESHED:
            self.pages[current].refresh()
        elif current == "welcome":
            self.pages[current].refresh(self.ready)

    def set_status(self, message: str, ready: bool) -> None:
        self.ready, self.status_message = ready, message
        self.pages["home"].set_ready(ready)
        self._show_status()
        current = self.current_page()
        if current == "welcome":
            self.pages["welcome"].refresh(ready)
        elif current == "home":
            self.pages["home"].refresh()

    def _show_status(self) -> None:
        """The sidebar's lamp: Ready and the speech model, or what Rflow is waiting for."""
        app = self.app
        in_use, loading, downloading = app.speech_in_use(), app.loading_speech, app.downloading
        fetching = bool(downloading) and downloading[0] == DEFAULT_MODEL and not self.ready
        model = SPEECH_MODELS.get(in_use)
        if self.ready and model is not None:
            where = {"local": "on this PC", "cloud": "in the cloud", "server": "your server"}.get(model.where, "")
            title, detail, state = "Ready", f"{model.name.removeprefix('NVIDIA ').split(' large')[0]}, {where}", "ok"
        elif fetching:
            done, total = downloading[1], downloading[2] or 1
            title, detail, state = "Not ready yet", f"Downloading, {done * 100 // total}%", "warn"
        elif loading or in_use:
            title, detail, state = "Getting ready", "Loading the speech model", "warn"
        elif self.status_message:
            title, detail, state = "Not ready yet", "Choose a speech model", "warn"
        else:
            title, detail, state = "Getting ready", "", "warn"
        self.status_title.setText(title)
        self.status_label.setText(detail)
        self.status_card.setToolTip(self.status_message)
        self.status_lamp.set_state(state)
        self.mark.set_state(state)

    def show_update(self, message: str, busy: bool = False, version: str = "") -> None:
        self.banner_text.setText(message)
        self.update_button.setEnabled(not busy)
        self.banner.show()
        self.banner_holder.show()
        if version:
            self.update_link.setText(f"Update to {version}")
            self.update_link.show()

    def set_update_status(self, message: str) -> None:
        self.pages["settings"].update_status.setText(message)

    def closeEvent(self, event):
        # Closing the window doesn't quit: dictation keeps working from the tray, like Wispr Flow.
        event.ignore()
        self.hide()  # hiding also stops a reading-test recording and the level meters (their hideEvent)
        self.app.window_closed()


# ---------------------------------------------------------------- a stand-in for the TrayApp

class PreviewApp:
    """Stands in for the TrayApp: the self-test, the tests and the website's screenshots use it (no model, no hook)."""

    def __init__(self, settings: Settings | None = None, history: list[dict] | None = None, stats: Stats | None = None,
                 microphones: list[str] | None = None, gateway: GatewayConfig | None = None,
                 profiles: Profiles | None = None, bench: Path | None = None):
        self.settings = settings or Settings(welcomed=True)
        self.profiles = profiles or Profiles()
        self._bench = bench or Path(os.environ.get("TEMP", ".")) / "rflow-preview-bench"
        self.gateway = gateway or GatewayConfig()
        self.history = history or []
        self.stats = stats or Stats()
        self._microphones = microphones if microphones is not None else ["Microphone (Realtek(R) Audio)"]
        self.loading_speech = ""
        self.downloading: tuple[str, int, int] | None = None
        self.scanning = ""
        self.last_scan: dict | None = None
        self.calls: list[tuple] = []  # what the window asked for
        self._dictionary = None

    def hotkey_label(self) -> str:
        return parse_hotkey(self.settings.hotkey).label

    def history_entries(self) -> list[dict]:
        return self.history

    def microphones(self) -> list[str]:
        return self._microphones

    def default_microphone(self) -> str:
        return self._microphones[0] if self._microphones else ""

    def new_recorder(self):
        from sst.audio import Recorder
        return Recorder(self.settings.microphone or None, raw=self.settings.raw_audio)

    def bench_dir(self) -> Path:
        return self.profiles.current.folder(self._bench)

    def speech_in_use(self) -> str:
        return usable(self.settings.speech_model, self.gateway)

    def choose_speech_model(self, key: str) -> None:
        self.settings.speech_model = key
        self.calls.append(("choose_speech_model", key))

    def set_speech_language(self, code: str) -> None:
        self.settings.speech_language = code
        self.calls.append(("set_speech_language", code))

    def download_speech_model(self, key: str) -> None:
        self.calls.append(("download_speech_model", key))

    def use_cloud_speech(self, provider: str, api_key: str, model: str) -> None:
        self.gateway = self.gateway.with_key(provider, api_key)
        self.settings.speech_cloud_models = {**self.settings.speech_cloud_models, provider: model}
        self.settings.speech_model = provider
        self.calls.append(("use_cloud_speech", provider, model))

    def test_cloud_speech(self, provider: str, api_key: str, model: str) -> str:
        self.calls.append(("test_cloud_speech", provider, model))
        return f"{model} answered in 0.6 s: After early nightfall the yellow lamps would light up."

    def use_server_speech(self, address: str, api_key: str, model: str) -> None:
        from sst.gateway import SPEECH_SERVER
        self.gateway = self.gateway.with_entry(SPEECH_SERVER, address, api_key)
        self.settings.speech_server_model, self.settings.speech_model = model, "server"
        self.calls.append(("use_server_speech", address, model))

    def test_server_speech(self, address: str, api_key: str, model: str) -> str:
        self.calls.append(("test_server_speech", address, model))
        return f"{model} answered in 0.3 s: After early nightfall the yellow lamps would light up."

    def server_models(self, address: str, api_key: str) -> list[str]:
        self.calls.append(("server_models", address))
        return ["whisper-1", "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8"]

    def ai_models(self, gateway: GatewayConfig) -> list[str]:
        self.calls.append(("ai_models", gateway.service.key))
        return {"gemini": ["gemini-2.0-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-pro", "text-embedding-004"],
                "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini"]}.get(gateway.service.key, ["model-a"])

    def check_ai(self, gateway: GatewayConfig, model: str) -> str:
        self.calls.append(("check_ai", gateway.service.key, model))
        return f"{model} answered in 0.6 s"

    def cancel_download(self) -> None:
        self.calls.append(("cancel_download",))

    def scan_computer(self) -> None:
        self.calls.append(("scan_computer",))

    def remove_speech_model(self, key: str) -> None:
        self.calls.append(("remove_speech_model", key))

    def apply_settings(self, new: Settings) -> None:
        self.settings = new
        self.calls.append(("apply_settings", new))

    def save_cleanup(self, on: bool, model: str, fallback: str, gateway: GatewayConfig) -> None:
        self.settings.cleanup, self.settings.cleanup_model, self.settings.cleanup_fallback = on, model, fallback
        self.gateway = gateway
        self.calls.append(("save_cleanup", on, model, fallback, gateway))

    def add_words(self, words: list[str]) -> int:
        known = {w.lower() for w in self.settings.vocabulary}
        added = [w for w in dict.fromkeys(words) if w.lower() not in known]
        self.settings.vocabulary = self.settings.vocabulary + added
        return len(added)

    def remove_word(self, word: str) -> None:
        self.settings.vocabulary = [w for w in self.settings.vocabulary if w != word]
        term = self.dictionary.find(word)
        if term is not None and term.source != "vocabulary":
            self.dictionary.remove_term(term.id)

    @property
    def dictionary(self):
        if self._dictionary is None:
            from sst.pipeline.dictionary import DictionaryStore
            self._dictionary = DictionaryStore()  # in memory: the preview keeps nothing
            self._dictionary.sync_vocabulary(self.settings.vocabulary)
        return self._dictionary

    def dictionary_terms(self) -> list:
        self.dictionary.sync_vocabulary(self.settings.vocabulary)
        return self.dictionary.terms(enabled_only=False)

    def correct_dictation(self, typed: str, corrected: str) -> None:
        from sst.pipeline.learning import CorrectionEvent, Learner
        Learner(self.dictionary).observe(CorrectionEvent(typed, corrected, app="history"))
        self.calls.append(("correct_dictation", typed, corrected))

    def correction_suggestions(self) -> list:
        from sst.pipeline.learning import Learner
        return Learner(self.dictionary).suggestions()

    def accept_suggestion(self, suggestion) -> None:
        from sst.pipeline.learning import Learner
        Learner(self.dictionary).accept(suggestion)

    def reject_suggestion(self, suggestion) -> None:
        from sst.pipeline.learning import Learner
        Learner(self.dictionary).reject(suggestion)

    def transform_model(self) -> str:
        return self.settings.cleanup_model if self.gateway.address else ""

    def translate_ready(self) -> bool:
        return bool(self.transform_model())

    def run_translation(self, text: str, target: str, second: str = ""):
        from sst.translate import Translation
        self.calls.append(("run_translation", target))
        return Translation("¿Podrías enviarme el informe actualizado antes del viernes? El presupuesto es de $25,000.",
                           target, text, 0.9)

    def run_transform(self, text: str, key: str):
        from sst.transform import TransformResult
        self.calls.append(("run_transform", key))
        plain = "Deployment looks good, but the database migration issue needs to be fixed before production."
        return TransformResult(key, text, plain, plain, f"<p>{plain}</p>", True, [], 1, 0.8)

    def add_sound_alike(self, heard: str, meant: str) -> None:
        from sst.pipeline.dictionary import TermMode
        term = self.dictionary.find(meant)
        if term is None:
            self.dictionary.add_term(meant, aliases=[heard], mode=TermMode.AUTOMATIC, source="user")  # asked for: always
        else:
            self.dictionary.add_alias(term.id, heard)
        self.calls.append(("add_sound_alike", heard, meant))

    def score_reading(self, folders, progress):
        raise RuntimeError("No speech model in the preview.")

    def finish_welcome(self, name: str = "") -> None:
        if name.strip():
            self.profiles.current.name = name.strip()
        self.settings.welcomed = True
        self.calls.append(("finish_welcome", name))

    def switch_profile(self, profile_id: str) -> None:
        self.profiles.active = profile_id
        self.calls.append(("switch_profile", profile_id))

    def create_profile(self, name: str) -> None:
        self.switch_profile(self.profiles.add(name).id)

    def rename_profile(self, profile_id: str, name: str) -> None:
        if name.strip():
            self.profiles.get(profile_id).name = name.strip()

    def delete_profile(self, profile_id: str) -> None:
        self.profiles.remove(profile_id)
        self.calls.append(("delete_profile", profile_id))

    def window_closed(self) -> None:
        self.calls.append(("window_closed",))

    def check_for_updates(self, manual: bool = False) -> None:
        self.calls.append(("check_for_updates", manual))

    def start_update(self) -> None:
        self.calls.append(("start_update",))

    def open_release_notes(self) -> None:
        self.calls.append(("open_release_notes",))

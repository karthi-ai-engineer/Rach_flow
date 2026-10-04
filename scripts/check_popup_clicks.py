"""Can every button of the Translate popup and every row of the Text Transform menu really be clicked? Run it by hand
on Windows after changing a popup (the tests can't: they run Qt off the screen, where no click can pass through):

  uv run python scripts/check_popup_clicks.py

For each one: ask Windows which window is under its centre (WindowFromPoint: how Windows routes a real click), and if
it is the popup, deliver a click there (WM_LBUTTONDOWN/UP to Rflow's own window only: the real mouse never moves and no
other window gets anything) and check that the button reacted. The popups appear for a moment at the bottom right of
the screen; they never take the focus.
"""
import ctypes
import os
import sys
from ctypes import wintypes

os.environ["QT_QPA_PLATFORM"] = "windows"
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sst.transformui import TransformMenu  # noqa: E402
from sst.translate import Translation  # noqa: E402
from sst.translateui import TranslatePopup  # noqa: E402

user32 = ctypes.WinDLL("user32")
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON, GA_ROOT = 0x0201, 0x0202, 0x0001, 2
WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x20, 0x80000, 0x08000000

app = QApplication([])
rows: list[tuple[str, str, str]] = []


def cls(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def click(window, widget_or_point, name: str, reacted) -> None:
    """Hit-test the centre of `widget_or_point` (a widget of `window`, or a QPoint in it), then click it if it's ours."""
    app.processEvents()
    hwnd = int(window.winId())
    local = widget_or_point if isinstance(widget_or_point, QPoint) else \
        widget_or_point.mapTo(window, widget_or_point.rect().center())
    dpr = window.devicePixelRatioF()
    pt = wintypes.POINT(round(local.x() * dpr), round(local.y() * dpr))
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    under = user32.WindowFromPoint(pt)
    root = user32.GetAncestor(under, GA_ROOT) if under else None
    if not root or int(root) != hwnd:
        rows.append((name, "NO", f"a real click goes THROUGH to the window below ({cls(under) if under else 'nothing'})"))
        return
    client = wintypes.POINT(pt.x, pt.y)
    user32.ScreenToClient(under, ctypes.byref(client))
    lparam = (client.y << 16) | (client.x & 0xFFFF)
    user32.SendMessageW(under, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    user32.SendMessageW(under, WM_LBUTTONUP, 0, lparam)
    app.processEvents()
    ok = reacted()
    rows.append((name, "yes" if ok else "NO", "clicked, and it reacted" if ok else "hit, but nothing happened"))


area = QGuiApplication.primaryScreen().availableGeometry()
anchor = QPoint(area.right() - 520, area.bottom() - 380)
heard: list = []
popup = TranslatePopup()
for signal in ("language", "copy", "replace", "retry", "setup", "closed"):
    getattr(popup, signal).connect(lambda *a, s=signal: heard.append((s, *a)))


def fresh(state: str = "result") -> None:
    heard.clear()
    if not popup.isVisible():
        popup.open_at(anchor, "来週の定例会議は木曜日の午後3時からに変更になりました。", "English",
                      ["English", "Tamil", "Hindi"], "Japanese")
    if state == "result":
        popup.show_result(Translation("Next week's regular meeting has been moved to Thursday from 3 PM.",
                                      "English", "", 1.0))
    elif state == "error":
        popup.show_error("The provider refused the API key. Check it on AI & models.", "HTTP 401", setup=True)
    elif state == "setup":
        popup.show_setup()
    app.processEvents()


fresh()
style = user32.GetWindowLongPtrW(int(popup.winId()), -20)
print(f"Translate popup window style: TRANSPARENT={bool(style & WS_EX_TRANSPARENT)} LAYERED={bool(style & WS_EX_LAYERED)} "
      f"NOACTIVATE={bool(style & WS_EX_NOACTIVATE)}")
for chip in list(popup.chips):
    name = chip.text()
    fresh()
    click(popup, popup.chips[[c.text() for c in popup.chips].index(name)], f"Translate: language button '{name}'",
          lambda n=name: ("language", n) in heard)
fresh()
click(popup, popup.result, "Translate: the translation text (select, scroll)", lambda: True)
fresh()
click(popup, popup.copy_button, "Translate: Copy", lambda: ("copy",) in heard)
fresh()
click(popup, popup.replace_button, "Translate: Replace", lambda: ("replace",) in heard)
fresh()
click(popup, popup.more, "Translate: More", lambda: not popup.grid_box.isHidden())
click(popup, popup.all_languages["Korean"], "Translate: a language in the full list", lambda: ("language", "Korean") in heard)
fresh("error")
click(popup, popup.retry_button, "Translate: Try again (after an error)", lambda: ("retry",) in heard)
fresh("error")
click(popup, popup.setup_button, "Translate: AI settings (after an error)", lambda: ("setup",) in heard)
fresh("setup")
click(popup, popup.setup_button, "Translate: Connect an AI (no model)", lambda: ("setup",) in heard)
fresh()
click(popup, popup.close_button, "Translate: ✕ close", lambda: ("closed",) in heard)
popup.close_popup()

chosen: list[str] = []
menu = TransformMenu()
menu.chosen.connect(chosen.append)
items = [("concise", "1", "Concise"), ("professional", "2", "Professional"), ("bullets", "3", "Bullet points")]
for i, (key, _, label) in enumerate(items):
    chosen.clear()
    menu.open_at(anchor, items, "Your selected text")
    click(menu, menu._row_rect(i).center().toPoint(), f"Text Transform menu: row '{label}'",
          lambda k=key: chosen == [k])
menu.close_menu()

width = max(len(r[0]) for r in rows)
print()
for name, ok, note in rows:
    print(f"{name:<{width}}  {ok:<3}  {note}")
failed = sum(r[1] == "NO" for r in rows)
print(f"\n{len(rows) - failed} of {len(rows)} clickable")
sys.exit(1 if failed else 0)

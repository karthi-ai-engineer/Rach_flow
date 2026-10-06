"""Type text into whichever app has keyboard focus: put it on the clipboard, press Ctrl+V,
then put back whatever was on the clipboard before.

Pasting is used instead of simulating each key press because it is instant for long text and
code editors do not auto-close brackets or pop up suggestions halfway through.
"""
import ctypes
import time
from contextlib import contextmanager
from ctypes import wintypes

from sst.hotkey import send_keys

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_V = 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x56
CF_UNICODETEXT, GMEM_MOVEABLE, HWND_MESSAGE = 13, 0x2, wintypes.HWND(-3)
# Content carrying this format is left out of Win+V clipboard history and cloud clipboard sync.
CF_EXCLUDE_FROM_HISTORY = user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
# Formats whose data is not a plain memory block (bitmaps, metafiles, palettes, GDI and private
# handles) cannot be copied byte for byte. Windows recreates the common ones, e.g. bitmaps from CF_DIB.
_NOT_MEMORY = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}

RESTORE_DELAY = 0.5  # seconds the target app gets to read the clipboard before the old content returns
HELD_WAIT = 200.0  # seconds a dictation waits for held keys: they may be held for the next one already (up to 3 min)


def paste_text(text: str) -> None:
    """Paste `text` into the focused app, leaving the user's clipboard as it was. Raises OSError when it can't: the
    clipboard held by another app, or keys held down for more than HELD_WAIT."""
    _wait_until_keys_released()
    with _clipboard():
        saved = _snapshot()
        # Windows text has "\r\n" line breaks (a snippet's lines): a classic edit box shows a bare "\n" as nothing.
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        _put([(CF_UNICODETEXT, (text + "\0").encode("utf-16-le")), (CF_EXCLUDE_FROM_HISTORY, b"\0" * 4)])
    ours = user32.GetClipboardSequenceNumber()
    _press_ctrl_v()
    time.sleep(RESTORE_DELAY)
    if user32.GetClipboardSequenceNumber() == ours:  # skip if the user copied something new meanwhile
        with _clipboard():
            _put(saved + [(CF_EXCLUDE_FROM_HISTORY, b"\0" * 4)] if saved else [])


def _modifiers_held() -> bool:
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN))


def _wait_for_modifiers_released(timeout: float = 5.0) -> None:
    # Ctrl+V pressed while the user still holds Alt from the hotkey would arrive as Ctrl+Alt+V.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _modifiers_held():
        time.sleep(0.02)


def _wait_until_keys_released(timeout: float = HELD_WAIT) -> None:
    """A dictation's Ctrl+V pressed while Ctrl+Win is still held would arrive as Win+Ctrl+V, not a paste. It waits until
    the keys are let go, also when they're held for the next dictation already, and is never pressed with them held."""
    deadline = time.monotonic() + timeout
    while _modifiers_held():
        if time.monotonic() >= deadline:
            raise OSError("the keys stayed held down")
        time.sleep(0.02)


def _press_ctrl_v() -> None:
    send_keys([(VK_CONTROL, False), (VK_V, False), (VK_V, True), (VK_CONTROL, True)])


@contextmanager
def _clipboard():
    # Setting clipboard data needs an owner window; a hidden message-only window is enough.
    hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    try:
        for _ in range(50):  # another app may have the clipboard open for a moment
            if user32.OpenClipboard(hwnd):
                break
            time.sleep(0.02)
        else:
            raise OSError("the clipboard is busy (another app is holding it open)")
        try:
            yield
        finally:
            user32.CloseClipboard()
    finally:
        user32.DestroyWindow(hwnd)


def _snapshot() -> list[tuple[int, bytes]]:
    """Copy every clipboard format that is a plain memory block (text, HTML, RTF, images as DIB, files...)."""
    saved, fmt = [], 0
    while fmt := user32.EnumClipboardFormats(fmt):
        if fmt in _NOT_MEMORY or 0x200 <= fmt <= 0x3FF:
            continue
        handle = user32.GetClipboardData(fmt)
        size = kernel32.GlobalSize(handle) if handle else 0
        pointer = kernel32.GlobalLock(handle) if size else None
        if pointer:
            try:
                saved.append((fmt, ctypes.string_at(pointer, size)))
            finally:
                kernel32.GlobalUnlock(handle)
    return saved


def _put(items: list[tuple[int, bytes]]) -> None:
    """Replace the clipboard contents with the given (format, data) items."""
    user32.EmptyClipboard()
    for fmt, data in items:
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):  # on success Windows owns the memory
            kernel32.GlobalFree(handle)

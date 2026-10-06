"""Typing a dictation (sst.paste), with the clipboard and the keys faked: nothing is pasted for real."""
from contextlib import contextmanager

from sst import paste


def test_line_breaks_reach_the_app_as_windows_line_breaks(monkeypatch):
    # A snippet's lines: a classic edit box shows a bare "\n" as nothing, so the clipboard gets "\r\n".
    puts = []

    @contextmanager
    def session():
        yield

    class User32:
        @staticmethod
        def GetClipboardSequenceNumber():
            return 1

    for name, fake in {"_clipboard": session, "_snapshot": lambda: [], "_put": puts.append, "_press_ctrl_v": lambda: None,
                       "_wait_until_keys_released": lambda: None, "user32": User32}.items():
        monkeypatch.setattr(paste, name, fake)
    monkeypatch.setattr(paste.time, "sleep", lambda seconds: None)
    paste.paste_text("Best regards,\nKarthi\r\nKarthi Labs ")
    text = puts[0][0][1].decode("utf-16-le").rstrip("\0")
    assert text == "Best regards,\r\nKarthi\r\nKarthi Labs "


def test_a_paste_waits_while_the_keys_are_held_for_the_next_dictation(monkeypatch):
    held = iter([True] * 50 + [False])
    clock = iter(range(0, 1000))
    monkeypatch.setattr(paste, "_modifiers_held", lambda: next(held))
    monkeypatch.setattr(paste.time, "monotonic", lambda: next(clock))  # 50 s pass: longer than the old 5 s
    monkeypatch.setattr(paste.time, "sleep", lambda seconds: None)
    paste._wait_until_keys_released()  # returns once they're let go


def test_a_paste_is_never_pressed_with_the_keys_still_held(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(paste, "_modifiers_held", lambda: True)
    monkeypatch.setattr(paste.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(paste.time, "sleep", lambda seconds: None)
    pressed = []
    monkeypatch.setattr(paste, "_press_ctrl_v", lambda: pressed.append(True))
    try:
        paste.paste_text("hello ")
        raise AssertionError("it should have refused")
    except OSError as e:
        assert "held down" in str(e) and pressed == []

"""Cover the Win32 conditions that gate the native Notepad operation."""

import ctypes as ct
from ctypes import wintypes as wt
from pathlib import Path
from time import monotonic

import pytest
from connectors.desktop.companion import DesktopExecutionError
from connectors.desktop.windows import (
    WTS_SESSION_INFO_EX,
    WindowsNotepad,
    _SessionInfo,
    _Target,
)


@pytest.mark.parametrize(
    ("connection", "flags", "desktop", "expected"),
    [(0, 1, "Default", True), (0, 0, "Default", False), (4, 1, "Default", False),
     (0, 1, "Winlogon", False)],
)
def test_ready_requires_active_unlocked_session_and_normal_input_desktop(
    connection, flags, desktop, expected,
):
    native = WindowsNotepad.__new__(WindowsNotepad)
    info = _SessionInfo(level=1)
    info.data.header.session_id = 2
    info.data.header.state = connection
    info.data.header.flags = flags

    def session_state(_server, _session, kind, pointer, size):
        assert kind == WTS_SESSION_INFO_EX
        ct.cast(pointer, ct.POINTER(ct.c_void_p))[0] = ct.addressof(info)
        ct.cast(size, ct.POINTER(wt.DWORD))[0] = ct.sizeof(info)
        return True

    def desktop_info(_handle, _class, name, _size, _needed):
        name.value = desktop
        return True

    native._session_state = session_state
    native._free_session = lambda _ptr: None
    native._open_desktop = lambda *_args: 1
    native._desktop_info = desktop_info
    native._close_desktop = lambda _handle: True
    assert native.ready() is expected


def test_switching_away_from_a_pinned_window_stops_before_a_message():
    native = WindowsNotepad.__new__(WindowsNotepad)
    target = _Target(2, 3, 4, Path("a.txt"))
    native._target = target
    native._initial_foreground = 1
    native._target_was_foreground = False
    native._inspect = lambda *_args: (target, True)
    native.ready = lambda: True
    foreground = 1
    native._foreground = lambda: foreground
    messages = []

    def send(hwnd, message, *_args):
        messages.append((hwnd, message))
        return 1

    native._send = send
    deadline = monotonic() + 5
    assert native.read(deadline) == ""
    foreground = target.window
    assert native.read(deadline) == ""
    foreground = 1
    with pytest.raises(DesktopExecutionError, match="window_changed"):
        native.write("never sent", deadline)
    assert messages == [(target.editor, 0x000D), (target.editor, 0x000D)]

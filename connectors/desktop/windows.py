"""Native Windows adapter for a fresh Notepad document."""

from __future__ import annotations

import asyncio
import ctypes as ct
import subprocess
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from connectors.desktop.companion import DesktopExecutionError
from ling.contracts.desktop import MAX_NOTE_LENGTH

WM_GETTEXT = 0x000D
EM_REPLACESEL = 0x00C2
SMTO_FLAGS = 0x0001 | 0x0002 | 0x0020  # BLOCK | ABORTIFHUNG | ERRORONEXIT
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_SESSION_INFO_EX = 25
WTS_ACTIVE = 0
WTS_UNLOCKED = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_NOTEPAD_PACKAGE = "microsoft.windowsnotepad_8wekyb3d8bbwe"
_EDITORS = {"Edit", "RichEditD2DPT"}


class _SessionHeader(ct.Structure):
    _fields_ = [
        ("session_id", ct.c_uint32),
        ("state", ct.c_int32),
        ("flags", ct.c_int32),
    ]


class _SessionData(ct.Union):
    # WTSINFOEX has a union aligned to LARGE_INTEGER. Only its header is needed.
    _fields_ = [("header", _SessionHeader), ("alignment", ct.c_int64)]


class _SessionInfo(ct.Structure):
    _fields_ = [("level", ct.c_uint32), ("data", _SessionData)]


@dataclass(frozen=True)
class _Target:
    window: int
    process_id: int
    editor: int
    note: Path


def _bind(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = args
    return function


class WindowsNotepad:
    def __init__(self) -> None:
        win_dll = getattr(ct, "WinDLL", None)
        callback_factory = getattr(ct, "WINFUNCTYPE", None)
        if win_dll is None or callback_factory is None:
            raise OSError("Windows desktop required")
        user = win_dll("user32", use_last_error=True)
        kernel = win_dll("kernel32", use_last_error=True)
        wts = win_dll("wtsapi32", use_last_error=True)
        self._open_desktop = _bind(
            user, "OpenInputDesktop", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD,
        )
        self._desktop_info = _bind(
            user, "GetUserObjectInformationW", wt.BOOL,
            wt.HANDLE, ct.c_int, wt.LPVOID, wt.DWORD, wt.LPDWORD,
        )
        self._close_desktop = _bind(user, "CloseDesktop", wt.BOOL, wt.HANDLE)
        self._session_state = _bind(
            wts, "WTSQuerySessionInformationW", wt.BOOL,
            wt.HANDLE, wt.DWORD, ct.c_int, ct.POINTER(wt.LPVOID), wt.LPDWORD,
        )
        self._free_session = _bind(wts, "WTSFreeMemory", None, wt.LPVOID)
        self._foreground = _bind(user, "GetForegroundWindow", wt.HWND)
        self._title = _bind(user, "GetWindowTextW", ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
        self._class = _bind(user, "GetClassNameW", ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
        self._window_pid = _bind(
            user, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, wt.LPDWORD,
        )
        self._visible = _bind(user, "IsWindowVisible", wt.BOOL, wt.HWND)
        self._enabled = _bind(user, "IsWindowEnabled", wt.BOOL, wt.HWND)
        self._unicode = _bind(user, "IsWindowUnicode", wt.BOOL, wt.HWND)
        self._callback = callback_factory(wt.BOOL, wt.HWND, wt.LPARAM)
        self._windows = _bind(user, "EnumWindows", wt.BOOL, self._callback, wt.LPARAM)
        self._children = _bind(user, "EnumChildWindows", wt.BOOL, wt.HWND, self._callback, wt.LPARAM)
        self._send = _bind(
            user, "SendMessageTimeoutW", ct.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPVOID,
            wt.UINT, wt.UINT, ct.POINTER(ct.c_size_t),
        )
        self._open_process = _bind(kernel, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
        self._close_process = _bind(kernel, "CloseHandle", wt.BOOL, wt.HANDLE)
        self._image = _bind(
            kernel, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR, wt.LPDWORD,
        )
        self._package = _bind(
            kernel, "GetPackageFamilyName", wt.LONG, wt.HANDLE, ct.POINTER(wt.UINT), wt.LPWSTR,
        )
        system_directory = _bind(kernel, "GetSystemDirectoryW", wt.UINT, wt.LPWSTR, wt.UINT)
        directory = ct.create_unicode_buffer(32768)
        if not 0 < system_directory(directory, len(directory)) < len(directory):
            raise OSError("Windows system directory unavailable")
        self._notepad = Path(directory.value) / "notepad.exe"
        self._target: _Target | None = None
        self._initial_foreground: int | None = None
        self._target_was_foreground = False

    def ready(self) -> bool:
        state, size = wt.LPVOID(), wt.DWORD()
        if not self._session_state(
            None, WTS_CURRENT_SESSION, WTS_SESSION_INFO_EX, ct.byref(state), ct.byref(size),
        ):
            return False
        try:
            if not state or size.value < ct.sizeof(_SessionInfo):
                return False
            info = ct.cast(state, ct.POINTER(_SessionInfo)).contents
            session = info.data.header
            if (
                info.level != 1 or session.session_id == 0
                or session.state != WTS_ACTIVE or session.flags != WTS_UNLOCKED
            ):
                return False
        finally:
            self._free_session(state)
        desktop = self._open_desktop(0, False, DESKTOP_READOBJECTS)
        if not desktop:
            return False
        try:
            name, needed = ct.create_unicode_buffer(128), wt.DWORD()
            return bool(self._desktop_info(
                desktop, UOI_NAME, name, ct.sizeof(name), ct.byref(needed),
            ) and name.value.casefold() == "default")
        finally:
            self._close_desktop(desktop)

    def _is_notepad(self, pid: int) -> bool:
        process = self._open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return False
        try:
            path = ct.create_unicode_buffer(32768)
            size = wt.DWORD(len(path))
            if not self._image(process, 0, path, ct.byref(size)):
                return False
            executable = Path(path.value)
            if executable == self._notepad:
                return True
            if executable.name.casefold() != "notepad.exe":
                return False
            package = ct.create_unicode_buffer(128)
            length = wt.UINT(len(package))
            return (
                self._package(process, ct.byref(length), package) == 0
                and package.value.casefold() == _NOTEPAD_PACKAGE
            )
        finally:
            self._close_process(process)

    def _inspect(self, window: int, note: Path) -> tuple[_Target | None, bool]:
        title = ct.create_unicode_buffer(512)
        if not self._visible(window) or not self._title(window, title, len(title)):
            return None, False
        document = title.value.split(" - ", 1)[0].strip("* \u200e\u200f")
        if document not in {note.name, note.stem}:
            return None, False
        pid = wt.DWORD()
        if not self._window_pid(window, ct.byref(pid)) or not self._is_notepad(pid.value):
            return None, False

        candidates: list[int] = []

        @self._callback
        def visit(child, _param):
            name = ct.create_unicode_buffer(64)
            self._class(child, name, len(name))
            if (
                name.value in _EDITORS and self._visible(child)
                and self._enabled(child) and self._unicode(child)
            ):
                candidates.append(child)
            return True

        self._children(window, visit, 0)
        if len(candidates) != 1:
            return None, True
        return _Target(window, pid.value, candidates[0], note), True

    def _find(self, note: Path) -> tuple[_Target | None, bool]:
        targets: list[_Target] = []
        found_window = False

        @self._callback
        def visit(window, _param):
            nonlocal found_window
            target, found = self._inspect(window, note)
            found_window |= found
            if target is not None:
                targets.append(target)
            return True

        if not self._windows(visit, 0):
            raise DesktopExecutionError("editor_error")
        return (targets[0] if len(targets) == 1 else None), found_window

    def _check_focus(self, target: _Target) -> None:
        foreground = self._foreground()
        if foreground == target.window:
            self._target_was_foreground = True
        elif self._target_was_foreground or foreground != self._initial_foreground:
            raise DesktopExecutionError("window_changed")

    def _check_desktop(self, deadline: float) -> None:
        if monotonic() >= deadline:
            raise DesktopExecutionError("lease_expired")
        if not self.ready():
            raise DesktopExecutionError("desktop_unavailable")

    async def open(self, directory: Path, deadline: float) -> None:
        self._target = None
        note = directory / f"{directory.name}.txt"
        note.touch(exist_ok=False)
        self._check_desktop(deadline)
        self._initial_foreground = self._foreground()
        self._target_was_foreground = False
        try:
            subprocess.Popen([self._notepad, note])
        except OSError as exc:
            raise DesktopExecutionError("open_failed") from exc
        wait_until = min(deadline, monotonic() + 5)
        saw_notepad = False
        while monotonic() < wait_until:
            self._check_desktop(deadline)
            target, found = self._find(note)
            if target is not None:
                self._check_focus(target)
                self._target = target
                return
            saw_notepad |= found
            await asyncio.sleep(0.08)
        if saw_notepad:
            raise DesktopExecutionError("editor_unavailable")
        raise DesktopExecutionError("open_failed")

    def _message(self, message: int, parameter: int, buffer, deadline: float) -> int:
        self._check_desktop(deadline)
        target = self._target
        if target is None or self._inspect(target.window, target.note)[0] != target:
            raise DesktopExecutionError("window_changed")
        self._check_focus(target)
        milliseconds = min(500, int((deadline - monotonic()) * 1000))
        if milliseconds <= 0:
            raise DesktopExecutionError("lease_expired")
        result = ct.c_size_t()
        if not self._send(
            target.editor, message, parameter, buffer, SMTO_FLAGS, milliseconds, ct.byref(result),
        ):
            raise DesktopExecutionError("editor_error")
        return result.value

    def write(self, text: str, deadline: float) -> None:
        if self.read(deadline):
            raise DesktopExecutionError("window_changed")
        # Replace the empty selection in the pinned edit HWND, with undo enabled.
        # This is literal text and never uses OS-wide input or the clipboard.
        buffer = ct.create_unicode_buffer(text.replace("\n", "\r\n"))
        self._message(EM_REPLACESEL, 1, buffer, deadline)

    def read(self, deadline: float) -> str:
        buffer = ct.create_unicode_buffer(2 * MAX_NOTE_LENGTH + 2)
        copied = self._message(WM_GETTEXT, len(buffer), buffer, deadline)
        if copied >= len(buffer) - 1:
            raise DesktopExecutionError("editor_error")
        return buffer.value

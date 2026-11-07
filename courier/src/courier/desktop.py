"""The device's OS: clipboard and notifications on Windows, macOS and Linux.

Text from a message never becomes part of a command line or script: it goes to helper programs
on stdin or as a separate argument, and to the Windows clipboard through the Win32 API.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Unavailable(RuntimeError):
    """This machine has no way to do that (no clipboard tool, no display)."""


# --- Windows clipboard -------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterClipboardFormatW.restype = wintypes.UINT
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def _open() -> None:
        # Another program may hold the clipboard for a moment.
        for _ in range(20):
            if _user32.OpenClipboard(None):
                return
            time.sleep(0.05)
        raise OSError(f"could not open the clipboard (error {ctypes.get_last_error()})")

    def _put(fmt: int, data: bytes) -> None:
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise MemoryError("GlobalAlloc failed")
        pointer = _kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(fmt, handle):
            _kernel32.GlobalFree(handle)
            raise OSError(f"SetClipboardData failed (error {ctypes.get_last_error()})")

    def _windows_set(text: str) -> None:
        _open()
        try:
            _user32.EmptyClipboard()
            _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
            # Keep it out of clipboard history, cloud clipboard sync and clipboard monitors.
            _put(_user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing"), b"\0")
            for name in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
                _put(_user32.RegisterClipboardFormatW(name), (0).to_bytes(4, "little"))
        finally:
            _user32.CloseClipboard()

    def _windows_get() -> str | None:
        _open()
        try:
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            pointer = _kernel32.GlobalLock(handle)
            try:
                return ctypes.wstring_at(pointer)
            finally:
                _kernel32.GlobalUnlock(handle)
        finally:
            _user32.CloseClipboard()

    def _windows_clear() -> None:
        _open()
        try:
            _user32.EmptyClipboard()
        finally:
            _user32.CloseClipboard()


# --- Other platforms: helper programs, text on stdin ---------------------------------------------

def _run(command: list[str], text: str | None = None) -> str:
    result = subprocess.run(command, input=text, capture_output=True, text=True, encoding="utf-8",
                            timeout=10, creationflags=NO_WINDOW)
    if result.returncode:
        raise OSError(f"{command[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _unix_tools() -> tuple[list[str], list[str], list[str]]:
    """(copy, paste, clear) commands for this session."""
    if sys.platform == "darwin":
        return ["pbcopy"], ["pbpaste"], ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy", "--type", "text/plain;charset=utf-8"], ["wl-paste", "--no-newline"], ["wl-copy", "--clear"]
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return (["xclip", "-selection", "clipboard", "-in"], ["xclip", "-selection", "clipboard", "-out"],
                ["xclip", "-selection", "clipboard", "-in"])
    raise Unavailable("no clipboard: need wl-copy (Wayland) or xclip (X11) and a graphical session")


class Clipboard:
    """Text in, text out; callers pass text with the host's own line endings."""

    def set(self, text: str) -> None:
        if sys.platform == "win32":
            return _windows_set(text)
        _run(_unix_tools()[0], text)

    def get(self) -> str | None:
        if sys.platform == "win32":
            return _windows_get()
        try:
            return _run(_unix_tools()[1])
        except OSError:
            return None

    def clear(self) -> None:
        if sys.platform == "win32":
            return _windows_clear()
        _run(_unix_tools()[2], "")


# --- Notifications ------------------------------------------------------------------------------

# Windows PowerShell 5.1 can reach the WinRT toast API; the text arrives in environment variables,
# so nothing in a message is ever parsed as PowerShell.
TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Security.SecurityElement]::Escape($env:COURIER_TITLE)
$b = [Security.SecurityElement]::Escape($env:COURIER_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""

APPLESCRIPT = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
               "-e", "end run"]


class Notifier:
    def notify(self, title: str, body: str) -> None:
        if sys.platform == "win32":
            env = {**os.environ, "COURIER_TITLE": title, "COURIER_BODY": body}
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", TOAST],
                                    env=env, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            if result.returncode:
                raise OSError(f"toast failed: {result.stderr.strip()}")
        elif sys.platform == "darwin":
            _run(["osascript", *APPLESCRIPT, title, body])
        elif shutil.which("notify-send"):
            _run(["notify-send", "--app-name=Courier", "--", title, body])
        else:
            raise Unavailable("no notifications: install notify-send (libnotify)")

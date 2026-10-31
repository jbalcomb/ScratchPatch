"""Process control and "what is the OS saying about it" diagnostics.

On Windows every IDA process is put in a Job Object with KILL_ON_JOB_CLOSE, so a timeout, a cancel or the
service stopping kills IDA and anything it started, and no orphan keeps a database locked. Other platforms
(the tests on Linux) fall back to killing the process tree with psutil.
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil

IS_WINDOWS = sys.platform == "win32"

# Well-known Windows exit codes (NTSTATUS), to say why a process died.
NTSTATUS = {
    0xC0000005: "access violation (crash)",
    0xC00000FD: "stack overflow (crash)",
    0xC0000409: "stack buffer overrun / fail-fast (crash)",
    0xC0000135: "a DLL was not found",
    0xC0000142: "DLL initialisation failed (often: no desktop/console for the service account)",
    0xC000013A: "stopped with Ctrl+C / Ctrl+Break",
    0x40010004: "killed (debugger/terminate)",
    0xC0000017: "out of memory",
    1: "general failure",
}


def describe_exit_code(code: int | None) -> str | None:
    if code is None:
        return None
    unsigned = code & 0xFFFFFFFF
    if unsigned in NTSTATUS:
        return NTSTATUS[unsigned]
    if unsigned >= 0xC0000000:
        return f"Windows error status 0x{unsigned:08X}"
    return None


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.IsWindowVisible.argtypes = [wintypes.HWND]


class KillGroup:
    """The process and everything it starts, killed together."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.handle = None
        if IS_WINDOWS:
            handle = _k32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _k32.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation,
                                              ctypes.byref(info), ctypes.sizeof(info))
            if ok and _k32.AssignProcessToJobObject(handle, wintypes.HANDLE(int(proc._handle))):  # type: ignore[attr-defined]
                self.handle = handle
            else:
                _k32.CloseHandle(handle)

    @property
    def in_job_object(self) -> bool:
        return self.handle is not None

    def kill(self) -> None:
        if self.handle is not None:
            _k32.TerminateJobObject(self.handle, 1)
        else:
            try:
                parent = psutil.Process(self.proc.pid)
                for child in parent.children(recursive=True):
                    child.kill()
                parent.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if self.handle is not None:
            _k32.CloseHandle(self.handle)
            self.handle = None


def windows_of(pids: set[int]) -> list[dict]:
    """Top-level windows owned by these processes: a visible one usually means IDA is waiting on a dialog."""
    if not IS_WINDOWS or not pids:
        return []
    found: list[dict] = []

    def callback(hwnd, _):
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            title = ctypes.create_unicode_buffer(512)
            cls = ctypes.create_unicode_buffer(256)
            _u32.GetWindowTextW(hwnd, title, 512)
            _u32.GetClassNameW(hwnd, cls, 256)
            visible = bool(_u32.IsWindowVisible(hwnd))
            if title.value or visible:
                found.append({"pid": pid.value, "title": title.value, "class": cls.value, "visible": visible})
        return True

    _u32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found


def _proc_info(p: psutil.Process) -> dict:
    info: dict = {"pid": p.pid}
    try:
        with p.oneshot():
            info["name"] = p.name()
            info["status"] = p.status()
            cpu = p.cpu_times()
            info["cpu_seconds"] = round(cpu.user + cpu.system, 2)
            info["memory_mb"] = round(p.memory_info().rss / 1048576, 1)
            info["threads"] = p.num_threads()
            try:
                io = p.io_counters()
                info["io_read_mb"] = round(io.read_bytes / 1048576, 2)
                info["io_write_mb"] = round(io.write_bytes / 1048576, 2)
            except (psutil.AccessDenied, AttributeError):
                pass
    except psutil.NoSuchProcess:
        info["status"] = "gone"
    except psutil.AccessDenied:
        info["status"] = "access denied"
    return info


def snapshot(pid: int, sample_seconds: float = 2.0) -> dict:
    """What the OS says about a process right now, and whether it looks busy or stuck."""
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"alive": False, "pid": pid}
    procs = [p]
    try:
        procs += p.children(recursive=True)
    except psutil.NoSuchProcess:
        pass
    before = {q.pid: _proc_info(q) for q in procs}
    time.sleep(sample_seconds)
    after = [_proc_info(q) for q in procs]
    cpu_delta = sum(a.get("cpu_seconds", 0) - before.get(a["pid"], {}).get("cpu_seconds", 0) for a in after)
    io_delta = sum((a.get("io_read_mb", 0) + a.get("io_write_mb", 0))
                   - (before.get(a["pid"], {}).get("io_read_mb", 0) + before.get(a["pid"], {}).get("io_write_mb", 0))
                   for a in after)
    alive = p.is_running() and after[0].get("status") not in ("gone", psutil.STATUS_ZOMBIE)
    windows = windows_of({a["pid"] for a in after})
    busy = cpu_delta > 0.05 * sample_seconds or io_delta > 0.01
    if not alive:
        verdict = "the process has exited"
    elif busy:
        verdict = "running and busy (using CPU or disk): probably still analysing"
    elif any(w["visible"] for w in windows):
        verdict = "running but idle with a window open: probably waiting on a dialog nobody can see"
    else:
        verdict = "running but idle: possibly stuck or waiting for input"
    return {
        "alive": alive,
        "pid": pid,
        "busy": busy,
        "verdict": verdict,
        "cpu_seconds_in_sample": round(cpu_delta, 2),
        "sample_seconds": sample_seconds,
        "processes": after,
        "windows": windows,
    }

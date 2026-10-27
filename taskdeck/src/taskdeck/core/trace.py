"""Diagnostics: a detailed, timestamped record of each background task, for analysis afterwards.

Each task writes one JSON-lines file (one event per line) on the phone first, flushed after
every event and synced to storage every few seconds, so it survives the app being killed. The
app copies the files to Google Drive as they grow (taskdeck.ui.diagnostics); that copy is a
convenience, the local file is the record.

Events (``ev``) include: ``start`` (app version, device), ``phases``, ``phase`` (a stage starts),
``note`` (e.g. paused / waiting for Wi-Fi), ``http`` (every request: method, host, path, status,
bytes, seconds, error), ``file`` (one file uploaded: bytes, seconds), ``heartbeat`` (every 30 s:
progress and bytes since the last one, so a stall shows as heartbeats without progress), ``app``
(foreground / background), ``network`` (Wi-Fi / mobile changes), ``service`` (background
service), ``end`` (status, message, traceback). Every line has ``t`` (Unix time) and ``s``
(seconds since the task started). No file contents, passwords or tokens are written.

Read them with ``tools/trace_report.py``.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

KEEP = 20  # trace files kept on the phone
HEARTBEAT = 30.0  # seconds
FSYNC_EVERY = 5.0  # seconds


class Trace:
    def __init__(self, path: Path, heartbeat: Callable[[], dict[str, Any]] | None = None,
                 interval: float = HEARTBEAT, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self.started = clock()
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8")
        self._last_sync = 0.0
        self._closed = False
        self._stop = threading.Event()
        if heartbeat:
            threading.Thread(target=self._beat, args=(heartbeat, interval), daemon=True).start()

    def event(self, ev: str, **fields: Any) -> None:
        now = self.clock()
        line = json.dumps({"t": round(now, 3), "s": round(now - self.started, 3), "ev": ev, **fields},
                          ensure_ascii=False, default=str)
        with self._lock:
            if self._closed:
                return
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
                if now - self._last_sync >= FSYNC_EVERY or ev == "end":
                    os.fsync(self._fh.fileno())
                    self._last_sync = now
            except OSError:  # a full disk must not stop the task
                pass

    __call__ = event  # usable directly as http.scope(trace=...)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    os.fsync(self._fh.fileno())
                except OSError:
                    pass
                self._fh.close()

    def _beat(self, heartbeat: Callable[[], dict[str, Any]], interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.event("heartbeat", **heartbeat())
            except Exception:
                pass


class TraceStore:
    """The trace folder: one file per task, the newest KEEP kept."""

    def __init__(self, folder: Path, keep: int = KEEP):
        self.folder = folder
        self.keep = keep

    def new(self, name: str, **kw: Any) -> Trace:
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:40]
        path = self.folder / f"{stamp}-{safe}.jsonl"
        n = 1
        while path.exists():
            n += 1
            path = self.folder / f"{stamp}-{safe}-{n}.jsonl"
        self.prune(self.keep - 1)
        return Trace(path, **kw)

    def files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(self.folder.glob("*.jsonl"))

    def prune(self, keep: int) -> None:
        files = self.files()
        for old in files[:max(0, len(files) - keep)]:
            try:
                old.unlink()
            except OSError:
                pass


def environment() -> dict[str, Any]:
    """What the trace's ``start`` event says about the app and device."""
    try:
        from taskdeck import _build

        version = _build.VERSION
    except Exception:
        version = "?"
    info: dict[str, Any] = {"app": version, "python": sys.version.split()[0], "platform": sys.platform,
                            "machine": platform.machine()}
    for key in ("ANDROID_ROOT", "ANDROID_NATIVE_LIBRARY_DIR"):
        if os.environ.get(key):
            info["android"] = True
    return info

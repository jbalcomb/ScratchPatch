"""Copies task traces (taskdeck.core.trace) to Google Drive as they grow, for analysis away from
the phone. The files on the phone are the record; this only mirrors them.

A sync looks at every local trace and uploads the ones that changed since their last upload
(replacing the earlier copy) into "TaskDeck diagnostics" in My Drive. It runs when a task ends,
every few minutes while tasks run, and when TaskDeck starts (catching up on anything a closed or
killed app didn't get to). It needs Google Drive connected (and the credentials unlocked);
otherwise, or on any error, it simply tries again next time. It ignores the jobs' Wi-Fi-only
setting: traces are small.
"""

from __future__ import annotations

import shutil
import threading
from typing import TYPE_CHECKING, Any

from taskdeck.core.trace import TraceStore

if TYPE_CHECKING:
    from .app import TaskDeckApp

FOLDER = "TaskDeck diagnostics"
SYNCED = "diagnostics:synced"  # store: {file name: [size, Drive file id]}
FOLDER_ID = "diagnostics:folder"
EVERY = 120.0  # seconds between syncs while a task runs


class DiagnosticsSync:
    def __init__(self, app: "TaskDeckApp", traces: TraceStore):
        self.app = app
        self.traces = traces
        self._lock = threading.Lock()
        self._again = False
        self._timer: threading.Event | None = None
        self.last_error = ""

    # ---- when to sync -------------------------------------------------------------------------------

    def on_task(self, task: Any) -> None:
        """Task listener: keep a periodic sync going while tasks run; sync once more when they end."""
        if self.app.tasks.running():
            if self._timer is None:
                self._timer = stop = threading.Event()
                threading.Thread(target=self._every, args=(stop,), daemon=True).start()
        else:
            if self._timer is not None:
                self._timer.set()
                self._timer = None
            if not task.running:
                self.sync_soon()

    def _every(self, stop: threading.Event) -> None:
        while not stop.wait(EVERY):
            self.sync()

    def sync_soon(self) -> None:
        threading.Thread(target=self.sync, daemon=True).start()

    # ---- the sync -------------------------------------------------------------------------------------

    def sync(self) -> int:
        """Upload changed traces; returns how many were uploaded. Never raises."""
        if not self._lock.acquire(blocking=False):
            self._again = True  # one is running: run once more after it
            return 0
        uploaded = 0
        try:
            while True:
                self._again = False
                uploaded += self._sync_once()
                if not self._again:
                    return uploaded
        except Exception as exc:  # no connection, signed out, Drive full...: try again next time
            self.last_error = f"{type(exc).__name__}: {exc}"
            return uploaded
        finally:
            self._lock.release()

    def _sync_once(self) -> int:
        google = self.app.google
        if not google.connected or google.locked:
            return 0
        synced: dict[str, list] = dict(self.app.store.get(SYNCED) or {})
        local = {p.name: p for p in self.traces.files()}
        changed = [p for name, p in local.items() if (synced.get(name) or [None])[0] != p.stat().st_size]
        for name in [n for n in synced if n not in local]:  # pruned on the phone; the Drive copy stays
            del synced[name]
        if not changed:
            self.app.store.set(SYNCED, synced)
            return 0
        drive = google.drive()
        folder = self._folder(drive)
        snapshot_dir = self.app.temp_dir / "diagnostics"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        try:
            for path in changed:
                snap = snapshot_dir / path.name  # a still-growing file is copied, so its size holds still
                shutil.copyfile(path, snap)
                size = snap.stat().st_size
                old_id = (synced.get(path.name) or [None, None])[1]
                try:
                    file_id = drive.upload(snap, folder, path.name, replace=old_id)
                except Exception:
                    if not old_id:
                        raise
                    file_id = drive.upload(snap, folder, path.name)  # the old copy was deleted in Drive
                synced[path.name] = [size, file_id]
                self.app.store.set(SYNCED, synced)
                count += 1
        finally:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
        self.last_error = ""
        return count

    def _folder(self, drive: Any) -> str:
        known = self.app.store.get(FOLDER_ID)
        folders, _ = drive.children("root")
        if known and known in folders.values():
            return known
        folder_id = folders.get(FOLDER) or drive.create_folder(FOLDER).id
        self.app.store.set(FOLDER_ID, folder_id)
        return folder_id

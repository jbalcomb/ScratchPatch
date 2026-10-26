"""Background tasks: long work (extracting an archive, uploading...) that keeps running while
the user goes back, opens other jobs, or comes back later.

A task runs a function in a worker thread. The function reports progress with
``task.report(done, total)`` and checks ``task.cancel_requested``; it returns a short
message for the "done" state, or raises (``Cancelled`` for a requested stop). Listeners
(the UI) are told about changes, at most a few times a second while running.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .pipeline import Cancelled

RUNNING, DONE, FAILED, CANCELLED = "running", "done", "failed", "cancelled"
NOTIFY_EVERY = 0.25  # seconds between progress notifications


@dataclass
class Task:
    id: int
    title: str  # e.g. "Extracting photos.7z"
    job_id: str
    status: str = RUNNING
    done: int = 0
    total: int = 0
    message: str = ""  # result or error, for the user
    result: Any = None  # anything the job wants to keep (e.g. the destination folder)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    cancel_requested: bool = False
    _manager: "TaskManager | None" = field(default=None, repr=False, compare=False)
    _last_notify: float = field(default=0.0, repr=False, compare=False)

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    @property
    def fraction(self) -> float | None:
        return self.done / self.total if self.total else None

    def eta(self, now: float | None = None) -> float | None:
        """Seconds left, from the average speed so far (None until there is enough to go on)."""
        now = now or time.time()
        elapsed = now - self.started
        if not self.total or self.done <= 0 or elapsed < 1:
            return None
        return max(0.0, (self.total - self.done) * elapsed / self.done)

    def report(self, done: int, total: int | None = None) -> None:
        self.done = done
        if total is not None:
            self.total = total
        now = time.time()
        if self._manager and now - self._last_notify >= NOTIFY_EVERY:
            self._last_notify = now
            self._manager._notify(self)


class TaskManager:
    def __init__(self, start_thread: Callable[[Callable[[], None]], None] | None = None):
        self.tasks: list[Task] = []
        self.listeners: list[Callable[[Task], None]] = []
        self._ids = itertools.count(1)
        self._start = start_thread or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def start(self, title: str, job_id: str, work: Callable[[Task], str], result: Any = None) -> Task:
        task = Task(next(self._ids), title, job_id, result=result, _manager=self)
        self.tasks.append(task)
        self._notify(task)

        def run() -> None:
            try:
                task.message = work(task) or "Done"
                task.status = DONE
            except Cancelled as exc:
                task.message = str(exc) or "Cancelled."
                task.status = CANCELLED
            except Exception as exc:  # shown to the user; the job formats its own errors
                task.message = str(exc) or type(exc).__name__
                task.status = FAILED
            task.finished = time.time()
            self._notify(task)

        self._start(run)
        return task

    def cancel(self, task: Task) -> None:
        if task.running:
            task.cancel_requested = True
            self._notify(task)

    def running(self) -> list[Task]:
        return [t for t in self.tasks if t.running]

    def latest(self, job_id: str) -> Task | None:
        for task in reversed(self.tasks):
            if task.job_id == job_id:
                return task
        return None

    def forget(self, task: Task) -> None:
        if not task.running and task in self.tasks:
            self.tasks.remove(task)

    def _notify(self, task: Task) -> None:
        for listener in list(self.listeners):
            try:
                listener(task)
            except Exception:  # a broken listener (e.g. a closed screen) must not stop the task
                pass

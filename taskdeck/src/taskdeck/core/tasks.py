"""Background tasks: long work (extracting an archive, uploading...) that keeps running while
the user goes back, opens other jobs, or comes back later.

A task runs a function in a worker thread. The function reports progress with
``task.report(done, total)`` and checks ``task.cancel_requested``; it returns a short
message for the "done" state, or raises (``Cancelled`` for a requested stop). Listeners
(the UI) are told about changes, at most a few times a second while running.
"""

from __future__ import annotations

import contextlib
import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager

from .pipeline import Cancelled
from .trace import Trace, TraceStore, environment

RUNNING, DONE, FAILED, CANCELLED = "running", "done", "failed", "cancelled"
NOTIFY_EVERY = 0.25  # seconds between progress notifications
SPEED_WINDOW = 10.0  # seconds the current speed is averaged over
STALL_AFTER = 60.0  # seconds without progress before the task says so


class Speedometer:
    """Current speed: bytes moved over (at least) the last SPEED_WINDOW seconds, up to now, so it
    falls towards 0 when progress stops."""

    def __init__(self) -> None:
        self.samples: list[tuple[float, int]] = []  # (time, done), oldest first

    def add(self, done: int, now: float) -> None:
        if self.samples and done < self.samples[-1][1]:  # went backwards (e.g. restarted): start over
            self.samples = []
        self.samples.append((now, done))
        # keep one sample at or before the window's start, as the base
        while len(self.samples) > 2 and self.samples[1][0] <= now - SPEED_WINDOW:
            self.samples.pop(0)

    def speed(self, now: float | None = None) -> float | None:
        """Bytes per second, or None until there are two samples a moment apart."""
        if len(self.samples) < 2:
            return None
        now = now or time.time()
        base_t, base_done = self.samples[0]
        if now - base_t < 1:
            return None
        return (self.samples[-1][1] - base_done) / (now - base_t)


@dataclass
class Phase:
    """One stage of a task with its own progress, e.g. "Unpacking" then "Uploading to Google Drive"."""

    label: str
    total: int = 0
    done: int = 0
    started: float | None = None
    meter: Speedometer = field(default_factory=Speedometer, repr=False, compare=False)

    def speed(self, now: float | None = None) -> float | None:
        return self.meter.speed(now)

    @property
    def fraction(self) -> float | None:
        return min(1.0, self.done / self.total) if self.total else None

    @property
    def complete(self) -> bool:
        return bool(self.total) and self.done >= self.total

    def eta(self, now: float | None = None) -> float | None:
        if self.started is None or not self.total or self.done <= 0:
            return None
        elapsed = (now or time.time()) - self.started
        return max(0.0, (self.total - self.done) * elapsed / self.done) if elapsed >= 1 else None


@dataclass
class Task:
    id: int
    title: str  # e.g. "Extracting photos.7z"
    job_id: str
    status: str = RUNNING
    done: int = 0
    total: int = 0
    message: str = ""  # result or error, for the user
    note: str = ""  # a passing state while running, e.g. "Paused: no connection"
    phases: list[Phase] = field(default_factory=list)  # optional stages, each shown with its own bar
    result: Any = None  # anything the job wants to keep (e.g. the destination folder)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    cancel_requested: bool = False
    trace: Trace | None = field(default=None, repr=False, compare=False)  # diagnostics (taskdeck.core.trace)
    _manager: "TaskManager | None" = field(default=None, repr=False, compare=False)
    _last_notify: float = field(default=0.0, repr=False, compare=False)
    _logged_phase: int = field(default=-1, repr=False, compare=False)
    _beat_done: int = field(default=0, repr=False, compare=False)
    meter: Speedometer = field(default_factory=Speedometer, repr=False, compare=False)
    _progress_at: float = field(default_factory=time.time, repr=False, compare=False)
    _stalled: bool = field(default=False, repr=False, compare=False)

    def log(self, ev: str, **fields: Any) -> None:
        """Add an event to the task's diagnostics trace (if it has one)."""
        if self.trace:
            self.trace.event(ev, **fields)

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

    def set_phases(self, *phases: tuple[str, int]) -> None:
        """Split the work into stages: set_phases(("Unpacking", n), ("Uploading", n))."""
        self.phases = [Phase(label, total) for label, total in phases]
        self.log("phases", phases=[[label, total] for label, total in phases])
        self.report(0, sum(total for _, total in phases))

    def advance(self, index: int, done: int) -> None:
        """Progress within one phase (earlier phases count as finished)."""
        now = time.time()
        for i, phase in enumerate(self.phases):
            if i < index and not phase.complete:
                phase.done = phase.total
            if i == index:
                phase.started = phase.started or now
                phase.done = done
                phase.meter.add(done, now)
        if index != self._logged_phase:
            self._logged_phase = index
            self.log("phase", index=index, label=self.phases[index].label if index < len(self.phases) else "",
                     done=done)
        self.report(sum(p.done for p in self.phases))

    def set_note(self, note: str) -> None:
        if note != self.note:
            self.note = note
            self.log("note", note=note)
            if self._manager:
                self._manager._notify(self)

    def watch(self, now: float | None = None) -> None:
        """Called every heartbeat: says so when nothing has moved for a while (unless the task already
        explains why, e.g. paused), and refreshes the screen so the speed falls while stalled."""
        now = now or time.time()
        idle = now - self._progress_at
        if self.running and idle >= STALL_AFTER and (not self.note or self._stalled):
            self._stalled = True
            minutes = int(idle // 60)
            self.set_note(f"No progress for {minutes} minute{'s' * (minutes != 1)}. Still trying…")
        if self._manager:
            self._manager._notify(self)

    def snapshot(self) -> dict[str, Any]:
        """Progress for the trace's heartbeat: where it is, and how much moved since the last one."""
        phase = self.current_phase
        moved, self._beat_done = self.done - self._beat_done, self.done
        out: dict[str, Any] = {"done": self.done, "total": self.total, "moved": moved}
        if phase:
            out.update(phase=phase.label, phase_done=phase.done, phase_total=phase.total)
        if self.note:
            out["note"] = self.note
        if self.cancel_requested:
            out["cancel_requested"] = True
        return out

    @property
    def current_phase(self) -> Phase | None:
        return next((p for p in self.phases if not p.complete), None)

    def speed(self, now: float | None = None) -> float | None:
        return self.meter.speed(now)

    def report(self, done: int, total: int | None = None) -> None:
        now = time.time()
        if done != self.done:
            self._progress_at = now
            if self._stalled:
                self._stalled = False
                self.set_note("")
        self.done = done
        if total is not None:
            self.total = total
        self.meter.add(done, now)
        if self._manager and now - self._last_notify >= NOTIFY_EVERY:
            self._last_notify = now
            self._manager._notify(self)


class TaskManager:
    """``traces`` (a TraceStore) gives each task a diagnostics trace; ``around(task)`` is a context
    manager the work runs inside (the app uses it to record the task's network requests)."""

    def __init__(self, start_thread: Callable[[Callable[[], None]], None] | None = None,
                 traces: TraceStore | None = None,
                 around: Callable[[Task], ContextManager[Any]] | None = None):
        self.traces = traces
        self.around = around or (lambda task: contextlib.nullcontext())
        self.tasks: list[Task] = []
        self.listeners: list[Callable[[Task], None]] = []
        self._ids = itertools.count(1)
        self._start = start_thread or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def start(self, title: str, job_id: str, work: Callable[[Task], str], result: Any = None) -> Task:
        task = Task(next(self._ids), title, job_id, result=result, _manager=self)
        if self.traces:
            try:
                task.trace = self.traces.new(job_id, heartbeat=lambda: (task.watch(), task.snapshot())[1])
                task.log("start", title=title, job=job_id, task=task.id, **environment())
            except OSError:
                task.trace = None
        self.tasks.append(task)
        self._notify(task)

        def run() -> None:
            error = ""
            try:
                with self.around(task):
                    task.message = work(task) or "Done"
                task.status = DONE
            except Cancelled as exc:
                task.message = str(exc) or "Cancelled."
                task.status = CANCELLED
            except Exception as exc:  # shown to the user; the job formats its own errors
                task.message = str(exc) or type(exc).__name__
                task.status = FAILED
                error = traceback.format_exc()
            task.finished = time.time()
            task.note = ""
            task.log("end", status=task.status, message=task.message, secs=round(task.finished - task.started, 1),
                     done=task.done, total=task.total, **({"traceback": error} if error else {}))
            if task.trace:
                task.trace.close()
            self._notify(task)

        self._start(run)
        return task

    def cancel(self, task: Task) -> None:
        if task.running:
            task.cancel_requested = True
            task.log("cancel_requested")
            self._notify(task)

    def running(self) -> list[Task]:
        return [t for t in self.tasks if t.running]

    def latest(self, job_id: str) -> Task | None:
        for task in reversed(self.tasks):
            if task.job_id == job_id:
                return task
        return None

    def log_running(self, ev: str, **fields: Any) -> None:
        """An event for every running task's trace (e.g. the app went to the background)."""
        for task in self.running():
            task.log(ev, **fields)

    def forget(self, task: Task) -> None:
        if not task.running and task in self.tasks:
            self.tasks.remove(task)

    def _notify(self, task: Task) -> None:
        for listener in list(self.listeners):
            try:
                listener(task)
            except Exception:  # a broken listener (e.g. a closed screen) must not stop the task
                pass

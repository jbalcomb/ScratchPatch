"""Pipeline primitives.

A job is executed as a :class:`Pipeline` of :class:`Step` objects that share a
mutable :class:`Context`. Every step declares the :class:`Stage` it belongs to.
Stages are the seed of the "bigger, more defined pipeline": today they are only
labels that the plugin review tool uses to spot common shapes; once enough jobs
share a shape it can be promoted to a template in ``taskdeck.core.templates``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable


class Stage(str, Enum):
    """Canonical pipeline stages, in the order they normally run."""

    VALIDATE = "validate"  # check and coerce user input
    PREPARE = "prepare"  # normalize / load / derive inputs
    EXECUTE = "execute"  # the job's actual work
    PRESENT = "present"  # shape the output for display

    @property
    def order(self) -> int:
        return list(Stage).index(self)


class StepError(Exception):
    """Raised by a step to stop the pipeline with a user-facing message."""


class Cancelled(Exception):
    """Raised when a running pipeline is cancelled."""


@dataclass
class Result:
    """What a job hands back to the UI.

    ``text`` is shown as a selectable block; ``rows`` as a two-column table.
    Either or both may be set.
    """

    text: str | None = None
    rows: list[tuple[str, str]] = field(default_factory=list)
    title: str | None = None

    def as_plain_text(self) -> str:
        parts = []
        if self.title:
            parts.append(self.title)
        if self.rows:
            width = max(len(k) for k, _ in self.rows)
            parts.extend(f"{k.ljust(width)}  {v}" for k, v in self.rows)
        if self.text:
            parts.append(self.text)
        return "\n".join(parts)


@dataclass
class Context:
    """State shared by all steps in one run."""

    inputs: dict[str, Any]
    data: dict[str, Any] = field(default_factory=dict)
    result: Result | None = None
    log: list[str] = field(default_factory=list)
    _cancelled: bool = False

    def __getitem__(self, key: str) -> Any:
        if key in self.data:
            return self.data[key]
        return self.inputs[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def cancel(self) -> None:
        self._cancelled = True

    def check_cancelled(self) -> None:
        if self._cancelled:
            raise Cancelled()


StepFn = Callable[[Context], None]


@dataclass(frozen=True)
class Step:
    """One unit of work in a pipeline.

    ``key`` identifies the implementation (not the instance) and is what the
    plugin review groups on. Shared steps get a ``shared.`` key; local steps
    default to ``<module>.<qualname>`` of their function.
    """

    name: str
    fn: StepFn
    stage: Stage = Stage.EXECUTE
    key: str = ""
    params: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.key:
            module = getattr(self.fn, "__module__", "?")
            qualname = getattr(self.fn, "__qualname__", type(self.fn).__name__)
            object.__setattr__(self, "key", f"{module}.{qualname}")

    def __call__(self, ctx: Context) -> None:
        self.fn(ctx)


def step(stage: Stage = Stage.EXECUTE, name: str | None = None) -> Callable[[StepFn], Step]:
    """Decorator turning a ``fn(ctx)`` into a :class:`Step`."""

    def wrap(fn: StepFn) -> Step:
        return Step(name=name or fn.__name__.replace("_", " "), fn=fn, stage=stage)

    return wrap


@dataclass
class StepReport:
    name: str
    stage: Stage
    seconds: float
    ok: bool
    error: str | None = None


@dataclass
class RunReport:
    ok: bool
    result: Result | None
    steps: list[StepReport]
    error: str | None = None
    cancelled: bool = False

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.steps)


ProgressFn = Callable[[int, int, Step], None]


class Pipeline:
    """An ordered list of steps run against one :class:`Context`."""

    def __init__(self, steps: Iterable[Step]):
        self.steps: list[Step] = list(steps)

    def __iter__(self):
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    @property
    def shape(self) -> tuple[Stage, ...]:
        """The run-length-collapsed stage sequence, e.g. (VALIDATE, EXECUTE, PRESENT)."""
        out: list[Stage] = []
        for s in self.steps:
            if not out or out[-1] != s.stage:
                out.append(s.stage)
        return tuple(out)

    def run(self, ctx: Context, on_progress: ProgressFn | None = None) -> RunReport:
        reports: list[StepReport] = []
        total = len(self.steps)
        for i, s in enumerate(self.steps):
            if on_progress:
                on_progress(i, total, s)
            start = time.perf_counter()
            try:
                ctx.check_cancelled()
                s(ctx)
            except Cancelled:
                reports.append(StepReport(s.name, s.stage, time.perf_counter() - start, False, "cancelled"))
                return RunReport(False, None, reports, "Cancelled", cancelled=True)
            except StepError as exc:
                reports.append(StepReport(s.name, s.stage, time.perf_counter() - start, False, str(exc)))
                return RunReport(False, None, reports, str(exc))
            except Exception as exc:  # unexpected bug inside a plugin
                msg = f"{type(exc).__name__} in step '{s.name}': {exc}"
                reports.append(StepReport(s.name, s.stage, time.perf_counter() - start, False, msg))
                return RunReport(False, None, reports, msg)
            reports.append(StepReport(s.name, s.stage, time.perf_counter() - start, True))
        if on_progress and self.steps:
            on_progress(total, total, self.steps[-1])
        return RunReport(True, ctx.result, reports)

"""Reusable pipeline steps.

Everything here was (or would have been) written more than once inside job
plugins. The plugin review (``python -m taskdeck.review``) is how new candidates
are found; when you extract one, give it a ``shared.`` key via
:func:`shared_step` so the review can track adoption.
"""

from __future__ import annotations

from typing import Any, Callable

from taskdeck.core.pipeline import Context, Result, Stage, Step, StepError


def shared_step(stage: Stage) -> Callable[[Callable[..., Callable[[Context], None]]], Callable[..., Step]]:
    """Decorate a step *factory* so the steps it builds carry a stable shared key."""

    def wrap(factory: Callable[..., Callable[[Context], None]]) -> Callable[..., Step]:
        def build(*args: Any, **kwargs: Any) -> Step:
            label = kwargs.pop("label", None) or factory.__name__.replace("_", " ")
            fn = factory(*args, **kwargs)
            params = tuple((f"arg{i}", a) for i, a in enumerate(args)) + tuple(sorted(kwargs.items()))
            return Step(name=label, fn=fn, stage=stage, key=f"shared.{factory.__name__}", params=params)

        build.__name__ = factory.__name__
        build.__doc__ = factory.__doc__
        return build

    return wrap


# ---- validate ---------------------------------------------------------------

@shared_step(Stage.VALIDATE)
def require(*names: str):
    """Fail unless every named input is present and not blank."""

    def run(ctx: Context) -> None:
        missing = [n for n in names if ctx.get(n) in (None, "") or (isinstance(ctx.get(n), str) and not ctx.get(n).strip())]
        if missing:
            raise StepError(f"Please fill in: {', '.join(missing)}")

    return run


@shared_step(Stage.VALIDATE)
def in_range(name: str, low: float | None = None, high: float | None = None):
    """Fail unless the numeric input ``name`` lies within [low, high]."""

    def run(ctx: Context) -> None:
        value = ctx[name]
        if low is not None and value < low:
            raise StepError(f"{name} must be at least {low:g}")
        if high is not None and value > high:
            raise StepError(f"{name} must be at most {high:g}")

    return run


# ---- prepare ----------------------------------------------------------------

@shared_step(Stage.PREPARE)
def normalize_text(name: str, strip: bool = True):
    """Normalize line endings (and optionally trim) for a text input; stores it under the same key."""

    def run(ctx: Context) -> None:
        text = (ctx[name] or "").replace("\r\n", "\n").replace("\r", "\n")
        ctx[name] = text.strip() if strip else text

    return run


# ---- present ----------------------------------------------------------------

@shared_step(Stage.PRESENT)
def present_rows(key: str, title: str | None = None):
    """Turn ``ctx[key]`` (a mapping or list of pairs) into a table result."""

    def run(ctx: Context) -> None:
        data = ctx[key]
        items = data.items() if hasattr(data, "items") else data
        ctx.result = Result(rows=[(str(k), _fmt(v)) for k, v in items], title=title)

    return run


@shared_step(Stage.PRESENT)
def present_text(key: str, title: str | None = None):
    """Show ``ctx[key]`` as a selectable text block."""

    def run(ctx: Context) -> None:
        ctx.result = Result(text=str(ctx[key]), title=title)

    return run


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.6g}" if abs(value) < 1e15 else f"{value:.6e}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)

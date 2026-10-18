import pytest

from taskdeck.core import Context, Pipeline, Result, Stage, Step, StepError, step


def make(name, stage=Stage.EXECUTE, fn=None):
    return Step(name, fn or (lambda ctx: ctx.log.append(name)), stage)


def test_runs_steps_in_order_and_returns_result():
    def present(ctx):
        ctx.result = Result(text=",".join(ctx.log))

    ctx = Context({})
    report = Pipeline([make("a"), make("b"), Step("p", present, Stage.PRESENT)]).run(ctx)
    assert report.ok
    assert report.result.text == "a,b"
    assert [s.name for s in report.steps] == ["a", "b", "p"]


def test_step_error_stops_pipeline_with_message():
    def boom(ctx):
        raise StepError("nope")

    ctx = Context({})
    report = Pipeline([make("a"), Step("boom", boom), make("never")]).run(ctx)
    assert not report.ok and report.error == "nope"
    assert ctx.log == ["a"]
    assert [s.ok for s in report.steps] == [True, False]


def test_unexpected_exception_is_reported_not_raised():
    report = Pipeline([Step("div", lambda ctx: 1 / 0)]).run(Context({}))
    assert not report.ok
    assert "ZeroDivisionError" in report.error and "div" in report.error


def test_cancel_before_next_step():
    def cancel(ctx):
        ctx.cancel()

    ctx = Context({})
    report = Pipeline([Step("c", cancel), make("never")]).run(ctx)
    assert report.cancelled and ctx.log == []


def test_progress_callback_sees_every_step_then_completion():
    seen = []
    Pipeline([make("a"), make("b")]).run(Context({}), on_progress=lambda i, n, s: seen.append((i, n)))
    assert seen == [(0, 2), (1, 2), (2, 2)]


def test_shape_collapses_runs_of_the_same_stage():
    p = Pipeline([make("v", Stage.VALIDATE), make("v2", Stage.VALIDATE), make("x"), make("p", Stage.PRESENT)])
    assert p.shape == (Stage.VALIDATE, Stage.EXECUTE, Stage.PRESENT)


def test_context_reads_data_before_inputs():
    ctx = Context({"x": 1})
    assert ctx["x"] == 1
    ctx["x"] = 2
    assert ctx["x"] == 2 and ctx.inputs["x"] == 1
    assert ctx.get("missing", "d") == "d"


def test_step_decorator_and_default_key():
    @step(Stage.PREPARE)
    def load_things(ctx):
        pass

    assert load_things.name == "load things"
    assert load_things.stage is Stage.PREPARE
    assert load_things.key.endswith("load_things")


def test_result_plain_text_aligns_rows():
    text = Result(rows=[("a", "1"), ("long", "2")], text="body").as_plain_text()
    assert text.splitlines() == ["a     1", "long  2", "body"]

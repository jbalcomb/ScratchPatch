"""Validate, pretty-print or minify JSON."""

import json

from taskdeck.core import Field, JobPlugin, Pipeline, QuickAction, Stage, StepError, step
from taskdeck.shared import steps


@step(Stage.PREPARE)
def parse(ctx):
    try:
        ctx["doc"] = json.loads(ctx["text"])
    except json.JSONDecodeError as exc:
        raise StepError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None


@step(Stage.EXECUTE)
def dump(ctx):
    if ctx["minify"]:
        out = json.dumps(ctx["doc"], separators=(",", ":"), sort_keys=ctx["sort_keys"], ensure_ascii=False)
    else:
        out = json.dumps(ctx["doc"], indent=int(ctx["indent"]), sort_keys=ctx["sort_keys"], ensure_ascii=False)
    ctx["formatted"] = out


class JsonFormat(JobPlugin):
    id = "json_format"
    title = "JSON Formatter"
    description = "Validate JSON and pretty-print or minify it."
    category = "Developer"
    icon = "DATA_OBJECT"
    tags = ("json", "pretty", "minify", "validate", "format")
    fields = (
        Field.multiline("text", "JSON", required=True),
        Field.number("indent", "Indent", default=2),
        Field.toggle("sort_keys", "Sort keys"),
        Field.toggle("minify", "Minify"),
    )
    quick_actions = (QuickAction("JSON: minify", {"minify": True}),)

    def build_pipeline(self):
        return Pipeline([
            steps.require("text"),
            steps.in_range("indent", 0, 8),
            steps.normalize_text("text"),
            parse,
            dump,
            steps.present_text("formatted"),
        ])


PLUGIN = JsonFormat()

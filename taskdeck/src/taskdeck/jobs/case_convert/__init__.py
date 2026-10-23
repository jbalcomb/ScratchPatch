"""Convert text between UPPER, lower, Title, snake_case, kebab-case, camelCase and slugs."""

from taskdeck.core import Field, JobPlugin, Pipeline, QuickAction, Stage, StepError, step
from taskdeck.shared import steps
from taskdeck.shared.text import words

STYLES = {
    "UPPER CASE": lambda t: t.upper(),
    "lower case": lambda t: t.lower(),
    "Title Case": lambda t: t.title(),
    "snake_case": lambda t: "_".join(w.lower() for w in words(t)),
    "kebab-case": lambda t: "-".join(w.lower() for w in words(t)),
    "camelCase": lambda t: "".join(w.lower() if i == 0 else w.capitalize() for i, w in enumerate(words(t))),
    "PascalCase": lambda t: "".join(w.capitalize() for w in words(t)),
    "url-slug": lambda t: "-".join(w.lower() for w in words(t))[:80],
}


@step(Stage.EXECUTE)
def convert(ctx):
    style = STYLES.get(ctx["style"])
    if style is None:
        raise StepError(f"Unknown style {ctx['style']!r}")
    ctx["converted"] = "\n".join(style(line) for line in ctx["text"].split("\n"))


class CaseConvert(JobPlugin):
    id = "case_convert"
    title = "Case Converter"
    description = "Change text case or turn phrases into identifiers and URL slugs."
    category = "Text"
    icon = "TEXT_FIELDS"
    tags = ("upper", "lower", "snake", "camel", "kebab", "slug")
    fields = (
        Field.multiline("text", "Text", required=True),
        Field.choice("style", "Style", STYLES.keys()),
    )
    quick_actions = (
        QuickAction("Case: snake_case", {"style": "snake_case"}),
        QuickAction("Case: URL slug", {"style": "url-slug"}),
    )

    def build_pipeline(self):
        return Pipeline([steps.require("text"), steps.normalize_text("text"), convert, steps.present_text("converted")])


PLUGIN = CaseConvert()

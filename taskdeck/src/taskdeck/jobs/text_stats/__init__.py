"""Word, character, line and sentence counts plus reading time."""

from collections import Counter

from taskdeck.core import Field, JobPlugin, Pipeline, Stage, step
from taskdeck.shared import steps
from taskdeck.shared.text import sentences, words


@step(Stage.EXECUTE)
def count(ctx):
    text = ctx["text"]
    ws = words(text)
    top = Counter(w.lower() for w in ws if len(w) > 3).most_common(3)
    ctx["stats"] = {
        "Characters": len(text),
        "Characters (no spaces)": len("".join(text.split())),
        "Words": len(ws),
        "Lines": text.count("\n") + 1 if text else 0,
        "Sentences": len(sentences(text)),
        "Reading time": f"{max(1, round(len(ws) / 230))} min" if ws else "0 min",
        "Top words": ", ".join(f"{w} ({n})" for w, n in top) or "-",
    }


class TextStats(JobPlugin):
    id = "text_stats"
    title = "Text Statistics"
    description = "Count words, characters, lines and sentences; estimate reading time."
    category = "Text"
    icon = "ANALYTICS_OUTLINED"
    tags = ("count", "words", "characters", "length")
    fields = (Field.multiline("text", "Text", required=True),)

    def build_pipeline(self):
        return Pipeline([steps.require("text"), steps.normalize_text("text"), count, steps.present_rows("stats")])


PLUGIN = TextStats()

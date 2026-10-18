"""Work out a tip and split the bill between people."""

import math

from taskdeck.core import Field, JobPlugin, Pipeline, Stage, step
from taskdeck.shared import steps


@step(Stage.EXECUTE)
def split(ctx):
    bill, pct, people = ctx["bill"], ctx["tip_percent"], int(ctx["people"])
    tip = bill * pct / 100
    total = bill + tip
    each = total / people
    if ctx["round_up"]:
        each = math.ceil(each)
    ctx["split"] = {
        "Tip": f"{tip:,.2f}",
        "Total": f"{total:,.2f}",
        "Each person pays": f"{each:,.2f}",
        "People": people,
    }


class TipSplit(JobPlugin):
    id = "tip_split"
    title = "Tip & Split"
    description = "Calculate a tip and split the total between people."
    category = "Everyday"
    icon = "PAYMENTS_OUTLINED"
    tags = ("restaurant", "bill", "gratuity", "divide")
    fields = (
        Field.number("bill", "Bill amount", required=True),
        Field.number("tip_percent", "Tip %", default=18),
        Field.number("people", "People", default=1),
        Field.toggle("round_up", "Round each share up"),
    )

    def build_pipeline(self):
        return Pipeline([
            steps.require("bill"),
            steps.in_range("bill", 0),
            steps.in_range("tip_percent", 0, 100),
            steps.in_range("people", 1, 1000),
            split,
            steps.present_rows("split"),
        ])


PLUGIN = TipSplit()

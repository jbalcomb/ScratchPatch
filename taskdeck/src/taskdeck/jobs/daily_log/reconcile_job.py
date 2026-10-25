"""Reconcile the Daily Log sheet: preview the clean-up, then apply it (after a backup copy)."""

from __future__ import annotations

import hashlib
import json
import time

from taskdeck.core import JobPlugin, Pipeline, Result, Stage, StepError, step

from . import reconcile, sheets
from .config import SETTINGS


def fingerprint(values) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


@step(Stage.EXECUTE, name="read sheet")
def read_sheet(ctx):
    ctx["values"] = sheets.read_checked(sheets.make_backend(ctx.settings))


@step(Stage.EXECUTE, name="plan clean-up")
def make_plan(ctx):
    ctx["plan"] = reconcile.plan(ctx["values"])


@step(Stage.PRESENT)
def preview(ctx):
    plan = ctx["plan"]
    lines = [f"• {c}" for c in plan.changes] or ["Nothing to fix: one row per date, in order, no blank rows."]
    if plan.remarks:
        lines += ["", "Check by hand (not changed). Put “-” in a cell when the value is known to be missing:"]
        lines += [f"• {r}" for r in plan.remarks]
    ctx.result = Result(
        title=f"{len(plan.changes)} fix{'es' * (len(plan.changes) != 1)} to apply" if plan.changed else "The sheet is tidy",
        text="\n".join(lines),
        payload={"rows": plan.rows, "span": plan.span, "width": plan.width,
                 "fingerprint": fingerprint(ctx["values"])} if plan.changed else None,
    )


class ReconcileSheet(JobPlugin):
    id = "daily_log_reconcile"
    title = sheets.RECONCILE_TITLE
    description = "Find blank rows, duplicate dates and rows out of order in the Daily Log sheet, then fix them."
    category = "Work"
    icon = "CLEANING_SERVICES_OUTLINED"
    tags = ("instacart", "google sheets", "duplicates", "tidy", "clean up", "reconcile", "log")
    settings_from = "daily_log"
    run_label = "Check sheet"
    commit_label = "Apply fixes"
    committed_label = "Fixed"

    settings = SETTINGS

    def check_ready(self, settings):
        return super().check_ready(settings) + sheets.setup_problems(settings)

    def build_pipeline(self):
        return Pipeline([read_sheet, make_plan, preview])

    def commit(self, payload, settings):
        backend = sheets.make_backend(settings)
        values = sheets.read_checked(backend)
        if fingerprint(values) != payload["fingerprint"]:
            raise StepError("The sheet changed since the check. Tap Check sheet again.")
        name = time.strftime("Backup %Y-%m-%d %H.%M")
        backend.backup(name)
        rows = payload["rows"]
        blank = [""] * payload["width"]
        # Rewrite rows 2..span: the tidy rows, then blanks for the rows no longer used.
        backend.write(2, [list(r) + [""] * (payload["width"] - len(r)) for r in rows]
                      + [blank] * (payload["span"] - 1 - len(rows)))
        return f"Sheet tidied. The old version is in the tab “{name}”"


PLUGIN = ReconcileSheet()

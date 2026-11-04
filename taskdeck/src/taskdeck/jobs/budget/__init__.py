"""Budget jobs: the SnapShot tab of the budget sheet against the bank transactions the budget service on
the AI Workstation collects (see docs/jobs/budget.md)."""

from __future__ import annotations

import re
from datetime import date

from taskdeck.core import Field, JobPlugin, Pipeline, Problem, Result, Stage, StepError, step

from . import data, reconcile, sheets, timeline
from .config import SETTINGS

RECONCILE_TAB = "Reconcile (auto)"
TIMELINE_TAB = "Timeline (auto)"
HISTORY_MONTHS = 4  # enough to tell repeats from one-offs and to average spending


@step(Stage.EXECUTE, name="read SnapShot tab and bank transactions")
def load(ctx):
    """The budget list from the sheet, and a few months of transactions from the budget service."""
    ctx["items"] = sheets.read_items(ctx.settings)
    since = data.add_months(ctx.get("month") or date.today(), -HISTORY_MONTHS)
    feed = data.fetch(ctx.settings.get("budget_url"), since)
    if feed.through is None:
        raise StepError("The budget service has no transactions yet. Upload a bank export first.")
    ctx["feed"] = feed


@step(Stage.VALIDATE)
def parse_month(ctx):
    text = (ctx.inputs.get("month") or "").strip()
    if text and not re.fullmatch(r"\d{4}-\d{1,2}", text):
        raise StepError("Month must look like 2026-10, or be left blank for the latest month.")
    ctx["month"] = date(int(text[:4]), int(text[5:]), 1) if text else None


@step(Stage.EXECUTE, name="compare")
def compare(ctx):
    month = ctx.get("month") or ctx["feed"].through.replace(day=1)
    ctx["report"] = reconcile.reconcile(ctx["items"], ctx["feed"], month)


@step(Stage.PRESENT)
def show_report(ctx):
    report = ctx["report"]
    missing = report.missing
    ctx.result = Result(
        title=f"{report.month:%B %Y} against the SnapShot tab",
        text=reconcile.as_text(report),
        warning=f"Missing: {', '.join(l.name for l in missing)}." if missing else None,
        payload={"rows": reconcile.as_rows(report), "through": report.through.isoformat()},
    )


@step(Stage.EXECUTE, name="project balances")
def project(ctx):
    ctx["timeline"] = timeline.build(ctx["items"], ctx["feed"])


@step(Stage.PRESENT)
def show_timeline(ctx):
    tl = ctx["timeline"]
    negative = tl.first_negative()
    ctx.result = Result(
        title=f"Balances to {tl.end:%-m/%-d}",
        text=timeline.as_text(tl),
        warning=("Goes negative: " + ", ".join(f"{data.ACCOUNT_LABELS[a]} on {r.date:%-m/%-d}"
                                               for a, r in negative.items()) + ".") if negative else None,
        payload={"rows": timeline.as_rows(tl), "through": tl.through.isoformat()},
    )


class _BudgetJob(JobPlugin):
    category = "Money"
    settings = SETTINGS
    commit_label = "Write to sheet"
    committed_label = "Written"
    tab = ""

    def check_ready(self, settings):
        return super().check_ready(settings) + sheets.setup_problems(settings)

    def check_ready_online(self, settings):
        """Find the address that answers. A different one than typed is used as is, and noted (not blocking)."""
        typed = (settings.get("budget_url") or "").strip()
        if not typed:
            return []
        try:
            used = data.service_url(typed)
        except StepError as exc:
            return [Problem(str(exc), fix="recheck")]
        if used.rstrip("/") != typed.rstrip("/"):
            return [Problem(f"Using {used} (the address in settings, {typed}, doesn't answer from the phone). "
                            "Putting it in settings skips this lookup.", blocking=False, fix="settings")]
        return []

    def commit(self, payload, settings):
        if not isinstance(payload, dict) or "rows" not in payload:
            raise StepError("Nothing to write. Run the job first.")
        return sheets.write_tab(settings, self.tab, sheets.note(date.fromisoformat(payload["through"])),
                                payload["rows"])


class ReconcileMonth(_BudgetJob):
    id = "budget_reconcile"
    title = "Reconcile budget month"
    description = ("Check a month of bank transactions against the SnapShot tab: bills paid, missing or changed, "
                   "spending against its targets, and anything that isn't on the list.")
    icon = "FACT_CHECK_OUTLINED"
    tags = ("budget", "bills", "bank", "reconcile", "snapshot", "google sheets", "money", "subscriptions")
    fields = (Field.text("month", "Month (YYYY-MM)", help="Leave blank for the latest month with bank data."),)
    run_label = "Check month"
    tab = RECONCILE_TAB

    def build_pipeline(self):
        return Pipeline([parse_month, load, compare, show_report])


class UpdateTimeline(_BudgetJob):
    id = "budget_timeline"
    title = "Update budget timeline"
    description = ("This month's bank transactions, then the bills, spending and pay still to come through the end "
                   "of next month, with each account's balance day by day.")
    icon = "TIMELINE_OUTLINED"
    tags = ("budget", "bills", "bank", "timeline", "forecast", "balance", "overdraft", "google sheets", "money")
    settings_from = "budget_reconcile"
    run_label = "Project balances"
    tab = TIMELINE_TAB

    def build_pipeline(self):
        return Pipeline([load, project, show_timeline])


PLUGIN = ReconcileMonth()
PLUGINS = (PLUGIN, UpdateTimeline())

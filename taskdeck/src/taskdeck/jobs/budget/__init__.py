"""Budget jobs: the SnapShot tab of the budget sheet against the bank transactions the budget service on
the AI Workstation collects (see docs/jobs/budget.md)."""

from __future__ import annotations

import re
from datetime import date, timedelta

from taskdeck.core import (
    Field, JobPlugin, Pipeline, Problem, Result, Stage, StepError, step,
)

from . import data, ledger, reconcile, sheets, thisweek, timeline
from .config import SETTINGS

RECONCILE_TAB = "Reconcile (auto)"
THISWEEK_TAB = "ThisWeek"
HISTORY_MONTHS = 4  # enough to tell repeats from one-offs and to average spending


@step(Stage.EXECUTE, name="read the sheet's SnapShot, Exceptions, Cash and TimeLine tabs and the bank transactions")
def load(ctx):
    """The budget list, exceptions, cash entries and TimeLine rows from the sheet, and a few months of
    transactions from the budget service. Cash tab and TimeLine rows the bank (or the Cash tab) now covers are
    dropped; the rest count like bank transactions (see ledger)."""
    items = ctx["items"] = sheets.read_items(ctx.settings)
    ctx["exceptions"] = sheets.read_exceptions(ctx.settings)
    cash = sheets.read_cash(ctx.settings)
    typed = sheets.read_timeline(ctx.settings, {i.name for i in items})
    since = data.add_months(ctx.get("month") or date.today(), -HISTORY_MONTHS)
    feed = data.fetch(ctx.settings.get("budget_url"), since)
    if feed.through is None:
        raise StepError("The budget service has no transactions yet. Upload a bank export first.")
    ctx["feed"] = data.with_cash(feed, ledger.merge(feed.txns, cash, typed, ledger.window_start(date.today())))


@step(Stage.VALIDATE)
def parse_month(ctx):
    text = (ctx.inputs.get("month") or "").strip()
    if text and not re.fullmatch(r"\d{4}-\d{1,2}", text):
        raise StepError("Month must look like 2026-10, or be left blank for the latest month.")
    ctx["month"] = date(int(text[:4]), int(text[5:]), 1) if text else None


@step(Stage.EXECUTE, name="compare")
def compare(ctx):
    month = ctx.get("month") or ctx["feed"].through.replace(day=1)
    ctx["report"] = reconcile.reconcile(ctx["items"], ctx["feed"], month, ctx["exceptions"])


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
    ctx["timeline"] = timeline.build(ctx["items"], ctx["feed"], rows=ctx["exceptions"],
                                     start=ledger.window_start(date.today()))


@step(Stage.PRESENT)
def show_timeline(ctx):
    tl = ctx["timeline"]
    negative = tl.first_negative()
    ctx.result = Result(
        title=f"Balances to {tl.end:%-m/%-d}",
        text=timeline.as_text(tl),
        warning=("Goes negative: " + ", ".join(f"{data.ACCOUNT_LABELS[a]} on {r.date:%-m/%-d}"
                                               for a, r in negative.items()) + ".") if negative else None,
        payload={"tabs": {"timeline_tab": timeline.timeline_rows(tl), "projected_tab": timeline.projected_rows(tl)},
                 "through": tl.through.isoformat()},
    )


@step(Stage.EXECUTE, name="lay out the week")
def lay_out_week(ctx):
    ctx["week"] = thisweek.build(ctx["items"], ctx["feed"], date.today(), ctx["exceptions"])


@step(Stage.PRESENT)
def show_week(ctx):
    tw = ctx["week"]
    negative = tw.timeline.first_negative()
    soon = {a: r for a, r in negative.items() if r.date <= tw.start + timedelta(days=7 * (1 + thisweek.WEEKS_AFTER))}
    ctx.result = Result(
        title=thisweek.title(tw),
        text=thisweek.as_text(tw),
        warning=("Goes negative: " + ", ".join(f"{data.ACCOUNT_LABELS[a]} on {r.date:%-m/%-d}"
                                               for a, r in soon.items()) + ".") if soon else None,
        payload={"rows": thisweek.as_rows(tw), "through": tw.timeline.through.isoformat()},
    )


class _BudgetJob(JobPlugin):
    category = "Money"
    settings = SETTINGS
    commit_label = "Write to sheet"
    committed_label = "Written"
    tab = ""

    def check_ready(self, settings):
        return super().check_ready(settings) + sheets.setup_problems(settings)

    def clean_settings(self, values):
        return {**values, "budget_url": data.tidy_url(values.get("budget_url") or "")}

    def check_ready_online(self, settings):
        """Both halves, each reported: the budget service (the address that answers is saved in place of a
        wrong one) and the budget sheet's SnapShot tab. Also used by Test settings."""
        out = []
        typed = (settings.get("budget_url") or "").strip()
        if typed:
            try:
                used = data.service_url(typed)
                feed = data.fetch(used, date.today())  # just the balances
            except StepError as exc:
                out.append(Problem(str(exc), fix="recheck"))
            else:
                if used != typed.rstrip("/"):
                    out.append(Problem(f"Budget service address updated to {used} (it was {typed}, which doesn't "
                                       f"answer from the phone). {data.why_address(typed, used)}", blocking=False,
                                       fix="recheck", settings={"budget_url": used}))
                if feed.through:
                    out.append(Problem.passed(f"Budget service at {used}: {len(feed.balances)} accounts, bank data "
                                              f"through {feed.through:%-m/%-d/%Y}."))
                else:
                    out.append(Problem(f"The budget service at {used} answers but has no transactions yet. Upload a "
                                       "bank export first.", fix="recheck"))
        try:
            items = sheets.read_items(settings)
        except StepError as exc:
            out.append(Problem(f"Budget sheet: {exc}", fix="settings"))
            return out
        tab = (settings.get("snapshot_tab") or "SnapShot").strip()
        incomes = sum(1 for i in items if i.section == "income")
        expenses = [i for i in items if i.section == "expense"]
        cancelled = sum(1 for i in expenses if i.cancelled)
        out.append(Problem.passed(f"Budget sheet: the {tab} tab lists {incomes} income and {len(expenses)} expense "
                                  f"lines" + (f" ({cancelled} cancelled)" if cancelled else "") + "."))
        try:
            rows = sheets.read_exceptions(settings)
        except StepError as exc:
            out.append(Problem(f"Exceptions tab: {exc}", blocking=False, fix="recheck"))
        else:
            out.append(Problem.passed(f"Exceptions tab: {len(rows)} charges set aside or disputed."))
        try:
            cash = sheets.read_cash(settings)
        except StepError as exc:
            out.append(Problem(f"Cash tab: {exc}", blocking=False, fix="recheck"))
        else:
            out.append(Problem.passed(f"Cash tab: {len(cash)} entries."))
        return out

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
    description = ("TimeLine: every transaction from the bank, the Cash tab and your own rows, with each account's "
                   "balance. Projected: the same, then what's expected through the end of next month.")
    icon = "TIMELINE_OUTLINED"
    tags = ("budget", "bills", "bank", "timeline", "forecast", "balance", "overdraft", "google sheets", "money")
    settings_from = "budget_reconcile"
    run_label = "Project balances"
    commit_label = "Write TimeLine and Projected"

    def build_pipeline(self):
        return Pipeline([load, project, show_timeline])

    def commit(self, payload, settings):
        if not isinstance(payload, dict) or "tabs" not in payload:
            raise StepError("Nothing to write. Run the job first.")
        defaults = {"timeline_tab": "TimeLine", "projected_tab": "Projected"}
        done = [sheets.write_tab(settings, (settings.get(key) or defaults[key]).strip(), None, rows)
                for key, rows in payload["tabs"].items()]
        return "; ".join(done)


class UpdateThisWeek(_BudgetJob):
    id = "budget_thisweek"
    title = "Update ThisWeek"
    description = ("This budget week (Wednesday to Tuesday) item by item with every account's balance, and the three "
                   "weeks after, summed. Run it after the Wednesday-morning budget update.")
    icon = "VIEW_WEEK_OUTLINED"
    tags = ("budget", "bills", "week", "wednesday", "payday", "balance", "google sheets", "money")
    settings_from = "budget_reconcile"
    run_label = "Lay out the week"
    tab = THISWEEK_TAB

    def build_pipeline(self):
        return Pipeline([load, lay_out_week, show_week])


PLUGIN = ReconcileMonth()
PLUGINS = (PLUGIN, UpdateTimeline(), UpdateThisWeek())

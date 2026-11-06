"""The budget job: the SnapShot tab of the budget sheet against the bank transactions the budget service on
the AI Workstation collects (see docs/jobs/budget.md)."""

from __future__ import annotations

from datetime import date, timedelta

from taskdeck.core import (
    JobPlugin, Pipeline, Problem, Result, Stage, StepError, step,
)

from . import data, ledger, reconcile, sheets, thisweek, timeline
from .config import SETTINGS

# The tabs the job writes (settings key -> default name). It alone writes ThisWeek and Reconcile; TimeLine is
# shared with you (rows you type there are kept), and Projected follows TimeLine.
TABS = {"timeline_tab": "TimeLine", "projected_tab": "Projected", "thisweek_tab": "ThisWeek",
        "reconcile_tab": "Reconcile (auto)"}
OWN_TABS = ("thisweek_tab", "reconcile_tab")
HISTORY_MONTHS = 4  # the TimeLine window (3 months) plus a month, to tell repeats from one-offs


@step(Stage.EXECUTE, name="read the sheet's SnapShot, Exceptions, Cash and TimeLine tabs and the bank transactions")
def load(ctx):
    """The budget list, exceptions, cash entries and TimeLine rows from the sheet, and a few months of
    transactions from the budget service. Cash tab and TimeLine rows the bank (or the Cash tab) now covers are
    dropped; the rest count like bank transactions (see ledger)."""
    items = ctx["items"] = sheets.read_items(ctx.settings)
    ctx["exceptions"] = sheets.read_exceptions(ctx.settings)
    cash = sheets.read_cash(ctx.settings)
    typed = sheets.read_timeline(ctx.settings, {i.name for i in items})
    since = data.add_months(date.today(), -HISTORY_MONTHS)
    feed = data.fetch(ctx.settings.get("budget_url"), since)
    if feed.through is None:
        raise StepError("The budget service has no transactions yet. Upload a bank export first.")
    ctx["feed"] = data.with_cash(feed, ledger.merge(feed.txns, cash, typed, ledger.window_start(date.today())))


@step(Stage.EXECUTE, name="reconcile, project and lay out the week")
def update(ctx):
    """Everything from the one snapshot of the data: TimeLine and Projected, ThisWeek, and every month in the
    TimeLine window reconciled against SnapShot."""
    feed, items, rows, today = ctx["feed"], ctx["items"], ctx["exceptions"], date.today()
    start = ledger.window_start(today)
    ctx["timeline"] = timeline.build(items, feed, rows=rows, start=start)
    ctx["week"] = thisweek.build(items, feed, today, rows)
    months, m = [], min(start, feed.through.replace(day=1))
    while m <= feed.through.replace(day=1):
        months.append(m)
        m = data.add_months(m, 1)
    ctx["reports"] = [reconcile.reconcile(items, feed, m, rows) for m in months]


@step(Stage.PRESENT)
def show(ctx):
    tl, tw, reports = ctx["timeline"], ctx["week"], ctx["reports"]
    latest = reports[-1]
    soon = {a: r for a, r in tl.first_negative().items()
            if r.date <= tw.start + timedelta(days=7 * (1 + thisweek.WEEKS_AFTER))}
    warnings = []
    if latest.missing:
        warnings.append(f"Missing in {latest.month:%B}: {', '.join(l.name for l in latest.missing)}.")
    if soon:
        warnings.append("Goes negative: " + ", ".join(f"{data.ACCOUNT_LABELS[a]} on {r.date:%-m/%-d}"
                                                      for a, r in soon.items()) + ".")
    ctx.result = Result(
        title=f"Budget updated from bank data through {tl.through:%-m/%-d}",
        text="\n\n".join([thisweek.as_text(tw), "BALANCES\n" + timeline.as_text(tl),
                           f"{latest.month:%B %Y} AGAINST SNAPSHOT\n" + reconcile.as_text(latest)]),
        warning=" ".join(warnings) or None,
        payload={"tabs": {"timeline_tab": timeline.timeline_rows(tl), "projected_tab": timeline.projected_rows(tl),
                          "thisweek_tab": thisweek.as_rows(tw), "reconcile_tab": reconcile.as_rows_by_month(reports)},
                 "through": tl.through.isoformat()},
    )


class UpdateBudget(JobPlugin):
    # The id is the one the first budget job had, so the settings already saved on the phone stay.
    id = "budget_reconcile"
    title = "Update budget"
    description = ("Reconcile the bank transactions and update the whole budget sheet: TimeLine, Projected, "
                   "ThisWeek and Reconcile (every month, with a Month column to filter by).")
    icon = "FACT_CHECK_OUTLINED"
    tags = ("budget", "bills", "bank", "reconcile", "timeline", "forecast", "balance", "week", "wednesday",
            "snapshot", "google sheets", "money")
    run_label = "Update budget"
    category = "Money"
    settings = SETTINGS
    commit_label = "Write to sheet"
    committed_label = "Written"

    def build_pipeline(self):
        return Pipeline([load, update, show])

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
        """Write every tab. TimeLine and Projected get no note line: their header stays in row 1 (you add rows to
        TimeLine); the tabs the code owns alone get one."""
        if not isinstance(payload, dict) or "tabs" not in payload:
            raise StepError("Nothing to write. Run the job first.")
        note = sheets.note(date.fromisoformat(payload["through"]))
        done = []
        for key, rows in payload["tabs"].items():
            tab = (settings.get(key) or TABS[key]).strip()
            done.append(sheets.write_tab(settings, tab, note if key in OWN_TABS else None, rows))
        return "; ".join(done)


PLUGIN = UpdateBudget()
PLUGINS = (PLUGIN,)

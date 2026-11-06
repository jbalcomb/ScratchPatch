"""The ThisWeek tab: this budget week (Wednesday to Tuesday) item by item with every account's balance, then
the three weeks after, summed (pure: no network, no Google)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from . import exceptions, timeline
from .data import ACCOUNT_LABELS, ATM, CASH, TRANSFER, Feed, money, week_start
from .snapshot import Item

WEEKS_AFTER = 3


@dataclass
class Week:
    start: date                  # Wednesday
    income: int                  # cents in (not counting transfers between accounts)
    expenses: int                # cents out, as a positive number
    balances: dict[str, int]     # at the end of the Tuesday


@dataclass
class ThisWeek:
    start: date
    timeline: timeline.Timeline
    rows: list[timeline.Row]     # this week's
    weeks: list[Week]            # the ones after


def _moves(row: timeline.Row) -> bool:
    return row.category in (TRANSFER, ATM)


def build(items: list[Item], feed: Feed, today: date, rows: list[exceptions.Entry] = ()) -> ThisWeek:
    start = week_start(today)
    end = start + timedelta(days=7 * (1 + WEEKS_AFTER) - 1)
    tl = timeline.build(items, feed, end=end, rows=rows, start=min(start, feed.through.replace(day=1)))
    week_rows = [r for r in tl.rows if start <= r.date <= start + timedelta(days=6)]
    weeks = []
    balances = next((dict(r.balances) for r in reversed(tl.rows) if r.date < start + timedelta(days=7)),
                    dict(tl.rows[-1].balances) if tl.rows else {a: 0 for a in timeline.ACCOUNTS})
    for n in range(1, WEEKS_AFTER + 1):
        first, last = start + timedelta(days=7 * n), start + timedelta(days=7 * n + 6)
        these = [r for r in tl.rows if first <= r.date <= last and not _moves(r)]
        for r in tl.rows:
            if first <= r.date <= last:
                balances = dict(r.balances)
        weeks.append(Week(first, sum(r.amount for r in these if r.amount > 0),
                          -sum(r.amount for r in these if r.amount < 0), dict(balances)))
    return ThisWeek(start, tl, week_rows, weeks)


def title(tw: ThisWeek) -> str:
    return f"Week of Wednesday {tw.start:%-m/%-d} to Tuesday {tw.start + timedelta(days=6):%-m/%-d}"


def _balances(balances: dict[str, int]) -> str:
    return ", ".join(("cash on hand" if a == CASH else ACCOUNT_LABELS[a]) + f" {money(balances[a])}"
                     for a in timeline.ACCOUNTS)


def as_text(tw: ThisWeek) -> str:
    out = [title(tw) + f" (bank data through {tw.timeline.through:%-m/%-d})"]
    for r in tw.rows:
        mark = "" if r.kind == timeline.ACTUAL else " (expected)"
        out.append(f"• {r.date:%a %-m/%-d} {ACCOUNT_LABELS[r.account]}: {r.description} {money(r.amount)}{mark}")
    if tw.rows:
        end = tw.rows[-1].balances
        out.append("Balances: " + _balances(end))
    out.append("")
    out.append(f"NEXT {WEEKS_AFTER} WEEKS")
    for w in tw.weeks:
        out.append(f"• Week of {w.start:%-m/%-d}: in {money(w.income)}, out {money(w.expenses)}; then "
                   + _balances(w.balances))
    return "\n".join(out)


def as_rows(tw: ThisWeek) -> list[list]:
    labels = ["Cash on hand" if a == CASH else ACCOUNT_LABELS[a] for a in timeline.ACCOUNTS]
    out = [[title(tw)]] + timeline.as_rows(tw.timeline, tw.rows)
    out += [[], [f"Next {WEEKS_AFTER} weeks"], ["Week of", "Income", "Expenses", *labels, "Total"]]
    for w in tw.weeks:
        out.append([f"{w.start:%-m/%-d/%Y}", w.income / 100, w.expenses / 100,
                    *[w.balances[a] / 100 for a in timeline.ACCOUNTS], sum(w.balances.values()) / 100])
    return out

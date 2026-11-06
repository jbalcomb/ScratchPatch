"""One month of bank transactions against the budget list (pure: no network, no Google)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import exceptions
from .data import (
    ACCOUNT_LABELS, ATM, CASH, TRANSFER, Feed, Txn, add_months, cash_withdrawal, due_date, merchant, money, month_end,
    wednesdays,
)
from .snapshot import Item

GRACE = timedelta(days=3)  # a bill can post a few days after its day
WEEK = timedelta(days=6)   # a budget week: Wednesday + 6 days = Tuesday

BILLS, SPENDING, INCOME, NOT_LISTED, SET_ASIDE, DISPUTED = (
    "Bills", "Spending", "Income", "Not on SnapShot", "Set aside", "Disputed")
SECTIONS = (INCOME, BILLS, SPENDING, NOT_LISTED, DISPUTED, SET_ASIDE)


@dataclass
class Line:
    section: str
    name: str
    status: str
    date: date | None = None
    amount: int | None = None     # cents, as the bank shows it (negative = out)
    expected: int | None = None   # cents, from the budget list (positive)
    detail: bool = False          # a week under its line


@dataclass
class Report:
    month: date                    # first day of the month
    through: date | None           # bank data goes up to this day
    lines: list[Line] = field(default_factory=list)

    def section(self, name: str) -> list[Line]:
        return [line for line in self.lines if line.section == name]

    @property
    def missing(self) -> list[Line]:
        return [line for line in self.section(BILLS) if line.status == "missing"]


def income_names(items: list[Item]) -> set[str]:
    return {i.name for i in items if i.section == "income"}


def _key(t: Txn) -> str:
    return t.category or f"Uncategorized: {merchant(t.description)}"


def _weekly(report: Report, item: Item, txns: list[Txn], through: date | None, section: str) -> None:
    """A weekly line, week by week: each Wednesday-to-Tuesday week whose Wednesday is in the month."""
    weeks = wednesdays(report.month)
    sign = 1 if item.section == "income" else -1
    total, lines = 0, []
    for wed in weeks:
        got = sign * sum(t.amount for t in txns if t.category == item.name and wed <= t.date <= wed + WEEK)
        total += got
        if through is not None and wed > through:
            status = "not yet"
        else:
            status = f"{money(got)} of {money(item.amount)}" if item.amount else money(got)
            if through is not None and wed + WEEK > through:
                status += " so far"
        lines.append(Line(section, f"week of {wed:%-m/%-d}", status, wed, sign * got, item.amount, detail=True))
    target = (item.amount or 0) * len(weeks)
    word = "baseline" if item.section == "income" else "target"
    if item.amount:
        status = f"{money(total)} of {money(target)} ({len(weeks)} weeks × {money(item.amount)} {word})"
        if item.section == "expense" and total > target:
            status += f", over by {money(total - target)}"
    else:
        status = f"{money(total)} (no Weekly amount set)"
    report.lines.append(Line(section, item.name, status, amount=sign * total, expected=target or None))
    report.lines += lines


def reconcile(items: list[Item], feed: Feed, month: date, rows: list[exceptions.Entry] = ()) -> Report:
    month = month.replace(day=1)
    last = month_end(month)
    report = Report(month, feed.through)
    applied = exceptions.apply(list(rows), feed.txns)
    usable = [t for t in feed.txns if id(t) not in applied.claimed]
    in_month = [t for t in usable if month <= t.date <= last]
    ever = {t.category for t in feed.txns if t.category}
    incomes = income_names(items)
    used: set[int] = set()

    # Income: SnapShot's income lines (weekly ones week by week, against the baseline), then any other income.
    for item in items:
        if item.section != "income":
            continue
        if item.weekly:
            _weekly(report, item, usable, feed.through, INCOME)
        else:
            got = sum(t.amount for t in in_month if t.category == item.name)
            report.lines.append(Line(INCOME, item.name, f"{money(got)} of {money(item.amount)}", amount=got,
                                     expected=item.amount))
    other: dict[str, int] = {}
    starting = next((t for t in feed.txns if t.account == CASH), None)  # the Cash tab's first row: cash on hand
    for t in in_month:
        if t.is_income and t.category not in incomes:
            other[t.category] = other.get(t.category, 0) + t.amount
        elif t.account == CASH and t.amount > 0 and not t.category and t is not starting:
            other["Cash received (no Line)"] = other.get("Cash received (no Line)", 0) + t.amount
    for name, total in sorted(other.items()):
        report.lines.append(Line(INCOME, name, f"{money(total)} (not on SnapShot)", amount=total))

    # Bills: each expected once (monthly on its day, yearly in its month), unless cancelled or disputed.
    disputed_names = {}
    for charge, row, credit in applied.disputed:
        if month <= charge.date <= last and charge.category:
            disputed_names.setdefault(charge.category, (charge, credit))
    for item in items:
        if item.section != "expense" or item.kind == "spending":
            continue
        if item.weekly:
            _weekly(report, item, usable, feed.through, BILLS)
            continue
        options = [t for t in in_month if t.category == item.name and t.amount < 0 and id(t) not in used]
        if item.kind == "annual":
            due = due_date(month.year, item.day[0], item.day[1]) if item.day else None
            if not options and (due is None or due.month != month.month):
                report.lines.append(Line(BILLS, item.name, "yearly" + (f", due {due:%-m/%-d}" if due else ""),
                                         expected=item.amount))
                continue
        else:
            due = due_date(month.year, month.month, item.day)
        if options:
            t = min(options, key=lambda t: abs(-t.amount - (item.amount or 0)))
            used.add(id(t))
            diff = -t.amount - (item.amount or 0)
            ok = item.amount is None or abs(diff) <= max(100, item.amount * 2 // 100)
            status = "paid" if ok else f"paid {money(diff, sign=True)} vs the budget"
            if item.cancelled:
                status = "cancelled, but still charged"
            report.lines.append(Line(BILLS, item.name, status, t.date, t.amount, item.amount))
        elif item.name in disputed_names:
            charge, credit = disputed_names[item.name]
            report.lines.append(Line(BILLS, item.name, exceptions.describe_dispute(charge, credit), charge.date,
                                     charge.amount, item.amount))
        elif item.cancelled:
            report.lines.append(Line(BILLS, item.name, "cancelled", expected=item.amount))
        elif item.account == CASH:  # paid only when the Cash tab has it (on its line)
            atm = cash_withdrawal(usable, due, item.amount or 0)
            seen = f"; ATM withdrawal from {ACCOUNT_LABELS[atm.account]} {atm.date:%-m/%-d}" if atm else ""
            late = feed.through is not None and feed.through >= due + GRACE
            status = (f"not paid: no {item.name} entry in the Cash tab" if late else f"due {due:%-m/%-d}") + seen
            report.lines.append(Line(BILLS, item.name, status, due, None, item.amount))
        elif item.name not in ever:
            report.lines.append(Line(BILLS, item.name, "never in the bank data: paid in cash?", expected=item.amount))
        else:
            late = feed.through is not None and feed.through >= due + GRACE
            report.lines.append(Line(BILLS, item.name, "missing" if late else f"due {due:%-m/%-d}",
                                     due, None, item.amount))

    # Spending lines: weekly ones week by week; any monthly one against its Monthly amount.
    for item in items:
        if item.section != "expense" or item.kind != "spending":
            continue
        if item.weekly:
            _weekly(report, item, usable, feed.through, SPENDING)
            continue
        spent = -sum(t.amount for t in in_month if t.category == item.name)
        status = (f"{money(spent)} of {money(item.amount)}" + (f", over by {money(spent - item.amount)}"
                                                                 if spent > item.amount else "")
                  if item.amount else f"{money(spent)} (no Monthly amount set)")
        report.lines.append(Line(SPENDING, item.name, status, amount=-spent, expected=item.amount))

    # Spending with no budget line, plus extra charges in a bill's category. Repeats = also in 2 of the
    # 3 months before, so a new subscription stands out from a one-off. ATM withdrawals move money to Cash.
    names = {i.name for i in items if i.section == "expense"}
    line_names = {i.name for i in items if i.section == "expense" and (i.kind == "spending" or i.weekly)}
    groups: dict[str, list[Txn]] = {}
    for t in in_month:
        if (t.amount >= 0 or id(t) in used or t.is_income or t.category in incomes
                or t.category in (TRANSFER, ATM) or t.category in line_names):
            continue
        groups.setdefault(_key(t), []).append(t)
    earlier = [add_months(month, -n) for n in (1, 2, 3)]
    not_listed = []
    for key, txns in groups.items():
        months_seen = sum(1 for m in earlier
                          if any(_key(t) == key and t.amount < 0 and m <= t.date <= month_end(m) for t in usable))
        total = sum(t.amount for t in txns)
        shops = sorted({merchant(t.description) for t in txns})
        if key.startswith("Uncategorized") or len(shops) == 1:
            shops = []
        what = "extra charge, beyond the budget line" if key in names else (
            "repeats: also in earlier months" if months_seen >= 2 else "one-off")
        detail = f" ({', '.join(shops[:4])}{', ...' if len(shops) > 4 else ''})" if shops else ""
        not_listed.append(Line(NOT_LISTED, key, f"{len(txns)}× {what}{detail}", txns[-1].date, total))
    # Repeats first, then the biggest amounts.
    report.lines += sorted(not_listed, key=lambda l: ("repeats" not in l.status, l.amount))

    for charge, row, credit in applied.disputed:
        if month <= charge.date <= last:
            note = f" ({row.note})" if row.note else ""
            report.lines.append(Line(DISPUTED, row.match, exceptions.describe_dispute(charge, credit) + note,
                                     charge.date, charge.amount))
    for charge, row in applied.set_aside:
        if month <= charge.date <= last:
            report.lines.append(Line(SET_ASIDE, row.match, "set aside" + (f": {row.note}" if row.note else ""),
                                     charge.date, charge.amount))
    return report


def as_text(report: Report) -> str:
    out = []
    for section in SECTIONS:
        lines = report.section(section)
        if not lines:
            continue
        out.append(section.upper())
        for l in lines:
            when = f" {l.date:%-m/%-d}" if l.date and l.amount is not None and not l.detail and section not in (
                SPENDING, INCOME) else ""
            amount = f" {money(l.amount)}" if l.amount is not None and section in (
                BILLS, NOT_LISTED, DISPUTED, SET_ASIDE) and not l.detail else ""
            out.append(f"{'    ◦' if l.detail else '•'} {l.name}: {l.status}{when}{amount}")
        out.append("")
    return "\n".join(out).strip()


def as_rows(report: Report) -> list[list]:
    rows = [["Section", "Item", "Status", "Date", "Amount", "Budget"]]
    for l in report.lines:
        rows.append([l.section, ("    " if l.detail else "") + l.name, l.status,
                     f"{l.date:%-m/%-d/%Y}" if l.date else "",
                     l.amount / 100 if l.amount is not None else "", l.expected / 100 if l.expected else ""])
    return rows

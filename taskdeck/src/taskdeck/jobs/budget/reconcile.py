"""One month of bank transactions against the budget list (pure: no network, no Google)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .data import TRANSFER, Feed, Txn, add_months, due_date, merchant, money, month_end
from .snapshot import Item

GRACE = timedelta(days=3)  # a bill can post a few days after its due day


@dataclass
class Line:
    section: str
    name: str
    status: str
    date: date | None = None
    amount: int | None = None     # cents, as the bank shows it (negative = out)
    expected: int | None = None   # cents, from the budget list (positive)


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


BILLS, SPENDING, NOT_LISTED, INCOME = "Bills", "Spending", "Not on SnapShot", "Income"


def _key(t: Txn) -> str:
    return t.category or f"Uncategorized: {merchant(t.description)}"


def reconcile(items: list[Item], feed: Feed, month: date) -> Report:
    month = month.replace(day=1)
    last = month_end(month)
    report = Report(month, feed.through)
    in_month = [t for t in feed.txns if month <= t.date <= last]
    ever = {t.category for t in feed.txns if t.category}
    used: set[int] = set()

    for item in items:
        if item.kind == "spending":
            continue
        options = [t for t in in_month if t.category == item.name and t.amount < 0 and id(t) not in used]
        if options:
            t = min(options, key=lambda t: abs(-t.amount - (item.amount or 0)))
            used.add(id(t))
            diff = -t.amount - (item.amount or 0)
            ok = item.amount is None or abs(diff) <= max(100, item.amount * 2 // 100)
            status = "paid" if ok else f"paid {money(diff, sign=True)} vs the budget"
            report.lines.append(Line(BILLS, item.name, status, t.date, t.amount, item.amount))
        elif item.kind == "annual":
            report.lines.append(Line(BILLS, item.name, "yearly, not charged this month", expected=item.amount))
        elif item.name not in ever:
            report.lines.append(Line(BILLS, item.name, "never in the bank data: paid in cash?", expected=item.amount))
        else:
            due = due_date(month.year, month.month, item.day) if item.day else last
            late = feed.through is not None and feed.through >= due + GRACE
            report.lines.append(Line(BILLS, item.name, "missing" if late else f"due {due:%-m/%-d}",
                                     due, None, item.amount))

    for item in items:
        if item.kind != "spending":
            continue
        spent = -sum(t.amount for t in in_month if t.category == item.name)
        if item.amount:
            status = f"{money(spent)} of {money(item.amount)}" + (
                f", over by {money(spent - item.amount)}" if spent > item.amount else "")
        else:
            status = f"{money(spent)} (no Monthly amount set)"
        report.lines.append(Line(SPENDING, item.name, status, amount=-spent, expected=item.amount))

    # Spending with no budget line, plus extra charges in a bill's category. Repeats = also in 2 of the
    # 3 months before, so a new subscription stands out from a one-off.
    names = {i.name for i in items}
    spending_names = {i.name for i in items if i.kind == "spending"}
    groups: dict[str, list[Txn]] = {}
    for t in in_month:
        if t.amount >= 0 or id(t) in used or t.is_income or t.category == TRANSFER or t.category in spending_names:
            continue
        groups.setdefault(_key(t), []).append(t)
    earlier = [add_months(month, -n) for n in (1, 2, 3)]
    not_listed = []
    for key, txns in groups.items():
        months_seen = sum(1 for m in earlier
                          if any(_key(t) == key and t.amount < 0 and m <= t.date <= month_end(m) for t in feed.txns))
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

    incomes: dict[str, int] = {}
    for t in in_month:
        if t.is_income:
            incomes[t.category] = incomes.get(t.category, 0) + t.amount
    for name, total in sorted(incomes.items()):
        report.lines.append(Line(INCOME, name, money(total), amount=total))
    return report


def as_text(report: Report) -> str:
    out = []
    for section in (BILLS, SPENDING, NOT_LISTED, INCOME):
        lines = report.section(section)
        if not lines:
            continue
        out.append(section.upper())
        for l in lines:
            when = f" {l.date:%-m/%-d}" if l.date and l.amount is not None and section != SPENDING else ""
            amount = f" {money(l.amount)}" if l.amount is not None and section in (BILLS, NOT_LISTED) else ""
            out.append(f"• {l.name}: {l.status}{when}{amount}")
        out.append("")
    return "\n".join(out).strip()


def as_rows(report: Report) -> list[list]:
    rows = [["Section", "Item", "Status", "Date", "Amount", "Budget"]]
    for l in report.lines:
        rows.append([l.section, l.name, l.status, f"{l.date:%-m/%-d/%Y}" if l.date else "",
                     l.amount / 100 if l.amount is not None else "", l.expected / 100 if l.expected else ""])
    return rows

"""Day-by-day balances: the bank's actual transactions this month, then what the budget list says is
coming, through the end of next month (pure: no network, no Google)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .data import ACCOUNT_LABELS, Feed, Txn, add_months, due_date, money, month_end
from .snapshot import Item

ACCOUNTS = tuple(ACCOUNT_LABELS)  # column order: checking (Daily), bills, savings
CASH = "cash"                     # paid outside the bank (rent): listed, but moves no balance


@dataclass
class Row:
    date: date
    account: str
    description: str
    category: str
    amount: int                  # cents
    balances: dict[str, int]     # every account's balance after this row
    projected: bool


@dataclass
class Timeline:
    start: date
    through: date                 # last day of bank data
    end: date
    rows: list[Row] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def first_negative(self) -> dict[str, Row]:
        """Per account, the first projected row that leaves it below zero."""
        out: dict[str, Row] = {}
        for row in self.rows:
            if row.projected and row.account in ACCOUNTS and row.balances[row.account] < 0:
                out.setdefault(row.account, row)
        return out

    def lowest(self) -> dict[str, tuple[int, date]]:
        """Per account, the lowest projected balance and the day it happens."""
        out: dict[str, tuple[int, date]] = {}
        for row in self.rows:
            if row.projected:
                for a in ACCOUNTS:
                    if a not in out or row.balances[a] < out[a][0]:
                        out[a] = (row.balances[a], row.date)
        return out


def _usual_account(txns: list[Txn], category: str) -> str:
    """The account that last paid in this category (a bill can move to another card); cash if never seen."""
    seen = [t.account for t in txns if t.category == category and t.amount < 0]
    return seen[-1] if seen else CASH


def _monthly_average(txns: list[Txn], category: str, through: date) -> int:
    """Average spent per month in a category over the last three full months (cents, positive)."""
    first = add_months(through, -3)
    last = date.fromordinal(through.replace(day=1).toordinal() - 1)
    return -sum(t.amount for t in txns if t.category == category and first <= t.date <= last) // 3


def build(items: list[Item], feed: Feed, end: date | None = None) -> Timeline:
    through = feed.through
    if through is None:
        raise ValueError("no bank data")
    start = through.replace(day=1)
    end = end or month_end(add_months(through, 1))
    tl = Timeline(start, through, end)

    # Balances at the start of the month: before each account's first transaction this month.
    balances = {a: feed.balances.get(a, (0, through))[0] for a in ACCOUNTS}
    actual = [t for t in feed.txns if t.date >= start]
    for a in ACCOUNTS:
        first = next((t for t in actual if t.account == a), None)
        if first and first.balance is not None:
            balances[a] = first.balance - first.amount
    for t in actual:
        if t.account in balances:
            balances[t.account] = t.balance if t.balance is not None else balances[t.account] + t.amount
        tl.rows.append(Row(t.date, t.account, t.description, t.category or "", t.amount, dict(balances), False))

    # What's coming, from the day after the bank data ends.
    coming: list[tuple[date, str, str, str, int]] = []
    months = []
    m = start
    while m <= end:
        months.append(m)
        m = add_months(m, 1)
    for item in items:
        if item.kind == "bill" and item.day:
            account = _usual_account(feed.txns, item.name)
            for m in months:
                due = due_date(m.year, m.month, item.day)
                paid = any(t.category == item.name and t.amount < 0 and m <= t.date <= month_end(m)
                           for t in feed.txns)
                if through < due <= end and not paid:
                    coming.append((due, account, item.name, item.name, -item.amount))
        elif item.kind == "spending":
            monthly = item.amount or _monthly_average(feed.txns, item.name, through)
            if monthly > 0:
                weekly = round(monthly * 12 / 52)
                account = _usual_account(feed.txns, item.name)
                day = through + timedelta(days=(7 - through.weekday()) % 7 or 7)  # next Monday
                while day <= end:
                    coming.append((day, account, f"{item.name} (weekly share)", item.name, -weekly))
                    day += timedelta(days=7)
    annual = [i.name for i in items if i.kind == "annual"]
    if annual:
        tl.notes.append("Not projected (yearly, no date): " + ", ".join(annual) + ".")

    # Income that came in at least 3 of the last 4 weeks repeats weekly, on the same weekday, at its average.
    recent = [t for t in feed.txns if t.is_income and t.amount > 0 and t.date > through - timedelta(days=28)]
    for category in sorted({t.category for t in recent}):
        deposits = [t for t in recent if t.category == category]
        if len(deposits) >= 3:
            average = sum(t.amount for t in deposits) // len(deposits)
            day = deposits[-1].date + timedelta(days=7)
            while day <= through:
                day += timedelta(days=7)
            while day <= end:
                coming.append((day, deposits[-1].account, f"{category} (expected)", category, average))
                day += timedelta(days=7)
            tl.notes.append(f"{category}: {money(average)} expected every {deposits[-1].date:%A} "
                            f"(average of the last {len(deposits)}).")

    for due, account, description, category, amount in sorted(coming, key=lambda c: (c[0], -c[4])):
        if account in balances:
            balances[account] += amount
        tl.rows.append(Row(due, account, description, category, amount, dict(balances), True))
    return tl


def as_text(tl: Timeline) -> str:
    out = [f"Bank data through {tl.through:%-m/%-d}; projected to {tl.end:%-m/%-d}."]
    negative = tl.first_negative()
    for a, (low, day) in tl.lowest().items():
        line = f"• {ACCOUNT_LABELS[a]}: lowest {money(low)} on {day:%-m/%-d}"
        if a in negative:
            row = negative[a]
            line += (f". Goes negative on {row.date:%-m/%-d} ({row.description} {money(row.amount)}): move at "
                     f"least {money(-low)} into {ACCOUNT_LABELS[a]} before then to stay above zero to {tl.end:%-m/%-d}")
        out.append(line)
    cash = [r for r in tl.rows if r.projected and r.account == CASH]
    if cash:
        out.append(f"• Cash needed: {money(-sum(r.amount for r in cash))} ("
                   + ", ".join(f"{r.description} {r.date:%-m/%-d}" for r in cash) + ")")
    out += [f"• {n}" for n in tl.notes]
    return "\n".join(out)


def as_rows(tl: Timeline) -> list[list]:
    labels = [ACCOUNT_LABELS[a] for a in ACCOUNTS]
    rows = [["Date", "Account", "Description", "Category", "Amount", *labels, "Total", "Kind"]]
    for r in tl.rows:
        rows.append([f"{r.date:%-m/%-d/%Y}", ACCOUNT_LABELS.get(r.account, r.account.title()), r.description,
                     r.category, r.amount / 100, *[r.balances[a] / 100 for a in ACCOUNTS],
                     sum(r.balances.values()) / 100, "projected" if r.projected else "actual"])
    return rows

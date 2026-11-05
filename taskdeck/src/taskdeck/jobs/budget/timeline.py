"""Day-by-day balances: the bank's actual transactions this month, then what the budget list says is
coming, through the end of next month (pure: no network, no Google)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import exceptions
from .data import ACCOUNT_LABELS, ATM, CASH, TRANSFER, Feed, Txn, add_months, due_date, money, month_end, week_start
from .snapshot import Item

ACCOUNTS = tuple(ACCOUNT_LABELS)  # column order: checking (Daily), bills, savings (Save), cash
BANK = tuple(a for a in ACCOUNTS if a != CASH)
ACTUAL, PROJECTED, ASSUMED = "actual", "projected", "cash, assumed paid"


@dataclass
class Row:
    date: date
    account: str
    description: str
    category: str
    amount: int                  # cents
    balances: dict[str, int]     # every account's balance after this row
    kind: str = PROJECTED        # ACTUAL (from the bank), PROJECTED, or ASSUMED (a cash bill already due)
    bank_balance: int | None = None  # for an actual bank row: the bank's running balance after it

    @property
    def projected(self) -> bool:
        return self.kind != ACTUAL


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
            if row.projected and row.balances[row.account] < 0:
                out.setdefault(row.account, row)
        return out

    def lowest(self) -> dict[str, tuple[int, date]]:
        """Per account, the lowest projected balance and the day it happens."""
        out: dict[str, tuple[int, date]] = {}
        for row in self.rows:
            if row.kind == PROJECTED:
                for a in ACCOUNTS:
                    if a not in out or row.balances[a] < out[a][0]:
                        out[a] = (row.balances[a], row.date)
        return out


def _account(item: Item, txns: list[Txn]) -> str:
    """SnapShot's Account column; when blank, the account that last paid in this category; else Daily."""
    if item.account:
        return item.account
    seen = [t.account for t in txns if t.category == item.name]
    return seen[-1] if seen else "checking"


def _months(start: date, end: date) -> list[date]:
    out, m = [], start.replace(day=1)
    while m <= end:
        out.append(m)
        m = add_months(m, 1)
    return out


def _paid_in(txns: list[Txn], item: Item, first: date, last: date) -> bool:
    return any(t.category == item.name and first <= t.date <= last for t in txns)


def _bills_funding(item: Item, feed: Feed, through: date, end: date, events: list[Row]) -> None:
    """The weekly transfer from Daily to Bills, every Wednesday: a row out of Daily and a row into Bills. This
    week's is left out when the bank already shows a transfer from Daily to Bills since its Wednesday; when
    it doesn't, it's expected the day after the bank data ends."""
    week = week_start(through)
    sent = {(t.date, -t.amount) for t in feed.txns
            if t.account == "checking" and t.category == TRANSFER and t.amount < 0 and t.date >= week}
    done = any((t.date, t.amount) in sent for t in feed.txns
               if t.account == "bills" and t.category == TRANSFER and t.amount > 0 and t.date >= week)
    day = week + timedelta(days=7)
    days = [] if done or through + timedelta(days=1) >= day else [through + timedelta(days=1)]
    while day <= end:
        days.append(day)
        day += timedelta(days=7)
    for day in days:
        if day <= end:
            events.append(Row(day, "checking", "Bills funding (to Bills)", TRANSFER, -item.amount, {}))
            events.append(Row(day, "bills", "Bills funding (from Daily)", TRANSFER, item.amount, {}))


def build(items: list[Item], feed: Feed, end: date | None = None, rows: list[exceptions.Entry] = (),
          start: date | None = None) -> Timeline:
    through = feed.through
    if through is None:
        raise ValueError("no bank data")
    start = start or through.replace(day=1)
    end = end or month_end(add_months(through, 1))
    tl = Timeline(start, through, end)
    applied = exceptions.apply(list(rows), feed.txns)

    # Balances at the start: before each bank account's first transaction from then on. The bank data can't
    # show the cash on hand, so Cash starts at what covered the cash bills already due (at least $0).
    balances = {a: feed.balances.get(a, (0, through))[0] for a in BANK} | {CASH: 0}
    actual = [t for t in feed.txns if start <= t.date <= through]
    for a in BANK:
        first = next((t for t in actual if t.account == a), None)
        if first and first.balance is not None:
            balances[a] = first.balance - first.amount

    events: list[Row] = []
    for t in actual:
        status = applied.status_of(t)
        events.append(Row(t.date, t.account, t.description + (f" ({status})" if status else ""), t.category or "",
                          t.amount, {}, ACTUAL, t.balance))
        if t.category == ATM:  # an ATM withdrawal moves money into Cash; a deposit moves it out
            events.append(Row(t.date, CASH, "ATM " + ("withdrawal" if t.amount < 0 else "deposit"), ATM,
                              -t.amount, {}, ACTUAL))

    def add(day: date, item: Item, amount: int, description: str = "", kind: str = PROJECTED):
        events.append(Row(day, _account(item, feed.txns), description or item.name, item.name, amount, {}, kind))

    for item in items:
        if item.section == "transfer" and not item.cancelled:
            _bills_funding(item, feed, through, end, events)
            continue
        if item.cancelled or not item.amount:
            if not item.cancelled and item.section == "expense" and item.kind == "spending":
                tl.notes.append(f"{item.name}: no amount set on SnapShot, so nothing is projected for it.")
            continue
        sign = 1 if item.section == "income" else -1
        if item.weekly and item.day is not None:  # weekly on a weekday (Instacart: Wednesday)
            day = through + timedelta(days=(item.day - through.weekday()) % 7 or 7)
            while day <= end:
                add(day, item, sign * item.amount, f"{item.name} (baseline)" if sign > 0 else "")
                day += timedelta(days=7)
        elif item.kind == "spending" and item.section == "income":
            tl.notes.append(f"{item.name}: income with no Day of Period, so nothing is projected for it.")
        elif item.kind == "spending":
            # Each budget week's target on its Wednesday; this week only what's left of it.
            weekly = item.amount if item.weekly else item.amount // 4
            week = week_start(through)
            spent = -sum(t.amount for t in feed.txns if t.category == item.name and week <= t.date <= through)
            left = weekly - spent
            if left > 0 and through + timedelta(days=1) <= week + timedelta(days=6):
                add(through + timedelta(days=1), item, -left, f"{item.name} (rest of this week)")
            day = week + timedelta(days=7)
            while day <= end:
                add(day, item, -weekly, f"{item.name} (week)")
                day += timedelta(days=7)
        elif item.kind == "annual":
            if item.day:
                for year in range(start.year, end.year + 1):
                    due = due_date(year, *item.day)
                    if through < due <= end and not _paid_in(feed.txns, item, due.replace(day=1), month_end(due)):
                        add(due, item, sign * item.amount)
        else:  # monthly, on its day
            for m in _months(start, end):
                due = due_date(m.year, m.month, item.day)
                if not start <= due <= end:
                    continue
                if _paid_in(feed.txns, item, m, month_end(m)):
                    continue  # paid (or disputed) this month already
                if due > through:
                    add(due, item, sign * item.amount)
                elif _account(item, feed.txns) == CASH:
                    add(due, item, sign * item.amount, kind=ASSUMED)  # rent: not in the bank, assume it was paid

    # Same day: actual before projected, money in before money out (the bank's own order kept for actual rows).
    events.sort(key=lambda r: (r.date, r.kind != ACTUAL, 0 if r.kind == ACTUAL else -r.amount))
    running, lowest = 0, 0
    for row in events:
        if row.account == CASH and row.date <= through:
            running += row.amount
            lowest = min(lowest, running)
    balances[CASH] = -lowest  # never below $0 up to the end of the bank data
    for row in events:
        balances[row.account] = (row.bank_balance if row.bank_balance is not None
                                 else balances[row.account] + row.amount)
        row.balances = dict(balances)
        tl.rows.append(row)
    return tl


def as_text(tl: Timeline) -> str:
    out = [f"Bank data through {tl.through:%-m/%-d}; projected to {tl.end:%-m/%-d}."]
    negative = tl.first_negative()
    for a, (low, day) in tl.lowest().items():
        line = f"• {ACCOUNT_LABELS[a]}: lowest {money(low)} on {day:%-m/%-d}"
        if a in negative:
            row = negative[a]
            how = (f"take out at least {money(-low)} in cash" if a == CASH
                   else f"move at least {money(-low)} into {ACCOUNT_LABELS[a]}")
            line += (f". Goes negative on {row.date:%-m/%-d} ({row.description} {money(row.amount)}): {how} "
                     f"before then to stay above zero to {tl.end:%-m/%-d}")
        out.append(line)
    out += [f"• {n}" for n in tl.notes]
    return "\n".join(out)


def as_rows(tl: Timeline, rows: list[Row] | None = None) -> list[list]:
    labels = [ACCOUNT_LABELS[a] for a in ACCOUNTS]
    out = [["Date", "Account", "Description", "Category", "Amount", *labels, "Total", "Kind"]]
    for r in tl.rows if rows is None else rows:
        out.append([f"{r.date:%-m/%-d/%Y}", ACCOUNT_LABELS[r.account], r.description, r.category, r.amount / 100,
                    *[r.balances[a] / 100 for a in ACCOUNTS], sum(r.balances.values()) / 100, r.kind])
    return out

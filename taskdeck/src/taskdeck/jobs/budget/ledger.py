"""The TimeLine tab: every real transaction, from the bank exports, the Cash tab and rows typed on TimeLine itself
(pending at the bank, or not entered anywhere else). See the design doc.

The same transaction can come from more than one place. The more official one replaces the other: bank export
(with the cash side of each ATM withdrawal), then the Cash tab, then a TimeLine row. They match when they're in
the same account for the same amount, and the replacing one is dated the same day or later (a purchase posts a
day or two after it's made).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .data import ATM, CASH, NICKNAMES, TIMELINE, Txn, add_months, sheet_date
from .snapshot import cents

WINDOW_MONTHS = 3  # TimeLine starts on the 1st of the month three months back
COLUMNS = ["Date", "Account", "Description", "Category", "Amount"]  # TimeLine's first columns


def window_start(today: date) -> date:
    return add_months(today, -WINDOW_MONTHS)


def parse(values: list[list[Any]], line_names: set[str] = frozenset()) -> list[Txn]:
    """TimeLine's rows (under the header row whose first cell is "Date"): Date, Account, Description, Category,
    Amount. Without a Category, a Description that is a SnapShot line's name counts as that line."""
    h = next((i for i, row in enumerate(values) if row and str(row[0]).strip().lower() == "date"), None)
    if h is None:  # header row lost: assume the usual columns rather than lose the rows typed there
        values, h = [COLUMNS] + list(values), 0
    header = [str(c).strip().lower() for c in values[h]]
    names = {n.lower(): n for n in line_names}

    def col(row, name):
        i = header.index(name) if name in header else -1
        return str(row[i]).strip() if 0 <= i < len(row) else ""

    out = []
    for row in values[h + 1:]:
        day, amount, account = sheet_date(col(row, "date")), cents(col(row, "amount")), NICKNAMES.get(
            col(row, "account").lower())
        if day is None or not amount or account is None:
            continue
        description = col(row, "description")
        category = col(row, "category") or names.get(description.lower())
        out.append(Txn(account, day, description, amount, None, category or None, TIMELINE))
    return out


def atm_cash_sides(bank: list[Txn]) -> list[Txn]:
    """The cash side of each ATM withdrawal (cash in) and deposit (cash out)."""
    return [Txn(CASH, t.date, "ATM " + ("withdrawal" if t.amount < 0 else "deposit"), -t.amount, None, ATM)
            for t in bank if t.category == ATM and t.account != CASH]


def _unmatched(entries: list[Txn], official: list[Txn]) -> list[Txn]:
    """The entries no official transaction covers; each official one covers at most one entry, the earliest
    dated one it can (same account and amount, dated the same day or later)."""
    used: set[int] = set()
    out = []
    for e in sorted(entries, key=lambda t: t.date):
        match = min((o for o in official if id(o) not in used and o.account == e.account and o.amount == e.amount
                     and o.date >= e.date), key=lambda o: o.date, default=None)
        if match is None:
            out.append(e)
        else:
            used.add(id(match))
    return out


def merge(bank: list[Txn], cash_tab: list[Txn], typed: list[Txn], start: date) -> list[Txn]:
    """What to add to the bank transactions: the Cash tab entries the bank doesn't cover, and the TimeLine rows
    (from ``start`` on; older ones have scrolled off) that neither the bank nor the Cash tab covers."""
    official = bank + atm_cash_sides(bank)
    cash = _unmatched(cash_tab, official)
    return cash + _unmatched([t for t in typed if t.date >= start], official + cash_tab)

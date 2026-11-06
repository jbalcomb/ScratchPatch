"""The Cash tab: cash coming in and going out that the bank can't see, entered by hand (see the design doc).

Columns: Date, Description, Amount (+ in, - out), Line (a SnapShot line it counts against, e.g. Rent or
Groceries; blank if none), Note. ATM withdrawals and deposits come from the bank, so they aren't entered here.
"""

from __future__ import annotations

from typing import Any

from .data import CASH, Txn, sheet_date
from .snapshot import cents


def parse(values: list[list[Any]]) -> list[Txn]:
    """The tab's rows as transactions of the Cash account; rows without a date or an amount are skipped."""
    if not values:
        return []
    header = [str(c).strip().lower() for c in values[0]]

    def col(row, name):
        i = header.index(name) if name in header else -1
        return str(row[i]).strip() if 0 <= i < len(row) else ""

    out = []
    for row in values[1:]:
        day, amount = sheet_date(col(row, "date")), cents(col(row, "amount"))
        if day is None or not amount:
            continue
        out.append(Txn(CASH, day, col(row, "description") or "Cash", amount, None, col(row, "line") or None))
    return out

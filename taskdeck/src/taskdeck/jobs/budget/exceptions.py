"""The Exceptions tab: charges set aside to look into later, and disputed ones (see the design doc).

Columns: Match (text in the bank description), Date and Amount (optional: pick out one charge), Status
(Set aside or Disputed), Note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .data import Txn, money, sheet_date
from .snapshot import cents

SET_ASIDE, DISPUTED = "set aside", "disputed"
CREDIT_WINDOW = timedelta(days=60)  # a dispute's temporary credit comes within days; allow for slow ones


@dataclass(frozen=True)
class Entry:
    match: str
    status: str               # SET_ASIDE or DISPUTED
    day: date | None = None
    amount: int | None = None  # cents, positive
    note: str = ""

    def applies(self, t: Txn) -> bool:
        return (self.match.upper() in t.description.upper() and t.amount < 0
                and (self.day is None or t.date == self.day)
                and (self.amount is None or -t.amount == self.amount))


def parse(values: list[list[Any]], tab: str = "Exceptions") -> list[Entry]:
    """Rows under the header row (Match, Date, Amount, Status, Note); rows without a Match or a known
    Status are skipped."""
    if not values:
        return []
    header = [str(c).strip().lower() for c in values[0]]

    def col(row, name):
        i = header.index(name) if name in header else -1
        return str(row[i]).strip() if 0 <= i < len(row) else ""

    out = []
    for row in values[1:]:
        match, status = col(row, "match"), col(row, "status").lower()
        if not match or status not in (SET_ASIDE, DISPUTED):
            continue
        amount = cents(col(row, "amount"))
        out.append(Entry(match, status, sheet_date(col(row, "date")), abs(amount) if amount else None,
                              col(row, "note")))
    return out


@dataclass
class Applied:
    """Which transactions the Exceptions tab claims, and how."""

    set_aside: list[tuple[Txn, Entry]] = field(default_factory=list)
    disputed: list[tuple[Txn, Entry, Txn | None]] = field(default_factory=list)  # charge, row, credit
    claimed: set[int] = field(default_factory=set)  # id() of every charge and credit above

    def status_of(self, t: Txn) -> str | None:
        for charge, _ in self.set_aside:
            if charge is t:
                return SET_ASIDE
        for charge, _, credit in self.disputed:
            if charge is t or credit is t:
                return DISPUTED
        return None


def apply(rows: list[Entry], txns: list[Txn]) -> Applied:
    """Match each charge to the first row that applies; a disputed charge is paired with a later credit for
    the same amount in the same account (the bank's Temporary Credit Adjustment)."""
    out = Applied()
    for t in txns:
        row = next((x for x in rows if x.applies(t)), None)
        if row is None:
            continue
        out.claimed.add(id(t))
        if row.status == SET_ASIDE:
            out.set_aside.append((t, row))
            continue
        credit = next((c for c in txns if c.account == t.account and c.amount == -t.amount and id(c) not in out.claimed
                       and t.date <= c.date <= t.date + CREDIT_WINDOW), None)
        if credit is not None:
            out.claimed.add(id(credit))
        out.disputed.append((t, row, credit))
    return out


def describe_dispute(charge: Txn, credit: Txn | None) -> str:
    if credit is None:
        return f"disputed {money(-charge.amount)}, credit expected"
    return f"disputed {money(-charge.amount)}, temporary credit received {credit.date:%-m/%-d}"

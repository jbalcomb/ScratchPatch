"""The budget list: the SnapShot tab. An Income section, then an Expense section, each starting with a
header row whose first cell is "Income" or "Expense" (see the BudgetManagerGoogleSheetDesign doc).

A line's name is also its category in the budget service's rules.txt: that is how a bank transaction is
tied to the line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from taskdeck.core import StepError

from .data import NICKNAMES

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
FUNDING = "bills funding"  # the row at the top: the weekly transfer from Daily to Bills, every Wednesday


@dataclass(frozen=True)
class Item:
    name: str
    section: str               # "income", "expense", or "transfer" (Bills funding)
    account: str | None        # the bank account it's paid from or into (checking/bills/savings/cash), if set
    periodicity: str           # "monthly", "weekly", "annually" (or "" when blank)
    day: Any = None            # monthly: day of the month; weekly: weekday 0-6 (Mon=0); annually: (month, day)
    amount: int | None = None  # cents per period: Monthly, Weekly or Annually column, by periodicity
    cancelled: bool = False    # Status "Cancelled": kept one more month, not projected

    @property
    def kind(self) -> str:
        """annual: once a year (on its month/day). bill: fixed, on a known day (a day of the month, or a weekday).
        spending: no posting day ("-"), compared against its amount as a target."""
        if self.periodicity == "annually":
            return "annual"
        return "spending" if self.day is None else "bill"

    @property
    def weekly(self) -> bool:
        return self.periodicity == "weekly"


def cents(value: Any) -> int | None:
    """'$1,156.97', '-$450.00', 71.15 -> cents; blank or not a number -> None."""
    if isinstance(value, (int, float)):
        return round(value * 100)
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return round(float(text) * 100)
    except ValueError:
        return None


def _day(periodicity: str, text: str) -> Any:
    text = text.strip().lower()
    if periodicity == "weekly":
        return WEEKDAYS.get(text[:3])
    if periodicity.startswith("annual"):
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
        return (int(m[1]), int(m[2])) if m else None
    return int(text) if re.fullmatch(r"\d{1,2}", text) else None  # "-" or blank: no posting day


def parse(values: list[list[Any]], tab: str = "SnapShot", struck: set[int] | None = None) -> list[Item]:
    """The lines of the Income and Expense sections. Columns are found by header name in each section. A line is
    cancelled when its Status says Cancelled; without a Status column, when its name is struck through
    (``struck``: those row numbers, 0-based)."""
    struck = struck or set()
    items: list[Item] = []
    section, header = None, []
    first_header = next(([str(c).strip().lower() for c in row] for row in values
                         if row and str(row[0]).strip().lower() in ("income", "expense")), [])
    for r, row in enumerate(values):
        first = str(row[0]).strip() if row else ""
        if first.lower() in ("income", "expense"):
            section, header = first.lower(), [str(c).strip().lower() for c in row]
            continue
        if first.lower() == FUNDING:  # above the sections: its Weekly cell is in the sections' Weekly column
            i = first_header.index("weekly") if "weekly" in first_header else -1
            amount = cents(row[i]) if 0 <= i < len(row) else None
            if amount:
                items.append(Item(first, "transfer", "bills", "weekly", WEEKDAYS["wed"], amount, r in struck))
            continue
        if not section or not first:
            continue

        def col(name: str) -> str:
            i = header.index(name) if name in header else -1
            return str(row[i]) if 0 <= i < len(row) else ""

        periodicity = col("periodicity").strip().lower()
        if periodicity.startswith("annual") or periodicity.startswith("year"):
            periodicity, amount = "annually", cents(col("annually"))
        elif periodicity.startswith("week"):
            periodicity, amount = "weekly", cents(col("weekly"))
        else:
            periodicity, amount = ("monthly" if periodicity.startswith("month") else ""), cents(col("monthly"))
        account = col("account").strip()
        cancelled = col("status").strip().lower() == "cancelled" if "status" in header else r in struck
        items.append(Item(first, section, NICKNAMES.get(account.lower()) if account else None, periodicity,
                          _day(periodicity, col("day of period")), amount, cancelled))
    if not any(i.section == "expense" for i in items):
        raise StepError(f"No expense lines in the {tab} tab: it needs a header row starting with “Expense”.")
    return items

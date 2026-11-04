"""The budget list: the SnapShot tab, one row per expense.

Expense names are also the category names in the budget service's rules.txt; that is how a bank
transaction is tied to a budget line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from taskdeck.core import StepError


@dataclass(frozen=True)
class Item:
    name: str
    periodicity: str      # "Monthly", "Annually", ... or "" (spending with a monthly target, e.g. Groceries)
    day: int | None       # day of the month it's due
    amount: int | None    # cents: the Monthly column (the monthly target, for spending items)

    @property
    def kind(self) -> str:
        """bill: a fixed charge every month. annual: once a year. spending: varies (groceries, fuel)."""
        p = self.periodicity.lower()
        if p.startswith("annual") or p.startswith("year"):
            return "annual"
        if p.startswith("month") and self.amount:
            return "bill"
        return "spending"


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


def parse(values: list[list[Any]], tab: str = "SnapShot") -> list[Item]:
    """The items under the header row whose first cell is 'Expense'; columns are found by header name."""
    for h, row in enumerate(values):
        if row and str(row[0]).strip().lower() == "expense":
            break
    else:
        raise StepError(f"No header row starting with “Expense” in the {tab} tab.")
    header = [str(c).strip().lower() for c in values[h]]

    def col(row: list[Any], name: str) -> Any:
        i = header.index(name) if name in header else -1
        return row[i] if 0 <= i < len(row) else ""

    items = []
    for row in values[h + 1:]:
        name = str(row[0]).strip() if row else ""
        if not name:
            continue
        day = str(col(row, "day of period")).strip()
        items.append(Item(name, str(col(row, "periodicity")).strip(),
                          int(day) if re.fullmatch(r"\d{1,2}", day) else None, cents(col(row, "monthly"))))
    if not items:
        raise StepError(f"The {tab} tab has no expenses under its header row.")
    return items

"""Bank transactions from the budget service on the AI Workstation (GET /api/transactions)."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import date
from typing import Any

from taskdeck.core import StepError
from taskdeck.shared import tailscale
from taskdeck.shared.http import HttpError, get_json

ACCOUNT_LABELS = {"checking": "Daily", "bills": "Bills", "savings": "Savings"}
TRANSFER = "Transfer"  # money moved between the three accounts: never spending


@dataclass(frozen=True)
class Txn:
    account: str
    date: date
    description: str
    amount: int            # cents; negative = money out
    balance: int | None    # the bank's running balance after it
    category: str | None

    @property
    def is_income(self) -> bool:
        return (self.category or "").startswith("Income")


@dataclass(frozen=True)
class Feed:
    balances: dict[str, tuple[int, date]]   # account -> (latest balance, as of)
    txns: list[Txn]                          # oldest first

    @property
    def through(self) -> date | None:
        """The latest day there is bank data for."""
        return max((d for _, d in self.balances.values()), default=None)


def parse_feed(data: dict[str, Any]) -> Feed:
    return Feed(
        {a: (v["balance"], date.fromisoformat(v["as_of"])) for a, v in data.get("accounts", {}).items()},
        [Txn(t["account"], date.fromisoformat(t["date"]), t["description"], t["amount"], t.get("balance"),
             t.get("category")) for t in data.get("transactions", [])],
    )


def fetch(base_url: str, since: date, timeout: float = 30) -> Feed:
    base = (base_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise StepError("Set the budget service address in this job's settings (it starts with https://).")
    url = f"{base}/api/transactions?" + urllib.parse.urlencode({"since": since.isoformat()})
    try:
        return parse_feed(get_json(url, timeout=timeout))
    except HttpError as exc:
        why = tailscale.why_unreachable(base)
        raise StepError(f"Couldn't get the bank transactions from the budget service: {why or exc}") from None


def merchant(description: str) -> str:
    """The leading part of a bank description, without dates and reference numbers:
    'WAL-MART #3608 02/14 PURCHASE MADISON OH' -> 'WAL-MART'."""
    head = re.split(r"\s\d\d/\d\d\s|\d{4,}|#| ID:| DES:", description)[0].strip(" -*")
    return head or description.split(" ")[0]


def money(c: int | None, sign: bool = False) -> str:
    if c is None:
        return ""
    text = f"${abs(c) / 100:,.2f}"
    return ("-" if c < 0 else "+" if sign and c > 0 else "") + text


def add_months(d: date, n: int) -> date:
    """The first day of the month n months from d's month."""
    m = d.year * 12 + d.month - 1 + n
    return date(m // 12, m % 12 + 1, 1)


def month_end(d: date) -> date:
    return date.fromordinal(add_months(d, 1).toordinal() - 1)


def due_date(year: int, month: int, day: int) -> date:
    """Day ``day`` of the month, or its last day when the month is shorter."""
    first = date(year, month, 1)
    return first.replace(day=min(day, month_end(first).day))

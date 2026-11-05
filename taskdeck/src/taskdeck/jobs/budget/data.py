"""Bank transactions from the budget service on the AI Workstation (GET /api/transactions)."""

from __future__ import annotations

import calendar
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date
from typing import Any

from taskdeck.core import StepError
from taskdeck.shared import tailscale
from taskdeck.shared.http import HttpError, get_json

# The budget service files exports as checking / bills / savings; everywhere else they go by their nicknames
# (the budget sheet's Accounts tab). Cash isn't a bank account: ATM withdrawals and deposits move money in
# and out of it.
ACCOUNT_LABELS = {"checking": "Daily", "bills": "Bills", "savings": "Save", "cash": "Cash"}
NICKNAMES = {label.lower(): account for account, label in ACCOUNT_LABELS.items()}
CASH = "cash"
TRANSFER = "Transfer"  # money moved between the bank accounts: never spending
ATM = "Cash"           # rules.txt category of ATM withdrawals and deposits: a move to or from Cash


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
        """Income by category name ("Income: Paycheck"); lines in SnapShot's Income section count too, see
        :func:`income_names`."""
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


UPDATE_SERVICE = ("cd ~/develai/ScratchPatch && git checkout main && git pull && cd budget && "
                  "docker compose up -d --build")
SERVICE = "budget"  # its Tailscale Service name: https://budget.<tailnet>.ts.net
_answered: dict[str, str] = {}  # address as typed in settings -> the address that answered


def tidy_url(typed: str) -> str:
    """What was typed, as a base address: https:// added when missing, any pasted path dropped."""
    text = (typed or "").strip()
    if not text:
        return ""
    parts = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
    return f"{parts.scheme}://{parts.netloc}"


def candidates(typed: str) -> list[str]:
    """Addresses to try for what was typed, in order. The service only answers through Tailscale:
    as the Tailscale Service https://budget.<tailnet>.ts.net, or Tailscale Serve on the workstation's own
    name (https://<machine>.<tailnet>.ts.net, 443 or 8443). So a missing https://, a pasted path, the
    workstation's IP or short name, the service's own port, or an old address are all turned into those."""
    base = tidy_url(typed)
    if not base:
        return []
    host = urllib.parse.urlsplit(base).hostname or ""
    out = [base]
    names = [n for n in (tailscale.full_name(host), host) if n]
    tailnets = [n.split(".", 1)[1] for n in names if n.endswith(".ts.net") and n.count(".") >= 3]
    names[:0] = [f"{SERVICE}.{t}" for t in tailnets]  # the Tailscale Service first
    for name in dict.fromkeys(names):
        out += [f"https://{name}", f"https://{name}:8443"]
    return list(dict.fromkeys(out))


def why_address(typed: str, used: str) -> str:
    """One sentence on why the address that answers differs from the one typed (port, name)."""
    before, after = urllib.parse.urlsplit(tidy_url(typed)), urllib.parse.urlsplit(used)
    if after.scheme == "https" and before.port != after.port and after.port in (None, 8443):
        door = "the standard HTTPS port, 443, which addresses leave out" if after.port is None else "port 8443"
        old = before.port or (443 if before.scheme == "https" else 80)
        return f"The phone reaches the budget service through Tailscale Serve, on {door}; port {old} doesn't answer."
    if before.hostname != after.hostname and (after.hostname or "").startswith(SERVICE + "."):
        return f"The budget service is now the Tailscale Service “{SERVICE}”, with its own address."
    if before.hostname != after.hostname:
        return "Tailscale Serve's HTTPS certificate is only valid for the workstation's full .ts.net name."
    return ""


def service_url(typed: str, timeout: float = 5) -> str:
    """The first candidate address where the budget service's feed answers (remembered per typed address).
    Raises StepError saying what was tried, or what to run when the workstation has an older service."""
    if typed in _answered:
        return _answered[typed]
    tried, outdated = [], None
    for base in candidates(typed):
        try:
            data = get_json(f"{base}/api/transactions?since={date.today().isoformat()}", timeout=timeout)
        except HttpError as exc:
            if exc.status == 404:
                outdated = outdated or base
            tried.append(base)
            continue
        if isinstance(data, dict) and "accounts" in data:
            _answered[typed] = base
            return base
        tried.append(base)
    if not tried:
        raise StepError("Set the budget service address in this job's settings (https://….ts.net).")
    if outdated:
        raise StepError(f"{outdated} is an older budget service, without the transactions feed. Update it on the "
                        f"AI Workstation: {UPDATE_SERVICE}")
    why = tailscale.why_unreachable(tried[0])
    raise StepError(f"The budget service didn't answer at {', '.join(tried)}"
                    + (f": {why}." if why else ". Check `tailscale serve status` on the AI Workstation."))


def fetch(typed_url: str, since: date, timeout: float = 30) -> Feed:
    base = service_url(typed_url)
    url = f"{base}/api/transactions?" + urllib.parse.urlencode({"since": since.isoformat()})
    try:
        return parse_feed(get_json(url, timeout=timeout))
    except HttpError as exc:
        _answered.pop(typed_url, None)
        raise StepError(f"Couldn't get the bank transactions from the budget service: {exc}") from None


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
    """The last day of d's month."""
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def week_start(d: date) -> date:
    """The Wednesday a budget week starts on (weeks run Wednesday to Tuesday: payday to the day before)."""
    return date.fromordinal(d.toordinal() - (d.weekday() - 2) % 7)


def wednesdays(month: date) -> list[date]:
    """The Wednesdays in d's month: the budget weeks that belong to it."""
    first = month.replace(day=1)
    day = date.fromordinal(first.toordinal() + (2 - first.weekday()) % 7)
    out = []
    while day.month == first.month:
        out.append(day)
        day = date.fromordinal(day.toordinal() + 7)
    return out


def due_date(year: int, month: int, day: int) -> date:
    """Day ``day`` of the month, or its last day when the month is shorter."""
    first = date(year, month, 1)
    return first.replace(day=min(day, month_end(first).day))

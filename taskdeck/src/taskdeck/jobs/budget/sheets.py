"""The budget sheet: read the SnapShot and Exceptions tabs; each job writes only its own tab, which it creates."""

from __future__ import annotations

import time
from typing import Any

from taskdeck.core import StepError
from taskdeck.shared.google import (
    GoogleError, ServiceAccountCredentials, ServiceAccountKey, SheetsClient, a1_range, key_problems,
)

from . import cashbook, exceptions, ledger, snapshot


def client(settings: dict[str, Any], timeout: float = 30) -> SheetsClient:
    key_text = (settings.get("service_account_json") or "").strip()
    if not key_text:
        raise StepError("Add the Google service account key in this job's settings (the tune icon at the top).")
    try:
        key = ServiceAccountKey.from_json(key_text)
        key.check()
    except GoogleError as exc:
        raise StepError(str(exc)) from None
    return SheetsClient(ServiceAccountCredentials(key), (settings.get("sheet_id") or "").strip(), timeout=timeout)


setup_problems = key_problems


def read_items(settings: dict[str, Any]) -> list[snapshot.Item]:
    """The SnapShot lines; a line whose name is struck through counts as cancelled."""
    tab = (settings.get("snapshot_tab") or "SnapShot").strip()
    try:
        grid = client(settings).get_grid(a1_range(tab))
    except GoogleError as exc:
        raise StepError(str(exc)) from None
    values = [[text for text, _ in row] for row in grid]
    struck = {r for r, row in enumerate(grid) if row and row[0][1]}
    return snapshot.parse(values, tab, struck)


def _optional_tab(settings: dict[str, Any], tab: str) -> list[list[Any]]:
    """A tab's values; none when the sheet has no such tab."""
    try:
        c = client(settings)
        if not any(t.get("title") == tab for t in c.tabs()):
            return []
        return c.get_values(a1_range(tab))
    except GoogleError as exc:
        raise StepError(str(exc)) from None


def read_exceptions(settings: dict[str, Any]) -> list[exceptions.Entry]:
    tab = (settings.get("exceptions_tab") or "Exceptions").strip()
    return exceptions.parse(_optional_tab(settings, tab), tab)


def read_cash(settings: dict[str, Any]):
    """The Cash tab's rows, as transactions of the Cash account."""
    return cashbook.parse(_optional_tab(settings, (settings.get("cash_tab") or "Cash").strip()))


def read_timeline(settings: dict[str, Any], line_names: set[str]):
    """The TimeLine tab's rows, as transactions (the code's own rows from the last run among them)."""
    return ledger.parse(_optional_tab(settings, (settings.get("timeline_tab") or "TimeLine").strip()), line_names)


def write_tab(settings: dict[str, Any], title: str, note: str | None, rows: list[list]) -> str:
    """Replace everything in the tab ``title`` (created if missing) with a note line (if any) and ``rows``."""
    width = max(len(r) for r in rows)
    body = ([[note] + [""] * (width - 1)] if note else []) + [list(r) + [""] * (width - len(r)) for r in rows]
    last_col = chr(ord("A") + width - 1)
    try:
        c = client(settings)
        c.add_tab(title)
        c.clear(a1_range(title))
        c.write_rows(a1_range(title, f"A1:{last_col}{len(body)}"), body)
    except GoogleError as exc:
        raise StepError(str(exc)) from None
    return f"Wrote {len(rows) - 1} rows to the “{title}” tab"


def note(through) -> str:
    return (f"Written by TaskDeck {time.strftime('%-m/%-d/%Y %H:%M')} from bank data through {through:%-m/%-d/%Y}. "
            "This tab is replaced on every run: edit the SnapShot tab or rules.txt instead.")

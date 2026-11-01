"""The budget sheet: read the SnapShot tab; each job writes only its own tab, which it creates."""

from __future__ import annotations

import time
from typing import Any

from taskdeck.core import StepError
from taskdeck.shared.google import (
    GoogleError, ServiceAccountCredentials, ServiceAccountKey, SheetsClient, a1_range, key_problems,
)

from . import snapshot


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
    tab = (settings.get("snapshot_tab") or "SnapShot").strip()
    try:
        values = client(settings).get_values(a1_range(tab))
    except GoogleError as exc:
        raise StepError(str(exc)) from None
    return snapshot.parse(values, tab)


def write_tab(settings: dict[str, Any], title: str, note: str, rows: list[list]) -> str:
    """Replace everything in the tab ``title`` (created if missing) with a note line and ``rows``."""
    width = max(len(r) for r in rows)
    body = [[note] + [""] * (width - 1)] + [list(r) + [""] * (width - len(r)) for r in rows]
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

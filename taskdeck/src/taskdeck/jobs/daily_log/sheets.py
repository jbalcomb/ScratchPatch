"""Where the row gets written.

The job talks to a :class:`SheetBackend`, so the way it reaches Google can change
without touching the job:

* ``service_account`` (now): the service-account key is stored on the phone and
  the phone calls the Sheets API directly.
* ``oauth`` (planned): sign in with the user's Google account on the phone.

The AI Workstation only parses logs; it never writes to the sheet.
"""

from __future__ import annotations

from typing import Any, Protocol

from taskdeck.core import NeedsConfirmation, Problem, StepError
from taskdeck.shared.google import (
    GoogleError, ServiceAccountCredentials, ServiceAccountKey, SheetsClient, a1_range,
)

from .reconcile import differences, find_date, last_filled_row, parse_date
from .record import build_row, column_letter, header_problems

RECONCILE_TITLE = "Reconcile Daily Log sheet"


class SheetBackend(Protocol):
    def rows(self) -> list[list[Any]]:
        """The whole tab as stored (row 1 = headers); trailing empty rows are left off."""

    def write(self, first_row: int, rows: list[list[Any]]) -> None:
        """Overwrite rows starting at ``first_row`` (1-based); "" clears a cell."""

    def backup(self, title: str) -> None:
        """Copy the tab to a new tab called ``title``."""


class ServiceAccountBackend:
    def __init__(self, key_json: str, sheet_id: str, tab: str, timeout: float = 30):
        key = ServiceAccountKey.from_json(key_json)
        key.check()
        self.client = SheetsClient(ServiceAccountCredentials(key), sheet_id, timeout=timeout)
        self.tab = tab

    def rows(self) -> list[list[Any]]:
        return self.client.get_values(a1_range(self.tab), raw=True)

    def write(self, first_row: int, rows: list[list[Any]]) -> None:
        width = max(len(r) for r in rows)
        last = first_row + len(rows) - 1
        self.client.write_rows(a1_range(self.tab, f"A{first_row}:{column_letter(width)}{last}"),
                               [list(r) + [""] * (width - len(r)) for r in rows])

    def backup(self, title: str) -> None:
        self.client.duplicate_tab(self.tab, title)


def make_backend(settings: dict[str, Any], timeout: float = 30) -> SheetBackend:
    if not (settings.get("service_account_json") or "").strip():
        raise StepError("Add the Google service account key in this job's settings (the tune icon at the top).")
    if not (settings.get("sheet_id") or "").strip():
        raise StepError("Set the Sheet ID in this job's settings.")
    try:
        return ServiceAccountBackend(settings["service_account_json"], settings["sheet_id"].strip(),
                                     (settings.get("sheet_tab") or "Sheet1").strip(), timeout=timeout)
    except GoogleError as exc:
        raise StepError(str(exc)) from None


def setup_problems(settings: dict[str, Any]) -> list[Problem]:
    """Offline check that the service-account key can be used."""
    key_text = (settings.get("service_account_json") or "").strip()
    if key_text:
        try:
            ServiceAccountKey.from_json(key_text).check()
        except GoogleError as exc:
            return [Problem(str(exc))]
    return []


def read_checked(backend: SheetBackend) -> list[list[Any]]:
    """The tab's rows, after checking the headers. Raises StepError if they don't match."""
    values = backend.rows()
    problems = header_problems(values[0] if values else [])
    if problems:
        raise StepError("Sheet headers don’t match: " + "; ".join(problems))
    return values


def save_record(record: dict, backend: SheetBackend) -> str:
    """Write the day's row below the last filled row, never a second row for the same date.

    If the date is already there with the same values nothing is written; with different
    values the user is asked before that row is replaced. Writes nothing if headers don't match.
    """
    try:
        values = read_checked(backend)
        day = parse_date(record.get("date"))
        if day is None:
            raise StepError("The row has no date, so it can't be checked against the sheet. Parse the log again.")
        row = build_row(record)
        same = find_date(values, day)
        replace = record.get("_replace_row")
        if replace:
            if same != [replace]:
                raise StepError(f"Row {replace} changed since you were asked. Tap Save to sheet again.")
            backend.write(replace, [row])
            return f"Replaced row {replace} ({record['date']})"
        if len(same) > 1:
            raise StepError(f"The sheet already has {len(same)} rows for {record['date']} (rows "
                            f"{', '.join(map(str, same))}). Run “{RECONCILE_TITLE}” to clean them up, then save again.")
        if same:
            n = same[0]
            diff = differences(values[n - 1][:len(row)], row)
            if not diff:
                return f"Already in the sheet (row {n}); nothing changed"
            raise NeedsConfirmation(f"Row {n} already has {record['date']}, with different values: "
                                    + "; ".join(diff) + ".", f"Replace row {n}", {**record, "_replace_row": n})
        n = last_filled_row(values) + 1
        backend.write(n, [row])
        return f"Saved row for {record['date']} (row {n})"
    except GoogleError as exc:
        raise StepError(str(exc)) from None

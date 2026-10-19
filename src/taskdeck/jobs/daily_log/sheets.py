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

from taskdeck.core import StepError
from taskdeck.shared.google import (
    GoogleError, ServiceAccountCredentials, ServiceAccountKey, SheetsClient, a1_range,
)

from .record import COLUMNS, build_row, column_letter, header_problems


class SheetBackend(Protocol):
    def header_row(self) -> list[Any]: ...

    def append(self, row: list[Any]) -> None: ...


class ServiceAccountBackend:
    def __init__(self, key_json: str, sheet_id: str, tab: str, timeout: float = 30):
        key = ServiceAccountKey.from_json(key_json)
        key.check()
        self.client = SheetsClient(ServiceAccountCredentials(key), sheet_id, timeout=timeout)
        self.tab = tab

    def header_row(self) -> list[Any]:
        last = column_letter(max(c.number for c in COLUMNS))
        values = self.client.get_values(a1_range(self.tab, f"A1:{last}1"))
        return values[0] if values else []

    def append(self, row: list[Any]) -> None:
        self.client.append_row(a1_range(self.tab, "A1"), row)


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


def save_record(record: dict, backend: SheetBackend) -> None:
    """Validate the sheet's headers, then append one row. Writes nothing if headers don't match."""
    try:
        problems = header_problems(backend.header_row())
        if problems:
            raise StepError("Sheet headers don’t match: " + "; ".join(problems))
        backend.append(build_row(record))
    except GoogleError as exc:
        raise StepError(str(exc)) from None

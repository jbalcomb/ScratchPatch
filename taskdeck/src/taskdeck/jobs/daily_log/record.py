"""Normalization, calculations and the sheet column map for the daily log.

Everything here is pure (no I/O). The model only extracts raw values; all
arithmetic happens in these functions.

To add a note type: add the key to the prompt in ``extract.py``, compute it in
:func:`build_record`, and add one :class:`Column` to :data:`COLUMNS` (plus the
header in the sheet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class Column:
    number: int  # 1-based sheet column
    header: str | None  # required row-1 header; None = not checked
    field: str  # key in the record
    label: str  # shown in the preview
    optional: bool = False  # missing value is normal (not listed under "Not found")
    short: str = ""  # for compact lists on a phone screen (e.g. the reconcile "Missing:" lines)


# The single source of truth for the sheet layout: validates headers and builds the row.
COLUMNS: tuple[Column, ...] = (
    Column(1, None, "date", "Date"),
    Column(2, "Departure Time", "departure_time", "Departure time", short="Dep time"),
    Column(3, "Departure Mileage", "departure_mileage", "Departure mileage", short="Dep mi"),
    Column(4, "Online Time", "online_time", "Online time", short="Online"),
    Column(5, "Offline Time", "offline_time", "Offline time", short="Offline"),
    Column(6, "Arrival Time", "arrival_time", "Arrival time", short="Arr time"),
    Column(7, "Arrival Mileage", "arrival_mileage", "Arrival mileage", short="Arr mi"),
    Column(8, "Batch Count", "batches", "Batch count", short="Batches"),
    Column(9, "Order Count", "orders", "Order count", short="Orders"),
    Column(10, "Earnings", "earnings", "Earnings", short="Earnings"),
    Column(11, "Online Hours", "online_hours", "Online hours", short="Online hrs"),
    Column(12, "Onclock Hours", "onclock_hours", "Onclock hours", short="Onclock hrs"),
    Column(13, "Gas At Mileage", "gas_at", "Gas at mileage", optional=True, short="Gas at"),
    Column(14, "Post-Gas Mileage", "gas_after", "Post-gas mileage", optional=True, short="Gas after"),
    Column(15, "Miles Driven", "miles_driven", "Miles driven", short="Miles"),
)


def num(v: Any) -> int | float | None:
    """Parse a number, ignoring ``$`` and thousands separators. Whole numbers become ints."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(f) if f == int(f) else round(f, 2)


def norm_time(v: Any) -> str | None:
    """``"10:38"``, ``"2146"`` or ``"938"`` -> ``"HH:MM"``; anything else -> None."""
    if v is None:
        return None
    digits = re.sub(r"\D", "", str(v))
    if len(digits) in (3, 4):
        digits = digits.zfill(4)
        h, m = int(digits[:2]), int(digits[2:])
        if h < 24 and m < 60:
            return f"{h:02d}:{m:02d}"
    return None


def resolve_date(month: Any, day: Any, today: date | None = None) -> str | None:
    """The log has no year: use this year, or last year if that is more than 7 days ahead."""
    today = today or date.today()
    try:
        m, d = int(month), int(day)
    except (TypeError, ValueError):
        return None
    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, m, d)
        except ValueError:  # e.g. Feb 29 in a non-leap year: try the other year
            continue
        if candidate <= today + timedelta(days=7):
            return candidate.strftime("%m/%d/%Y")
    return None


def hours_between(start: str | None, end: str | None) -> float | None:
    """Hours from start to end, rounded to 2 decimals; a negative span crossed midnight."""
    if not start or not end:
        return None
    fmt = "%H:%M"
    h = (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds() / 3600
    if h < 0:
        h += 24
    return round(h, 2)


def miles_driven(start: float | None, end: float | None, fills: list[dict]) -> int | float | None:
    """Range burned: restart from the post-fill range after each gas stop."""
    if start is None or end is None:
        return None
    total, prev = 0, start
    for f in fills:
        total += prev - f["at"]
        prev = f["after"]
    return num(total + prev - end)


# Calculated columns and the columns they are calculated from (the gas columns also feed
# Miles Driven when there was a gas stop). Used to check and complete rows already in the sheet.
DERIVED: dict[str, tuple[str, ...]] = {
    "online_hours": ("online_time", "offline_time"),
    "onclock_hours": ("departure_time", "arrival_time"),
    "miles_driven": ("departure_mileage", "arrival_mileage"),
}


def calculate(field: str, values: dict[str, Any]) -> int | float | None:
    """A calculated column from sheet cells (``values`` by record field), or None if it can't be."""
    if field in ("online_hours", "onclock_hours"):
        start, end = DERIVED[field]
        return hours_between(norm_time(values.get(start)), norm_time(values.get(end)))
    if field == "miles_driven":
        fills = sheet_gas_fills(values.get("gas_at"), values.get("gas_after"))
        if fills is None:
            return None
        return miles_driven(num(values.get("departure_mileage")), num(values.get("arrival_mileage")), fills)
    raise KeyError(field)


def sheet_gas_fills(at: Any, after: Any) -> list[dict] | None:
    """The gas columns as written to the sheet ("32, 40" / "540, 520"); None if they don't pair up."""
    ats = [num(x) for x in str(at if at is not None else "").split(",") if x.strip()]
    afters = [num(x) for x in str(after if after is not None else "").split(",") if x.strip()]
    if len(ats) != len(afters) or None in ats or None in afters:
        return None
    return [{"at": a, "after": b} for a, b in zip(ats, afters)]


def gas_fills(raw: Any) -> list[dict]:
    fills = []
    for f in raw or []:
        if not isinstance(f, dict):
            continue
        at, after = num(f.get("at")), num(f.get("after"))
        if at is not None and after is not None:
            fills.append({"at": at, "after": after})
    return fills


def build_record(raw: dict, today: date | None = None) -> dict[str, Any]:
    """Turn the model's raw extraction into the record shown in the preview and written to the sheet."""
    fills = gas_fills(raw.get("gas_fills"))
    dep_t, on_t = norm_time(raw.get("departure_time")), norm_time(raw.get("online_time"))
    off_t, arr_t = norm_time(raw.get("offline_time")), norm_time(raw.get("arrival_time"))
    dep_m, arr_m = num(raw.get("departure_mileage")), num(raw.get("arrival_mileage"))
    return {
        "date": resolve_date(raw.get("month"), raw.get("day"), today),
        "departure_time": dep_t,
        "departure_mileage": dep_m,
        "online_time": on_t,
        "offline_time": off_t,
        "arrival_time": arr_t,
        "arrival_mileage": arr_m,
        "batches": num(raw.get("batches")),
        "orders": num(raw.get("orders")),
        "earnings": num(raw.get("earnings")),
        "online_hours": hours_between(on_t, off_t),
        "onclock_hours": hours_between(dep_t, arr_t),
        "gas_at": ", ".join(str(f["at"]) for f in fills) or None,
        "gas_after": ", ".join(str(f["after"]) for f in fills) or None,
        "miles_driven": miles_driven(dep_m, arr_m, fills),
    }


def display_value(field: str, value: Any) -> str:
    if value is None or value == "":
        return "—"
    if field == "earnings":
        return f"${float(value):,.2f}"
    return str(value)


def missing_fields(record: dict) -> list[str]:
    return [c.label for c in COLUMNS if not c.optional and record.get(c.field) in (None, "")]


def column_letter(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def header_problems(actual: list[Any]) -> list[str]:
    """Compare sheet row 1 against :data:`COLUMNS`. Empty list means the layout is correct."""
    problems = []
    for c in COLUMNS:
        if c.header is None:
            continue
        found = str(actual[c.number - 1]).strip() if c.number - 1 < len(actual) else ""
        if found != c.header:
            problems.append(f"{column_letter(c.number)}1 should be “{c.header}” but is “{found}”")
    return problems


def build_row(record: dict) -> list[Any]:
    row: list[Any] = [""] * max(c.number for c in COLUMNS)
    for c in COLUMNS:
        value = record.get(c.field)
        if value is not None:
            row[c.number - 1] = value
    return row

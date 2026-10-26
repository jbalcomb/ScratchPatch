"""Keeping the sheet tidy: one row per date, no blank rows, in date order.

Pure functions over the sheet's values (row 1 = headers). :func:`find_date` is used
when saving a new row; :func:`plan` works out what "Reconcile sheet" would change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .record import COLUMNS

WIDTH = max(c.number for c in COLUMNS)
_LABELS = {c.number - 1: (c.header or c.label) for c in COLUMNS}


def parse_date(value: Any) -> date | None:
    """A date cell as the sheet shows it: 10/15/2026, 10/15/26 or 2026-10-15."""
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", text)
    if m:
        month, day, year = int(m[1]), int(m[2]), int(m[3])
        year += 2000 if year < 100 else 0
    else:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if not m:
            return None
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    try:
        return date(year, month, day)
    except ValueError:
        return None


def format_date(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def is_sheet_date(value: Any) -> bool:
    """Shown the way the sheet's date format shows a real date (M/D/YYYY, leading zeros or not).
    Anything else (2026-10-18, 10/18/26) was most likely typed in as text."""
    return bool(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", str(value or "").strip()))


def is_blank(cells: list[Any]) -> bool:
    return all(str(v).strip() == "" for v in cells)


def _key(i: int, value: Any) -> Any:
    """What a cell means, for comparing rows: "-" is empty, 52 == "52" == 52.0, 8:37 == 08:37."""
    text = str(value if value is not None else "").strip()
    if i == 0:
        return parse_date(text) or text
    if text in ("-", "—"):
        return ""
    try:
        return round(float(text.replace(",", "").lstrip("$")), 6)
    except ValueError:
        pass
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return text.zfill(5)
    return text.lower()


def _cells(row: list[Any], width: int) -> list[Any]:
    return list(row) + [""] * (width - len(row))


def same_values(a: list[Any], b: list[Any]) -> bool:
    width = max(len(a), len(b), WIDTH)
    return all(_key(i, x) == _key(i, y) for i, (x, y) in enumerate(zip(_cells(a, width), _cells(b, width))))


def differences(old: list[Any], new: list[Any]) -> list[str]:
    """``["Earnings: 190 → 187.82", ...]`` for the cells that differ."""
    width = max(len(old), len(new), WIDTH)
    out = []
    for i, (x, y) in enumerate(zip(_cells(old, width), _cells(new, width))):
        if _key(i, x) != _key(i, y):
            out.append(f"{_LABELS.get(i, f'column {i + 1}')}: {_show(x)} → {_show(y)}")
    return out


def _show(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return text if text else "(empty)"


def filled(cells: list[Any]) -> int:
    return sum(1 for i, v in enumerate(cells) if _key(i, v) != "")


def last_filled_row(values: list[list[Any]]) -> int:
    """1-based number of the last row with anything in it (1 if only headers)."""
    for i in range(len(values) - 1, -1, -1):
        if not is_blank(values[i]):
            return i + 1
    return 1


def find_date(values: list[list[Any]], d: date) -> list[int]:
    """Row numbers (1-based) of the data rows dated ``d``."""
    return [i + 1 for i, row in enumerate(values[1:], start=1) if row and parse_date(row[0]) == d]


@dataclass
class Plan:
    rows: list[list[Any]]  # the data rows (from row 2) as they should be
    span: int  # last row number currently used; rows 2..span get rewritten
    width: int
    changes: list[str] = field(default_factory=list)  # what applying the plan would do
    remarks: list[str] = field(default_factory=list)  # things to check by hand; not changed

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def plan(values: list[list[Any]]) -> Plan:
    """Work out the tidy version of the sheet: blank rows dropped, one row per date
    (the most complete one, or the later one if equally complete), dates not in the
    sheet's M/D/YYYY form rewritten, sorted by date. Rows without a readable date are kept, at the end."""
    span = last_filled_row(values)
    width = max([WIDTH] + [len(r) for r in values[:span]])
    data = [(n, _cells(values[n - 1], width)) for n in range(2, span + 1)]
    notes: list[str] = []

    blank = [n for n, cells in data if is_blank(cells)]
    if blank:
        notes.append(f"Remove {len(blank)} blank row{'s' * (len(blank) != 1)} ({_ranges(blank)}).")

    by_date: dict[date, list[tuple[int, list[Any]]]] = {}
    undated: list[tuple[int, list[Any]]] = []
    for n, cells in data:
        if is_blank(cells):
            continue
        d = parse_date(cells[0])
        if d is None:
            undated.append((n, cells))
        else:
            by_date.setdefault(d, []).append((n, cells))

    kept: list[tuple[date, int, list[Any]]] = []
    for d, group in by_date.items():
        best_n, best = max(group, key=lambda item: (filled(item[1]), item[0]))
        # Of identical copies keep the first, which is usually the one typed in by hand.
        best_n, best = next((n, cells) for n, cells in group if same_values(cells, best))
        for n, cells in group:
            if n == best_n:
                continue
            if same_values(cells, best):
                notes.append(f"Remove row {n}: a duplicate of row {best_n} ({format_date(d)}).")
            else:
                notes.append(f"Remove row {n}: another row for {format_date(d)} that differs from row {best_n}, "
                             f"which is kept as the more complete one ({'; '.join(differences(cells, best))}).")
        if not is_sheet_date(best[0]):
            notes.append(f"Row {best_n}: write the date “{str(best[0]).strip()}” as {format_date(d)}.")
            best = [format_date(d)] + best[1:]
        kept.append((d, best_n, best))

    in_order = [n for _, n, _ in sorted(kept, key=lambda k: k[1])]
    kept.sort(key=lambda k: (k[0], k[1]))
    if [n for _, n, _ in kept] != in_order:
        notes.append("Put the rows in date order.")
    if undated and kept and notes:
        notes.append(f"Move the row{'s' * (len(undated) != 1)} without a readable date to the end.")
    remarks = [f"Row {n} has no readable date (“{str(cells[0]).strip()}”); fix it by hand." for n, cells in undated]
    required = [c.number - 1 for c in COLUMNS if not c.optional]
    remarks += [f"Row {n} ({format_date(d)}) is missing values; check it by hand."
                for d, n, cells in sorted(kept, key=lambda k: k[1]) if any(_key(i, cells[i]) == "" for i in required)]

    rows = [cells for _, _, cells in kept] + [cells for _, cells in undated]
    return Plan(rows, span, width, notes, remarks)


def _ranges(numbers: list[int]) -> str:
    """[3, 4, 5, 9] -> "rows 3–5, 9"."""
    parts, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(str(start) if start == n else f"{start}–{n}")
            start = None
    return ("row " if len(numbers) == 1 else "rows ") + ", ".join(parts)

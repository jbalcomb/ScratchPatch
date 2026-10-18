"""Pinned tiles (Windows 11 start-menu style), recent jobs and run history."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .storage import JsonStore


class Favorites:
    """An ordered list of pinned job ids."""

    KEY = "pinned"

    def __init__(self, store: JsonStore):
        self.store = store

    @property
    def ids(self) -> list[str]:
        return list(self.store.get(self.KEY, []))

    def is_pinned(self, job_id: str) -> bool:
        return job_id in self.ids

    def pin(self, job_id: str) -> None:
        if not self.is_pinned(job_id):
            self.store.set(self.KEY, self.ids + [job_id])

    def unpin(self, job_id: str) -> None:
        self.store.set(self.KEY, [i for i in self.ids if i != job_id])

    def toggle(self, job_id: str) -> bool:
        """Pin or unpin; returns the new pinned state."""
        if self.is_pinned(job_id):
            self.unpin(job_id)
            return False
        self.pin(job_id)
        return True

    def move(self, job_id: str, new_index: int) -> None:
        ids = self.ids
        if job_id not in ids:
            return
        ids.remove(job_id)
        ids.insert(max(0, min(new_index, len(ids))), job_id)
        self.store.set(self.KEY, ids)

    def shift(self, job_id: str, delta: int) -> None:
        if job_id in self.ids:
            self.move(job_id, self.ids.index(job_id) + delta)

    def prune(self, known_ids: set[str]) -> None:
        """Drop pins for jobs that are no longer installed."""
        kept = [i for i in self.ids if i in known_ids]
        if kept != self.ids:
            self.store.set(self.KEY, kept)


@dataclass
class HistoryEntry:
    job_id: str
    at: float
    ok: bool
    summary: str
    inputs: dict


class History:
    """Recent runs, newest first. Also drives the "Recent" row and palette ranking."""

    KEY = "history"
    LIMIT = 100

    def __init__(self, store: JsonStore):
        self.store = store

    def entries(self) -> list[HistoryEntry]:
        return [HistoryEntry(**e) for e in self.store.get(self.KEY, [])]

    def add(self, job_id: str, ok: bool, summary: str, inputs: dict) -> None:
        safe_inputs = {k: v for k, v in inputs.items() if isinstance(v, (str, int, float, bool)) or v is None}
        entry = HistoryEntry(job_id, time.time(), ok, summary[:200], safe_inputs)
        raw = [asdict(entry)] + self.store.get(self.KEY, [])
        self.store.set(self.KEY, raw[: self.LIMIT])

    def clear(self) -> None:
        self.store.set(self.KEY, [])

    def recent_job_ids(self, limit: int = 6) -> list[str]:
        seen: list[str] = []
        for e in self.entries():
            if e.job_id not in seen:
                seen.append(e.job_id)
            if len(seen) >= limit:
                break
        return seen

    def usage_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries():
            counts[e.job_id] = counts.get(e.job_id, 0) + 1
        return counts

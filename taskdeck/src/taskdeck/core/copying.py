"""The two kinds of jobs that copy files somewhere (e.g. to Google Drive), and what each does with a
file that is already there. See docs/JOB_KINDS.md.

* ONE_TIME_COPY: copy a fixed snapshot once, e.g. Extract Archive: an archive's contents never change.
  The destination should end up an exact, complete copy. A same-named file that matches (size and
  checksum) is already done. One that doesn't can't be a newer version: it's a broken leftover of an
  earlier, interrupted run, so it is replaced by a fresh upload (new file, new created time) and the
  leftover goes to the trash. Duplicates of the name go too. Nothing is worth keeping about leftovers
  (links, sharing, version history); being complete and correct is what matters.

* ONGOING_SYNC: keep copies of files that keep changing, e.g. a folder that's edited and synced again
  and again. The destination files live on: people link to them and share them. A different file
  there is an older version, so it is updated in place: same ID, link, sharing and version history;
  its created time stays (same document) and its modified time follows the edits. (No job is like
  this yet; this is the rule for when one is.)

A job declares which one it is with ``JobPlugin.nature``; the copying code decides with ``plan()``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable


class JobNature(Enum):
    ONE_TIME_COPY = "one-time copy"
    ONGOING_SYNC = "ongoing sync"


@dataclass(frozen=True)
class Existing:
    """A same-named file already at the destination."""

    id: str
    size: int  # -1 if unknown
    md5: str = ""  # "" if unknown (e.g. Google Docs)


@dataclass
class Plan:
    """What to do for one file: skip it (``keep`` is the good copy), or upload it (fresh, or into
    ``replace`` in place), and which copies to move to the trash afterwards."""

    upload: bool
    keep: str | None = None
    replace: str | None = None
    trash: list[str] = field(default_factory=list)


def plan(nature: JobNature, size: int, md5: Callable[[], str], existing: list[Existing]) -> Plan:
    """``md5`` is only called when there is something to compare with (hashing costs time)."""
    if not existing:
        return Plan(upload=True)
    local = md5()
    same = [e for e in existing if e.size == size and e.md5 == local]
    if nature is JobNature.ONE_TIME_COPY:
        if same:  # done before; any other copies of the name are leftovers
            return Plan(upload=False, keep=same[0].id, trash=[e.id for e in existing if e.id != same[0].id])
        return Plan(upload=True, trash=[e.id for e in existing])  # fresh upload first, then the leftovers go
    if same:  # ONGOING_SYNC: unchanged
        return Plan(upload=False, keep=same[0].id)
    return Plan(upload=True, replace=existing[0].id)  # a newer version of the same file: update in place


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

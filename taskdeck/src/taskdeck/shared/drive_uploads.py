"""Uploading many files to Google Drive: two lanes, a self-adjusting number of parallel uploads,
and an honest time-left estimate.

Measured on the phone (traces in My Drive > TaskDeck diagnostics): Google takes ~3-5 s to create
each file however small, and big files move at the connection's speed (~1 MB/s). So:

* Small files (up to gdrive.SMALL, one request each) are limited by Google's per-file time: N at
  once give about N / (seconds per file) files per second. The fast lane starts with START uploads
  at once and every ADJUST_EVERY seconds adds one, up to MAX, while things go well; when Google
  says "slow down" (or each file gets much slower) it halves. That settles at what Google and the
  connection allow, which differs between Wi-Fi and mobile data and over time.
* Large files (resumable, in pieces) are limited by bandwidth, which more parallel uploads don't
  add to. They get their own lane of LARGE at once, so they never hold up the small ones.
* Time left = the longer of: small files left x seconds per file / uploads at once, and bytes of
  large files left / measured speed. (Bytes alone said "99%, seconds left" for hours; files alone
  ignore a big file still on its way.)
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from taskdeck.core import Task
from taskdeck.shared import gdrive, http

START, MAX = 4, 20  # small-file uploads at once: first, and at most
LARGE = 2  # large-file uploads at once
ADJUST_EVERY = 30.0  # seconds between adjustments of the fast lane
SLOWER = 2.0  # each file this many times slower than the best seen so far counts as "too many at once"

# (local path, parent folder id, name, path in the archive, size, id to replace in place or None,
#  ids of leftover copies to move to the trash once this one is uploaded)
Item = tuple


PAUSED = "Paused: no connection. Open TaskDeck to continue."  # the task's note while the network is gone


def were_uploaded(count: int) -> str:
    return "1 file was uploaded" if count == 1 else f"{count:,} files were uploaded"


class Interrupted(Exception):
    """The uploads stopped (cancel or error) after ``count`` files / ``sent`` bytes."""

    def __init__(self, error: BaseException, count: int, sent: int):
        super().__init__(str(error))
        self.error, self.count, self.sent = error, count, sent


class Uploads:
    def __init__(self, task: Task, drive: gdrive.DriveClient, todo: list[Item], *, start: int | None = None,
                 most: int | None = None, large: int | None = None, adjust_every: float | None = None,
                 clock: Callable[[], float] = time.monotonic, keep: bool = False):
        start, most = start or START, most or MAX
        large, adjust_every = large or LARGE, ADJUST_EVERY if adjust_every is None else adjust_every
        self.task, self.drive, self.clock = task, drive, clock
        self.keep = keep  # keep the local files (they're the result, not a temporary copy)
        self.small = deque(i for i in todo if i[4] <= gdrive.SMALL)
        self.big = deque(i for i in todo if i[4] > gdrive.SMALL)
        self.total = len(todo)
        self.most, self.large, self.adjust_every = most, large, adjust_every
        self.limit = min(start, most)  # small-file uploads allowed at once right now
        self.lock = threading.Condition()
        self.active_small = 0
        self.count = self.sent = 0
        self.errors: list[BaseException] = []
        self.small_secs: deque[float] = deque(maxlen=100)  # recent small-file times
        self.window: list[float] = []  # small-file times since the last adjustment
        self.best_median: float | None = None
        self.last_adjust = clock()
        self.last_hits = drive.throttle.hits
        self.big_rates: deque[float] = deque(maxlen=10)  # bytes/s of recent large files
        self.big_in_flight: dict[int, tuple[int, int]] = {}  # thread -> (size, bytes sent) of its large file

    # ---- running ------------------------------------------------------------------------------------

    def run(self) -> tuple[int, int]:
        scope = http.current_scope()  # the task's trace and Wi-Fi-only gate, in every worker
        threads = [threading.Thread(target=self._worker, args=(scope, False), daemon=True)
                   for _ in range(min(self.most, len(self.small)))]
        threads += [threading.Thread(target=self._worker, args=(scope, True), daemon=True)
                    for _ in range(min(self.large, len(self.big)))]
        self.task.log("uploads", files=self.total, small=len(self.small), large=len(self.big),
                      workers=self.limit, large_workers=min(self.large, len(self.big)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if self.errors:
            raise Interrupted(self.errors[0], self.count, self.sent)
        if self.task.cancel_requested and self.count < self.total:
            raise Interrupted(gdrive.UploadCancelled(), self.count, self.sent)
        return self.count, self.sent

    def _stopping(self) -> bool:
        return bool(self.errors) or self.task.cancel_requested

    def _take(self, big: bool) -> Item | None:
        with self.lock:
            if big:
                return None if self._stopping() or not self.big else self.big.popleft()
            while not self._stopping() and self.small and self.active_small >= self.limit:
                self.lock.wait(1.0)  # the fast lane is full (or was just made smaller)
            if self._stopping() or not self.small:
                return None
            self.active_small += 1
            return self.small.popleft()

    def _worker(self, scope: dict, big: bool) -> None:
        me = threading.get_ident()
        with http.scope(**scope):
            while (item := self._take(big)) is not None:
                local, parent_id, name, rel, size, replace, trash = (tuple(item) + ((),))[:7]

                if big:
                    self.big_in_flight[me] = (size, 0)

                def progress(n: int, size: int = size) -> bool:
                    if big:
                        self.big_in_flight[me] = (size, n)
                    return self.task.cancel_requested

                started = self.clock()
                try:
                    self.drive.upload(local, parent_id, name, progress=progress, replace=replace, not_ids=trash)
                    for leftover in trash:  # only after the fresh copy is safely in Drive
                        self.drive.trash(leftover)
                except BaseException as exc:  # noqa: BLE001 - handed to the task thread
                    with self.lock:
                        self.errors.append(exc)
                        self.big_in_flight.pop(me, None)
                        if not big:
                            self.active_small -= 1
                        self.lock.notify_all()
                    return
                secs = self.clock() - started
                self.task.log("file", path=str(rel), bytes=size, secs=round(secs, 3), replaced=bool(replace),
                              trashed=len(trash))
                if not self.keep:
                    local.unlink()  # free the space as we go
                with self.lock:
                    self.count += 1
                    self.sent += size
                    if big:
                        self.big_in_flight.pop(me, None)
                        if secs > 0:
                            self.big_rates.append(size / secs)
                    else:
                        self.active_small -= 1
                        self.small_secs.append(secs)
                        self.window.append(secs)
                        self._maybe_adjust()
                    self.lock.notify_all()
                self.task.advance(1, self.count)

    # ---- how many at once -----------------------------------------------------------------------------

    def _maybe_adjust(self) -> None:
        """Called with the lock held after each small file. Additive increase, halving on trouble."""
        now = self.clock()
        if now - self.last_adjust < self.adjust_every or len(self.window) < max(3, self.limit // 2):
            return
        median = statistics.median(self.window)
        hits = self.drive.throttle.hits - self.last_hits
        before = self.limit
        if hits:
            self.limit, reason = max(1, self.limit // 2), f"Google asked to slow down ({hits}x)"
        elif self.best_median is not None and median > SLOWER * self.best_median:
            self.limit, reason = max(1, self.limit // 2), f"each file got slower ({median:.1f} s)"
        else:
            self.limit, reason = min(self.most, self.limit + 1), "going well"
        if self.best_median is None or median < self.best_median:
            self.best_median = median
        self.task.log("workers", limit=self.limit, was=before, reason=reason, median=round(median, 2),
                      files_per_min=round(len(self.window) / (now - self.last_adjust) * 60, 1))
        self.window, self.last_adjust, self.last_hits = [], now, self.drive.throttle.hits

    # ---- time left ----------------------------------------------------------------------------------------

    def eta(self) -> float | None:
        """Seconds left: the longer of the small files (count x time each / at once) and the large ones
        (bytes left / speed). None until there's something to go on."""
        with self.lock:
            small_left = len(self.small) + self.active_small
            big_left = sum(i[4] for i in self.big) + sum(max(0, size - n) for size, n in self.big_in_flight.values())
            small_time = (small_left * statistics.median(self.small_secs) / max(1, self.limit)
                          if small_left and self.small_secs else (0.0 if not small_left else None))
            if not big_left:
                big_time = 0.0
            elif self.big_rates:  # each large upload's own speed; LARGE of them share the connection
                running = len(self.big_in_flight) + len(self.big)
                big_time = big_left / (statistics.median(self.big_rates) * max(1, min(self.large, running)))
            else:
                big_time = None
        if small_time is None or big_time is None:
            return None
        return max(small_time, big_time)


def upload_all(task: Task, drive: gdrive.DriveClient, todo: list[Item], **kw) -> tuple[int, int]:
    """Upload ``todo`` (see Uploads); returns (files, bytes) or raises Interrupted."""
    uploads = Uploads(task, drive, todo, **kw)
    if task.phases:
        task.phases[1].estimate = uploads.eta
    return uploads.run()


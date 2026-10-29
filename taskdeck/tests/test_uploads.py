"""Uploading many files: lanes, how many at once, the shared slow-down pause, and time left."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskdeck.core import TaskManager
from taskdeck.jobs.extract_archive import uploads
from taskdeck.shared import gdrive, http

from test_gdrive import resp


def make_task(files=0):
    task = TaskManager(start_thread=lambda fn: None).start("x", "j", lambda t: "")
    task.set_phases(("Unpacking", 1), ("Uploading", files, "files"))
    return task


class FakeDrive:
    """upload() takes ``delay`` seconds (Google confirming the file); records how many ran at once."""

    def __init__(self, delay=0.03, fail=()):
        self.delay, self.fail = delay, set(fail)
        self.throttle = gdrive.Throttle()
        self.lock = threading.Lock()
        self.running = self.peak = 0
        self.order = []

    def upload(self, path, parent, name, progress=None, replace=None, not_ids=()):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay(name) if callable(self.delay) else self.delay)
            if name in self.fail:
                raise gdrive.DriveError(f"Google Drive couldn't upload “{name}”: HTTP 400")
            if progress:
                progress(path.stat().st_size)
            with self.lock:
                self.order.append(name)
            return "id"
        finally:
            with self.lock:
                self.running -= 1


def files(tmp_path, names, size=5):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"x" * size)
        out.append((p, "root", n, Path(n), size, None))
    return out


def test_runs_in_parallel_and_stops_on_the_first_error(tmp_path):
    drive = FakeDrive()
    task = make_task(12)
    started = time.monotonic()
    assert uploads.upload_all(task, drive, files(tmp_path, [f"f{i}" for i in range(12)]), start=6, most=6) == (12, 60)
    assert drive.peak == 6 and time.monotonic() - started < 0.3  # ~2 rounds of 6, not 12 one by one
    assert task.phases[1].done == 12 and not any((tmp_path / f"f{i}").exists() for i in range(12))

    with pytest.raises(uploads.Interrupted) as stop:
        uploads.upload_all(make_task(31), FakeDrive(fail={"bad"}), files(tmp_path, ["bad"] + [f"g{i}" for i in range(30)]),
                           start=3, most=3)
    assert "HTTP 400" in str(stop.value.error) and stop.value.count < 30  # no new files after the error


def test_large_files_have_their_own_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(gdrive, "SMALL", 10)
    drive = FakeDrive(delay=lambda name: 0.3 if name.startswith("big") else 0.01)
    todo = files(tmp_path, ["big1", "big2", "big3"], size=100) + files(tmp_path, [f"s{i}" for i in range(20)])
    uploads.upload_all(make_task(23), drive, todo, start=2, most=2, large=2)
    small_done = [i for i, n in enumerate(drive.order) if n.startswith("s")]
    assert max(small_done) < drive.order.index("big1")  # all the small ones went by while the big ones uploaded
    assert drive.peak == 4  # 2 large + 2 small


def test_adjusts_how_many_at_once():
    clock = [0.0]
    task = make_task(0)
    drive = SimpleNamespace(throttle=SimpleNamespace(hits=0))
    u = uploads.Uploads(task, drive, [], start=4, most=6, adjust_every=30, clock=lambda: clock[0])

    def window(secs, n=6):
        clock[0] += 30
        for _ in range(n):
            u.window.append(secs)
        u._maybe_adjust()
    window(3.0)
    assert u.limit == 5  # going well: one more
    window(3.0)
    window(3.0)
    assert u.limit == 6  # never above the most
    drive.throttle.hits += 2  # Google said slow down
    window(3.0)
    assert u.limit == 3
    window(7.0)  # each file more than twice as slow as the best
    assert u.limit == 1
    window(3.0)
    assert u.limit == 2


def test_time_left_takes_the_longer_of_files_and_bytes(tmp_path):
    task = make_task(0)
    drive = SimpleNamespace(throttle=SimpleNamespace(hits=0))
    small = [(Path(f"s{i}"), "p", f"s{i}", Path(f"s{i}"), 1000, None) for i in range(100)]
    big = [(Path("b"), "p", "b", Path("b"), 60_000_000, None)]
    u = uploads.Uploads(task, drive, small + big, start=5, large=2)
    assert u.eta() is None  # nothing measured yet
    u.small_secs.extend([3.0] * 10)
    u.big_rates.append(1_000_000)  # 1 MB/s
    # small: 100 files x 3 s / 5 at once = 60 s; large: 60 MB at 1 MB/s (one file, so one upload) = 60 s
    assert u.eta() == pytest.approx(60)
    u.limit = 10  # more at once: the large file is now the slower part
    assert u.eta() == pytest.approx(60)
    u.big.clear()
    assert u.eta() == pytest.approx(30)  # only small files left: 100 x 3 / 10


# ---- the shared slow-down pause -------------------------------------------------------------------------

def busy_then_ok(n, status=429):
    calls = []

    def send(method, url, body=None, headers=None, timeout=60):
        calls.append(method)
        if len(calls) <= n:
            return resp(status, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
        return resp(200, {"id": "f1"})
    return send, calls


def test_slow_down_pauses_and_never_fails_the_job(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    send, calls = busy_then_ok(12, status=403)
    sleeps, paused = [], []
    d = gdrive.DriveClient(lambda: "AT", send=send, sleep=sleeps.append, keep_waiting=lambda: True)
    d.on_throttle = lambda wait, status: paused.append((round(wait), status))
    assert d.upload(f, "root") == "f1"  # 12 slow-downs in a row: still done
    assert len(calls) == 13 and d.throttle.hits == 12 and paused[0][1] == 403
    waits = [w for w, _ in paused]
    assert waits[0] <= 2 and waits[5] >= 24 and max(waits) <= 80  # 1, 2, 4 ... up to 64 s (with jitter)

    send, calls = busy_then_ok(99)
    d = gdrive.DriveClient(lambda: "AT", send=send, sleep=lambda s: None)  # not waiting: gives up
    with pytest.raises(gdrive.DriveError):
        d.upload(f, "root")
    assert len(calls) == gdrive.RETRIES


def test_one_slow_down_pauses_every_upload_and_cancel_still_works():
    clock = [0.0]
    sleeps = []
    d = gdrive.DriveClient(lambda: "AT", send=lambda *a, **k: resp(200, {}), sleep=sleeps.append,
                           keep_waiting=lambda: True)
    d.throttle = gdrive.Throttle(clock=lambda: clock[0], rand=lambda: 0.5)
    d.throttle.hit()
    d.throttle.hit()  # 2 s pause (second in a row)
    d._call("GET", "https://example/")  # another upload: waits it out before sending
    assert sum(sleeps) == pytest.approx(2.0)
    d.throttle.ok()
    d.throttle.hit()
    d.keep_waiting = lambda: False  # Cancel during a pause
    with pytest.raises(gdrive.UploadCancelled):
        d._call("GET", "https://example/")

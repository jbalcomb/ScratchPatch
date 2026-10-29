"""The two kinds of copying jobs (taskdeck.core.copying): what happens to a same-named file already there."""

import hashlib

from taskdeck.core.copying import Existing, JobNature, Plan, md5_of, plan

ONCE, SYNC = JobNature.ONE_TIME_COPY, JobNature.ONGOING_SYNC
MD5 = hashlib.md5(b"hello").hexdigest()


def never():
    raise AssertionError("no checksum needed when nothing is there")


def test_nothing_there_uploads_without_hashing():
    assert plan(ONCE, 5, never, []) == Plan(upload=True)
    assert plan(SYNC, 5, never, []) == Plan(upload=True)


def test_one_time_copy():
    good, broken, other = Existing("g", 5, MD5), Existing("b", 3, "x"), Existing("o", 5, "y")  # o: same size, other bytes
    assert plan(ONCE, 5, lambda: MD5, [good]) == Plan(upload=False, keep="g")
    assert plan(ONCE, 5, lambda: MD5, [broken, good, other]) == Plan(upload=False, keep="g", trash=["b", "o"])
    # no matching copy: a fresh upload, then every leftover goes (never updated in place)
    assert plan(ONCE, 5, lambda: MD5, [broken, other]) == Plan(upload=True, trash=["b", "o"])


def test_ongoing_sync():
    assert plan(SYNC, 5, lambda: MD5, [Existing("g", 5, MD5)]) == Plan(upload=False, keep="g")
    # a newer version of the same document: updated in place (same id, link, sharing, history)
    assert plan(SYNC, 5, lambda: MD5, [Existing("old", 3, "x")]) == Plan(upload=True, replace="old")


def test_md5_of(tmp_path):
    f = tmp_path / "a"
    f.write_bytes(b"hello")
    assert md5_of(f) == MD5

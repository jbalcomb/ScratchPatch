"""Load times, to measure and manage how fast the pages are.

* Every request: one log line (method, path, status, size, milliseconds) plus the parts of its time that
  matter, e.g. ``photo from Immich 820 ms, shrunk 410 ms`` (``note``).
* The phone's own view: the pages report how long they and each photo took to arrive, network included
  (``POST /api/timings``), logged as ``phone /products: ...``.
* The last few thousand of both, summarized at ``GET /api/stats`` and on the ``/stats`` page.
"""
import re
import statistics
import threading
import time
from collections import Counter, deque

KEEP = 2000
_local = threading.local()


def start() -> None:
    """A request begins: forget the parts noted for the last one on this thread."""
    _local.parts = []


def note(what: str, ms: float | None = None) -> None:
    """A part of the current request's time, e.g. note("shrunk", 410) or note("ready-made")."""
    if not hasattr(_local, "parts"):
        _local.parts = []
    _local.parts.append((what, ms))


def parts() -> list[tuple[str, float | None]]:
    return list(getattr(_local, "parts", []))


class Timer:
    """with Timer("photo from Immich"): ... notes how long the block took."""

    def __init__(self, what: str):
        self.what = what

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        note(self.what, (time.perf_counter() - self.t0) * 1000)


def kind(path: str, query: dict) -> str:
    """Requests grouped for the stats: /api/receipts/17 -> "GET /api/receipts/{id}", photos by size asked."""
    if path.startswith("/api/images/"):
        return f"photo ?w={query.get('w')}" if query.get("w") else "photo original"
    return re.sub(r"/\d+", "/{id}", path)


def describe(parts_: list[tuple[str, float | None]]) -> str:
    return ", ".join(f"{w} {ms:.0f} ms" if ms is not None else w for w, ms in parts_)


def _summary(values: list[float]) -> dict:
    values = sorted(values)
    return {"count": len(values), "median_ms": round(statistics.median(values)),
            "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))]), "max_ms": round(values[-1])}


class Stats:
    def __init__(self, keep: int = KEEP):
        self.server = deque(maxlen=keep)  # (kind, method, ms, bytes, status, parts, at, path)
        self.phone = deque(maxlen=keep)   # (page, page_ms or None, [(url, ms, kb)], at)
        self.lock = threading.Lock()

    def add(self, method, path, query, status, nbytes, ms, parts_):
        with self.lock:
            self.server.append((kind(path, query), method, ms, nbytes, status, parts_, time.time(),
                                path + (f"?w={query['w']}" if query.get("w") else "")))

    def add_phone(self, page, page_ms, items):
        with self.lock:
            self.phone.append((page, page_ms, items, time.time()))

    def summary(self) -> dict:
        with self.lock:
            server, phone = list(self.server), list(self.phone)
        by_kind: dict[str, list] = {}
        for k, method, ms, nbytes, *_ in server:
            by_kind.setdefault(f"{method} {k}" if not k.startswith("photo") else k, []).append((ms, nbytes))
        out = {"server": {k: {**_summary([m for m, _ in v]), "median_kb": round(statistics.median(b for _, b in v) / 1024)}
                          for k, v in sorted(by_kind.items())}}
        photo_parts = Counter(w for k, *_rest in server if k.startswith("photo") for w, _ in _rest[4])
        out["photo_parts"] = dict(photo_parts)  # how often a photo was ready-made, shrunk, or fetched from Immich
        pages: dict[str, dict] = {}
        for page, page_ms, items, _ in phone:
            p = pages.setdefault(page, {"loads": [], "photos": [], "requests": []})
            if page_ms:
                p["loads"].append(page_ms)
            for url, ms, _kb in items:
                (p["photos"] if "/api/images/" in url else p["requests"]).append(ms)
        out["phone"] = {page: {"page_load": _summary(p["loads"]) if p["loads"] else None,
                               "photo": _summary(p["photos"]) if p["photos"] else None,
                               "api": _summary(p["requests"]) if p["requests"] else None}
                        for page, p in sorted(pages.items())}
        slowest = sorted(server, key=lambda r: -r[2])[:10]
        out["slowest"] = [{"request": f"{m} {path}", "ms": round(ms), "kb": round(nb / 1024), "status": st,
                           "parts": describe(pp)} for _k, m, ms, nb, st, pp, _at, path in slowest]
        return out


def phone_line(page: str, page_ms, items: list) -> str:
    """One log line for what a page reported: its load time and its photos' times, slowest named."""
    out = [f"phone {page}:"]
    if page_ms:
        out.append(f"page {page_ms / 1000:.1f} s")
    photos_ = [(u, ms, kb) for u, ms, kb in items if "/api/images/" in u]
    if photos_:
        ms_ = sorted(ms for _, ms, _ in photos_)
        worst = max(photos_, key=lambda x: x[1])
        out.append(f"{len(photos_)} photo{'s' * (len(photos_) != 1)} median {statistics.median(ms_):.0f} ms, slowest "
                   f"{worst[1]:.0f} ms ({worst[0]}, {worst[2]} KB)")
    other = [(u, ms) for u, ms, _ in items if "/api/images/" not in u]
    if other:
        worst = max(other, key=lambda x: x[1])
        out.append(f"{len(other)} request{'s' * (len(other) != 1)}, slowest {worst[1]:.0f} ms ({worst[0]})")
    return " ".join(out[:1]) + " " + "; ".join(out[1:])

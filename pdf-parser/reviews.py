"""Human review of a parsed PDF (REVIEW_SPEC.md): the store of reviews, each element's review state, the review
queues and the coverage scores. No HTTP here; web.py serves it, and the review page (review.html) shows it.

Reviews of a document live in one file, ``<home>/reviews/<sha256 of the PDF>.json``, so every upload of the
same PDF shares them. Only :meth:`Store.add` writes it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from pathlib import Path

DECISIONS = ("accepted", "corrected", "rejected")
CHECKED = ("reviewed", "confirmed", "settled")
BBOX_AGREE = 0.9            # two boxes agree when their intersection over union is at least this
REVIEWABLE_IMAGES = ("kept", "needs_review")


class ReviewError(ValueError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    """The reviews of one document."""

    _locks: dict[str, threading.Lock] = {}

    def __init__(self, root, sha256, file=None):
        if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
            raise ReviewError("bad document hash")
        self.path = Path(root) / "reviews" / f"{sha256}.json"
        self.sha256, self.file = sha256, file
        self.lock = Store._locks.setdefault(str(self.path), threading.Lock())

    def load(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"document": {"file": self.file, "sha256": self.sha256}, "reviews": []}

    def add(self, review, elements):
        """Record one review. A reviewer's newer review of an element replaces their own earlier one.
        -> the review as stored."""
        by = str(review.get("by") or "").strip()
        element = str(review.get("element") or "")
        decision = review.get("decision")
        if not by:
            raise ReviewError("say who you are (reviewer name)")
        if element not in elements:
            raise ReviewError(f"no element {element!r} in this document")
        if decision not in DECISIONS:
            raise ReviewError(f"decision must be one of {', '.join(DECISIONS)}")
        stored = {"element": element, "decision": decision, "by": by, "at": now()}
        if decision == "corrected":
            changes = {k: review[k] for k in ("text", "rows", "bbox") if review.get(k) is not None}
            if not changes:
                raise ReviewError("a correction needs the corrected text, table rows or box")
            if "bbox" in changes:
                b = changes["bbox"]
                if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)
                        and b[0] < b[2] and b[1] < b[3]):
                    raise ReviewError("bbox must be [x0, y0, x1, y1]")
                changes["bbox"] = [round(float(v), 2) for v in b]
            if "rows" in changes and not (isinstance(changes["rows"], list)
                                          and all(isinstance(r, list) for r in changes["rows"])):
                raise ReviewError("rows must be a list of rows")
            stored.update(changes)
        if decision == "rejected" and review.get("reason"):
            stored["reason"] = str(review["reason"])[:500]
        with self.lock:
            data = self.load()
            data["reviews"] = [r for r in data["reviews"] if not (r["element"] == element and r["by"] == by)]
            data["reviews"].append(stored)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            os.replace(tmp, self.path)
        return stored


# ---------------------------------------------------------------- agreement and state

def _norm_text(t):
    return " ".join(str(t or "").split())


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def agree(a, b, element):
    """Same decision, and for corrections the same content: text equal ignoring whitespace, the same table grid,
    boxes overlapping at least BBOX_AGREE. What a correction leaves out is taken as the element's own."""
    if a["decision"] != b["decision"]:
        return False
    if a["decision"] != "corrected":
        return True
    if _norm_text(a.get("text", element.get("text"))) != _norm_text(b.get("text", element.get("text"))):
        return False
    if ("rows" in a or "rows" in b) and [[_norm_text(c) for c in r] for r in a.get("rows") or []] != \
            [[_norm_text(c) for c in r] for r in b.get("rows") or []]:
        return False
    return _iou(a.get("bbox", element["bbox"]), b.get("bbox", element["bbox"])) >= BBOX_AGREE


def state(reviews, element):
    """REVIEW_SPEC.md section 3a. -> (state, the review whose version applies or None)."""
    rs = sorted(reviews, key=lambda r: r["at"])
    if not rs:
        return "unreviewed", None
    newest = rs[-1]
    if len(rs) == 1:
        return "reviewed", newest
    if len(rs) == 2:
        return ("confirmed" if agree(rs[0], rs[1], element) else "disputed"), newest
    # Three or more: the version most reviews agree on wins (the newest such review applies).
    best, votes = None, 1
    for r in rs:
        n = sum(agree(r, o, element) for o in rs)
        if n > votes or (n == votes and n > 1):
            best, votes = r, n
    return ("settled", best) if best else ("escalated", newest)


# ---------------------------------------------------------------- queues and coverage

def reviewable(e):
    return e["type"] in ("text", "table") or (e["type"] == "image" and e.get("status") in REVIEWABLE_IMAGES)


def source_of(e):
    return e.get("source") if e["type"] == "text" else e["type"]


def queue_of(e):
    """REVIEW_SPEC.md section 1: vision and needs_review are required; then OCR; then everything else."""
    if e["type"] == "text" and e.get("source") == "vision":
        return "vision"
    if e["type"] == "image" and (e.get("vision") or any(a.get("vision") for a in e.get("attempts") or [])):
        return "vision"
    if (e["type"] == "image" and e.get("status") == "needs_review") or e.get("low_confidence"):
        return "needs_review"
    if e["type"] == "text" and e.get("source") == "ocr":
        return "ocr"
    return "other"


def annotate(manifest, data):
    """The reviewable elements, each with its reviews, state, applied review and queue."""
    by_element = {}
    for r in data["reviews"]:
        by_element.setdefault(r["element"], []).append(r)
    out = []
    for e in manifest["elements"]:
        if not reviewable(e):
            continue
        rs = by_element.get(e["id"], [])
        st, applied = state(rs, e)
        out.append({**e, "queue": queue_of(e), "review": {"state": st, "applied": applied,
                                                         "reviews": sorted(rs, key=lambda r: r["at"])}})
    return out


def _pct(n, d):
    return round(100 * n / d, 1) if d else None


def coverage(manifest, elements):
    """REVIEW_SPEC.md section 4."""
    checked = lambda e: e["review"]["state"] in CHECKED  # noqa: E731
    pages = {}
    for e in elements:
        pages.setdefault(e["page"], []).append(e)
    texts = [e for e in elements if e["type"] == "text"]
    chars = lambda es: sum(len(e.get("text") or "") for e in es)  # noqa: E731
    by_source = {}
    for src in sorted({e.get("source") for e in texts}):
        es = [e for e in texts if e.get("source") == src]
        by_source[src] = _pct(chars([e for e in es if checked(e)]), chars(es))
    mixed = [p for p, es in pages.items() if any(e["type"] == "image" for e in es)
             and any(e["type"] == "text" for e in es)]
    required = [e for e in elements if e["queue"] in ("vision", "needs_review")]
    accuracy = {}
    for src in sorted({source_of(e) for e in elements}):
        done = [e for e in elements if source_of(e) == src and checked(e)]
        accuracy[src] = {"checked": len(done),
                         "accepted": _pct(sum(e["review"]["applied"]["decision"] == "accepted" for e in done), len(done))}
    return {
        "pages": _pct(sum(all(checked(e) for e in es) for es in pages.values()), len(pages)),
        "pages_total": len(pages),
        "text": _pct(chars([e for e in texts if checked(e)]), chars(texts)),
        "text_by_source": by_source,
        "tables": _pct(sum(checked(e) for e in elements if e["type"] == "table"),
                       sum(e["type"] == "table" for e in elements)),
        "images": _pct(sum(checked(e) for e in elements if e["type"] == "image"),
                       sum(e["type"] == "image" for e in elements)),
        "mixed_pages": _pct(sum(all(checked(e) for e in pages[p]) for p in mixed), len(mixed)),
        "required": _pct(sum(checked(e) for e in required), len(required)),
        "required_left": sum(not checked(e) for e in required),
        "disputed": sum(e["review"]["state"] == "disputed" for e in elements),
        "escalated": sum(e["review"]["state"] == "escalated" for e in elements),
        "accuracy": accuracy,
    }

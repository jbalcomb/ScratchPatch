"""Images on a page: extract, check, retry, keep the best.

Each image placed on a page is a candidate. The first attempt takes the embedded image itself (or renders the
page under its box when it can't be taken out). Every attempt gets the tier-1 checks below; a flagged attempt
(or every attempt, with PDF_PARSER_VISION_ALL=1) is shown to the vision model (tier 2). An attempt that fails
is retried with the next strategy, in this order, picking ones that fit the problem first:

    hidpi    render the page under the box at a higher DPI
    pad      grow the box on every side (content cut off at an edge)
    merge    join the box with image fragments touching it
    enhance  straighten (deskew) and stretch the contrast

At most MAX_ATTEMPTS attempts per image. The best-scoring one is kept; if none passed it is needs_review.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps, ImageStat

import ocr
import vision as vision_mod

MAX_ATTEMPTS = 3
STRATEGIES = ("hidpi", "pad", "merge", "enhance")
FIRST_DPI, HI_DPI = 150, 300
ENOUGH_DPI = 200         # an image this sharp gains nothing from a higher-DPI render, unless it reads badly
MIN_BOX_PT = 24          # smaller boxes are bullets and icons: kept as they are, not checked or retried
MIN_PIXELS = 48          # an image narrower or shorter than this is too small to use
BLANK_STDDEV = 4.0       # grey-level spread below which an image is blank / near-uniform
ODD_ASPECT = 8.0
LOW_OCR_CONF, LOW_OCR_MIN_WORDS = 50, 3
TEXT_OVERLAP = 0.5       # share of the image's box covered by text blocks
DUPLICATE_BITS = 4       # difference hash: at most this many of 64 bits differ
MERGE_GAP_PT = 6
PAD_FRACTION, PAD_MIN_PT = 0.10, 6

WEIGHTS = {"blank": 0.6, "too_small": 0.4, "clipped": 0.3, "text_overlap": 0.2, "odd_aspect": 0.15, "low_ocr": 0.15}
HARD = {"blank", "too_small", "clipped"}   # without the vision model, an attempt with one of these fails


def vision_for_all():
    return os.environ.get("PDF_PARSER_VISION_ALL", "") not in ("", "0", "false")


# ---------------------------------------------------------------- candidates and attempts

def candidates(page, page_no, skip_page_scans=False):
    """Image placements on the page: [{id, page, rect, xref}], each placement once."""
    page_area = abs(page.rect)
    seen, out = set(), []
    for info in page.get_image_info(xrefs=True):
        rect = pymupdf.Rect(info["bbox"])
        if rect.is_empty or rect.is_infinite:
            continue
        key = (info.get("xref", 0), tuple(round(v, 1) for v in rect))
        if key in seen:
            continue
        seen.add(key)
        if skip_page_scans and abs(rect & page.rect) > 0.5 * page_area:
            continue  # the scan of a scanned page: the page itself is OCR'd instead
        out.append({"id": f"p{page_no}-i{len(out) + 1}", "page": page_no, "rect": rect, "xref": info.get("xref", 0)})
    return out


def _save_embedded(doc, xref, dest):
    """The embedded image as stored (JPEG/PNG) or converted to PNG. -> path, or None if it can't be taken out."""
    try:
        info = doc.extract_image(xref)
        if info and info.get("ext") in ("png", "jpeg", "jpg") and not info.get("smask"):
            path = dest.with_suffix("." + ("jpg" if info["ext"] == "jpeg" else info["ext"]))
            path.write_bytes(info["image"])
            return path
        pix = pymupdf.Pixmap(doc, xref)
        if info and info.get("smask"):
            pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, info["smask"]))
        if pix.colorspace and pix.colorspace.n > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        path = dest.with_suffix(".png")
        pix.save(path)
        return path
    except Exception:  # odd colour spaces, JBIG2 without a decoder, broken streams: render the page instead
        return None


def _render(page, rect, dpi, dest):
    path = dest.with_suffix(".png")
    page.get_pixmap(clip=rect, dpi=dpi).save(path)
    return path


def _enhance(src, dest):
    """Deskew (projection profile, within ±5°) and stretch the contrast."""
    with Image.open(src) as im:
        grey = ImageOps.grayscale(im)
    if ImageStat.Stat(grey).stddev[0] < BLANK_STDDEV:  # nothing to bring out; stretching would invent contrast
        path = dest.with_suffix(".png")
        grey.save(path)
        return path, 0.0
    hist = grey.histogram()
    background = max(range(256), key=lambda v: hist[v])
    small = grey.copy()
    small.thumbnail((800, 800))
    best_angle, best_var = 0.0, -1.0
    for tenth in range(-50, 51, 5):
        angle = tenth / 10
        rotated = small.rotate(angle, expand=True, fillcolor=background)
        rows = rotated.resize((1, rotated.height), Image.BOX)  # mean grey of every row
        var = ImageStat.Stat(rows).var[0]
        if var > best_var:
            best_angle, best_var = angle, var
    out = grey.rotate(best_angle, expand=True, fillcolor=background) if best_angle else grey
    path = dest.with_suffix(".png")
    ImageOps.autocontrast(out, cutoff=1).save(path)
    return path, best_angle


def _neighbours(cand, all_cands, consumed):
    near = cand["rect"] + (-MERGE_GAP_PT, -MERGE_GAP_PT, MERGE_GAP_PT, MERGE_GAP_PT)
    return [c for c in all_cands if c["id"] != cand["id"] and c["id"] not in consumed and near.intersects(c["rect"])]


# ---------------------------------------------------------------- tier 1

def dhash(path):
    with Image.open(path) as im:
        small = ImageOps.grayscale(im).resize((9, 8), Image.LANCZOS)
    px = small.tobytes()
    return sum(1 << i for i, (a, b) in enumerate((px[r * 9 + c], px[r * 9 + c + 1]) for r in range(8) for c in range(8))
               if a > b)


def _busy_edges(grey):
    """Edges (of 4) where content runs into the border: likely cut off."""
    hist = grey.histogram()
    background = max(range(256), key=lambda v: hist[v])
    w, h = grey.size
    strip = max(1, min(3, w // 20, h // 20))
    busy = 0
    for box in ((0, 0, w, strip), (0, h - strip, w, h), (0, 0, strip, h), (w - strip, 0, w, h)):
        px = grey.crop(box).tobytes()
        if px and sum(abs(p - background) > 48 for p in px) / len(px) > 0.25:
            busy += 1
    return busy


def check(path, rect, text_rects, source, page_rect):
    """Tier-1 checks on one attempt. -> (flags, metrics)."""
    flags = []
    with Image.open(path) as im:
        grey = ImageOps.grayscale(im)
    w, h = grey.size
    stddev = ImageStat.Stat(grey).stddev[0]
    if min(w, h) < MIN_PIXELS:
        flags.append("too_small")
    if stddev < BLANK_STDDEV:
        flags.append("blank")
    busy = _busy_edges(grey) if "blank" not in flags else 0
    # A photo fills the whole box (4 edges); content touching only some edges was probably cut off.
    if (source != "embedded" and 1 <= busy <= 3) or not page_rect.contains(rect):
        flags.append("clipped")
    if max(w, h) / max(1, min(w, h)) > ODD_ASPECT:
        flags.append("odd_aspect")
    read = ocr.summary(ocr.words(path)) if "blank" not in flags else {"text": "", "confidence": None, "words": 0}
    if read["words"] >= LOW_OCR_MIN_WORDS and (read["confidence"] or 0) < LOW_OCR_CONF:
        flags.append("low_ocr")
    covered = sum(abs(rect & t) for t in text_rects) / max(1e-6, abs(rect))
    if covered > TEXT_OVERLAP:
        flags.append("text_overlap")
    return flags, {"width": w, "height": h, "stddev": round(stddev, 1), "busy_edges": busy,
                   "text_covered": round(min(1.0, covered), 2), "ocr": read}


def score(flags):
    return round(max(0.0, 1.0 - sum(WEIGHTS[f] for f in flags)), 2)


# ---------------------------------------------------------------- the loop

def _next_strategy(tried, attempt, has_neighbours):
    flags, verdict = attempt["flags"], attempt.get("vision") or {}
    issue = (verdict.get("issue") or "").lower()
    cut = "clipped" in flags or verdict.get("complete") is False or verdict.get("content_type") == "fragment"
    fits = {
        "hidpi": attempt["dpi"] < ENOUGH_DPI or bool({"too_small", "low_ocr"} & set(flags)),
        "pad": cut,
        "merge": has_neighbours and (cut or "too_small" in flags),
        "enhance": "low_ocr" in flags or any(w in issue for w in
                                                                    ("skew", "blur", "contrast", "faint", "rotat")),
    }
    untried = [s for s in STRATEGIES if s not in tried and (s != "merge" or has_neighbours)]
    return next((s for s in untried if fits[s]), untried[0] if untried else None)


class PageImages:
    """Runs the loop for every image of one document. ``work`` is a scratch folder inside the output."""

    def __init__(self, doc, out_dir, budget, log=None):
        self.doc, self.out, self.budget = doc, Path(out_dir), budget
        self.images_dir = self.out / "images"
        self.work = self.images_dir / ".work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.kept_hashes = []          # (hash, element id) of kept images, for near-duplicates
        self.by_xref = {}              # xref -> element id of the first placement
        self.log = log or budget.log

    def page(self, page, page_no, text_rects, skip_page_scans=False):
        cands = candidates(page, page_no, skip_page_scans)
        elements, consumed = {}, set()
        for cand in cands:
            if cand["id"] in consumed:
                continue
            el = self._one(page, cand, cands, consumed, text_rects)
            elements[cand["id"]] = el
            for other in el.get("merged", []):
                consumed.add(other)
                if other in elements:
                    elements[other].update(status="merged", merged_into=el["id"])
        for c in cands:
            if c["id"] in consumed and c["id"] not in elements:
                merged_into = next(e["id"] for e in elements.values() if c["id"] in e.get("merged", []))
                elements[c["id"]] = {"id": c["id"], "type": "image", "page": page_no, "bbox": _box(c["rect"]),
                                     "xref": c["xref"], "status": "merged", "merged_into": merged_into, "file": None}
        return [elements[c["id"]] for c in cands]

    def finish(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def _attempt(self, page, cand, n, strategy, state, prev):
        dest = self.work / f"{cand['id']}-a{n}"
        if strategy == "embedded":
            path = _save_embedded(self.doc, cand["xref"], dest) if cand["xref"] else None
            if path is None:
                strategy, path = "clip", _render(page, state["rect"], state["dpi"], dest)
            else:
                with Image.open(path) as im:
                    state["dpi"] = round(im.width / max(1e-6, cand["rect"].width / 72))
        elif strategy == "hidpi":
            state["dpi"] = HI_DPI if state["dpi"] < HI_DPI else state["dpi"] + 100
            path = _render(page, state["rect"], state["dpi"], dest)
        elif strategy == "pad":
            r = state["rect"]
            dx, dy = max(PAD_MIN_PT, r.width * PAD_FRACTION), max(PAD_MIN_PT, r.height * PAD_FRACTION)
            state["rect"] = (r + (-dx, -dy, dx, dy)) & page.rect
            path = _render(page, state["rect"], max(state["dpi"], FIRST_DPI), dest)
        elif strategy == "merge":
            for other in state["neighbours"]:
                state["rect"] |= other["rect"]
            state["merged"] = [o["id"] for o in state["neighbours"]]
            path = _render(page, state["rect"], max(state["dpi"], FIRST_DPI), dest)
        else:  # enhance the previous attempt's image
            path, angle = _enhance(prev["path"], dest)
            state["deskew_degrees"] = angle
        flags, metrics = check(path, state["rect"], self._text_rects, "embedded" if strategy == "embedded" else "clip",
                               page.rect)
        return {"n": n, "strategy": strategy, "path": path, "dpi": state["dpi"], "rect": pymupdf.Rect(state["rect"]),
                "merged": list(state.get("merged", [])), "flags": flags, "metrics": metrics, "score": score(flags),
                "vision": None}

    def _judge(self, attempt, cand):
        """Tier 2 when flagged; -> whether the attempt passes. Adjusts its score with the vision verdict."""
        if not attempt["flags"] and not vision_for_all():
            return True
        verdict = self.budget.ask(attempt["path"], cand["page"], image=cand["id"], attempt=attempt["n"],
                                  strategy=attempt["strategy"], flags=attempt["flags"])
        attempt["vision"] = verdict
        if verdict is None:
            attempt["unverified"] = self.budget.stopped or "vision request failed"
            return not (HARD & set(attempt["flags"]))
        ok = verdict["complete"] and vision_mod.meaningful(verdict)
        attempt["score"] = max(attempt["score"], 0.9) if ok else round(attempt["score"] * 0.5, 2)
        return ok

    def _one(self, page, cand, cands, consumed, text_rects):
        self._text_rects = text_rects
        base = {"id": cand["id"], "type": "image", "page": cand["page"], "bbox": _box(cand["rect"]), "xref": cand["xref"]}
        if cand["xref"] and cand["xref"] in self.by_xref:  # the same image placed again
            return {**base, "status": "duplicate", "duplicate_of": self.by_xref[cand["xref"]], "file": None}
        state = {"rect": pymupdf.Rect(cand["rect"]), "dpi": FIRST_DPI, "neighbours": _neighbours(cand, cands, consumed)}
        first = self._attempt(page, cand, 1, "embedded", state, None)
        if cand["rect"].width < MIN_BOX_PT or cand["rect"].height < MIN_BOX_PT:
            return self._save(base, [first], first, "small", note="smaller than 24 pt: an icon or bullet, not checked")
        if "blank" not in first["flags"]:
            h = dhash(first["path"])
            twin = next((eid for kh, eid in self.kept_hashes if bin(kh ^ h).count("1") <= DUPLICATE_BITS), None)
            if twin:
                return {**base, "status": "duplicate", "duplicate_of": twin, "file": None,
                        "flags": first["flags"], "attempts": [_public(first)]}
        attempts, tried, passed = [first], set(), []
        attempt = first
        while True:
            if self._judge(attempt, cand):
                passed.append(attempt)
                break
            if len(attempts) >= MAX_ATTEMPTS:
                break
            strategy = _next_strategy(tried, attempt, bool(state["neighbours"]))
            if strategy is None:
                break
            tried.add(strategy)
            attempt = self._attempt(page, cand, len(attempts) + 1, strategy, dict(state, rect=pymupdf.Rect(state["rect"])),
                                    attempt)
            state.update(rect=attempt["rect"], dpi=attempt["dpi"], merged=attempt["merged"])
            attempts.append(attempt)
        best = max(passed or attempts, key=lambda a: a["score"])
        status = "kept" if passed else "needs_review"
        el = self._save(base, attempts, best, status)
        if status == "kept":
            self.kept_hashes.append((dhash(best["path"]), el["id"]))
        if cand["xref"]:
            self.by_xref[cand["xref"]] = el["id"]
        self.log(event="image", image=cand["id"], status=status, first_flags=first["flags"], attempts=len(attempts),
                 strategies=[a["strategy"] for a in attempts],
                 vision_needed=sum(a["vision"] is not None or "unverified" in a for a in attempts),
                 vision_answered=sum(a["vision"] is not None for a in attempts),
                 vision_ok=bool(best["vision"] and best["vision"]["complete"] and vision_mod.meaningful(best["vision"])))
        return el

    def _save(self, base, attempts, best, status, note=None):
        self.images_dir.mkdir(exist_ok=True)
        final = self.images_dir / (base["id"] + best["path"].suffix)
        shutil.copyfile(best["path"], final)
        public = [_public(a) for a in attempts]
        if status == "needs_review" and len(attempts) > 1:  # keep every attempt so a person can choose
            (self.images_dir / "attempts").mkdir(exist_ok=True)
            for a, p in zip(attempts, public):
                p["file"] = f"images/attempts/{base['id']}-a{a['n']}{a['path'].suffix}"
                shutil.copyfile(a["path"], self.out / p["file"])
        el = {**base, "bbox": _box(best["rect"]), "file": f"images/{final.name}", "status": status,
              "source": "embedded" if best["strategy"] == "embedded" else "render",
              "width": best["metrics"]["width"], "height": best["metrics"]["height"], "dpi": best["dpi"],
              "score": best["score"], "flags": best["flags"], "vision": best["vision"], "ocr": best["metrics"]["ocr"],
              "attempts": public, "best_attempt": best["n"]}
        if best["merged"]:
            el["merged"] = best["merged"]
        if best.get("unverified"):
            el["vision_skipped"] = best["unverified"]
        if note:
            el["note"] = note
        return el


def _public(a):
    return {"n": a["n"], "strategy": a["strategy"], "dpi": a["dpi"], "bbox": _box(a["rect"]), "score": a["score"],
            "flags": a["flags"], "vision": a["vision"],
            **({"vision_skipped": a["unverified"]} if a.get("unverified") else {})}


def _box(rect):
    return [round(v, 2) for v in rect]

#!/usr/bin/env python3
"""Parse a PDF into a folder: text, tables, images, OCR, and manifest.json that describes all of it.

    python parse_pdf.py file.pdf                 # -> ./file/ (next to where you run it)
    python parse_pdf.py file.pdf -o out/         # -> out/file/
    python parse_pdf.py file.pdf --no-vision     # tier-1 checks only
    python parse_pdf.py --stats out/             # how often the vision model was needed, from the QA logs

The vision model comes from PDF_PARSER_LLM_URL (or LLM_URL) and PDF_PARSER_VISION_MODEL; see vision.py.

The folder holds:
    manifest.json     pages, and every element in reading order with page, bbox, type (and fonts for text)
    text.txt          all text in reading order, page by page (text layer, or OCR on scanned pages)
    tables/*.csv      one per table
    images/*          one per image kept (or needing review); images/attempts/ for those needing review
    pages/*.png       renders of scanned pages (what the OCR read)
    qa_log.jsonl      every vision request and every image's outcome
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import shutil
import tempfile
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import images as images_mod  # noqa: E402
import ocr  # noqa: E402
import vision as vision_mod  # noqa: E402

SCHEMA = "pdf-parser/1"
OCR_DPI = 300
SCANNED_MAX_CHARS = 20       # a page with less text than this and a page-sized image has no usable text layer
SCANNED_IMAGE_SHARE = 0.5
# ...and it is a scan of text when OCR reads this much off it; otherwise it is an illustration (a title page,
# a full-page picture), whose picture is kept as an image and whose few words are kept as OCR text.
SCANNED_MIN_WORDS, SCANNED_MIN_CONF = 40, 60
OCR_LOW_CONF = 60            # OCR blocks read with less mean confidence are marked low_confidence (and kept)
REREAD_PAD_PT = 6


# ---------------------------------------------------------------- per page

def triage(page):
    """'text' (it has a text layer) or 'ocr' (a page-sized image and no text layer: OCR decides whether it is
    a scan of text or an illustration)."""
    chars = len(page.get_text("text").strip())
    if chars >= SCANNED_MAX_CHARS:
        return "text"
    page_rect = images_mod.native_rect(page)
    area = abs(page_rect)
    covered = max((abs(pymupdf.Rect(i["bbox"]) & page_rect) for i in page.get_image_info()), default=0)
    return "ocr" if covered >= SCANNED_IMAGE_SHARE * area or chars == 0 and covered else "text"


def _font(span):
    flags = span.get("flags", 0)
    return {"font": span.get("font"), "size": round(span.get("size", 0), 2),
            "bold": bool(flags & 16) or "bold" in (span.get("font") or "").lower(),
            "italic": bool(flags & 2) or "italic" in (span.get("font") or "").lower(),
            "monospace": bool(flags & 8), "serif": bool(flags & 4), "superscript": bool(flags & 1),
            "color": f"#{span.get('color', 0):06x}"}


def text_blocks(page, page_no):
    """Text-layer blocks in reading order, with lines and spans (text, bbox, font)."""
    out = []
    for block in page.get_text("dict", sort=True)["blocks"]:
        if block.get("type") != 0:
            continue
        lines = []
        for line in block["lines"]:
            spans = [{"text": s["text"], "bbox": _box(s["bbox"]), **_font(s)} for s in line["spans"] if s["text"].strip()]
            if spans:
                lines.append({"bbox": _box(line["bbox"]), "dir": [round(v, 3) for v in line.get("dir", (1, 0))],
                              "spans": spans})
        if not lines:
            continue
        text = "\n".join("".join(s["text"] for s in ln["spans"]).strip() for ln in lines)
        out.append({"type": "text", "page": page_no, "bbox": _box(block["bbox"]), "source": "text_layer",
                    "text": text, "lines": lines})
    return out


def ocr_page(page, page_no, out_dir):
    """Full-page OCR of a scanned page: its render is saved; OCR blocks come back with boxes in PDF points."""
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    path = pages_dir / f"p{page_no:03d}.png"
    page.get_pixmap(dpi=OCR_DPI).save(path)
    scale = 72 / OCR_DPI
    found = ocr.words(path)
    blocks = []
    for b in ocr.blocks(found):
        to_pt = lambda box: _box([v * scale for v in box])  # noqa: E731
        blocks.append({"type": "text", "page": page_no, "bbox": to_pt(b["box"]), "source": "ocr", "text": b["text"],
                       "confidence": b["confidence"],
                       "lines": [{"bbox": to_pt(ln["box"]), "spans": [{"text": ln["text"], "bbox": to_pt(ln["box"])}]}
                                 for ln in b["lines"]]})
    return blocks, {"render": f"pages/{path.name}", "ocr_confidence": ocr.summary(found)["confidence"],
                    "ocr_words": len(found)}


def reread_with_vision(blocks, render, page_no, budget):
    """Illustration pages: the vision model reads each OCR block again (Tesseract does badly on decorative
    lettering). Its reading becomes the block's text (source "vision"); Tesseract's stays as ocr_text. A block it
    finds no text in keeps an empty text: drawing or noise that Tesseract took for letters."""
    if not budget.available or not blocks:
        return
    from PIL import Image
    scale = OCR_DPI / 72
    with Image.open(render) as im, tempfile.TemporaryDirectory() as tmp:
        for i, block in enumerate(blocks, 1):
            box = pymupdf.Rect(block["bbox"]) + (-REREAD_PAD_PT, -REREAD_PAD_PT, REREAD_PAD_PT, REREAD_PAD_PT)
            crop = Path(tmp) / f"b{i}.png"
            im.crop(tuple(round(v * scale) for v in box)).save(crop)
            text = budget.read_text(crop, page_no, block["text"], block=f"p{page_no}-ocr{i}")
            if text is None:
                continue
            ocr_lines = block["lines"]
            new_lines = text.splitlines() if text else []
            block.update(ocr_text=block["text"], text=text, source="vision")
            block["lines"] = ([{**ln, "spans": [{**ln["spans"][0], "text": t}]} for ln, t in zip(ocr_lines, new_lines)]
                              if len(new_lines) == len(ocr_lines) else
                              [{"bbox": block["bbox"], "spans": [{"text": t, "bbox": block["bbox"]}]} for t in new_lines])


def _cell_text(words, cell):
    """The words whose centre is in the cell, line by line. (PyMuPDF's own table text can drop the spaces,
    e.g. on pages that were rotated; the words of the text layer keep them.)"""
    r = pymupdf.Rect(cell)
    lines = {}
    for x0, y0, x1, y1, word, block, line, _ in words:
        if r.contains(pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)):
            lines.setdefault((block, line), []).append(word)
    return "\n".join(" ".join(ws) for ws in lines.values())


def _flat_copy(page):
    """The page alone, with its rotation taken off (content in the page's own coordinates). find_tables on a
    rotated page answers in another frame than the text and images; on this copy it agrees with them."""
    copy = pymupdf.open()
    copy.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    copy[0].set_rotation(0)
    return copy


def _grid(table, rotation_matrix):
    """The table's cells as rows of boxes, in reading order on the page as displayed (on a rotated page, the
    table's own rows run down the displayed columns)."""
    if rotation_matrix.is_rectilinear and rotation_matrix.b == 0:  # not rotated: the table's rows as they are
        return [list(row.cells) for row in table.rows]
    cells = [(pymupdf.Rect(c) * rotation_matrix, pymupdf.Rect(c)) for row in table.rows for c in row.cells if c]
    cells.sort(key=lambda dc: (round(dc[0].y0), dc[0].x0))
    rows = []
    for shown, native in cells:
        if rows and abs(shown.y0 - rows[-1][0][0].y0) <= 2:
            rows[-1].append((shown, native))
        else:
            rows.append([(shown, native)])
    return [[native for _, native in sorted(row, key=lambda dc: dc[0].x0)] for row in rows]


def tables(page, page_no, out_dir):
    flat = _flat_copy(page) if page.rotation else None
    source = flat[0] if flat else page  # kept in a variable: the tables refer back to it while they're read
    try:
        found = source.find_tables().tables
    except Exception:  # find_tables can trip on odd drawings; text and images still come out
        return []
    words = page.get_text("words")
    out = []
    for i, table in enumerate(found, 1):
        grid = _grid(table, page.rotation_matrix)
        rows = [[_cell_text(words, cell) if cell else "" for cell in row] for row in grid]
        if not rows or not any(any(c.strip() for c in row) for row in rows):
            continue
        if max(len(r) for r in rows) < 2:  # one column is a framed list or box of text, not a table
            continue
        tdir = out_dir / "tables"
        tdir.mkdir(exist_ok=True)
        name = f"p{page_no:03d}-t{i}.csv"
        with (tdir / name).open("w", newline="") as f:
            csv.writer(f).writerows(rows)
        header = getattr(table, "header", None)
        has_header = header is not None and not getattr(header, "external", False)
        out.append({"type": "table", "page": page_no, "bbox": _box(table.bbox), "file": f"tables/{name}",
                    "rows": len(rows), "cols": max(len(r) for r in rows),
                    "header": [c.replace("\n", " ") for c in rows[0]] if has_header else None,
                    "cells": [[_box(c) if c else None for c in row] for row in grid]})
    return out


def _to_displayed(el, m):
    """Boxes (and text direction) of an element found in the page's own coordinates, as the page is displayed."""
    shown = lambda b: _box(pymupdf.Rect(b) * m)  # noqa: E731
    el["bbox"] = shown(el["bbox"])
    for line in el.get("lines", []):
        line["bbox"] = shown(line["bbox"])
        if "dir" in line:
            dx, dy = line["dir"]
            line["dir"] = [round(dx * m.a + dy * m.c, 3) + 0.0, round(dx * m.b + dy * m.d, 3) + 0.0]
        for span in line["spans"]:
            span["bbox"] = shown(span["bbox"])
    if el.get("cells"):
        el["cells"] = [[shown(c) if c else None for c in row] for row in el["cells"]]
    for a in el.get("attempts", []):
        a["bbox"] = shown(a["bbox"])


def reading_order(elements):
    """Top to bottom, then left to right (in 4-pt bands). Text inside a table comes right after the table."""
    tables_by_file = {e["file"]: e for e in elements if e["type"] == "table"}

    def key(e):
        own = (round(e["bbox"][1] / 4), e["bbox"][0])
        table = tables_by_file.get(e.get("in_table"))
        return (round(table["bbox"][1] / 4), table["bbox"][0], 1, own) if table else (*own, 0, ())
    return sorted(elements, key=key)


def _box(b):
    return [round(float(v), 2) for v in b]


def _inside(inner, outer, share=0.6):
    r, o = pymupdf.Rect(inner), pymupdf.Rect(outer)
    return abs(r & o) >= share * max(1e-6, abs(r))


# ---------------------------------------------------------------- the document

def parse(pdf_path, out_root, vision=None, use_vision=True, progress=None):
    """Parse one PDF into ``out_root/<name>/``. -> the manifest (also written as manifest.json)."""
    pdf_path = Path(pdf_path)
    say = progress or (lambda msg: None)
    out = Path(out_root) / pdf_path.stem
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    budget = vision_mod.Budget(vision, out / "qa_log.jsonl", enabled=use_vision)
    doc = pymupdf.open(pdf_path)
    if doc.needs_pass:
        raise ValueError("the PDF is password-protected")
    imgs = images_mod.PageImages(doc, out, budget)
    pages, elements, text_out = [], [], []
    for index, page in enumerate(doc):
        n = index + 1
        say(f"page {n} of {doc.page_count}")
        rotation = page.rotation
        kind = triage(page)
        info = {"page": n, "width": round(page.rect.width, 2), "height": round(page.rect.height, 2),
                "rotation": rotation, "kind": kind}
        if kind == "ocr":
            texts, extra = ocr_page(page, n, out)
            scanned = extra["ocr_words"] >= SCANNED_MIN_WORDS and (extra["ocr_confidence"] or 0) >= SCANNED_MIN_CONF
            kind = "scanned" if scanned else "image"
            info.update(extra, kind=kind)
            if kind == "image":
                reread_with_vision(texts, out / extra["render"], n, budget)
            for t in texts:
                if t["source"] == "ocr":
                    t["low_confidence"] = (t["confidence"] or 0) < OCR_LOW_CONF
            found_tables = []
        else:
            texts = text_blocks(page, n)
            found_tables = tables(page, n, out)
        for t in texts:
            t["in_table"] = next((tb["file"] for tb in found_tables if _inside(t["bbox"], tb["bbox"])), None)
        # Text, tables and images are found in the page's own coordinates; OCR boxes are already as displayed.
        native_texts = [pymupdf.Rect(t["bbox"]) * (page.derotation_matrix if t["source"] != "text_layer"
                                                   else pymupdf.Identity) for t in texts]
        page_images = imgs.page(page, n, native_texts, skip_page_scans=kind == "scanned")
        for el in texts + found_tables + page_images:
            if el.get("source") not in ("ocr", "vision") and el.get("bbox"):
                _to_displayed(el, page.rotation_matrix)
        ordered = reading_order(texts + found_tables + page_images)
        for i, el in enumerate(ordered, 1):
            el["order"] = i
            el["id"] = el.get("id") if el["type"] == "image" else f"p{n}-{el['type']}{i}"
        elements.extend(ordered)
        pages.append(info)
        text_out.append(f"--- page {n} ---\n" + "\n\n".join(t["text"] for t in reading_order(texts) if t["text"]))
    imgs.finish()
    (out / "text.txt").write_text("\n\n".join(text_out) + "\n")
    manifest = {
        "schema": SCHEMA,
        "units": "PDF points (1/72 in); bbox is [x0, y0, x1, y1] from the top-left corner of the page as it is "
                 "displayed (its rotation applied; pages[].rotation is the rotation the PDF had)",
        "source": {"file": pdf_path.name, "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                   "pages": doc.page_count, "metadata": {k: v for k, v in (doc.metadata or {}).items() if v}},
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "pages": pages,
        "elements": elements,
        "summary": summarize(pages, elements, budget),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    doc.close()
    return manifest


def summarize(pages, elements, budget):
    imgs = [e for e in elements if e["type"] == "image"]
    count = lambda status: sum(e["status"] == status for e in imgs)  # noqa: E731
    return {"pages": len(pages), "text_pages": sum(p["kind"] == "text" for p in pages),
            "scanned_pages": sum(p["kind"] == "scanned" for p in pages),
            "image_pages": sum(p["kind"] == "image" for p in pages),
            "text_blocks": sum(e["type"] == "text" for e in elements),
            "tables": sum(e["type"] == "table" for e in elements),
            "images_kept": count("kept"), "images_needs_review": count("needs_review"),
            "images_duplicate": count("duplicate"), "images_merged": count("merged"), "images_small": count("small"),
            "vision": budget.summary()}


# ---------------------------------------------------------------- vision statistics

def stats(root):
    """Read every qa_log.jsonl under ``root``: how often tier 2 was needed, whether it answered, what it decided.

    ``kept_by_vision``: images tier 1 flagged that the vision model passed (without it they'd be needs_review or
    retried). ``needs_review_without_vision``: images that went to review because it wasn't there to ask.
    """
    s = {"pdfs": 0, "images": 0, "flagged_by_tier1": 0, "needed_vision": 0, "vision_requests": 0, "text_rereads": 0, "vision_failed": 0,
         "pdfs_that_stopped_asking": 0, "kept_by_vision": 0, "needs_review": 0, "needs_review_without_vision": 0}
    ms = []
    for log in Path(root).rglob("qa_log.jsonl"):
        s["pdfs"] += 1
        for line in log.read_text().splitlines():
            e = json.loads(line)
            if e["event"] == "vision":
                s["vision_requests"] += 1
                s["text_rereads"] += e.get("task") == "read_text"
                s["vision_failed"] += not e["ok"]
                if e["ok"]:
                    ms.append(e["ms"])
            elif e["event"] == "vision_stopped":
                s["pdfs_that_stopped_asking"] += 1
            elif e["event"] == "image":
                s["images"] += 1
                s["flagged_by_tier1"] += bool(e["first_flags"])
                s["needed_vision"] += e["vision_needed"] > 0
                s["kept_by_vision"] += bool(e["first_flags"]) and e["vision_ok"] and e["status"] == "kept"
                s["needs_review"] += e["status"] == "needs_review"
                s["needs_review_without_vision"] += (e["status"] == "needs_review"
                                                     and e["vision_answered"] < e["vision_needed"])
    ms.sort()
    s["vision_median_ms"] = ms[len(ms) // 2] if ms else None
    s["vision_availability"] = (round(1 - s["vision_failed"] / s["vision_requests"], 3)
                                if s["vision_requests"] else None)
    return s


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("pdf", nargs="?", help="the PDF to parse")
    p.add_argument("-o", "--out", default=".", help="folder to put <name>/ in (default: here)")
    p.add_argument("--no-vision", action="store_true", help="tier-1 checks only; don't ask the vision model")
    p.add_argument("--stats", metavar="DIR", help="summarize the QA logs of every bundle under DIR")
    args = p.parse_args(argv)
    if args.stats:
        print(json.dumps(stats(args.stats), indent=1))
        return 0
    if not args.pdf:
        p.error("give a PDF (or --stats DIR)")
    if not ocr.available():
        print("warning: tesseract not found: scanned pages and image OCR are skipped", file=sys.stderr)
    vision = None
    if not args.no_vision:
        vision = vision_mod.Vision.from_env()
        ok, message = vision.check()
        print(f"vision: {message}", file=sys.stderr)
        if ok is False:
            print("vision: carrying on without it; images that look wrong go to needs_review", file=sys.stderr)
    manifest = parse(args.pdf, args.out, vision, use_vision=not args.no_vision,
                     progress=lambda msg: print(msg, file=sys.stderr))
    print(json.dumps(manifest["summary"], indent=1))
    print(Path(args.out) / Path(args.pdf).stem)
    return 0


if __name__ == "__main__":
    sys.exit(main())

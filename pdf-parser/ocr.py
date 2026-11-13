"""Tesseract, run as a program: words with boxes and confidence, grouped into lines and blocks."""
from __future__ import annotations

import os
import shutil
import subprocess

LANG = os.environ.get("PDF_PARSER_OCR_LANG", "eng")


def available():
    return shutil.which("tesseract") is not None


def words(image_path, psm=3, lang=LANG):
    """-> [{text, conf (0-100), box (x0, y0, x1, y1) in pixels, block, line}]; [] when Tesseract is missing."""
    if not available():
        return []
    try:
        tsv = subprocess.run(["tesseract", str(image_path), "stdout", "-l", lang, "--psm", str(psm), "tsv"],
                             capture_output=True, text=True, check=True, timeout=300).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    out = []
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        x, y, w, h = (int(v) for v in f[6:10])
        out.append({"text": f[11], "conf": max(0.0, float(f[10])), "box": (x, y, x + w, y + h),
                    "block": (int(f[1]), int(f[2])), "line": (int(f[1]), int(f[2]), int(f[3]), int(f[4]))})
    return out


def summary(found):
    """-> {text, confidence (mean word confidence, 0-100, or None), words}."""
    lines, current, key = [], [], None
    for w in found:
        if w["line"] != key and current:
            lines.append(" ".join(current))
            current = []
        key = w["line"]
        current.append(w["text"])
    if current:
        lines.append(" ".join(current))
    conf = round(sum(w["conf"] for w in found) / len(found), 1) if found else None
    return {"text": "\n".join(lines), "confidence": conf, "words": len(found)}


def blocks(found):
    """Words grouped into Tesseract's blocks: [{box, text, confidence, lines: [{box, text}]}], in reading order."""
    grouped = {}
    for w in found:
        grouped.setdefault(w["block"], {}).setdefault(w["line"], []).append(w)
    out = []
    for lines in grouped.values():
        all_words = [w for ws in lines.values() for w in ws]
        out.append({
            "box": _union(w["box"] for w in all_words),
            "text": "\n".join(" ".join(w["text"] for w in ws) for ws in lines.values()),
            "confidence": round(sum(w["conf"] for w in all_words) / len(all_words), 1),
            "lines": [{"box": _union(w["box"] for w in ws), "text": " ".join(w["text"] for w in ws)}
                      for ws in lines.values()],
        })
    return out


def _union(boxes):
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

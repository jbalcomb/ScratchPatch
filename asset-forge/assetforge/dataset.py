"""Build a LoRA training set from the exported originals: each image enlarged with crisp pixels, its transparent
parts filled with a flat key colour, and a caption that starts with its bucket's trigger word.

    python3 -m assetforge.dataset out/originals out/dataset
    python3 -m assetforge.dataset out/originals out/dataset --bucket figure --bucket unit_icon --frames all

The point of the key colour: an image model can't draw transparency, and SDXL ignores "plain magenta background"
in a prompt. Trained on originals that always sit on one flat colour, it learns to draw that colour, and
assetforge.pixelize can then key it out exactly. Each bucket gets the key colour farthest from every colour its
originals use (magenta clashes with purple dragons, green with green banners).

Output, in the folder layout kohya_ss and OneTrainer read:
    OUT/img/<repeats>_<token>/<archive>_<entry>_<frame>.png and .txt (caption)
    OUT/manifest.csv     one row per image: where it came from and where its caption came from
    OUT/buckets.json     per bucket: trigger word, key colour, images, repeats
Repeats even out the buckets: a bucket with fewer images than --balance is repeated up to about that many.

Captions use, in order: the vision model's prompt (describe.json from assetforge.describe), the game-data hint
(hints.csv from assetforge.remom_hints), or the bucket's plain noun. Identical images (the game has many, such as
the combat walls in two archives) are kept once.
"""

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from .buckets import BUCKETS, bucket_of
from .generate import model_size

KEYS = {"magenta": (255, 0, 255), "cyan": (0, 255, 255), "green": (0, 255, 0), "yellow": (255, 255, 0),
        "blue": (0, 0, 255), "red": (255, 0, 0)}
MIN_SIDE = 6                 # smaller pictures (dots, 1x1 spacers, thin bars) teach nothing
MIN_COLOUR_SHARE = 0.0005    # colours rarer than this in a bucket don't count against a key colour


def background_phrase(key):
    return f"isolated on a plain flat {key} background"


def load_rgba(path):
    """RGB array and a drawn mask (any alpha) for an exported frame."""
    with Image.open(path) as img:
        a = np.asarray(img.convert("RGBA"))
    return a[:, :, :3], a[:, :, 3] > 0


def pick_key(colour_counts):
    """The key colour whose nearest commonly used colour is farthest away. colour_counts: Counter of (r, g, b)."""
    total = sum(colour_counts.values()) or 1
    used = np.array([c for c, n in colour_counts.items() if n / total >= MIN_COLOUR_SHARE] or [(0, 0, 0)], int)

    def nearest(rgb):
        return np.sqrt(((used - np.array(rgb)) ** 2).sum(axis=1)).min()
    return max(KEYS, key=lambda k: nearest(KEYS[k]))


def enlarge(rgb, drawn, key_rgb):
    """Enlarge by the biggest whole number that fits the SDXL training size closest in shape (nearest neighbour, so
    pixels stay square and sharp). With transparency, centre it on a canvas of exactly that size in the key colour,
    which keeps the trainer from resizing it; fully drawn pictures are just enlarged."""
    h, w = drawn.shape
    cw, ch = model_size(w, h)
    k = max(1, min(cw // w, ch // h))
    img = Image.fromarray(np.where(drawn[:, :, None], rgb, np.array(key_rgb, np.uint8)).astype(np.uint8), "RGB")
    img = img.resize((w * k, h * k), Image.NEAREST)
    if drawn.all():
        return img, k
    canvas = Image.new("RGB", (cw, ch), key_rgb)
    canvas.paste(img, ((cw - w * k) // 2, (ch - h * k) // 2))
    return canvas, k


def has_magenta(rgb, drawn):
    """Whether the drawn pixels include anything magenta-like (strong red and blue, little green)."""
    px = rgb[drawn].astype(int)
    return bool(((px[:, 0] > 150) & (px[:, 2] > 150) & (px[:, 1] < 100)).any())


def caption(bucket, key, desc, hint, transparent, magenta_drawn=True):
    """(caption text, where it came from). The vision model saw transparency as magenta, so unless the picture itself
    has magenta in it (magenta_drawn), a description's "magenta" means the transparent parts: the key colour here."""
    if desc:
        phrases = [p.strip() for p in desc["prompt"].replace("transparent background", "").split(",") if p.strip()]
        text, source = ", ".join(phrases), "describe"
        if not magenta_drawn and key != "magenta":
            text = re.sub(r"\bmagenta\b", key, text, flags=re.I)
    elif hint:
        text, source = hint, "hint"
    else:
        text, source = BUCKETS[bucket].noun, "bucket"
    parts = [BUCKETS[bucket].token, text] + ([background_phrase(key)] if transparent else [])
    return ", ".join(parts), source


def read_catalog(export_dir):
    with open(export_dir / "catalog.csv", newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["kind"] == "sprite"]
    for r in rows:
        if not r.get("bucket"):
            r["bucket"] = bucket_of(r["archive"], int(r["width"]), int(r["height"]))
    return rows


def read_hints(export_dir):
    path = export_dir / "hints.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {(r["archive"], int(r["entry"])): r["hint"] for r in csv.DictReader(f)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("originals", type=Path, help="output folder of assetforge.export")
    ap.add_argument("out", type=Path, help="folder for the training set (replaced)")
    ap.add_argument("--bucket", action="append", choices=sorted(BUCKETS), help="only this bucket (repeatable)")
    ap.add_argument("--frames", choices=["first", "all"], default="first",
                    help="first frame of each sprite (default), or every animation frame")
    ap.add_argument("--balance", type=int, default=500,
                    help="repeat smaller buckets up to about this many images per epoch (0: no repeats)")
    args = ap.parse_args(argv)

    rows = [r for r in read_catalog(args.originals) if not args.bucket or r["bucket"] in args.bucket]
    rows = [r for r in rows if min(int(r["width"]), int(r["height"])) >= MIN_SIDE]
    hints = read_hints(args.originals)

    # Pass 1: the frames to use, without duplicates, and the colours each bucket uses.
    seen, items, colours = set(), defaultdict(list), defaultdict(Counter)
    duplicates = 0
    for r in rows:
        folder = args.originals / r["folder"]
        frames = sorted(folder.glob("frame_[0-9][0-9].png"))
        for frame in frames[:1] if args.frames == "first" else frames:
            rgb, drawn = load_rgba(frame)
            if not drawn.any():
                continue
            digest = hashlib.sha1(rgb.tobytes() + drawn.tobytes()).digest()
            if digest in seen:
                duplicates += 1
                continue
            seen.add(digest)
            items[r["bucket"]].append((r, frame, rgb, drawn))
            colours[r["bucket"]].update(map(tuple, rgb[drawn].tolist()))

    if args.out.exists():
        shutil.rmtree(args.out / "img", ignore_errors=True)
    args.out.mkdir(parents=True, exist_ok=True)
    manifest, summary = [], {}
    for name in BUCKETS:
        if not items[name]:
            continue
        key = pick_key(colours[name])
        count = len(items[name])
        repeats = max(1, round(args.balance / count)) if args.balance else 1
        token = BUCKETS[name].token
        folder = args.out / "img" / f"{repeats}_{token}"
        folder.mkdir(parents=True)
        sources = Counter()
        for r, frame, rgb, drawn in items[name]:
            desc_file = args.originals / r["folder"] / "describe.json"
            desc = json.loads(desc_file.read_text(encoding="utf-8")) if desc_file.exists() else None
            transparent = not drawn.all()
            image, scale = enlarge(rgb, drawn, KEYS[key])
            text, source = caption(name, key, desc, hints.get((r["archive"], int(r["entry"]))), transparent,
                                   has_magenta(rgb, drawn))
            stem = f"{r['archive']}_{int(r['entry']):04d}_{frame.stem.removeprefix('frame_')}"
            image.save(folder / f"{stem}.png")
            (folder / f"{stem}.txt").write_text(text + "\n", encoding="utf-8")
            sources[source] += 1
            manifest.append({"file": (folder / f"{stem}.png").relative_to(args.out).as_posix(), "bucket": name,
                             "archive": r["archive"], "entry": r["entry"], "frame": frame.name,
                             "width": r["width"], "height": r["height"], "scale": scale,
                             "transparent": int(transparent), "caption_source": source})
        summary[name] = {"token": token, "key": key, "key_rgb": KEYS[key], "images": count, "repeats": repeats,
                         "captions": dict(sources)}
        print(f"{name:15} {count:5} images x{repeats:<3} key {key:8} captions {dict(sources)}")

    with open(args.out / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(manifest[0]) if manifest else ["file"])
        w.writeheader()
        w.writerows(manifest)
    (args.out / "buckets.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"{len(manifest)} images ({duplicates} duplicates skipped) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

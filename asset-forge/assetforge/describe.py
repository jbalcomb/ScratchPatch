"""Build step 2: for each exported sprite, ask a vision model for a description and a prompt that would
make an image model draw it again.

    python3 -m assetforge.describe out/originals --archive SPELLS --limit 20
    python3 -m assetforge.describe out/originals --endpoint http://localhost:8080/v1 --model qwen2.5-vl

Reads catalog.csv and the sprite sheets written by assetforge.export, and hints.csv if there is one (what each
sprite is, from the game's data: see assetforge.remom_hints). Writes describe.json in each sprite's
folder and appends one line per sprite to descriptions.jsonl. Sprites that already have describe.json are
skipped, so an interrupted run carries on where it stopped (--redo to describe them again).
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from PIL import Image

from .buckets import BUCKETS, bucket_of
from .vision import DEFAULT_ENDPOINT, VisionError, chat, parse_json_reply

PROMPT_VERSION = 2
MIN_SIDE = 384          # small sprites are enlarged (nearest neighbour) so the model can see the pixels
MAX_SIDE = 1024
LARGEST = 2048          # bigger sheets (long animations of big sprites) are shrunk to fit

SYSTEM = """You are an art director cataloguing the sprites of a 1994 fantasy strategy game (Master of Magic)
so new, original replacement art can be made for every one. You see each sprite enlarged with hard pixel
edges; the checkerboard or empty areas are transparent. Describe only what is drawn. Answer with one JSON
object and nothing else."""

REQUEST = """Sprite {archive}.LBX entry {entry}. Name in the archive: "{name}". Archive note: "{description}".
(Archive names are 8-letter abbreviations and can mislead; go by the picture and by the known identity below.)
Size {width}x{height} pixels, {frames} animation frame(s) shown left to right.

Return JSON with these keys:
  "subject": a few words naming what this is (e.g. "dwarf swordsman unit", "grassland terrain tile", "fire spell icon", "stone UI frame")
  "category": one of unit, hero, wizard_portrait, terrain, city, building, spell_icon, spell_effect, item, ui, background, font, cursor, other
  "description": 2-4 sentences: what is depicted, pose and view angle, colours, lighting, notable details
  "animation": what changes between frames, or "" for one frame
  "style": one sentence on rendering style (pixel art, shading, outline, dithering, palette)
  "prompt": a text-to-image prompt that would reproduce this as closely as possible: subject first, then composition, colours, style; include "transparent background" when the background is empty
  "negative_prompt": things the image model should avoid
  "tags": a JSON list of 5-15 short lowercase tags
Do not mention the magenta background or transparency in description, style or tags."""

REQUIRED = ("subject", "category", "description", "prompt")
# A bad reply is asked for again once, less greedily and penalising repeats: in the first full run the failures were
# the model stuck repeating itself.
ATTEMPTS = [{}, {"temperature": 0.7, "extra": {"repeat_penalty": 1.15}}]


def enlarge(image):
    w, h = image.size
    scale = max(1, min(MAX_SIDE // max(w, h), -(-MIN_SIDE // max(w, h))))
    out = image.resize((w * scale, h * scale), Image.NEAREST)
    if max(out.size) > LARGEST:
        f = LARGEST / max(out.size)
        out = out.resize((max(1, round(out.width * f)), max(1, round(out.height * f))), Image.LANCZOS)
    bg = Image.new("RGBA", out.size, (255, 0, 255, 255))      # magenta shows the model what is transparent
    bg.alpha_composite(out)
    return bg.convert("RGB")


def describe_sprite(row, folder, args):
    with Image.open(folder / "sheet.png") as sheet:
        image = enlarge(sheet.convert("RGBA"))
    text = REQUEST.format(**row) + "\nTransparent pixels are shown as solid magenta (255, 0, 255)."
    hint = args.hints.get((row["archive"], int(row["entry"])))
    if hint:
        text += f"\nKnown identity, from the game's data (trust it): {hint}."
    started = time.time()
    for attempt, extra in enumerate(ATTEMPTS, 1):
        try:
            reply = chat(args.endpoint, args.model, SYSTEM, text, [image], json_mode=not args.no_json_mode,
                         max_tokens=args.max_tokens, **extra)
            result = parse_json_reply(reply)
            missing = [k for k in REQUIRED if not result.get(k)]
            if missing:
                raise VisionError(f"reply is missing {', '.join(missing)}", reply=reply)
            break
        except VisionError as e:
            if e.reply is None or attempt == len(ATTEMPTS):     # unreachable server, or out of attempts
                raise
    if isinstance(result.get("tags"), str):
        result["tags"] = [t.strip() for t in result["tags"].split(",") if t.strip()]
    return {
        "archive": row["archive"], "entry": int(row["entry"]), "name": row["name"], "folder": row["folder"],
        "width": int(row["width"]), "height": int(row["height"]), "frames": int(row["frames"]),
        **result,
        "hint": hint or "",
        "provenance": {"model": args.model, "endpoint": args.endpoint, "prompt_version": PROMPT_VERSION,
                       "seconds": round(time.time() - started, 1), "attempts": attempt,
                       "image_size": list(image.size),
                       "when": time.strftime("%Y-%m-%dT%H:%M:%S")},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir", type=Path, help="output folder of assetforge.export")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"OpenAI-compatible API base (default {DEFAULT_ENDPOINT})")
    ap.add_argument("--model", default="local", help="model name to send (llama-server ignores it)")
    ap.add_argument("--archive", action="append", help="only this archive, e.g. SPELLS (repeatable)")
    ap.add_argument("--bucket", action="append", choices=sorted(BUCKETS), help="only this bucket (repeatable)")
    ap.add_argument("--skip-bucket", action="append", choices=sorted(BUCKETS), help="leave out this bucket (repeatable)")
    ap.add_argument("--limit", type=int, help="describe at most this many sprites")
    ap.add_argument("--redo", action="store_true", help="describe sprites again even if describe.json exists")
    ap.add_argument("--no-json-mode", action="store_true", help="don't ask the server for JSON output mode")
    ap.add_argument("--max-tokens", type=int, default=1024,
                    help="longest reply allowed (default 1024; a good one is about 300)")
    args = ap.parse_args(argv)

    with open(args.export_dir / "catalog.csv", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["kind"] == "sprite"]
    args.hints = {}
    if (args.export_dir / "hints.csv").exists():      # written by assetforge.remom_hints, or by hand
        with open(args.export_dir / "hints.csv", encoding="utf-8") as f:
            args.hints = {(r["archive"], int(r["entry"])): r["hint"] for r in csv.DictReader(f)}
    if args.archive:
        wanted = {a.upper().removesuffix(".LBX") for a in args.archive}
        rows = [r for r in rows if r["archive"] in wanted]
    if args.bucket or args.skip_bucket:
        for r in rows:
            r["bucket"] = r.get("bucket") or bucket_of(r["archive"], int(r["width"]), int(r["height"]))
        rows = [r for r in rows if (not args.bucket or r["bucket"] in args.bucket)
                and r["bucket"] not in (args.skip_bucket or [])]

    done = failed = skipped = 0
    seconds = 0.0
    with open(args.export_dir / "descriptions.jsonl", "a", encoding="utf-8") as log:
        for row in rows:
            if args.limit is not None and done + failed >= args.limit:
                break
            folder = args.export_dir / row["folder"]
            out = folder / "describe.json"
            if out.exists() and not args.redo:
                skipped += 1
                continue
            label = f"{row['archive']}[{row['entry']}] {row['name']}"
            started = time.time()
            try:
                result = describe_sprite(row, folder, args)
            except VisionError as e:
                failed += 1
                if e.reply is not None:
                    (folder / "describe_failed.txt").write_text(f"{e}\n\n{e.reply}\n", encoding="utf-8")
                print(f"{label}: FAILED {e} ({time.time() - started:.1f}s)", flush=True)
                if "cannot reach" in str(e):
                    break
                continue
            out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            (folder / "describe_failed.txt").unlink(missing_ok=True)
            log.write(json.dumps(result) + "\n")
            log.flush()
            done += 1
            seconds += result["provenance"]["seconds"]
            print(f"{label}: {result['subject']} ({result['provenance']['seconds']}s)", flush=True)
    average = f", {seconds / done:.1f}s each" if done else ""
    print(f"{done} described{average}, {failed} failed, {skipped} already done")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    sys.exit(main())

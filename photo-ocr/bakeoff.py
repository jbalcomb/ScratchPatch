#!/usr/bin/env python3
"""Receipt parsing bake-off: run each candidate engine on hand-labeled receipts, score them, compare.

Phase 1 of PRD.md. The model and OCR calls are in parsing.py, shared with the service.

    python bakeoff.py add ~/Downloads/IMG_1.jpg ~/Downloads/IMG_2.jpg --tags thermal,multi-photo
    python bakeoff.py run qwen3-vl-8b            # or: run --all
    python bakeoff.py label qwen3-vl-8b          # drafts label.draft.json from an engine's output
    python bakeoff.py check                      # sanity-check the hand labels
    python bakeoff.py score                      # results table -> results/report.md
"""
import argparse
import difflib
import json
import os
import re
import shutil
import statistics
import sys
from datetime import date, datetime
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parsing import (OCR, PROMPT, SCHEMA, chat, extract_json, image_part, load_engines, norm_item,  # noqa: E402,F401
                     norm_store, normalize, ocr_photos, ocr_text, parse_receipt, sum_check, to_cents, to_date)

HERE = Path(__file__).resolve().parent
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
NAME_THRESHOLD = 0.6   # line-item name similarity (0..1) needed to count as the same item
PRICE_SLACK = 1        # cents: line-item prices this close count as equal


def home():
    return Path(os.environ.get("RECEIPTS_BAKEOFF_HOME") or Path.home() / "receipts" / "bakeoff")


# ---------------------------------------------------------------- scoring

def match_items(truth, predicted, threshold=NAME_THRESHOLD):
    """Pair line items: names at least `threshold` similar and prices within PRICE_SLACK. Best pairs first."""
    pairs = []
    for i, t in enumerate(truth):
        for j, p in enumerate(predicted):
            if t["price"] is None or p["price"] is None or abs(t["price"] - p["price"]) > PRICE_SLACK:
                continue
            ratio = difflib.SequenceMatcher(None, norm_item(t["name"]), norm_item(p["name"])).ratio()
            if ratio >= threshold:
                pairs.append((ratio, i, j))
    used_t, used_p, matched = set(), set(), []
    for ratio, i, j in sorted(pairs, reverse=True):
        if i not in used_t and j not in used_p:
            used_t.add(i), used_p.add(j)
            matched.append((i, j, ratio))
    return matched


def score(label, parsed, threshold=NAME_THRESHOLD):
    truth, pred = normalize(label), normalize(parsed)
    stores = truth["store"] if isinstance(truth["store"], list) else [truth["store"]]
    matched = match_items(truth["items"], pred["items"], threshold)
    s = {
        "total": pred["total"] is not None and pred["total"] == truth["total"],
        "date": pred["date"] is not None and pred["date"] == truth["date"],
        "store": bool(pred["store"]) and norm_store(pred["store"]) in {norm_store(x) for x in stores if x},
        "tax": (pred["tax"] or 0) == (truth["tax"] or 0),
        "items_matched": len(matched),
        "items_truth": len(truth["items"]),
        "items_pred": len(pred["items"]),
        "sum_check": sum_check(pred),
    }
    s["items_all"] = s["items_matched"] == s["items_truth"] == s["items_pred"]
    s["all"] = s["total"] and s["date"] and s["store"] and s["tax"] and s["items_all"]
    return s


# ---------------------------------------------------------------- test set

def receipts(set_dir):
    """Receipt folders in the test set, in order: each holds its photos (in name order) and label.json."""
    if not set_dir.is_dir():
        return []
    return sorted(d for d in set_dir.iterdir() if d.is_dir() and photos(d))


def photos(receipt_dir):
    return sorted(p for p in receipt_dir.iterdir() if p.suffix.lower() in PHOTO_EXTS)


def load_label(receipt_dir):
    path = receipt_dir / "label.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def cmd_add(args):
    """Copy one receipt's photos (in the order given) into a new numbered folder of the test set."""
    set_dir = args.home / "set"
    set_dir.mkdir(parents=True, exist_ok=True)
    taken = [int(m.group(1)) for d in set_dir.iterdir() if (m := re.fullmatch(r"r(\d+)", d.name))]
    dest = set_dir / f"r{max(taken, default=0) + 1:03d}"
    dest.mkdir()
    for n, src in enumerate(args.photos, 1):
        src = Path(src)
        if src.suffix.lower() not in PHOTO_EXTS:
            sys.exit(f"{src}: not a photo this harness reads ({', '.join(sorted(PHOTO_EXTS))}); convert HEIC to JPEG first")
        shutil.copy2(src, dest / f"{n}{src.suffix.lower()}")
    if args.tags:
        (dest / "tags.txt").write_text("\n".join(t.strip() for t in args.tags.split(",") if t.strip()) + "\n")
    print(f"{dest}  ({len(args.photos)} photo{'s' if len(args.photos) != 1 else ''}); label it in {dest / 'label.json'}")


def tags(receipt_dir):
    path = receipt_dir / "tags.txt"
    found = path.read_text().split() if path.exists() else []
    if len(photos(receipt_dir)) > 1 and "multi-photo" not in found:
        found.append("multi-photo")
    return found


def cmd_check(args):
    """Catch labeling mistakes: unparseable values, implausible dates, items + tax != total."""
    problems = 0
    rs = receipts(args.home / "set")
    for r in rs:
        label = load_label(r)
        if label is None:
            print(f"{r.name}: no label.json yet")
            problems += 1
            continue
        rec, raw = normalize(label), label
        issues = []
        for field in ("total", "tax"):
            if raw.get(field) not in (None, "") and rec[field] is None:
                issues.append(f"{field} {raw.get(field)!r} isn't an amount")
        if rec["total"] is None:
            issues.append("no total")
        if rec["date"] is None:
            issues.append(f"date {raw.get('date')!r} doesn't parse (use YYYY-MM-DD)")
        elif not date(2000, 1, 1) <= date.fromisoformat(rec["date"]) <= date.today():
            issues.append(f"date {rec['date']} is implausible")
        if not rec["store"]:
            issues.append("no store")
        for n, it in enumerate(rec["items"], 1):
            if it["price"] is None or not it["name"]:
                issues.append(f"item {n} needs a name and a price")
        if not rec["items"]:
            issues.append("no items")
        elif sum_check(rec) is False:
            total = sum(i["price"] for i in rec["items"]) + (rec["tax"] or 0)
            issues.append(f"items + tax = {total / 100:.2f}, total = {rec['total'] / 100:.2f}")
        if issues:
            problems += 1
            print(f"{r.name}: " + "; ".join(issues))
    print(f"{len(rs)} receipts, {problems} with problems")
    return 1 if problems else 0


def cmd_label(args):
    """Start labels from an engine's output, so hand-labeling is correcting rather than typing."""
    engine_dir = args.home / "results" / args.engine
    n = 0
    for r in receipts(args.home / "set"):
        run_file = engine_dir / f"{r.name}.json"
        if (r / "label.json").exists() or not run_file.exists():
            continue
        parsed = json.loads(run_file.read_text()).get("parsed")
        if not parsed:
            continue
        rec = normalize(parsed)
        draft = {
            "store": rec["store"], "date": rec["date"],
            "tax": None if rec["tax"] is None else f"{rec['tax'] / 100:.2f}",
            "total": None if rec["total"] is None else f"{rec['total'] / 100:.2f}",
            "items": [{"name": i["name"], "quantity": i["quantity"],
                       "price": None if i["price"] is None else f"{i['price'] / 100:.2f}"} for i in rec["items"]],
        }
        (r / "label.draft.json").write_text(json.dumps(draft, indent=2) + "\n")
        n += 1
        print(f"{r / 'label.draft.json'}")
    print(f"{n} drafts; correct each against the photos, then rename it to label.json")


# ---------------------------------------------------------------- running engines
def run_engine(engine, set_dir, out_dir, force=False, only=None, log=print):
    """Run one engine over the test set; one JSON per receipt (the engine_runs row of PRD.md), kept between runs.

    Two-step engines OCR every receipt first and then structure every receipt, so with one model loaded at a
    time (llama-swap) the GPU swaps models once per run instead of once per receipt."""
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [r for r in receipts(set_dir) if (not only or r.name in only)
            and (force or not (out_dir / f"{r.name}.json").exists())]
    if engine["kind"] == "ocr_llm":
        for r in todo:
            ocr_file = out_dir / f"{r.name}.ocr.json"
            if ocr_file.exists() and not force:
                continue
            ocr = ocr_photos(engine, photos(r))
            ocr_file.write_text(json.dumps(ocr, indent=1))
            log(f"  {engine['name']} {r.name}: OCR {ocr['seconds']:.1f}s" + (f"  ERROR {ocr['error']}" if ocr.get("error") else ""))
    for r in todo:
        ocr = json.loads((out_dir / f"{r.name}.ocr.json").read_text()) if engine["kind"] == "ocr_llm" else None
        run = {"receipt": r.name, "photos": [p.name for p in photos(r)], **parse_receipt(engine, photos(r), ocr=ocr)}
        (out_dir / f"{r.name}.json").write_text(json.dumps(run, indent=1) + "\n")
        log(f"  {engine['name']} {r.name}: {run['seconds']:.1f}s" + (f"  ERROR {run['error']}" if run["error"] else ""))
    return len(todo)


def cmd_run(args):
    engines = load_engines(args.engines)
    names = list(engines) if args.all else args.engine
    if not names:
        sys.exit(f"name an engine ({', '.join(engines)}) or use --all")
    for name in names:
        if name not in engines:
            sys.exit(f"no engine {name!r} in {args.engines} ({', '.join(engines)})")
    for name in names:
        print(f"{name}:")
        n = run_engine(engines[name], args.home / "set", args.home / "results" / name, args.force, args.receipt)
        print(f"  {n} receipts run")


# ---------------------------------------------------------------- report

def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "–"


def collect(home, threshold=NAME_THRESHOLD):
    """{engine: {receipt: score or None (failed)}}, {engine: {receipt: seconds}}, labels, tags."""
    labeled = {r.name: (load_label(r), tags(r)) for r in receipts(home / "set") if load_label(r) is not None}
    scores, seconds = {}, {}
    results = home / "results"
    for engine_dir in sorted(d for d in results.iterdir() if d.is_dir()) if results.is_dir() else []:
        for rid, (label, _) in labeled.items():
            f = engine_dir / f"{rid}.json"
            if not f.exists():
                continue
            run = json.loads(f.read_text())
            scores.setdefault(engine_dir.name, {})[rid] = score(label, run["parsed"], threshold) if run.get("parsed") else None
            seconds.setdefault(engine_dir.name, {})[rid] = run.get("seconds")
    return scores, seconds, labeled


def engine_row(name, sc, secs, labeled):
    ok = [s for s in sc.values() if s]
    n = len(sc)
    matched = sum(s["items_matched"] for s in ok)
    truth = sum(len(normalize(labeled[rid][0])["items"]) for rid in sc)  # failed receipts count as all missed
    pred = sum(s["items_pred"] for s in ok)
    p, r = (matched / pred if pred else 0), (matched / truth if truth else 0)
    f1 = 2 * p * r / (p + r) if p + r else 0
    passed = [s for s in ok if s["sum_check"]]
    times = [t for t in secs.values() if t is not None]
    return [name, str(n), str(n - len(ok)),
            pct(sum(s["total"] for s in ok), n), pct(sum(s["date"] for s in ok), n),
            pct(sum(s["store"] for s in ok), n), pct(sum(s["tax"] for s in ok), n),
            f"{p:.2f} / {r:.2f} / {f1:.2f}", pct(sum(s["all"] for s in ok), n),
            pct(len(passed), n), pct(sum(s["all"] for s in passed), len(passed)),
            f"{statistics.median(times):.1f}" if times else "–", f"{statistics.fmean(times):.1f}" if times else "–"]


def table(header, rows):
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
                     + ["| " + " | ".join(r) + " |" for r in rows])


def report(home, threshold=NAME_THRESHOLD):
    scores, seconds, labeled = collect(home, threshold)
    if not labeled:
        return "No labeled receipts yet: add photos with `bakeoff.py add`, then write each label.json.\n"
    out = [f"# Bake-off results\n\n{datetime.now():%Y-%m-%d %H:%M} · {len(labeled)} labeled receipts · "
           f"item names match at similarity ≥ {threshold}\n"]
    header = ["Engine", "Receipts", "Failed", "Total", "Date", "Store", "Tax", "Items P / R / F1",
              "All right", "Sum check passes", "All right when it passes", "Median s", "Mean s"]
    out += ["## Per engine\n", table(header, [engine_row(e, scores[e], seconds[e], labeled) for e in scores]), "",
            "**Failed** = no usable JSON (error or unparseable). **All right** = total, date, store, tax and every line "
            "item correct. **Sum check** = the engine's own items + tax equal its total; the last column is how far "
            "that check alone can be trusted for auto-accept.\n"]

    all_tags = sorted({t for _, ts in labeled.values() for t in ts})
    if all_tags and scores:
        rows = []
        for e in scores:
            row = [e]
            for t in all_tags:
                ids = [rid for rid, (_, ts) in labeled.items() if t in ts and rid in scores[e]]
                good = sum(1 for rid in ids if scores[e][rid] and scores[e][rid]["total"])
                row.append(f"{pct(good, len(ids))} ({len(ids)})" if ids else "–")
            rows.append(row)
        out += ["## Total right, by receipt condition\n", table(["Engine"] + all_tags, rows), ""]

    if len(scores) > 1:
        rows = []
        for a, b in combinations(scores, 2):
            both = [rid for rid in scores[a] if rid in scores[b]]
            agree = [rid for rid in both if _agree(home, a, b, rid)]
            agree_right = sum(1 for rid in agree if scores[a][rid] and scores[a][rid]["total"] and scores[a][rid]["date"])
            differ = [rid for rid in both if rid not in agree]
            either = sum(1 for rid in differ if any(scores[x][rid] and scores[x][rid]["total"] for x in (a, b)))
            rows.append([f"{a} + {b}", str(len(both)), pct(len(agree), len(both)),
                         pct(agree_right, len(agree)), str(len(differ)), pct(either, len(differ))])
        out += ["## Engine pairs: is disagreement a useful signal?\n",
                table(["Pair", "Receipts", "Agree on total + date", "Right when they agree",
                       "Disagree", "One of them right"], rows), "",
                "If the pair is nearly always right when it agrees, agreement can auto-accept and disagreement can "
                "send a receipt to review (PRD.md, Confidence and review).\n"]

    rows = []
    for rid, (_, ts) in labeled.items():
        row = [rid, ", ".join(ts) or ""]
        for e in scores:
            s = scores[e].get(rid, "skip")
            row.append("–" if s == "skip" else "failed" if s is None else _cell(s))
        rows.append(row)
    out += ["## Per receipt\n", table(["Receipt", "Tags"] + list(scores), rows), "",
            "T total, D date, S store, X tax (✓ right, ✗ wrong), then line items matched / labeled / returned.\n"]
    return "\n".join(out)


def _cell(s):
    flags = "".join(k + ("✓" if s[f] else "✗") for k, f in (("T", "total"), ("D", "date"), ("S", "store"), ("X", "tax")))
    return f"{flags} {s['items_matched']}/{s['items_truth']}/{s['items_pred']}"


def _agree(home, a, b, rid):
    recs = []
    for e in (a, b):
        run = json.loads((home / "results" / e / f"{rid}.json").read_text())
        if not run.get("parsed"):
            return False
        recs.append(normalize(run["parsed"]))
    return all(r["total"] is not None and r["date"] for r in recs) and \
        recs[0]["total"] == recs[1]["total"] and recs[0]["date"] == recs[1]["date"]


def cmd_score(args):
    text = report(args.home, args.name_threshold)
    out = args.home / "results" / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"(saved to {out})")


# ---------------------------------------------------------------- command line

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--home", type=Path, default=home(),
                   help="test set and results (default ~/receipts/bakeoff, or RECEIPTS_BAKEOFF_HOME)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add one receipt (one or more photos, top to bottom) to the test set")
    a.add_argument("photos", nargs="+")
    a.add_argument("--tags", help="comma-separated conditions, e.g. thermal,faded,crumpled,long")
    a.set_defaults(func=cmd_add)
    r = sub.add_parser("run", help="run engines over the test set (results are kept; --force re-runs)")
    r.add_argument("engine", nargs="*")
    r.add_argument("--all", action="store_true")
    r.add_argument("--force", action="store_true")
    r.add_argument("--receipt", action="append", help="only this receipt (repeatable), e.g. r007")
    r.add_argument("--engines", default=os.environ.get("RECEIPTS_ENGINES") or HERE / "engines.json")
    r.set_defaults(func=cmd_run)
    lb = sub.add_parser("label", help="draft label.draft.json for unlabeled receipts from an engine's output")
    lb.add_argument("engine")
    lb.set_defaults(func=cmd_label)
    c = sub.add_parser("check", help="sanity-check the hand labels")
    c.set_defaults(func=cmd_check)
    s = sub.add_parser("score", help="score every engine's results against the labels; writes results/report.md")
    s.add_argument("--name-threshold", type=float, default=NAME_THRESHOLD)
    s.set_defaults(func=cmd_score)
    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())

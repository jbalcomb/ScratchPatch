#!/usr/bin/env python3
"""Receipt parsing bake-off: run each candidate engine on hand-labeled receipts, score them, compare.

Phase 1 of PRD.md. Standard library only; Pillow (optional) fixes phone-photo rotation and shrinks
photos before they go to a vision model. OCR engines need their own install (see README).

    python bakeoff.py add ~/Downloads/IMG_1.jpg ~/Downloads/IMG_2.jpg --tags thermal,multi-photo
    python bakeoff.py run qwen3-vl-8b            # or: run --all
    python bakeoff.py label qwen3-vl-8b          # drafts label.draft.json from an engine's output
    python bakeoff.py check                      # sanity-check the hand labels
    python bakeoff.py score                      # results table -> results/report.md
"""
import argparse
import base64
import difflib
import io
import json
import mimetypes
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
NAME_THRESHOLD = 0.6   # line-item name similarity (0..1) needed to count as the same item
PRICE_SLACK = 1        # cents: line-item prices this close count as equal
SUM_SLACK = 2          # cents: items + tax may differ from the total by this much (rounding)


def home():
    return Path(os.environ.get("RECEIPTS_BAKEOFF_HOME") or Path.home() / "receipts" / "bakeoff")


# ---------------------------------------------------------------- normalizing values

def to_cents(value):
    """12.34 / "12.34" / "$1,234.56" / "1.00-" / "(1.00)" -> integer cents; anything else -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(value * 100)
    s = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        s, negative = s[1:-1], True
    if s.endswith("-"):  # receipts print discounts as 1.00-
        s, negative = s[:-1], True
    if s.startswith("-"):
        s, negative = s[1:], not negative
    if not re.fullmatch(r"\d+(\.\d{1,2})?|\.\d{1,2}", s):
        return None
    whole, _, frac = s.partition(".")
    cents = int(whole or 0) * 100 + int((frac + "00")[:2])
    return -cents if negative else cents


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d", "%b %d %Y", "%d %b %Y")


def to_date(value):
    """A receipt date in any common US form -> "YYYY-MM-DD", or None."""
    if not value:
        return None
    s = re.sub(r"\s+", " ", str(value).strip().replace(",", ""))
    s = s.split("T")[0] if re.match(r"\d{4}-\d\d-\d\dT", s) else s
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def norm_store(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold().replace("&", "and"))


def norm_item(name):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(name or "").casefold()).split())


def normalize(record):
    """Model output or a hand label -> {store, date, tax, total, items[{name, quantity, price}]}, amounts in cents."""
    record = record if isinstance(record, dict) else {}
    items = []
    for it in record.get("items") or []:
        if not isinstance(it, dict):
            continue
        price = to_cents(it.get("price", it.get("line_total", it.get("total"))))
        items.append({"name": str(it.get("name") or "").strip(), "quantity": it.get("quantity"), "price": price})
    store = record.get("store")
    return {
        "store": store if isinstance(store, list) else (str(store).strip() if store else None),
        "date": to_date(record.get("date")),
        "tax": to_cents(record.get("tax")),
        "total": to_cents(record.get("total")),
        "items": items,
    }


def extract_json(text):
    """The first JSON object in a model's reply, tolerating code fences and chatter around it."""
    if text is None:
        raise ValueError("empty reply")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


# ---------------------------------------------------------------- scoring

def sum_check(rec):
    """Line items plus tax equal the total, within rounding. None when there is nothing to check."""
    if rec["total"] is None or not rec["items"] or any(i["price"] is None for i in rec["items"]):
        return None
    return abs(sum(i["price"] for i in rec["items"]) + (rec["tax"] or 0) - rec["total"]) <= SUM_SLACK


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


# ---------------------------------------------------------------- talking to models

PROMPT = """These photos are one store receipt, in order from top to bottom. They may overlap: a line that appears
in two photos is still one line, so list it once.

Reply with JSON only, in this shape:
{"store": "store name as printed", "date": "YYYY-MM-DD", "items": [{"name": "line text", "quantity": 1, "price": 3.49}],
 "subtotal": 0.00, "tax": 0.00, "total": 0.00}

- price is the amount printed for that line (the line total, not the unit price).
- Discounts, coupons and savings printed as their own lines are items with a negative price.
- Bottle deposits and bag fees are items.
- Leave out payment lines, change, card numbers and loyalty balances.
- Use null for anything you can't read; don't guess."""

TEXT_PROMPT = """Below is OCR text from one store receipt (it may come from several overlapping photos, separated by
"--- photo N ---"; a line repeated where photos overlap is still one line, so list it once).

""" + PROMPT.split("\n\n", 1)[1] + "\n\nOCR text:\n{text}"

SCHEMA = {
    "type": "object",
    "properties": {
        "store": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"]},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "quantity": {"type": ["number", "null"]}, "price": {"type": ["number", "null"]}},
            "required": ["name", "price"]}},
        "subtotal": {"type": ["number", "null"]},
        "tax": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"]},
    },
    "required": ["store", "date", "items", "tax", "total"],
}


def chat(engine, content, schema=None):
    """One request to an OpenAI-compatible chat endpoint (llama-server, or llama-swap in front of it)."""
    body = {
        **({"model": engine["model"]} if engine.get("model") else {}),
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": engine.get("max_tokens", 4096),
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if schema is not None and engine.get("json_schema", True):
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "receipt", "schema": schema}}
    if engine.get("logprobs"):
        body["logprobs"] = True
    headers = {"Content-Type": "application/json"}
    key = engine.get("api_key") or os.environ.get(engine.get("api_key_env", "LLM_API_KEY"))
    if key:
        headers["Authorization"] = f"Bearer {key}"
    endpoint = engine["url"].rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/v1/chat/completions"
    req = urllib.request.Request(endpoint, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=engine.get("timeout", 600)) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {endpoint}: {e.read()[:300].decode(errors='replace')}") from None
    return reply["choices"][0]["message"].get("content"), reply


def image_part(path, max_side=None):
    """A photo as a data: URL, upright (EXIF rotation applied) and, with max_side, shrunk."""
    data, mime = path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/jpeg"
    try:
        from PIL import Image, ImageOps
    except ImportError:
        Image = None
    if Image is not None:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            if max_side and max(im.size) > max_side:
                im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            data, mime = buf.getvalue(), "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}


# ---------------------------------------------------------------- OCR back ends (text plus per-line confidence)

def ocr_tesseract(paths, cfg):
    lines = []
    for path in paths:
        tsv = subprocess.run(["tesseract", str(path), "stdout", "--psm", str(cfg.get("psm", 4)), "tsv"],
                             capture_output=True, text=True, check=True).stdout
        cur, words = None, []
        for row in tsv.splitlines()[1:]:
            f = row.split("\t")
            if len(f) < 12 or f[0] != "5" or not f[11].strip():
                continue
            key = tuple(f[1:5])
            if key != cur and words:
                lines.append(_line(words))
                words = []
            cur = key
            words.append((f[11], float(f[10]) / 100))
        if words:
            lines.append(_line(words))
        lines.append(None)  # photo break
    return lines


def ocr_doctr(paths, cfg):
    from doctr.io import DocumentFile
    from doctr.models import ocr_predictor
    model = _cached("doctr", lambda: ocr_predictor(pretrained=True))
    lines = []
    for page in model(DocumentFile.from_images([str(p) for p in paths])).export()["pages"]:
        for block in page["blocks"]:
            for line in block["lines"]:
                lines.append(_line([(w["value"], w["confidence"]) for w in line["words"]]))
        lines.append(None)
    return lines


def ocr_paddleocr(paths, cfg):
    from paddleocr import PaddleOCR
    model = _cached("paddleocr", lambda: PaddleOCR(lang=cfg.get("lang", "en")))
    lines = []
    for path in paths:
        if hasattr(model, "predict"):  # PaddleOCR 3.x
            for res in model.predict(str(path)):
                res = getattr(res, "json", res)
                res = res.get("res", res)
                lines += [{"text": t, "conf": float(c)} for t, c in zip(res["rec_texts"], res["rec_scores"])]
        else:  # 2.x
            for page in model.ocr(str(path)) or []:
                lines += [{"text": t, "conf": float(c)} for _box, (t, c) in page or []]
        lines.append(None)
    return lines


def ocr_vlm(paths, cfg):
    """An OCR-only vision model (e.g. PaddleOCR-VL) behind an OpenAI-compatible endpoint: plain text, no confidence."""
    lines = []
    for path in paths:
        text, _ = chat(cfg, [image_part(path, cfg.get("max_side")), {"type": "text", "text": cfg.get("prompt", "OCR:")}])
        lines += [{"text": t, "conf": None} for t in (text or "").splitlines() if t.strip()]
        lines.append(None)
    return lines


OCR = {"tesseract": ocr_tesseract, "doctr": ocr_doctr, "paddleocr": ocr_paddleocr, "vlm": ocr_vlm}
_models = {}


def _cached(name, make):
    if name not in _models:
        _models[name] = make()
    return _models[name]


def _line(words):
    confs = [c for _, c in words if c >= 0]
    return {"text": " ".join(w for w, _ in words), "conf": round(min(confs), 3) if confs else None}


def ocr_text(lines):
    out, n = ["--- photo 1 ---"], 1
    for line in lines[:-1] if lines and lines[-1] is None else lines:
        if line is None:
            n += 1
            out.append(f"--- photo {n} ---")
        else:
            out.append(line["text"])
    return "\n".join(out)


# ---------------------------------------------------------------- running engines

def load_engines(path):
    """engines.json, with ${VAR} and ${VAR:-default} filled in from the environment."""
    def fill(v):
        while isinstance(v, str):  # innermost first, so ${A:-${B}} works
            filled = re.sub(r"\$\{(\w+)(?::-([^${}]*))?\}", lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), v)
            if filled == v:
                return v
            v = filled
        if isinstance(v, dict):
            return {k: fill(x) for k, x in v.items()}
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v
    return {e["name"]: e for e in fill(json.loads(Path(path).read_text(encoding="utf-8"))["engines"])}


def run_engine(engine, set_dir, out_dir, force=False, only=None, log=print):
    """Run one engine over the test set; one JSON per receipt (the engine_runs row of PRD.md), kept between runs.

    Two-step engines OCR every receipt first and then structure every receipt, so with one model loaded at a
    time (llama-swap) the GPU swaps models once per run instead of once per receipt."""
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [r for r in receipts(set_dir) if (not only or r.name in only)
            and (force or not (out_dir / f"{r.name}.json").exists())]
    if engine["kind"] == "ocr_llm":
        ocr_cfg = {**engine, **engine.get("ocr_engine", {})}
        for r in todo:
            ocr_file = out_dir / f"{r.name}.ocr.json"
            if ocr_file.exists() and not force:
                continue
            t0 = time.monotonic()
            try:
                lines = OCR[engine["ocr"]](photos(r), ocr_cfg)
                ocr_file.write_text(json.dumps({"lines": lines, "seconds": round(time.monotonic() - t0, 2)}, indent=1))
            except Exception as e:  # noqa: BLE001 - recorded in the run, the bake-off goes on
                ocr_file.write_text(json.dumps({"error": f"{type(e).__name__}: {e}", "seconds": 0}))
            log(f"  {engine['name']} {r.name}: OCR {time.monotonic() - t0:.1f}s")
    for r in todo:
        run = {"engine": engine["name"], "receipt": r.name, "photos": [p.name for p in photos(r)],
               "raw": None, "parsed": None, "error": None, "seconds": None, "at": datetime.now().isoformat(timespec="seconds")}
        t0, extra = time.monotonic(), 0.0
        try:
            if engine["kind"] == "vlm":
                content = [image_part(p, engine.get("max_side")) for p in photos(r)] + [{"type": "text", "text": PROMPT}]
            elif engine["kind"] == "ocr_llm":
                ocr = json.loads((out_dir / f"{r.name}.ocr.json").read_text())
                if ocr.get("error"):
                    raise RuntimeError(f"OCR failed: {ocr['error']}")
                extra, run["ocr"] = ocr["seconds"], ocr["lines"]
                content = TEXT_PROMPT.replace("{text}", ocr_text(ocr["lines"]))
            else:
                raise ValueError(f"unknown engine kind {engine['kind']!r} (vlm or ocr_llm)")
            run["raw"], reply = chat(engine, content, SCHEMA)
            if engine.get("logprobs"):
                run["logprobs"] = reply["choices"][0].get("logprobs")
            run["parsed"] = extract_json(run["raw"])
        except Exception as e:  # noqa: BLE001
            run["error"] = f"{type(e).__name__}: {e}"
        run["seconds"] = round(time.monotonic() - t0 + extra, 2)
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

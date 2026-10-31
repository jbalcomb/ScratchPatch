"""Reading a receipt with a local model: the prompts, the model and OCR calls, and turning replies into records.

Shared by the bake-off (bakeoff.py) and the service (web.py). Standard library only; Pillow (optional)
fixes phone-photo rotation and shrinks photos before they go to a vision model.
"""
import base64
import io
import json
import mimetypes
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SUM_SLACK = 2          # cents: items + tax may differ from the total by this much (rounding)

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



def sum_check(rec):
    """Line items plus tax equal the total, within rounding. None when there is nothing to check."""
    if rec["total"] is None or not rec["items"] or any(i["price"] is None for i in rec["items"]):
        return None
    return abs(sum(i["price"] for i in rec["items"]) + (rec["tax"] or 0) - rec["total"]) <= SUM_SLACK



# ---------------------------------------------------------------- talking to models

PROMPT = """These photos are one store receipt, in order from top to bottom. They may overlap: a line that appears
in two photos is still one line, so list it once.

Reply with JSON only, in this shape:
{"store": "store name as printed", "location": "city, state as printed, or null", "date": "YYYY-MM-DD", "items": [{"name": "line text", "quantity": 1, "price": 3.49}],
 "subtotal": 0.00, "tax": 0.00, "total": 0.00}

- price is the amount printed for that line (the line total, not the unit price).
- Discounts, coupons and savings printed as their own lines are items with a negative price.
- Bottle deposits and bag fees are items.
- Leave out payment lines, change, card numbers and loyalty balances.
- Use null for anything you can't read; don't guess."""

TEXT_PROMPT = """Below is OCR text from one store receipt (it may come from several overlapping photos, separated by
"--- photo N ---"; a line repeated where photos overlap is still one line, so list it once).

""" + PROMPT.split("\n\n", 1)[1] + "\n\nOCR text:\n{text}"


ORDER_PROMPT = """Below is an online grocery order confirmation (for example an Instacart email). Structure the
items actually delivered (leave out items marked refunded, unavailable or not found; use the replacement when
an item was replaced).

""" + PROMPT.split("\n\n", 1)[1].replace("printed for that line", "charged for that line") + """
- Service fees, delivery fees and tips are items too, so that items + tax = the total charged.

Order:
{text}"""


SCHEMA = {
    "type": "object",
    "properties": {
        "store": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
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



# ---------------------------------------------------------------- engines.json

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



# ---------------------------------------------------------------- one receipt through one engine

def ocr_photos(engine, paths):
    """Step 1 of an ocr_llm engine: {"lines": [...], "seconds"} or {"error", "seconds"}."""
    t0 = time.monotonic()
    try:
        lines = OCR[engine["ocr"]](list(paths), {**engine, **engine.get("ocr_engine", {})})
        return {"lines": lines, "seconds": round(time.monotonic() - t0, 2)}
    except Exception as e:  # noqa: BLE001 - recorded with the run
        return {"error": f"{type(e).__name__}: {e}", "seconds": round(time.monotonic() - t0, 2)}


def parse_receipt(engine, paths, ocr=None, text=None):
    """One receipt (its photos in order) through one engine; never raises.

    -> {"engine", "raw", "parsed", "error", "seconds", "ocr" (lines, ocr_llm only), "logprobs" (if asked)}.
    ocr: a result of ocr_photos done earlier (so a batch can OCR everything before loading the text model).
    text: structure this text instead of photos (an online order's email), with the engine's text model."""
    run = {"engine": engine["name"], "raw": None, "parsed": None, "error": None, "seconds": None,
           "at": datetime.now().isoformat(timespec="seconds")}
    t0, extra = time.monotonic(), 0.0
    try:
        if text is not None:
            content = ORDER_PROMPT.replace("{text}", text)
        elif engine["kind"] == "vlm":
            content = [image_part(Path(p), engine.get("max_side")) for p in paths] + [{"type": "text", "text": PROMPT}]
        elif engine["kind"] == "ocr_llm":
            ocr = ocr or ocr_photos(engine, paths)
            extra = ocr.get("seconds") or 0
            if ocr.get("error"):
                raise RuntimeError(f"OCR failed: {ocr['error']}")
            run["ocr"] = ocr["lines"]
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
    return run

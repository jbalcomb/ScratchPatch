"""Confidence and review (PRD.md): checks that work on any engine, engine signals, and the three buckets.

    auto    all checks pass and the engines agree       -> saved without review
    review  a check failed or the engines disagree      -> the review page, suspect fields marked
    retake  total or date unreadable                    -> ask for a new photo
"""
import re
from datetime import date, timedelta

from parsing import SUM_SLACK, norm_store, normalize, to_cents

FIELDS = ("merchant", "purchase_date", "total", "tax", "items")
LOW_OCR_CONF = 0.6       # an OCR line read with less confidence than this makes its field suspect
PLAUSIBLE_DAYS = 400     # a purchase date older than this (or in the future) is implausible


def _amount_ok(value):
    """Prices must look like currency: a number, or text such as 12.34 / $12.34 / 1.00-."""
    return value is None or isinstance(value, (int, float)) and not isinstance(value, bool) or to_cents(value) is not None


def run_checks(parsed, known_stores, today=None, receipt_source="in-store"):
    """parsed: one engine's JSON. -> (record in cents, [{name, ok, detail, fields}])."""
    today = today or date.today()
    rec = normalize(parsed)
    raw = parsed if isinstance(parsed, dict) else {}
    out = []

    if rec["total"] is None or not rec["items"] or any(i["price"] is None for i in rec["items"]):
        out.append({"name": "sum", "ok": False, "fields": ["total", "items"],
                    "detail": "no total" if rec["total"] is None else "an item has no price" if rec["items"] else "no items"})
    else:
        got = sum(i["price"] for i in rec["items"]) + (rec["tax"] or 0)
        ok = abs(got - rec["total"]) <= SUM_SLACK
        out.append({"name": "sum", "ok": ok, "fields": [] if ok else ["total", "tax", "items"],
                    "detail": f"items + tax = {got / 100:.2f}, total = {rec['total'] / 100:.2f}"})

    if rec["date"] is None:
        out.append({"name": "date", "ok": False, "fields": ["purchase_date"],
                    "detail": f"{raw.get('date')!r} isn't a date" if raw.get("date") else "no date"})
    else:
        d = date.fromisoformat(rec["date"])
        ok = today - timedelta(days=PLAUSIBLE_DAYS) <= d <= today + timedelta(days=1)
        out.append({"name": "date", "ok": ok, "fields": [] if ok else ["purchase_date"],
                    "detail": rec["date"] if ok else f"{rec['date']} is implausible"})

    store = rec["store"] if isinstance(rec["store"], str) else None
    if not store:
        out.append({"name": "store", "ok": False, "fields": ["merchant"], "detail": "no store name"})
    else:
        known = norm_store(store) in known_stores
        out.append({"name": "store", "ok": known, "fields": [] if known else ["merchant"],
                    "detail": "known store" if known else f"new store: {store}"})

    bad = [str(v) for v in [raw.get("total"), raw.get("tax")] + [i.get("price") for i in raw.get("items") or []
                                                                   if isinstance(i, dict)] if not _amount_ok(v)]
    out.append({"name": "prices", "ok": not bad, "fields": [] if not bad else ["total", "items"],
                "detail": "all amounts look like prices" if not bad else "not prices: " + ", ".join(bad[:5])})
    return rec, out


def disagreements(records):
    """records {engine: normalized record} from two or more engines -> {field: {engine: value}} where they differ."""
    if len(records) < 2:
        return {}
    views = {
        "merchant": lambda r: norm_store(r["store"]) if isinstance(r["store"], str) else None,
        "purchase_date": lambda r: r["date"],
        "total": lambda r: r["total"],
        "tax": lambda r: r["tax"] or 0,
        "items": lambda r: len(r["items"]),
    }
    shown = {"merchant": lambda r: r["store"]}
    out = {}
    for field, view in views.items():
        if len({view(r) for r in records.values()}) > 1:
            out[field] = {e: shown.get(field, view)(r) for e, r in records.items()}
    return out


def ocr_confidence(rec, lines):
    """Per-field confidence from classic OCR's per-line confidence: the line that shows the total or tax amount."""
    out = {}
    if not lines:
        return out
    lines = [ln for ln in lines if ln]
    for field, key in (("total", "total"), ("tax", "tax")):
        if rec[key] is None:
            continue
        text = f"{abs(rec[key]) / 100:.2f}"
        confs = [ln["conf"] for ln in lines if ln.get("conf") is not None and re.search(rf"(?<!\d){re.escape(text)}(?!\d)", ln["text"])]
        if confs:
            out[field] = max(confs)
    item_confs = [ln["conf"] for ln in lines if ln.get("conf") is not None and re.search(r"\d\.\d\d", ln["text"])]
    if item_confs:
        out["items"] = min(item_confs)
    return out


def assess(primary_parsed, others, known_stores, ocr_lines=None, today=None):
    """Everything the review needs from one receipt's engine runs.

    primary_parsed: the chosen engine's JSON; others: {engine: parsed JSON} for the second opinion(s),
    keyed with the primary under "primary" too if you want it shown. -> dict with record, checks,
    disagreements, confidence {field: 0..1}, suspect [fields], bucket."""
    rec, checks = run_checks(primary_parsed, known_stores, today)
    diff = disagreements({name: normalize(p) for name, p in others.items() if p is not None}) if others else {}
    conf = {f: 1.0 for f in FIELDS}
    for c in checks:
        for f in c["fields"]:
            conf[f] = min(conf[f], 0.5 if c["name"] == "store" else 0.3)
    for f in diff:
        conf[f] = min(conf[f], 0.4)
    for f, v in ocr_confidence(rec, ocr_lines).items():
        if v < LOW_OCR_CONF:
            conf[f] = min(conf[f], round(v, 2))
    if rec["total"] is None or rec["date"] is None:
        bucket = "retake"
    elif all(c["ok"] for c in checks) and not diff and min(conf.values()) >= LOW_OCR_CONF:
        bucket = "auto"
    else:
        bucket = "review"
    suspect = [f for f in FIELDS if conf[f] < 0.9]
    return {"record": rec, "checks": [{k: c[k] for k in ("name", "ok", "detail")} for c in checks],
            "disagreements": diff, "confidence": conf, "suspect": suspect, "bucket": bucket}

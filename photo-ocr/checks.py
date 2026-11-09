"""Confidence and review (PRD.md): checks that work on any engine, engine signals, and the three buckets.
The same idea for both kinds of photo, receipts (assess) and products (assess_product):

    auto    all checks pass and the engines agree       -> saved without review
    review  a check failed or the engines disagree      -> the review page, suspect fields marked
    retake  receipt total or date, or product name, unreadable -> ask for a new photo
"""
import difflib
import re
from datetime import date, timedelta

from parsing import DATE_ORDERS, SUM_SLACK, date_options, norm_item, norm_size, norm_store, normalize, normalize_product, to_cents

FIELDS = ("merchant", "purchase_date", "total", "tax", "items")
LOW_OCR_CONF = 0.6       # an OCR line read with less confidence than this makes its field suspect
PLAUSIBLE_DAYS = 400     # a purchase date older than this (or in the future) is implausible


def _amount_ok(value):
    """Prices must look like currency: a number, or text such as 12.34 / $12.34 / 1.00-."""
    return value is None or isinstance(value, (int, float)) and not isinstance(value, bool) or to_cents(value) is not None


def store_date_order(parsed, known_stores):
    """The date order ("mdy"/"dmy") learned for the store this reading names, if any."""
    store = parsed.get("store") if isinstance(parsed, dict) else None
    if not isinstance(known_stores, dict) or not isinstance(store, str):
        return None
    return known_stores.get(norm_store(store))


def run_checks(parsed, known_stores, today=None, receipt_source="in-store"):
    """parsed: one engine's JSON. known_stores: the known stores' normalized names, or {name: date order}
    ("mdy", "dmy" or None) to read their dates by. -> (record in cents, [{name, ok, detail, fields}])."""
    today = today or date.today()
    raw = parsed if isinstance(parsed, dict) else {}
    rec = normalize(parsed, store_date_order(raw, known_stores))
    out = []

    if rec["total"] is None or not rec["items"] or any(i["price"] is None for i in rec["items"]):
        out.append({"name": "sum", "ok": False, "fields": ["total", "items"],
                    "detail": "no total" if rec["total"] is None else "an item has no price" if rec["items"] else "no items"})
    else:
        got = sum(i["price"] for i in rec["items"]) + (rec["tax"] or 0)
        ok = abs(got - rec["total"]) <= SUM_SLACK
        check = {"name": "sum", "ok": ok, "fields": [] if ok else ["total", "tax", "items"],
                 "detail": f"items + tax = {got / 100:.2f}, total = {rec['total'] / 100:.2f}"}
        # Lines whose removal makes it add up (a summary line or a line read twice, usually): offer to remove one,
        # each a button; when two lines would each do it (a coupon the same as the savings total) you pick.
        # Only offered, never done; whether it was a mistake is logged when the receipt is approved.
        fits = {(i["name"], i["price"]): None for i in rec["items"] if abs(got - i["price"] - rec["total"]) <= SUM_SLACK}
        if not ok and fits:
            check["fix"] = {"remove": [{"name": n, "price": p} for n, p in fits]}
        out.append(check)

    if rec["savings"] and any(i["price"] is not None and i["price"] < 0 for i in rec["items"]):
        # the savings total printed against the discount lines read; a store that prints savings only inside
        # its prices (no discount lines) has nothing to check
        discounts = -sum(i["price"] for i in rec["items"] if i["price"] is not None and i["price"] < 0)
        ok = abs(discounts - abs(rec["savings"])) <= SUM_SLACK
        out.append({"name": "savings", "ok": ok, "fields": [] if ok else ["items"],
                    "detail": f"discount lines = {discounts / 100:.2f}, savings printed = {abs(rec['savings']) / 100:.2f}"})

    printed = raw.get("date_printed") or raw.get("date")
    options = date_options(printed)
    if rec["date"] is None and len(options) > 1:
        out.append({"name": "date", "ok": False, "fields": ["purchase_date"],
                    "detail": f"{printed!r} could be " + " or ".join(f"{d} ({DATE_ORDERS[o]})" for o, d in options.items())
                              + "; this store's date order isn't known yet. Pick one: approving teaches it.",
                    "choices": [{"order": o, "date": d, "label": DATE_ORDERS[o]} for o, d in options.items()]})
    elif rec["date"] is None:
        out.append({"name": "date", "ok": False, "fields": ["purchase_date"],
                    "detail": f"{printed!r} isn't a date" if printed else "no date"})
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
    diff = disagreements({name: normalize(p, store_date_order(p, known_stores)) for name, p in others.items()
                          if p is not None}) if others else {}
    conf = {f: 1.0 for f in FIELDS}
    for c in checks:
        for f in c["fields"]:
            conf[f] = min(conf[f], 0.5 if c["name"] == "store" else 0.3)
    for f in diff:
        conf[f] = min(conf[f], 0.4)
    for f, v in ocr_confidence(rec, ocr_lines).items():
        if v < LOW_OCR_CONF:
            conf[f] = min(conf[f], round(v, 2))
    two_ways = any(c.get("choices") for c in checks)  # a readable date, read two ways: pick one, no retake
    if rec["total"] is None or rec["date"] is None and not two_ways:
        bucket = "retake"
    elif all(c["ok"] for c in checks) and not diff and min(conf.values()) >= LOW_OCR_CONF:
        bucket = "auto"
    else:
        bucket = "review"
    suspect = [f for f in FIELDS if conf[f] < 0.9]
    return {"record": rec, "checks": [{k: c[k] for k in ("name", "ok", "detail", "choices", "fix") if k in c} for c in checks],
            "disagreements": diff, "confidence": conf, "suspect": suspect, "bucket": bucket}


# ---------------------------------------------------------------- products

PRODUCT_FIELDS = ("name", "brand", "size")
NAME_AGREE = 0.8          # two engines' product names this similar count as the same
GENERIC_NAMES = {"product", "item", "unknown", "food", "package", "grocery", "n a", "na"}


def names_agree(a, b):
    return difflib.SequenceMatcher(None, norm_item(a), norm_item(b)).ratio() >= NAME_AGREE


def run_product_checks(parsed):
    """-> (record, [{name, ok, detail, fields}]) for one engine's reading of a product."""
    rec = normalize_product(parsed)
    out = []
    name = rec["name"]
    if not name:
        out.append({"name": "name", "ok": False, "fields": ["name"], "detail": "no product name"})
    elif norm_item(name) in GENERIC_NAMES or len(norm_item(name)) < 3:
        out.append({"name": "name", "ok": False, "fields": ["name"], "detail": f"{name!r} doesn't say what it is"})
    else:
        out.append({"name": "name", "ok": True, "fields": [], "detail": name})
    if not rec["size"]:
        out.append({"name": "size", "ok": False, "fields": ["size"], "detail": "no size"})
    elif norm_size(rec["size"]) is None:
        out.append({"name": "size", "ok": False, "fields": ["size"], "detail": f"{rec['size']!r} has no amount and unit"})
    else:
        out.append({"name": "size", "ok": True, "fields": [], "detail": norm_size(rec["size"])})
    return rec, out


def product_disagreements(records):
    """records {engine: normalized product} from two or more engines -> {field: {engine: value}} where they differ."""
    if len(records) < 2:
        return {}
    out = {}
    recs = list(records.values())
    if any(not names_agree(recs[0]["name"], r["name"]) for r in recs[1:]):
        out["name"] = {e: r["name"] for e, r in records.items()}
    for field, view in (("brand", lambda r: norm_store(r["brand"]) or None), ("size", lambda r: norm_size(r["size"]))):
        if len({view(r) for r in recs}) > 1:
            out[field] = {e: r[field] for e, r in records.items()}
    return out


def assess_product(primary_parsed, others):
    """Like assess, for one product: record, checks, disagreements, confidence, suspect fields, bucket."""
    rec, checks = run_product_checks(primary_parsed)
    diff = product_disagreements({n: normalize_product(p) for n, p in others.items() if p is not None}) if others else {}
    conf = {f: 1.0 for f in PRODUCT_FIELDS}
    for c in checks:
        for f in c["fields"]:
            conf[f] = min(conf[f], 0.3)
    for f in diff:
        conf[f] = min(conf[f], 0.4)
    if not rec["name"]:
        bucket = "retake"
    elif all(c["ok"] for c in checks) and not diff:
        bucket = "auto"
    else:
        bucket = "review"
    return {"record": rec, "checks": [{k: c[k] for k in ("name", "ok", "detail")} for c in checks],
            "disagreements": diff, "confidence": conf, "suspect": [f for f in PRODUCT_FIELDS if conf[f] < 0.9],
            "bucket": bucket}

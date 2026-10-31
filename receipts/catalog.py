"""Product gallery and matching (PRD.md): catalog entries, receipt text -> product aliases, purchase
history and shopping lists."""
import difflib
import statistics
from datetime import date

from db import now
from parsing import chat, extract_json, image_part, norm_item

SUGGEST_AT = 0.6      # text similarity at which a product is offered as a suggestion
MODEL_BELOW = 0.85    # ask the text model when the best text match is weaker than this
DUE_FACTOR = 0.9      # due when the days since the last purchase reach this share of the usual interval

PRODUCT_PROMPT = """These photos show one grocery or household product. Read its packaging.
Reply with JSON only: {"name": "what the product is, e.g. Whole milk", "brand": "brand or null", "size": "size with unit, e.g. 1 gal, or null"}"""

PRODUCT_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "brand": {"type": ["string", "null"]},
                                                   "size": {"type": ["string", "null"]}}, "required": ["name"]}

PICK_PROMPT = """A store receipt line reads: "{text}" (price {price}).
Which of these catalog products is it? Receipt lines are abbreviated (GV = Great Value, WHL = whole, MLK = milk, ...).
{choices}
Reply with JSON only: {{"product_id": <id from the list, or null if none fits>}}"""


def product_label(p):
    return " ".join(x for x in (p.get("brand"), p.get("name"), p.get("size")) if x)


def alias_for(store, raw_text):
    """-> (found, product_id). found with product_id None means "not a product" (skip)."""
    row = store.one("SELECT product_id FROM product_aliases WHERE norm_text = ?", norm_item(raw_text))
    return (True, row["product_id"]) if row else (False, None)


def text_candidates(store, raw_text, limit=8):
    """[(score, product)] by similarity of the receipt text to product labels and to known aliases."""
    target = norm_item(raw_text)
    best = {}
    for p in store.q("SELECT * FROM products WHERE status = 'ready'"):
        best[p["id"]] = (difflib.SequenceMatcher(None, target, norm_item(product_label(p))).ratio(), p)
    for a in store.q("SELECT a.norm_text, p.* FROM product_aliases a JOIN products p ON p.id = a.product_id"):
        s = difflib.SequenceMatcher(None, target, a["norm_text"]).ratio()
        if s > best.get(a["id"], (0, None))[0]:
            best[a["id"]] = (s, {k: v for k, v in a.items() if k != "norm_text"})
    return sorted(best.values(), key=lambda sp: -sp[0])[:limit]


def model_pick(engine, raw_text, price, candidates):
    """Ask the text model to pick among candidates; -> product id or None."""
    if not engine or not candidates:
        return None
    choices = "\n".join(f"- id {p['id']}: {product_label(p)}" for _, p in candidates)
    text = PICK_PROMPT.format(text=raw_text, price="?" if price is None else f"{price / 100:.2f}", choices=choices)
    reply, _ = chat(engine, text)
    pid = extract_json(reply).get("product_id")
    return pid if pid in {p["id"] for _, p in candidates} else None


def match_receipt_items(store, receipt_id, text_engine=None):
    """Alias matches apply at once; everything else gets a suggestion (text, else the model) for the
    unmatched queue. Returns the number matched by alias."""
    matched = 0
    for it in store.q("SELECT * FROM receipt_items WHERE receipt_id = ? AND match IS NULL", receipt_id):
        found, pid = alias_for(store, it["raw_text"])
        if found:
            store.x("UPDATE receipt_items SET product_id = ?, match = ?, confidence = 1.0 WHERE id = ?",
                    pid, "alias" if pid else "skip", it["id"])
            matched += 1
            continue
        cands = text_candidates(store, it["raw_text"])
        sugg, by, score = None, None, None
        if cands and cands[0][0] >= SUGGEST_AT:
            score, sugg, by = round(cands[0][0], 2), cands[0][1]["id"], "text"
        if (not cands or cands[0][0] < MODEL_BELOW) and text_engine:
            try:
                pid = model_pick(text_engine, it["raw_text"], it["line_total"], cands)
                if pid:
                    sugg, by, score = pid, "model", 0.7
            except Exception:  # noqa: BLE001 - a suggestion is optional
                pass
        store.x("UPDATE receipt_items SET suggested_product_id = ?, suggested_by = ?, suggestion_score = ? WHERE id = ?",
                sugg, by, score, it["id"])
    return matched


def confirm_match(store, item_id, product_id=None, new_product=None, skip=False):
    """Save the alias for this item's text and apply it to every unmatched item with the same text."""
    item = store.one("SELECT * FROM receipt_items WHERE id = ?", item_id)
    if not item:
        raise KeyError(f"no item {item_id}")
    with store.tx():
        if new_product:
            product_id = store.x("INSERT INTO products (name, brand, size, created_at) VALUES (?, ?, ?, ?)",
                                 new_product.get("name") or item["raw_text"], new_product.get("brand"),
                                 new_product.get("size"), now())
        if not skip and not store.one("SELECT id FROM products WHERE id = ?", product_id):
            raise KeyError(f"no product {product_id}")
        pid = None if skip else product_id
        store.x("INSERT INTO product_aliases (norm_text, raw_text, product_id, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(norm_text) DO UPDATE SET product_id = excluded.product_id, raw_text = excluded.raw_text",
                norm_item(item["raw_text"]), item["raw_text"], pid, now())
        n = 0
        for other in store.q("SELECT id, raw_text FROM receipt_items WHERE match IS NULL OR id = ?", item_id):
            if norm_item(other["raw_text"]) == norm_item(item["raw_text"]):
                store.x("UPDATE receipt_items SET product_id = ?, match = ?, confidence = 1.0 WHERE id = ?",
                        pid, "skip" if skip else ("manual" if other["id"] == item_id else "alias"), other["id"])
                n += 1
    return {"matched": n, "product_id": pid}


def read_product(engine, photo_paths):
    """The vision model reads name, brand and size off the product photos. -> (dict, raw reply)."""
    content = [image_part(p, engine.get("max_side")) for p in photo_paths] + [{"type": "text", "text": PRODUCT_PROMPT}]
    raw, _ = chat(engine, content, PRODUCT_SCHEMA)
    return extract_json(raw), raw


def history(store, product_id):
    return store.q(
        "SELECT r.purchase_date AS date, r.merchant, r.location, i.line_total, i.quantity, r.id AS receipt_id "
        "FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id "
        "WHERE i.product_id = ? AND r.status = 'approved' AND r.purchase_date IS NOT NULL "
        "ORDER BY r.purchase_date DESC", product_id)


def shopping_list(store, today=None):
    """A product is due when the days since its last purchase reach DUE_FACTOR of its usual (median) days
    between purchases. Needs at least two purchases on different days. PRD.md leaves the real rules open."""
    today = today or date.today()
    out = []
    for p in store.q("SELECT * FROM products WHERE status = 'ready'"):
        h = history(store, p["id"])
        days = sorted({date.fromisoformat(x["date"]) for x in h})
        if len(days) < 2:
            continue
        every = statistics.median((b - a).days for a, b in zip(days, days[1:]))
        since = (today - days[-1]).days
        if every <= 0 or since < DUE_FACTOR * every:
            continue
        stores = [x["merchant"] for x in h if x["merchant"]]
        priced = [x for x in h if x["line_total"] is not None]
        best = min(priced, key=lambda x: x["line_total"] / (x["quantity"] or 1)) if priced else None
        out.append({"product_id": p["id"], "name": p["name"], "brand": p["brand"], "size": p["size"],
                    "last_bought": days[-1].isoformat(), "every_days": round(every), "overdue_days": round(since - every),
                    "usual_store": max(set(stores), key=stores.count) if stores else None,
                    "last_price": priced[0]["line_total"] if priced else None,
                    "best_price": best["line_total"] if best else None, "best_store": best["merchant"] if best else None})
    out.sort(key=lambda x: -x["overdue_days"])
    return {"rule": f"due when the days since the last purchase reach {round(DUE_FACTOR * 100)}% of the usual days "
                    "between purchases (needs two or more purchases)", "items": out}


def product_summary(store, p):
    h = history(store, p["id"])
    img = store.one("SELECT id FROM images WHERE product_id = ? ORDER BY id LIMIT 1", p["id"])
    return {"id": p["id"], "name": p["name"], "brand": p["brand"], "size": p["size"], "status": p["status"],
            "image_url": f"/api/images/{img['id']}" if img else None, "purchases": len(h),
            "last_bought": h[0]["date"] if h else None, "last_price": h[0]["line_total"] if h else None,
            "last_store": h[0]["merchant"] if h else None}


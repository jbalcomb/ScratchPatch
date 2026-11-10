"""Reading errors: what the model got wrong, by category, found without guessing.

A reading (the model's JSON) is compared with the truth: a receipt as you approved it, or a bake-off label.
Every difference gets a category by a fixed rule, and anything no rule explains is "other", never guessed:

    summary_line    a line that isn't on the receipt as an item, and is a summary: its amount is the other
                    lines' total (a subtotal) or the discount lines' total (a savings total), or its text says
                    subtotal / total / savings / saved / balance
    duplicate_line  a line that isn't on the receipt twice: the same text and price as another line it read
                    (where two photos overlap)
    extra_line      any other line that isn't on the receipt
    missed_line     a line on the receipt the reading left out
    misread_price   a line read with the right text and the wrong price
    misread_text    a line read with the right price and the wrong text
    misread_total, misread_tax
    misread_date    the date approved isn't any reading of the date the model says is printed
    wrong_date_order the date is one reading of what is printed, but the store's learned order picked the other
                    (the code's mistake, not the model's: counted apart)

Errors are counted from the model's own reading, before anything is fixed in code or by hand, so a fix
elsewhere can't flatter the numbers.
"""
import re
from collections import Counter

from parsing import SUM_SLACK, date_options, norm_item, normalize

CATEGORIES = {
    "summary_line": "summary line read as an item",
    "duplicate_line": "line listed twice",
    "extra_line": "line not on the receipt",
    "missed_line": "line left out",
    "misread_price": "price misread",
    "misread_text": "line text misread",
    "misread_total": "total misread",
    "misread_tax": "tax misread",
    "misread_date": "date misread",
    "wrong_date_order": "date order (code, not model)",
}
SUMMARY_WORDS = re.compile(r"\b(sub ?total|total|savings?|you saved|saved|balance)\b", re.I)


def money(cents):
    return "none" if cents is None else f"{cents / 100:.2f}"


def line(name, price):
    """How an error names a line: 'SAVINGS TOTAL' -3.00."""
    return f"{name!r} {money(price)}"


def is_summary(p, items):
    """A line that sums others: its amount is the other lines' total (a subtotal) or the other discount lines'
    total (a savings total), or its text says it is a summary."""
    if SUMMARY_WORDS.search(p["name"] or ""):
        return True
    others = [i["price"] for i in items if i is not p]
    if p["price"] is None or not others or any(x is None for x in others):
        return False
    discounts = [x for x in others if x < 0]
    return abs(sum(others) - p["price"]) <= SUM_SLACK or bool(discounts) and abs(sum(discounts) - p["price"]) <= SUM_SLACK


def classify(parsed, truth, date_order=None):
    """parsed: the model's JSON; truth: a normalized record (normalize() of a label, or the approved receipt in
    the same shape, amounts in cents). -> [{"category", "detail"}]."""
    raw = parsed if isinstance(parsed, dict) else {}
    pred = normalize(raw, date_order)
    out = []

    def add(category, detail):
        out.append({"category": category, "detail": detail})

    key = lambda i: (norm_item(i["name"]), i["price"])  # noqa: E731
    left_p, left_t = list(pred["items"]), list(truth["items"])
    for t in list(left_t):  # the same text and price: right
        same = next((p for p in left_p if key(p) == key(t)), None)
        if same is not None:
            left_p.remove(same)
            left_t.remove(t)
    for t in list(left_t):  # the same text, another price
        p = next((p for p in left_p if norm_item(p["name"]) == norm_item(t["name"])), None)
        if p is not None:
            left_p.remove(p), left_t.remove(t)
            add("misread_price", f"{t['name']}: read {money(p['price'])}, is {money(t['price'])}")
    for t in list(left_t):  # the same price, other text
        p = next((p for p in left_p if p["price"] == t["price"] and p["price"] is not None), None)
        if p is not None:
            left_p.remove(p), left_t.remove(t)
            add("misread_text", f"read {p['name']!r}, is {t['name']!r} ({money(t['price'])})")
    for p in left_p:  # lines the reading has and the receipt doesn't
        twins = [q for q in pred["items"] if key(q) == key(p)]
        this = line(p["name"], p["price"])
        if len(twins) > 1:
            add("duplicate_line", this)
        elif is_summary(p, pred["items"]):
            add("summary_line", this)
        else:
            add("extra_line", this)
    for t in left_t:
        add("missed_line", line(t["name"], t["price"]))

    if pred["total"] != truth["total"]:
        add("misread_total", f"read {money(pred['total'])}, is {money(truth['total'])}")
    if (pred["tax"] or 0) != (truth["tax"] or 0):
        add("misread_tax", f"read {money(pred['tax'])}, is {money(truth['tax'])}")
    if truth["date"]:
        printed = raw.get("date_printed") or raw.get("date")
        readings = set(date_options(printed).values())
        if truth["date"] not in readings:
            add("misread_date", f"read {printed!r}, is {truth['date']}")
        elif pred["date"] and pred["date"] != truth["date"]:
            add("wrong_date_order", f"{printed!r} taken as {pred['date']}, is {truth['date']}")
    return out


def counts(errors):
    return dict(Counter(e["category"] for e in errors))

"""Read-only Photo OCR jobs: the review queue (receipts and products) and the shopping list."""

from __future__ import annotations

from taskdeck.core import Pipeline, Result, Stage, step

from . import api


def receipt_row(r: dict) -> tuple[str, str]:
    """One receipt summary as a result row: "#17 Giant Eagle" → "2026-10-12 · $45.19 · review · check: total"."""
    parts = [r.get("purchase_date") or "no date", api.dollars(r.get("total")), r.get("bucket") or r.get("status") or ""]
    suspect = [api.FIELD_NAMES.get(f, f) for f in r.get("suspect") or []]
    if suspect:
        parts.append("check: " + ", ".join(suspect))
    if r.get("bucket") == "retake":
        parts.append("new photo needed")
    return f"#{r.get('id')} {r.get('merchant') or 'Unknown store'}", " · ".join(p for p in parts if p)


def product_row(p: dict) -> tuple[str, str]:
    """One product to check as a result row: "Product #5 Whole milk" → "Great Value · 1 gal · check: size"."""
    parts = [p.get("brand") or "", p.get("size") or ""]
    if p.get("suspect"):
        parts.append("check: " + ", ".join(p["suspect"]))
    if p.get("bucket") == "retake":
        parts.append("new photo needed")
    return f"Product #{p.get('id')} {p.get('name') or 'unnamed'}", " · ".join(x for x in parts if x) or "check it"


@step(Stage.EXECUTE, name="get queue")
def get_queue(ctx):
    ctx["summary"] = api.call(ctx.settings, "GET", "/api/summary")
    ctx["receipts"] = api.call(ctx.settings, "GET", "/api/receipts", params={"status": "parsed"}).get("receipts") or []
    ctx["products"] = api.call(ctx.settings, "GET", "/api/products", params={"status": "review"}).get("products") or []


@step(Stage.PRESENT)
def show_queue(ctx):
    s, receipts, products = ctx["summary"], ctx["receipts"], ctx["products"]
    counts = [("Receipts to review", s.get("review", 0)), ("Receipts to retake", s.get("retake", 0)),
              ("Products to check", s.get("products_review", 0)),
              ("Waiting to be read", s.get("pending", 0) + s.get("products_pending", 0)), ("Receipts approved", s.get("approved", 0))]
    retakes = sum(r.get("bucket") == "retake" for r in receipts) + sum(p.get("bucket") == "retake" for p in products)
    n = len(receipts) + len(products)
    parts = [f"{len(receipts)} receipt{'s' * (len(receipts) != 1)}"] * bool(receipts) + \
            [f"{len(products)} product{'s' * (len(products) != 1)}"] * bool(products)
    ctx.result = Result(
        title=" and ".join(parts) + " to check" if n else "Nothing to review",
        rows=[(k, str(v)) for k, v in counts] + [receipt_row(r) for r in receipts] + [product_row(p) for p in products],
        text=f"Review them on {api.page_url(ctx.settings, '/review')}",
        warning=f"{retakes} photo{'s need' if retakes != 1 else ' needs'} retaking." if retakes else None,
    )


def due_text(item: dict) -> str:
    days = item.get("overdue_days")
    if not isinstance(days, int):
        due = "due"
    elif days > 0:
        due = f"{days} day{'s' * (days != 1)} overdue"
    elif days == 0:
        due = "due today"
    else:
        due = f"due in {-days} day{'s' * (days != -1)}"
    parts = [due]
    if item.get("usual_store"):
        parts.append(f"usually {item['usual_store']}")
    if item.get("last_price") is not None:
        parts.append(f"last {api.dollars(item['last_price'])}")
    if item.get("best_price") is not None:
        parts.append(f"best {api.dollars(item['best_price'])}" + (f" at {item['best_store']}" if item.get("best_store") else ""))
    return " · ".join(parts)


@step(Stage.PRESENT)
def show_shopping_list(ctx):
    data = ctx["shopping"]
    items = data.get("items") or []
    ctx.result = Result(
        title=f"{len(items)} item{'s' * (len(items) != 1)} due" if items else "Nothing is due",
        rows=[(" · ".join(str(i[k]) for k in ("name", "brand", "size") if i.get(k)), due_text(i)) for i in items],
        text=f"Rule: {data['rule']}." if data.get("rule") else None,
    )


class ReceiptsReview(api.PhotoOcrJob):
    id = "photo_ocr_review"
    title = "Review queue"
    description = "List the receipts and products whose readings need a person to check them, with the fields to look at."
    icon = "FACT_CHECK_OUTLINED"
    tags = ("receipt", "product", "review", "queue", "retake", "approve", "check", "groceries", "ocr")
    run_label = "Check queue"

    def build_pipeline(self):
        return Pipeline([api.service_configured, get_queue, show_queue])


class ShoppingList(api.PhotoOcrJob):
    id = "photo_ocr_shopping"
    title = "Shopping list"
    description = "Products that are due again, from how often you buy them, with the usual and the cheapest store."
    icon = "SHOPPING_CART_OUTLINED"
    tags = ("shopping", "groceries", "list", "due", "price", "store", "receipts")
    run_label = "Show list"

    def build_pipeline(self):
        return Pipeline([api.service_configured, api.fetch("shopping", "/api/shopping-list", "get shopping list"),
                         show_shopping_list])


REVIEW = ReceiptsReview()
SHOPPING = ShoppingList()

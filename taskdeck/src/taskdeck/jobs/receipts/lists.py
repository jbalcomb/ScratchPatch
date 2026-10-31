"""Read-only Receipts jobs: the review queue and the shopping list."""

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


@step(Stage.EXECUTE, name="get queue")
def get_queue(ctx):
    ctx["summary"] = api.call(ctx.settings, "GET", "/api/summary")
    ctx["receipts"] = api.call(ctx.settings, "GET", "/api/receipts", params={"status": "parsed"}).get("receipts") or []


@step(Stage.PRESENT)
def show_queue(ctx):
    s, receipts = ctx["summary"], ctx["receipts"]
    counts = [("To review", s.get("review", 0)), ("Retake", s.get("retake", 0)),
              ("Waiting to be parsed", s.get("pending", 0)), ("Approved", s.get("approved", 0))]
    retakes = sum(r.get("bucket") == "retake" for r in receipts)
    ctx.result = Result(
        title=f"{len(receipts)} receipt{'s' * (len(receipts) != 1)} to check" if receipts else "Nothing to review",
        rows=[(k, str(v)) for k, v in counts] + [receipt_row(r) for r in receipts],
        text=f"Review them on {api.page_url(ctx.settings, '/review')}",
        warning=f"{retakes} receipt{'s need' if retakes != 1 else ' needs'} a new photo." if retakes else None,
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


class ReceiptsReview(api.ReceiptsJob):
    id = "receipts_review"
    title = "Receipts to review"
    description = "List the parsed receipts that need a person to check them, with the fields to look at."
    icon = "FACT_CHECK_OUTLINED"
    tags = ("receipt", "review", "queue", "retake", "approve", "check", "groceries")
    run_label = "Check queue"

    def build_pipeline(self):
        return Pipeline([api.service_configured, get_queue, show_queue])


class ShoppingList(api.ReceiptsJob):
    id = "receipts_shopping"
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

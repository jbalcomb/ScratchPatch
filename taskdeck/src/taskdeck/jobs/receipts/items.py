"""Unmatched items: receipt lines not yet tied to a catalog product, and accepting the confident suggestions."""

from __future__ import annotations

from taskdeck.core import Pipeline, Result, Stage, StepError, step

from . import api

ACCEPT_SCORE = 0.9  # suggestions at or above this score are accepted by "Accept suggestions"


def item_label(item: dict) -> str:
    """ "GV WHL MLK GAL (Walmart, 2026-10-12, $3.29)" """
    where = [item.get("merchant") or "", item.get("purchase_date") or "", api.dollars(item.get("line_total"))]
    return f"{item.get('raw_text') or '?'} ({', '.join(w for w in where if w)})"


def suggestion_text(item: dict) -> str:
    s = item.get("suggestion")
    if not s:
        return "no suggestion"
    score = s.get("score")
    return f"→ {s.get('name')}" + (f" ({score:.0%})" if isinstance(score, (int, float)) else "")


def acceptable(item: dict) -> bool:
    s = item.get("suggestion") or {}
    return isinstance(s.get("score"), (int, float)) and s["score"] >= ACCEPT_SCORE and s.get("product_id") is not None


@step(Stage.PRESENT)
def show_items(ctx):
    items = ctx["unmatched"].get("items") or []
    accept = [i for i in items if acceptable(i)]
    lines = ([f"Accept suggestions will match these {len(accept)} item{'s' * (len(accept) != 1)}:"]
             + [f"• {i.get('raw_text')} → {i['suggestion'].get('name')}" for i in accept]) if accept else [
        f"No suggestion scores {ACCEPT_SCORE:.0%} or more, so there is nothing to accept here."]
    lines.append(f"Match the rest on {api.page_url(ctx.settings, '/items')}")
    ctx.result = Result(
        title=f"{len(items)} unmatched item{'s' * (len(items) != 1)}" if items else "Every item is matched",
        rows=[(item_label(i), suggestion_text(i)) for i in items],
        text="\n".join(lines) if items else None,
        payload=[{"id": i["id"], "product_id": i["suggestion"]["product_id"], "raw_text": i.get("raw_text")}
                 for i in accept] or None,
    )


class UnmatchedItems(api.ReceiptsJob):
    id = "receipts_unmatched"
    title = "Unmatched items"
    description = "Receipt lines not yet matched to a product, with the suggested match; accept the confident ones."
    icon = "LINK_OFF"
    tags = ("items", "match", "products", "catalog", "alias", "suggestions", "receipts")
    run_label = "Find unmatched"
    commit_label = "Accept suggestions"
    committed_label = "Accepted"

    def build_pipeline(self):
        return Pipeline([api.service_configured, api.fetch("unmatched", "/api/items/unmatched", "get unmatched items"),
                         show_items])

    def commit(self, payload, settings):
        if not payload:
            raise StepError("Nothing to accept. Tap Find unmatched first.")
        accepted, lines, failed, done = 0, 0, [], set()
        for item in payload:
            key = (item.get("raw_text"), item["product_id"])
            if key in done:  # the service already applied this match to items with the same text
                accepted += 1
                continue
            try:
                answer = api.call(settings, "POST", f"/api/items/{item['id']}/match",
                                  json_body={"product_id": item["product_id"]})
            except StepError as exc:
                failed.append(f"{item.get('raw_text')}: {exc}")
                continue
            done.add(key)
            accepted += 1
            lines += answer["matched"] if isinstance(answer.get("matched"), int) else 1
        if failed and not accepted:
            raise StepError("Nothing was accepted. " + "; ".join(failed))
        message = f"Accepted {accepted} suggestion{'s' * (accepted != 1)}: {lines} receipt line{'s' * (lines != 1)} matched."
        return message + (f" Not accepted: {'; '.join(failed)}" if failed else "")


PLUGIN = UnmatchedItems()

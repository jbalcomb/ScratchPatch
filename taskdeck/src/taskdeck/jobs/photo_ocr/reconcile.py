"""Reconciliation: match receipts to bank charges, and list what is unmatched on either side.

Bank charges come from the budget store, which the service reads on each run; bank exports are imported in
the budget project, not here."""

from __future__ import annotations

from taskdeck.core import Pipeline, Result, Stage, step

from . import api
from .lists import receipt_row


@step(Stage.EXECUTE, name="match")
def run_match(ctx):
    ctx["run"] = api.call(ctx.settings, "POST", "/api/reconcile/run", timeout=120)
    ctx["reconcile"] = api.call(ctx.settings, "GET", "/api/reconcile")


@step(Stage.PRESENT)
def show_unmatched(ctx):
    data = ctx["reconcile"]
    receipts, bank = data.get("receipts_unmatched") or [], data.get("bank_unmatched") or []
    rows = [("New bank charges from budget", str(ctx["run"].get("bank_new", 0))),
            ("Matched now", str(ctx["run"].get("matched", 0))),
             ("Receipts with no bank charge", str(len(receipts))), ("Bank charges with no receipt", str(len(bank)))]
    rows += [(key, f"{value} · no bank charge") for key, value in map(receipt_row, receipts)]
    rows += [(f"Bank {b.get('date') or ''} {b.get('description') or ''}".strip(),
              " · ".join(p for p in (api.dollars(b.get("amount")), b.get("account") or "", "no receipt") if p))
             for b in bank]
    ctx.result = Result(
        title="Everything is matched" if not receipts and not bank else
        f"{len(receipts)} receipt{'s' * (len(receipts) != 1)} and {len(bank)} bank charge{'s' * (len(bank) != 1)} unmatched",
        rows=rows,
        text=f"Match them by hand on {api.page_url(ctx.settings, '/reconcile')}" if receipts or bank else None,
    )


class Reconciliation(api.PhotoOcrJob):
    id = "photo_ocr_reconcile"
    title = "Reconciliation"
    description = ("Match receipts to the bank charges in the budget store, then list receipts with no bank match "
                   "and charges with no receipt.")
    icon = "COMPARE_ARROWS"
    tags = ("bank", "reconcile", "statement", "budget", "charges", "receipts")
    run_label = "Reconcile"

    def build_pipeline(self):
        return Pipeline([api.service_configured, run_match, show_unmatched])


PLUGIN = Reconciliation()

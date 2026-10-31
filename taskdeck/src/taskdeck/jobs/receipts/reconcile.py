"""Reconciliation: match receipts to bank charges, and list what is unmatched on either side."""

from __future__ import annotations

from taskdeck.core import Field, Pipeline, Result, Stage, step

from . import api
from .lists import receipt_row


@step(Stage.VALIDATE)
def read_csv(ctx):
    path = (ctx.get("bank_csv") or "").strip()
    ctx["csv_part"] = api.read_files([path], (".csv",), field="file") if path else []


@step(Stage.EXECUTE, name="import bank data")
def import_bank(ctx):
    ctx["imports"] = []
    if ctx["csv_part"]:
        ctx["imports"].append(("Bank CSV", api.call(ctx.settings, "POST", "/api/bank/import", files=ctx["csv_part"],
                                                    timeout=120)))
    if ctx.get("sync_budget"):
        ctx["imports"].append(("Budget store", api.call(ctx.settings, "POST", "/api/bank/sync-budget", timeout=120)))


@step(Stage.EXECUTE, name="match")
def run_match(ctx):
    ctx["run"] = api.call(ctx.settings, "POST", "/api/reconcile/run", timeout=120)
    ctx["reconcile"] = api.call(ctx.settings, "GET", "/api/reconcile")


@step(Stage.PRESENT)
def show_unmatched(ctx):
    data = ctx["reconcile"]
    receipts, bank = data.get("receipts_unmatched") or [], data.get("bank_unmatched") or []
    rows = [(name, f"{r.get('rows', 0)} rows, {r.get('new', 0)} new") for name, r in ctx["imports"]]
    rows += [("Matched now", str(ctx["run"].get("matched", 0))),
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


class Reconciliation(api.ReceiptsJob):
    id = "receipts_reconcile"
    title = "Reconciliation"
    description = "Match receipts to bank charges, then list receipts with no bank match and charges with no receipt."
    icon = "COMPARE_ARROWS"
    tags = ("bank", "reconcile", "statement", "csv", "budget", "charges", "receipts", "instacart")
    fields = (
        Field.file("bank_csv", "Bank CSV (optional)", extensions=(".csv",),
                   help="A Bank of America export, or any CSV with Date, Description and Amount columns. "
                        "Rows already imported are skipped."),
        Field.toggle("sync_budget", "Copy new transactions from the budget store"),
    )
    run_label = "Reconcile"

    def build_pipeline(self):
        return Pipeline([api.service_configured, read_csv, import_bank, run_match, show_unmatched])


PLUGIN = Reconciliation()

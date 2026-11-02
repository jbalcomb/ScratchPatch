"""Bank and online-order reconciliation (PRD.md, Later): match each receipt to its bank charge.

Bank transactions come only from the budget project's store (budget.db), read-only. Budget is where bank
exports are imported; more accounts or banks are added there, not here.
"""
import difflib
import re
import sqlite3
from datetime import date, timedelta

from db import now
from parsing import norm_store

DAYS_BEFORE, DAYS_AFTER = 1, 5   # the bank date may be this far before / after the purchase date


def add_bank_rows(store, rows, account, origin):
    """Insert, skipping rows already stored (overlapping exports are fine). -> {rows, new}."""
    with store.tx():
        before = store.one("SELECT total_changes() AS n")["n"]
        for r in rows:
            store.x("INSERT OR IGNORE INTO bank_txns (account, date, description, amount, balance, origin) "
                    "VALUES (?, ?, ?, ?, ?, ?)", account, r["date"], r["description"], r["amount"], r["balance"], origin)
        new = store.one("SELECT total_changes() AS n")["n"] - before
    return {"rows": len(rows), "new": new}


def sync_budget(store, budget_db):
    """Copy transactions from budget.db (read-only)."""
    src = sqlite3.connect(f"file:{budget_db}?mode=ro", uri=True)
    try:
        rows = [{"date": d, "description": desc, "amount": amt, "balance": bal, "account": acct}
                for acct, d, desc, amt, bal in src.execute(
                    "SELECT account, date, description, amount, balance FROM transactions")]
    finally:
        src.close()
    total = {"rows": 0, "new": 0}
    for acct in sorted({r["account"] for r in rows}):
        res = add_bank_rows(store, [r for r in rows if r["account"] == acct], acct, "budget")
        total = {k: total[k] + res[k] for k in total}
    return total


def _merchant_score(merchant, description):
    """How well a receipt's store name fits a bank description (0..1)."""
    m, d = norm_store(merchant), norm_store(description)
    if not m or not d:
        return 0.0
    if m in d:
        return 1.0
    words = [w for w in re.findall(r"[a-z0-9]+", (merchant or "").lower()) if len(w) >= 4]
    if words and any(w in d for w in words):
        return 0.8
    return difflib.SequenceMatcher(None, m, d[: len(m) + 6]).ratio()


def candidates(store, receipt, bank_rows=None):
    """[(score, bank row)] for a receipt: same amount, date in range, merchant similar, best first."""
    if receipt["total"] is None or not receipt["purchase_date"]:
        return []
    d = date.fromisoformat(receipt["purchase_date"])
    rows = bank_rows if bank_rows is not None else store.q(
        "SELECT * FROM bank_txns WHERE id NOT IN (SELECT bank_id FROM reconcile_matches)")
    out = []
    for b in rows:
        if b["amount"] != -receipt["total"]:
            continue
        bd = date.fromisoformat(b["date"])
        if not d - timedelta(days=DAYS_BEFORE) <= bd <= d + timedelta(days=DAYS_AFTER):
            continue
        ms = _merchant_score(receipt["merchant"], b["description"])
        if receipt["source"] == "instacart" and "instacart" in b["description"].lower():
            ms = max(ms, 1.0)
        if ms < 0.5:
            continue
        out.append((ms - abs((bd - d).days) * 0.02, b))
    return sorted(out, key=lambda sb: -sb[0])


def run(store):
    """Match unmatched approved receipts to unmatched charges, best pairs first. -> number matched."""
    receipts = store.q("SELECT * FROM receipts WHERE status = 'approved' AND id NOT IN (SELECT receipt_id FROM reconcile_matches)")
    bank = store.q("SELECT * FROM bank_txns WHERE id NOT IN (SELECT bank_id FROM reconcile_matches)")
    pairs = sorted(((s, r["id"], b["id"]) for r in receipts for s, b in candidates(store, r, bank)), reverse=True)
    used_r, used_b, n = set(), set(), 0
    for _, rid, bid in pairs:
        if rid in used_r or bid in used_b:
            continue
        store.x("INSERT INTO reconcile_matches (receipt_id, bank_id, method, at) VALUES (?, ?, 'auto', ?)", rid, bid, now())
        used_r.add(rid), used_b.add(bid)
        n += 1
    return n


def match(store, receipt_id, bank_id=None, unmatch=False):
    store.x("DELETE FROM reconcile_matches WHERE receipt_id = ?", receipt_id)
    if not unmatch:
        store.x("DELETE FROM reconcile_matches WHERE bank_id = ?", bank_id)
        store.x("INSERT INTO reconcile_matches (receipt_id, bank_id, method, at) VALUES (?, ?, 'manual', ?)",
                receipt_id, bank_id, now())


def report(store, since=None):
    """Receipts with no bank match, money-out charges with no receipt, and the matches."""
    since = since or (date.today() - timedelta(days=120)).isoformat()
    receipts = store.q("SELECT * FROM receipts WHERE status = 'approved' AND (purchase_date IS NULL OR purchase_date >= ?) "
                       "AND id NOT IN (SELECT receipt_id FROM reconcile_matches) ORDER BY purchase_date DESC", since)
    bank = store.q("SELECT id, date, description, amount, account FROM bank_txns WHERE amount < 0 AND date >= ? "
                   "AND id NOT IN (SELECT bank_id FROM reconcile_matches) ORDER BY date DESC", since)
    matched = store.q("SELECT receipt_id, bank_id, method FROM reconcile_matches ORDER BY at DESC")
    return receipts, bank, matched

"""Budget jobs: SnapShot parsing, the month reconcile and the balance timeline. No network or Google needed."""

from datetime import date

import pytest

from taskdeck.core import StepError
from taskdeck.jobs import budget
from taskdeck.jobs.budget import data, reconcile, snapshot, timeline

# The budget sheet's SnapShot tab as the Sheets API returns it (formatted values, a totals row on top).
SNAPSHOT = [
    ["", "", "", "$1,156.97"], [], [],
    ["Expense", "Periodicity", "Day of Period", "Monthly", "Weekly", "Annually", "Rounded"],
    ["Rent", "Monthly", "1", "$450.00"],
    ["Phone Payment", "Monthly", "7", "$71.15"],
    ["Car Payment", "Monthly", "18", "$338.58"],
    ["Claude AI Subscription", "Monthly", "17", "$106.75"],
    ["Amazon Prime", "Annually"],
    ["Groceries", "", "", "$200.00"],
    ["Motor Fuel"],
]


def txn(day, account, description, amount, balance, category):
    return {"account": account, "date": day, "description": description, "amount": amount,
            "balance": balance, "category": category}


def feed(*txns, balances=None):
    accounts = balances or {}
    for t in txns:
        accounts[t["account"]] = {"balance": t["balance"], "as_of": t["date"]}
    return data.parse_feed({"accounts": accounts, "transactions": list(txns)})


@pytest.fixture
def items():
    return snapshot.parse(SNAPSHOT)


def test_snapshot_kinds_and_amounts(items):
    by = {i.name: i for i in items}
    assert by["Rent"] == snapshot.Item("Rent", "Monthly", 1, 45000)
    assert by["Rent"].kind == "bill"
    assert by["Amazon Prime"].kind == "annual"
    assert by["Groceries"].kind == "spending" and by["Groceries"].amount == 20000
    assert by["Motor Fuel"].kind == "spending" and by["Motor Fuel"].amount is None


def test_snapshot_needs_its_header():
    with pytest.raises(StepError, match="Expense"):
        snapshot.parse([["Rent", "Monthly"]])


SEPT = feed(
    txn("2026-08-07", "bills", "VERIZON WIRELESS", -7115, 50000, "Phone Payment"),
    txn("2026-08-12", "checking", "NETFLIX.COM", -2134, 10000, "Netflix"),
    txn("2026-07-12", "checking", "NETFLIX.COM", -2134, 12134, "Netflix"),
    txn("2026-09-08", "bills", "VERIZON WIRELESS", -14831, 35169, "Phone Payment"),
    txn("2026-09-09", "checking", "STRIPE DES:Instacart", 40000, 50000, "Income: Instacart"),
    txn("2026-09-10", "checking", "GIANT EAGLE #4 09/10 PURCHASE", -5000, 45000, "Groceries"),
    txn("2026-09-12", "checking", "NETFLIX.COM", -2134, 42866, "Netflix"),
    txn("2026-09-13", "checking", "SQ *LEVY @ CLEVEL 09/13 PURCHASE", -1164, 41702, None),
    txn("2026-09-14", "checking", "Online Banking transfer to CHK 2389", -20000, 21702, "Transfer"),
    txn("2026-09-17", "bills", "ANTHROPIC* CLAUDE", -2135, 33034, "Claude AI Subscription"),
    txn("2026-09-17", "bills", "ANTHROPIC* CLAUDE", -10675, 22359, "Claude AI Subscription"),
    txn("2026-09-25", "bills", "EXETERFINA DES:LOAN PMNT", -33858, -11499, "Car Payment"),
)


def test_reconcile_month(items):
    report = reconcile.reconcile(items, SEPT, date(2026, 9, 1))
    bills = {l.name: l for l in report.section(reconcile.BILLS)}
    assert bills["Rent"].status.startswith("never in the bank")
    assert bills["Phone Payment"].status == "paid +$77.16 vs the budget"
    # The $106.75 charge is the budget line; the $21.35 one is listed as an extra.
    assert bills["Claude AI Subscription"].amount == -10675
    assert bills["Car Payment"].status == "paid"
    assert bills["Amazon Prime"].status.startswith("yearly")

    spending = {l.name: l.status for l in report.section(reconcile.SPENDING)}
    assert spending == {"Groceries": "$50.00 of $200.00", "Motor Fuel": "$0.00 (no Monthly amount set)"}

    listed = {l.name: l.status for l in report.section(reconcile.NOT_LISTED)}
    assert listed["Netflix"].endswith("repeats: also in earlier months")
    assert listed["Claude AI Subscription"] == "1× extra charge, beyond the budget line"
    assert listed["Uncategorized: SQ *LEVY @ CLEVEL"] == "1× one-off"
    assert "Transfer" not in listed and "Income: Instacart" not in listed
    assert [l.amount for l in report.section(reconcile.INCOME)] == [40000]
    assert reconcile.as_rows(report)[0] == ["Section", "Item", "Status", "Date", "Amount", "Budget"]


def test_bill_not_yet_due_vs_missing(items):
    f = feed(txn("2026-10-08", "bills", "VERIZON", -7115, 1000, "Phone Payment"),
             txn("2026-09-18", "bills", "EXETERFINA", -33858, 8115, "Car Payment"))
    early = {l.name: l.status for l in reconcile.reconcile(items, f, date(2026, 10, 1)).section(reconcile.BILLS)}
    assert early["Car Payment"] == "due 10/18"  # data only goes to 10/8
    late = feed(txn("2026-10-22", "checking", "GIANT EAGLE", -100, 500, "Groceries"),
                txn("2026-09-18", "bills", "EXETERFINA", -33858, 8115, "Car Payment"))
    report = reconcile.reconcile(items, late, date(2026, 10, 1))
    assert [l.name for l in report.missing] == ["Car Payment"]


def test_timeline_projects_bills_income_and_flags_negative(items):
    f = feed(
        txn("2026-09-25", "bills", "EXETERFINA", -33858, 20000, "Car Payment"),
        txn("2026-10-01", "checking", "STRIPE DES:Instacart", 40000, 40000, "Income: Instacart"),
        txn("2026-10-07", "bills", "VERIZON", -7115, 12885, "Phone Payment"),
        txn("2026-10-08", "checking", "STRIPE DES:Instacart", 40000, 80000, "Income: Instacart"),
        txn("2026-10-15", "checking", "STRIPE DES:Instacart", 40000, 120000, "Income: Instacart"),
        txn("2026-10-16", "checking", "GIANT EAGLE", -1000, 119000, "Groceries"),
        balances={"savings": {"balance": 500, "as_of": "2026-10-16"}},
    )
    tl = timeline.build(items, f)
    assert (tl.start, tl.through, tl.end) == (date(2026, 10, 1), date(2026, 10, 16), date(2026, 11, 30))
    actual = [r for r in tl.rows if not r.projected]
    # After the first row (10/1): Daily has the deposit; Bills still has its start-of-month balance.
    assert actual[0].balances == {"checking": 40000, "bills": 20000, "savings": 500}
    assert actual[-1].balances == {"checking": 119000, "bills": 12885, "savings": 500}
    coming = [(r.date, r.account, r.category, r.amount) for r in tl.rows if r.projected]
    assert (date(2026, 10, 18), "bills", "Car Payment", -33858) in coming  # not paid yet in October
    assert (date(2026, 10, 7), "bills", "Phone Payment", -7115) not in coming  # already paid
    assert (date(2026, 11, 1), "cash", "Rent", -45000) in coming  # never in the bank: cash, no balance change
    assert (date(2026, 10, 22), "checking", "Income: Instacart", 40000) in coming  # weekly, Thursdays
    assert (date(2026, 10, 19), "checking", "Groceries", -4615) in coming  # $200 a month, Mondays
    assert not any(c[2] in ("Amazon Prime", "Motor Fuel") for c in coming)  # yearly / no amount, no history
    negative = tl.first_negative()
    assert set(negative) == {"bills"} and negative["bills"].date == date(2026, 10, 18)
    assert "move at least" in timeline.as_text(tl)
    rows = timeline.as_rows(tl)
    assert rows[0][-5:] == ["Daily", "Bills", "Savings", "Total", "Kind"]
    rent = next(r for r in rows if r[3] == "Rent")
    assert rent[1] == "Cash" and rent[-1] == "projected"


def test_merchant():
    assert data.merchant("WAL-MART #3608 02/14 PURCHASE MADISON OH") == "WAL-MART"
    assert data.merchant("VERIZON WIRELESS DES:PAYMENTS ID:0686") == "VERIZON WIRELESS"


def test_jobs_register_and_share_settings():
    reconcile_job, timeline_job = budget.PLUGINS
    assert timeline_job.settings_id == reconcile_job.id
    problems = reconcile_job.check_ready(reconcile_job.resolve_settings({"budget_url": "https://ai.ts.net"}))
    assert [p.message for p in problems] == ["Set “Google service account key (JSON)” in this job's settings."]


def test_month_input_is_checked(monkeypatch, items):
    report = budget.PLUGIN.run({"month": "October"}, settings={})
    assert not report.ok and "2026-10" in report.error


def test_address_without_scheme_is_a_settings_problem():
    job = budget.PLUGIN
    settings = job.resolve_settings({"budget_url": "ai-workstation.tail1234.ts.net", "service_account_json": "{}"})
    messages = [p.message for p in job.check_ready(settings) if "budget service address" in p.message]
    assert messages and "https://" in messages[0]


def test_unreachable_direct_port_says_to_use_tailscale_serve(monkeypatch):
    monkeypatch.setattr(budget, "reachable", lambda url, timeout: False)
    monkeypatch.setattr(budget.tailscale, "why_unreachable", lambda url: "")
    [problem] = budget.PLUGIN.check_ready_online({"budget_url": "http://100.83.88.59:8092"})
    assert "100.83.88.59:8092" in problem.message and "tailscale serve status" in problem.message
    [problem] = budget.PLUGIN.check_ready_online({"budget_url": "https://ai.tail1234.ts.net"})
    assert "tailscale serve status" not in problem.message

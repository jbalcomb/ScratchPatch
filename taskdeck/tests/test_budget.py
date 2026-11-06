"""Budget jobs: SnapShot and Exceptions parsing, the month reconcile, the balance timeline and ThisWeek.
No network or Google needed."""

from datetime import date

import pytest

from taskdeck.core import StepError
from taskdeck.jobs import budget
from taskdeck.jobs.budget import cashbook, data, exceptions, ledger, reconcile, snapshot, thisweek, timeline

H = ["Account", "Periodicity", "Day of Period", "Monthly", "Weekly", "Annually", "Rounded"]
# The budget sheet's SnapShot tab as the Sheets API returns it: a totals row, Income section, Expense section.
SNAPSHOT = [
    ["Expense totals", "", "", "", "$1,156.97", "$289.24"],
    ["Bills total", "Bills", "", "", "$674.03", "$168.51"],
    ["Bills funding", "Daily → Bills", "Weekly", "Wed", "$1,200.00", "$300.00"],
    ["Income", *H],
    ["Instacart", "Daily", "Weekly", "Wed", "$2,000.00", "$500.00"], [],
    ["Expense", *H],
    ["Rent", "Cash", "Monthly", "1", "$450.00"],
    ["Phone Payment", "Bills", "Monthly", "7", "$71.15"],
    ["Car Payment", "Bills", "Monthly", "18", "$338.58"],
    ["Claude AI Subscription", "Bills", "Monthly", "17", "$106.75"],
    ["LinkedIn", "Daily", "Monthly", "8", "$44.19"],
    ["Amazon Prime", "Daily", "Annually", "4/15", "$12.37", "$3.09", "$148.38"],
    ["Groceries", "Daily", "Weekly", "-", "$240.00", "$60.00"],
    ["Motor Fuel", "Daily", "Weekly", "-", "$0.00", ""],
]
LINKEDIN_ROW = 11  # struck through: cancelled


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
    return snapshot.parse(SNAPSHOT, struck={LINKEDIN_ROW})


def test_snapshot_sections_accounts_and_periods(items):
    by = {i.name: i for i in items}
    assert by["Instacart"] == snapshot.Item("Instacart", "income", "checking", "weekly", 2, 50000)
    assert by["Rent"] == snapshot.Item("Rent", "expense", "cash", "monthly", 1, 45000)
    assert by["Rent"].kind == "bill" and by["Phone Payment"].account == "bills"
    assert by["Amazon Prime"].kind == "annual" and by["Amazon Prime"].day == (4, 15) and by["Amazon Prime"].amount == 14838
    assert by["Groceries"].kind == "spending" and by["Groceries"].amount == 6000 and by["Groceries"].weekly
    assert by["Motor Fuel"].amount is None
    assert by["LinkedIn"].cancelled and not by["Car Payment"].cancelled
    assert by["Bills funding"] == snapshot.Item("Bills funding", "transfer", "bills", "weekly", 2, 30000)
    assert "Bills total" not in by and "Expense totals" not in by


def test_status_column_decides_cancelled():
    rows = [["Expense", "Status", *H], ["YMCA", "Cancelled", "Daily", "Monthly", "8", "$52.68"],
            ["Netflix", "Active", "Bills", "Monthly", "25", "$21.34"]]
    by = {i.name: i for i in snapshot.parse(rows, struck={2})}  # strikethrough is only for people now
    assert by["YMCA"].cancelled and not by["Netflix"].cancelled
    assert by["YMCA"].account == "checking" and by["Netflix"].amount == 2134


def test_snapshot_needs_an_expense_section():
    with pytest.raises(StepError, match="Expense"):
        snapshot.parse([["Rent", "Monthly"]])


def test_week_helpers():
    assert data.week_start(date(2026, 11, 10)) == date(2026, 11, 4)  # Tuesday -> that week's Wednesday
    assert data.week_start(date(2026, 11, 4)) == date(2026, 11, 4)
    assert [d.day for d in data.wednesdays(date(2026, 9, 1))] == [2, 9, 16, 23, 30]  # five Wednesdays


def test_exceptions_tab():
    rows = exceptions.parse([["Match", "Date", "Amount", "Status", "Note"], ["KLARNA", "", "", "Set aside", "?"],
                             ["ASHTABULA COUNTY", "10/8/2026", "$82.68", "Disputed", "claim 1"], ["X", "", "", "maybe", ""]])
    assert rows == [exceptions.Entry("KLARNA", "set aside", note="?"),
                    exceptions.Entry("ASHTABULA COUNTY", "disputed", date(2026, 10, 8), 8268, "claim 1")]


SEPT = feed(
    txn("2026-08-07", "bills", "VERIZON WIRELESS", -7115, 50000, "Phone Payment"),
    txn("2026-08-12", "checking", "NETFLIX.COM", -2134, 10000, "Netflix"),
    txn("2026-07-12", "checking", "NETFLIX.COM", -2134, 12134, "Netflix"),
    txn("2026-09-08", "bills", "VERIZON WIRELESS", -14831, 35169, "Phone Payment"),
    txn("2026-09-08", "checking", "GOOGLE *Linked", -4419, 5581, "LinkedIn"),
    txn("2026-09-09", "checking", "STRIPE DES:Instacart", 40000, 50000, "Instacart"),
    txn("2026-09-10", "checking", "GIANT EAGLE #4 09/10 PURCHASE", -5000, 45000, "Groceries"),
    txn("2026-09-12", "checking", "NETFLIX.COM", -2134, 42866, "Netflix"),
    txn("2026-09-13", "checking", "SQ *LEVY @ CLEVEL 09/13 PURCHASE", -1164, 41702, None),
    txn("2026-09-14", "checking", "Online Banking transfer to CHK 2389", -20000, 21702, "Transfer"),
    txn("2026-09-15", "checking", "BKOFAMERICA ATM 09/15 WITHDRWL", -10000, 11702, "Cash"),
    txn("2026-09-15", "checking", "KLARNA*KLARNA 09/15 PURCHASE", -580, 11122, "Buy now pay later"),
    txn("2026-09-16", "checking", "STRIPE DES:Instacart", 61000, 72122, "Instacart"),
    txn("2026-09-17", "bills", "ANTHROPIC* CLAUDE", -2135, 33034, "Claude AI Subscription"),
    txn("2026-09-17", "bills", "ANTHROPIC* CLAUDE", -10675, 22359, "Claude AI Subscription"),
    txn("2026-09-18", "bills", "EXETERFINA DES:LOAN PMNT", -33858, -11499, "Car Payment"),
    txn("2026-09-19", "bills", "Temporary Credit Adjustment on 09/19/26 Claim # 1", 33858, 22359, "Refunds"),
)
EXCEPTIONS = [exceptions.Entry("KLARNA", "set aside", note="look into later"),
              exceptions.Entry("EXETERFINA", "disputed", date(2026, 9, 18), 33858, "claim 1")]


def test_reconcile_month(items):
    report = reconcile.reconcile(items, SEPT, date(2026, 9, 1), EXCEPTIONS)
    bills = {l.name: l for l in report.section(reconcile.BILLS)}
    assert bills["Rent"].status == "not paid: no Rent entry in the Cash tab"  # September's rent: a debt now
    assert bills["Phone Payment"].status == "paid +$77.16 vs the budget"
    # The $106.75 charge is the budget line; the $21.35 one is listed as an extra.
    assert bills["Claude AI Subscription"].amount == -10675
    assert bills["Car Payment"].status == "disputed $338.58, temporary credit received 9/19"
    assert bills["LinkedIn"].status == "cancelled, but still charged"
    assert bills["Amazon Prime"].status == "yearly, due 4/15"

    # Weekly lines week by week: September has five Wednesdays.
    groceries = [l for l in report.section(reconcile.SPENDING) if l.name == "Groceries" or l.detail]
    assert groceries[0].status == "$50.00 of $300.00 (5 weeks × $60.00 target)"
    assert [l.name for l in groceries[1:6]] == ["week of 9/2", "week of 9/9", "week of 9/16", "week of 9/23",
                                                "week of 9/30"]
    assert groceries[2].status == "$50.00 of $60.00" and groceries[5].status == "not yet"
    income = report.section(reconcile.INCOME)
    assert income[0].status == "$1,010.00 of $2,500.00 (5 weeks × $500.00 baseline)"
    assert [l.status for l in income[1:4]] == ["$0.00 of $500.00", "$400.00 of $500.00", "$610.00 of $500.00 so far"]

    listed = {l.name: l.status for l in report.section(reconcile.NOT_LISTED)}
    assert listed["Netflix"].endswith("repeats: also in earlier months")
    assert listed["Claude AI Subscription"] == "1× extra charge, beyond the budget line"
    assert listed["Uncategorized: SQ *LEVY @ CLEVEL"] == "1× one-off"
    for gone in ("Transfer", "Cash", "Instacart", "Buy now pay later", "Refunds"):
        assert gone not in listed  # moves, income, set aside, or a dispute's credit
    assert [(l.name, l.status) for l in report.section(reconcile.SET_ASIDE)] == [
        ("KLARNA", "set aside: look into later")]
    assert report.section(reconcile.DISPUTED)[0].status == ("disputed $338.58, temporary credit received 9/19 "
                                                             "(claim 1)")
    assert reconcile.as_rows(report)[0] == ["Section", "Item", "Status", "Date", "Amount", "Budget"]


def test_bill_not_yet_due_vs_missing(items):
    f = feed(txn("2026-10-08", "bills", "VERIZON", -7115, 1000, "Phone Payment"),
             txn("2026-09-18", "bills", "EXETERFINA", -33858, 8115, "Car Payment"))
    early = {l.name: l.status for l in reconcile.reconcile(items, f, date(2026, 10, 1)).section(reconcile.BILLS)}
    assert early["Car Payment"] == "due 10/18"  # data only goes to 10/8
    assert early["LinkedIn"] == "cancelled"
    late = feed(txn("2026-10-22", "checking", "GIANT EAGLE", -100, 500, "Groceries"),
                txn("2026-09-18", "bills", "EXETERFINA", -33858, 8115, "Car Payment"))
    report = reconcile.reconcile(items, late, date(2026, 10, 1))
    assert [l.name for l in report.missing] == ["Car Payment"]


OCT = feed(
    txn("2026-09-25", "bills", "EXETERFINA", -33858, 20000, "Car Payment"),
    txn("2026-10-07", "bills", "VERIZON", -7115, 12885, "Phone Payment"),
    txn("2026-10-14", "checking", "STRIPE DES:Instacart", 40000, 40000, "Instacart"),
    txn("2026-10-15", "checking", "BKOFAMERICA ATM 10/15 WITHDRWL", -10000, 30000, "Cash"),
    txn("2026-10-16", "checking", "GIANT EAGLE", -1000, 29000, "Groceries"),
    balances={"savings": {"balance": 500, "as_of": "2026-10-16"}},
)


def test_timeline(items):
    items = [i for i in items if i.section != "transfer"]  # Bills funding has its own test
    tl = timeline.build(items, OCT)  # Friday 10/16 is the last day of bank data
    assert (tl.start, tl.through, tl.end) == (date(2026, 10, 1), date(2026, 10, 16), date(2026, 11, 30))
    actual = [r for r in tl.rows if r.kind == timeline.ACTUAL]
    # October's rent (cash, due 10/1) isn't in the Cash tab: not paid, so not on the timeline (it's a debt now).
    assert not any(r.category == "Rent" and r.date.month == 10 for r in tl.rows)
    assert tl.rows[0].category == "Phone Payment"
    assert tl.rows[0].balances == {"checking": 0, "bills": 12885, "savings": 500, "cash": 0}
    # The ATM withdrawal moves $100 from Daily into Cash.
    atm = [r for r in actual if r.category == "Cash"]
    assert [(r.account, r.amount) for r in atm] == [("checking", -10000), ("cash", 10000)]
    assert actual[-1].balances == {"checking": 29000, "bills": 12885, "savings": 500, "cash": 10000}

    coming = [(r.date, r.account, r.description, r.amount) for r in tl.rows if r.kind == timeline.PROJECTED]
    assert (date(2026, 10, 18), "bills", "Car Payment", -33858) in coming  # not paid yet in October
    assert not any(c[2] == "Phone Payment" and c[0].month == 10 for c in coming)  # already paid
    assert (date(2026, 11, 1), "cash", "Rent", -45000) in coming
    assert (date(2026, 10, 21), "checking", "Instacart (baseline)", 50000) in coming  # $500 every Wednesday
    assert (date(2026, 10, 17), "checking", "Groceries (rest of this week)", -5000) in coming  # $60 - $10 spent
    assert (date(2026, 10, 21), "checking", "Groceries (week)", -6000) in coming
    assert not any(c[2].startswith(("LinkedIn", "Amazon Prime", "Motor Fuel")) for c in coming)
    assert "Motor Fuel: no amount set" in " ".join(tl.notes)

    # Rent's money is kept in Bills: moved to Daily, withdrawn there (its ATM card), then paid in cash.
    assert sorted((c[1], c[3]) for c in coming if c[0] == date(2026, 11, 1)) == sorted([
        ("bills", -45000), ("checking", 45000), ("checking", -45000), ("cash", 45000), ("cash", -45000)])
    negative = tl.first_negative()
    assert set(negative) == {"bills"} and negative["bills"].date == date(2026, 10, 18)
    assert "move at least" in timeline.as_text(tl)
    rows = timeline.as_rows(tl)
    assert rows[0][-6:] == ["Daily", "Bills", "Save", "Cash", "Total", "Actual / Expected"]


def test_income_without_a_day_is_not_projected_as_spending():
    items = snapshot.parse([["Income", *H], ["Tips", "Daily", "Weekly", "-", "$400.00", "$100.00"], ["Expense", *H],
                            ["Rent", "Cash", "Monthly", "1", "$450.00"]])
    tl = timeline.build(items, OCT)
    assert not any(r.category == "Tips" for r in tl.rows) and "Tips: income with no Day of Period" in tl.notes[0]


def test_timeline_shows_bills_funding_every_wednesday(items):
    tl = timeline.build(items, OCT)  # bank data ends Friday 10/16; no transfer to Bills since Wednesday 10/14
    funding = [(r.date, r.account, r.amount) for r in tl.rows if r.description.startswith("Bills funding")]
    assert sorted(funding[:4]) == [(date(2026, 10, 17), "bills", 30000), (date(2026, 10, 17), "checking", -30000),
                                   (date(2026, 10, 21), "bills", 30000), (date(2026, 10, 21), "checking", -30000)]
    assert all(c == "Transfer" for c in [r.category for r in tl.rows if r.description.startswith("Bills funding")])
    # This week's transfer already in the bank data: not projected again.
    paid = feed(txn("2026-10-14", "checking", "Online Banking transfer to CHK 2389", -30000, 10000, "Transfer"),
                txn("2026-10-14", "bills", "Online Banking transfer from CHK 2392", 30000, 30000, "Transfer"),
                txn("2026-10-16", "checking", "GIANT EAGLE", -1000, 9000, "Groceries"))
    days = [r.date for r in timeline.build(items, paid).rows if r.description == "Bills funding (to Bills)"]
    assert days[0] == date(2026, 10, 21)
    # Data ending on a Tuesday: the next one is Wednesday's, not an extra one.
    tuesday = feed(txn("2026-10-20", "checking", "GIANT EAGLE", -1000, 9000, "Groceries"))
    days = [r.date for r in timeline.build(items, tuesday).rows if r.description == "Bills funding (to Bills)"]
    assert days[0] == date(2026, 10, 21) and days.count(date(2026, 10, 21)) == 1


def test_thisweek_leaves_transfers_out_of_income_and_expenses(items):
    tw = thisweek.build(items, OCT, date(2026, 10, 20))
    assert tw.weeks[0].income == 50000 and tw.weeks[0].expenses == 6000  # the $300 to Bills moves balances only


def test_rent_withdrawn_early_is_not_expected_again(items):
    items = [i for i in items if i.section != "transfer"]
    f = feed(txn("2026-10-29", "bills", "BKOFAMERICA ATM 10/29 WITHDRWL", -45000, 10000, "Cash"))
    tl = timeline.build(items, f)
    nov1 = [(r.account, r.amount) for r in tl.rows if r.date == date(2026, 11, 1)]
    assert nov1 == [("cash", -45000)]  # withdrawn on 10/29 already: no second withdrawal
    assert not any(r.account == "cash" and r.balances["cash"] < 0 for r in tl.rows if r.date <= date(2026, 11, 30))
    report = reconcile.reconcile(items, feed(txn("2026-11-02", "checking", "X", -1, 1, None),
                                             txn("2026-10-29", "bills", "BKOFAMERICA ATM 10/29 WITHDRWL", -45000,
                                                 10000, "Cash")), date(2026, 11, 1))
    rent = next(l for l in report.section(reconcile.BILLS) if l.name == "Rent")
    assert rent.status == "due 11/1; ATM withdrawal from Bills 10/29"


CASH_TAB = [["Date", "Description", "Amount", "Line", "Note"],
            ["10/10/2026", "Cash on hand", "$80.00", "", "counted"],
            ["10/12/2026", "Tip", "$15.00", "", ""],
            ["10/13/2026", "Farm stand", "-$12.00", "Groceries", ""],
            ["10/14/2026", "", "", "", "a blank row"],
            ["10/20/2026", "Rent to landlord", "-$450.00", "Rent", "late"],
            ["11/1/2026", "Rent to landlord", "-$450.00", "Rent", ""]]


def test_cash_tab():
    cash = cashbook.parse(CASH_TAB)
    assert [(t.date, t.amount, t.category) for t in cash] == [
        (date(2026, 10, 10), 8000, None), (date(2026, 10, 12), 1500, None), (date(2026, 10, 13), -1200, "Groceries"),
        (date(2026, 10, 20), -45000, "Rent"), (date(2026, 11, 1), -45000, "Rent")]
    assert all(t.account == "cash" and t.balance is None for t in cash)


def test_cash_tab_feeds_the_reconcile_and_timeline(items):
    items = [i for i in items if i.section != "transfer"]
    f = data.with_cash(OCT, cashbook.parse(CASH_TAB))
    report = reconcile.reconcile(items, f, date(2026, 10, 1))
    rent = next(l for l in report.section(reconcile.BILLS) if l.name == "Rent")
    assert (rent.status, rent.date) == ("paid", date(2026, 10, 20))  # paid late, in cash
    groceries = next(l for l in report.section(reconcile.SPENDING) if l.name == "Groceries")
    assert groceries.amount == -2200  # $10 at Giant Eagle + $12 cash at the farm stand
    income = {l.name: l.amount for l in report.section(reconcile.INCOME)}
    assert income["Cash received (no Line)"] == 1500  # the tip; the first row is the cash on hand, not income

    tl = timeline.build(items, f)
    cash_rows = [(r.date, r.description, r.balances["cash"]) for r in tl.rows if r.account == "cash"]
    # $80 on hand, +$15 tip, -$12, +$100 from the ATM (10/15), -$450 rent paid 10/20 (after the bank data).
    assert cash_rows[:5] == [(date(2026, 10, 10), "Cash on hand", 8000), (date(2026, 10, 12), "Tip", 9500),
                             (date(2026, 10, 13), "Farm stand", 8300), (date(2026, 10, 15), "ATM withdrawal", 18300),
                             (date(2026, 10, 20), "Rent to landlord", -26700)]
    # November's rent is already in the Cash tab: no projected withdrawal or payment for it.
    assert not any(r.kind == timeline.PROJECTED and r.date == date(2026, 11, 1) for r in tl.rows)
    assert any(r.kind == timeline.ACTUAL and r.date == date(2026, 11, 1) and r.category == "Rent" for r in tl.rows)


def test_cash_starts_at_zero_without_cash_tab_entries(items):
    tl = timeline.build([i for i in items if i.section != "transfer"], OCT)
    assert [r.balances["cash"] for r in tl.rows if r.account == "cash"][0] == 10000  # just the ATM withdrawal


def test_timeline_includes_yearly_lines_on_their_day(items):
    f = feed(txn("2026-03-20", "checking", "GIANT EAGLE", -1000, 29000, "Groceries"))
    coming = [(r.date, r.description) for r in timeline.build(items, f).rows if r.kind == timeline.PROJECTED]
    assert (date(2026, 4, 15), "Amazon Prime") in coming


def test_thisweek(items):
    tw = thisweek.build(items, OCT, date(2026, 10, 20))  # a Tuesday: the week started Wednesday 10/14
    assert tw.start == date(2026, 10, 14)
    assert [r.date for r in tw.rows] == sorted(r.date for r in tw.rows)
    assert all(date(2026, 10, 14) <= r.date <= date(2026, 10, 20) for r in tw.rows)
    assert {r.description for r in tw.rows} >= {"STRIPE DES:Instacart", "Car Payment", "Groceries (rest of this week)"}
    assert [w.start for w in tw.weeks] == [date(2026, 10, 21), date(2026, 10, 28), date(2026, 11, 4)]
    assert tw.weeks[0].income == 50000 and tw.weeks[0].expenses == 6000  # Instacart, and the groceries week
    rows = thisweek.as_rows(tw)
    assert rows[0] == ["Week of Wednesday 10/14 to Tuesday 10/20"]
    assert ["Next 3 weeks"] in rows
    assert "NEXT 3 WEEKS" in thisweek.as_text(tw)


def test_merchant():
    assert data.merchant("WAL-MART #3608 02/14 PURCHASE MADISON OH") == "WAL-MART"
    assert data.merchant("VERIZON WIRELESS DES:PAYMENTS ID:0686") == "VERIZON WIRELESS"


def test_default_address_is_the_budget_service():
    assert budget.PLUGIN.resolve_settings({})["budget_url"] == "https://budget.tailc54221.ts.net"


def test_one_job_keeps_the_saved_settings():
    assert budget.PLUGINS == (budget.PLUGIN,) and budget.PLUGIN.id == "budget_reconcile"  # settings stay under it
    problems = budget.PLUGIN.check_ready(budget.PLUGIN.resolve_settings({}))
    assert [p.message for p in problems] == ["Set “Google service account key (JSON)” in this job's settings."]


def test_one_run_updates_every_tab(monkeypatch, items):
    class Today(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 20)

    monkeypatch.setattr(budget, "date", Today)
    monkeypatch.setattr(budget.sheets, "read_items", lambda settings: items)
    monkeypatch.setattr(budget.sheets, "read_exceptions", lambda settings: EXCEPTIONS)
    monkeypatch.setattr(budget.sheets, "read_cash", lambda settings: cashbook.parse(CASH_TAB))
    monkeypatch.setattr(budget.sheets, "read_timeline", lambda settings, names: [])
    monkeypatch.setattr(budget.data, "fetch", lambda url, since: OCT)
    report = budget.PLUGIN.run({}, settings={"budget_url": "https://x.ts.net"})
    assert report.ok, report.error
    tabs = report.result.payload["tabs"]
    assert list(tabs) == ["timeline_tab", "projected_tab", "thisweek_tab", "reconcile_tab"]
    rec = tabs["reconcile_tab"]
    assert rec[0] == ["Month", "Section", "Item", "Status", "Date", "Amount", "Budget"]
    assert [m for m in dict.fromkeys(r[0] for r in rec[1:])] == ["2026-10", "2026-09", "2026-08", "2026-07"]
    assert "ThisWeek" not in report.result.text and "AGAINST SNAPSHOT" in report.result.text

    written = []
    monkeypatch.setattr(budget.sheets, "write_tab", lambda settings, tab, note, rows: written.append((tab, note)) or tab)
    assert budget.PLUGIN.try_commit(report.result.payload, budget.PLUGIN.resolve_settings({}))[0]
    assert [(tab, note is not None) for tab, note in written] == [
        ("TimeLine", False), ("Projected", False), ("ThisWeek", True), ("Reconcile (auto)", True)]


def test_candidates_turn_what_was_typed_into_the_serve_address(monkeypatch):
    monkeypatch.setattr(data.tailscale, "full_name",
                        lambda host, timeout=2: "ai-workstation.tail1234.ts.net" if host in ("100.83.88.59", "ai-workstation")
                        else host if host.endswith(".ts.net") else None)
    assert data.candidates("http://100.83.88.59:8092/upload") == [
        "http://100.83.88.59:8092", "https://budget.tail1234.ts.net", "https://budget.tail1234.ts.net:8443",
        "https://ai-workstation.tail1234.ts.net", "https://ai-workstation.tail1234.ts.net:8443",
        "https://100.83.88.59", "https://100.83.88.59:8443"]
    # An old Serve address on the workstation's own name also finds the Tailscale Service.
    assert data.candidates("ai-workstation.tail1234.ts.net/")[:3] == [
        "https://ai-workstation.tail1234.ts.net", "https://budget.tail1234.ts.net", "https://budget.tail1234.ts.net:8443"]
    assert data.candidates("ai-workstation")[1] == "https://budget.tail1234.ts.net"
    assert data.candidates("https://budget.tail1234.ts.net")[:2] == [
        "https://budget.tail1234.ts.net", "https://budget.tail1234.ts.net:8443"]
    assert data.candidates("") == []


def answering(monkeypatch, answers):
    """get_json that answers per base address: a dict, or an HTTP status to fail with."""
    from taskdeck.shared.http import HttpError

    def get_json(url, timeout=30):
        base = url.split("/api/")[0]
        answer = answers.get(base, None)
        if isinstance(answer, dict):
            return answer
        raise HttpError(f"HTTP {answer}" if answer else f"Can't reach {base}", answer)
    monkeypatch.setattr(data, "get_json", get_json)
    monkeypatch.setattr(data, "candidates", lambda typed: ["http://ip:8092", "https://ws.ts.net", "https://ws.ts.net:8443"])
    monkeypatch.setattr(data.tailscale, "why_unreachable", lambda url: "")
    monkeypatch.setattr(budget.sheets, "read_items", lambda settings: snapshot.parse(SNAPSHOT, struck={LINKEDIN_ROW}))
    monkeypatch.setattr(budget.sheets, "read_exceptions", lambda settings: EXCEPTIONS)
    monkeypatch.setattr(budget.sheets, "read_cash", lambda settings: cashbook.parse(CASH_TAB))
    data._answered.clear()


FEED = {"accounts": {"bills": {"balance": 642, "as_of": "2026-10-19"}}, "transactions": []}


def test_service_url_uses_the_address_that_answers_and_says_so(monkeypatch):
    answering(monkeypatch, {"https://ws.ts.net:8443": FEED})
    assert data.service_url("http://ip:8092") == "https://ws.ts.net:8443"
    note, service, sheet, excepted, cash = budget.PLUGIN.check_ready_online({"budget_url": "http://ip:8092"})
    assert not note.blocking and note.settings == {"budget_url": "https://ws.ts.net:8443"}
    assert note.message.startswith("Budget service address updated to https://ws.ts.net:8443")
    assert service.ok and service.message == ("Budget service at https://ws.ts.net:8443: 1 accounts, bank data "
                                              "through 10/19/2026.")
    assert sheet.ok and sheet.message == "Budget sheet: the SnapShot tab lists 1 income and 8 expense lines (1 cancelled)."  # Bills funding isn't one
    assert excepted.ok and excepted.message == "Exceptions tab: 2 charges set aside or disputed."
    assert cash.ok and cash.message == "Cash tab: 5 entries."
    assert all(p.ok for p in budget.PLUGIN.check_ready_online({"budget_url": "https://ws.ts.net:8443"}))  # already right


def test_saving_tidies_the_address():
    tidy = budget.PLUGIN.clean_settings
    assert tidy({"budget_url": " ai.tail1.ts.net/api/transactions?since=x "})["budget_url"] == "https://ai.tail1.ts.net"
    assert tidy({"budget_url": "https://ai.tail1.ts.net:8443/"})["budget_url"] == "https://ai.tail1.ts.net:8443"
    assert tidy({"budget_url": ""})["budget_url"] == ""


def test_service_url_explains_an_older_service_or_no_answer(monkeypatch):
    answering(monkeypatch, {"https://ws.ts.net": 404})
    with pytest.raises(StepError, match="older budget service.*docker compose up -d --build"):
        data.service_url("http://ip:8092")
    answering(monkeypatch, {})
    problem, sheet, excepted, cash = budget.PLUGIN.check_ready_online({"budget_url": "http://ip:8092"})
    assert problem.blocking and "http://ip:8092, https://ws.ts.net" in problem.message
    assert sheet.ok and excepted.ok and cash.ok  # the sheet is checked (and reported) even when the service isn't there


def test_settings_test_reports_both_halves_and_applies_fixes(monkeypatch):
    from taskdeck.core import check_settings

    answering(monkeypatch, {"https://ws.ts.net": FEED})

    def no_sheet(settings):
        raise StepError("Google Sheets refused access. Share the sheet with x@y.iam.gserviceaccount.com as Editor.")
    monkeypatch.setattr(budget.sheets, "read_items", no_sheet)
    monkeypatch.setattr(budget.sheets, "setup_problems", lambda settings: [])
    result = check_settings(budget.PLUGIN, {"budget_url": "100.83.88.59:8092/upload", "service_account_json": "{}"})
    assert result.fixes == {"budget_url": "https://ws.ts.net"} and result.settings["budget_url"] == "https://ws.ts.net"
    assert result.passed == ["Budget service at https://ws.ts.net: 1 accounts, bank data through 10/19/2026."]
    assert not result.ok and "Share the sheet" in result.problems[-1].message


def test_reverse_lookup_asks_tailscale_dns(monkeypatch):
    import socket
    import threading

    from taskdeck.shared import tailscale

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))

    def answer():
        query, client = server.recvfrom(1500)
        name = b"".join(bytes([len(p)]) + p for p in b"ai-workstation.tail1234.ts.net.".split(b".")[:-1]) + b"\x00"
        # Same id, response flags, 1 question, 1 answer; the answer's name points back at the question (0xC00C).
        reply = (query[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00" + query[12:]
                 + b"\xc0\x0c\x00\x0c\x00\x01\x00\x00\x02\x58" + len(name).to_bytes(2, "big") + name)
        server.sendto(reply, client)

    threading.Thread(target=answer, daemon=True).start()
    monkeypatch.setattr(tailscale, "QUAD100", server.getsockname())
    try:
        assert tailscale.full_name("100.83.88.59") == "ai-workstation.tail1234.ts.net"
    finally:
        server.close()
    assert tailscale.full_name("192.168.1.50") is None
    assert tailscale.full_name("ai-workstation.tail1234.ts.net") == "ai-workstation.tail1234.ts.net"


def test_why_the_address_changed():
    assert "443, which addresses leave out" in data.why_address("http://100.83.88.59:8092", "https://ws.ts.net")
    assert "port 8092 doesn't answer" in data.why_address("https://ws.ts.net:8092", "https://ws.ts.net")
    assert data.why_address("https://ws.ts.net", "https://ws.ts.net:8443").endswith("port 8443; port 443 doesn't answer.")
    assert "certificate" in data.why_address("https://ai-workstation", "https://ai-workstation.tail1.ts.net")
    assert "Tailscale Service" in data.why_address("https://ai-workstation.tail1.ts.net", "https://budget.tail1.ts.net")


# ---- TimeLine: the bank, the Cash tab and rows typed on TimeLine, merged -----------------------------------

TL_HEADER = ["Date", "Account", "Description", "Category", "Amount", "Daily", "Bills", "Save", "Cash", "Total"]


def test_timeline_tab_rows():
    rows = ledger.parse([TL_HEADER,
                         ["11/6/2026", "Bills", "Google Play Pass", "", "-5.51", "188.06", "442.47", "0.2", "2", "632.73"],
                         ["11/4/2026", "Daily", "Pizza", "Dining Out", "-$12.00"],
                         ["11/4/2026", "Nowhere", "Bad account", "", "-1"], ["", "Daily", "No date", "", "-1"]],
                        {"Google Play Pass", "Dining Out"})
    assert [(t.date, t.account, t.amount, t.category, t.source) for t in rows] == [
        (date(2026, 11, 6), "bills", -551, "Google Play Pass", "timeline"),  # its Description is a SnapShot line
        (date(2026, 11, 4), "checking", -1200, "Dining Out", "timeline")]


def bank(day, account, description, amount, balance, category=None):
    return data.Txn(account, date.fromisoformat(day), description, amount, balance, category)


def typed(day, account, description, amount, category=None):
    return data.Txn(account, date.fromisoformat(day), description, amount, None, category, data.TIMELINE)


def cash_entry(day, description, amount, category=None):
    return data.Txn("cash", date.fromisoformat(day), description, amount, None, category, data.CASH_TAB)


def test_timeline_header_lost_or_changed_stops_the_job():
    with pytest.raises(StepError, match="header row is missing"):
        ledger.parse([["11/6/2026", "Bills", "Google Play Pass", "", "-5.51"]])
    with pytest.raises(StepError, match="changed.*missing: Category"):
        ledger.parse([["Date", "Account", "Description", "Type", "Amount"], ["11/6/2026", "Bills", "X", "", "-1"]])
    assert ledger.parse([]) == [] and ledger.parse([[], [""]]) == []  # an empty tab: the first run


def test_merge_keeps_only_what_the_bank_and_cash_tab_dont_have():
    exported = [bank("2026-11-06", "bills", "GOOGLE *Play P 11/04 PURCHASE", -551, 44196, "Google Play Pass"),
                bank("2026-11-04", "checking", "BKOFAMERICA ATM 11/04 WITHDRWL", -45000, 10000, "Cash")]
    cash = [cash_entry("2026-11-04", "Cash On-Hand", 200), cash_entry("2026-11-04", "From Daily, To Cash", 45000),
            cash_entry("2026-11-04", "Landlord - Rent", -45000, "Back Rent")]
    rows = [typed("2026-11-04", "bills", "Google Play Pass", -551),          # pending, posted on 11/6: replaced
            typed("2026-11-07", "bills", "Netflix", -2134),                  # still pending: kept
            typed("2026-11-04", "cash", "Rent", -45000),                     # the Cash tab has it: replaced
            typed("2026-11-02", "checking", "ATM for rent", -45000),         # typed before it posted 11/4: replaced
            typed("2026-07-30", "checking", "Old row", -100)]                # before the window: scrolled off
    kept = ledger.merge(exported, cash, rows, date(2026, 8, 1))
    # The Cash tab's +$450 is the cash side of the ATM withdrawal: covered. The other two stay.
    assert [(t.description, t.source) for t in kept] == [
        ("Cash On-Hand", "cash tab"), ("Landlord - Rent", "cash tab"), ("Netflix", "timeline")]


def test_merge_matches_the_earliest_typed_row_and_needs_the_bank_on_or_after_it():
    exported = [bank("2026-11-06", "checking", "SUNOCO 11/05", -4000, 100)]
    rows = [typed("2026-11-05", "checking", "Gas", -4000), typed("2026-11-07", "checking", "Gas again", -4000)]
    kept = ledger.merge(exported, [], rows, date(2026, 8, 1))
    assert [t.description for t in kept] == ["Gas again"]  # the bank's 11/6 can't be a purchase made on 11/7


def test_last_runs_rows_are_replaced_by_the_same_transactions():
    exported = [bank("2026-11-02", "bills", "VERIZON", -15128, 14798, "Phone Payment"),
                bank("2026-10-28", "checking", "BKOFAMERICA ATM", -10000, 5000, "Cash")]
    cash = [cash_entry("2026-11-04", "Cash On-Hand", 200)]
    previous = [typed("2026-11-02", "bills", "VERIZON", -15128), typed("2026-10-28", "cash", "ATM withdrawal", 10000),
                typed("2026-11-04", "cash", "Cash On-Hand", 200)]
    assert ledger.merge(exported, cash, previous, date(2026, 8, 1)) == cash  # nothing typed is left over


def test_pending_rows_carry_on_top_of_the_bank_balance(items):
    items = [i for i in items if i.section != "transfer"]
    f = data.with_cash(OCT, [typed("2026-10-15", "checking", "Pizza", -1200, "Dining Out")])
    tl = timeline.build(items, f)
    daily = [(r.date, r.description, r.balances["checking"]) for r in tl.rows
             if r.account == "checking" and r.kind == timeline.ACTUAL]
    # The bank says $290 after Giant Eagle on 10/16; with the pending $12 pizza (10/15) it's $278.
    assert daily[-1] == (date(2026, 10, 16), "GIANT EAGLE", 27800)
    assert (date(2026, 10, 15), "Pizza", 28800) in daily


def test_timeline_and_projected_tabs(items):
    tl = timeline.build([i for i in items if i.section != "transfer"], OCT, start=date(2026, 8, 1))
    tab = timeline.timeline_rows(tl)
    assert tab[0] == TL_HEADER
    dates = [r[0] for r in tab[1:]]
    assert dates[0] == "10/16/2026" and dates[-1] == "9/25/2026"  # newest first, history from 8/1 on
    projected = timeline.projected_rows(tl)
    assert projected[0] == TL_HEADER + ["Actual / Expected"]
    assert projected[1][-1] == "Expected" and projected[-1][-1] == "Actual"
    assert projected[1][0].startswith("11/")  # the furthest projection, at the top
    assert len(projected) - 1 == len(tl.rows) and len(tab) - 1 == sum(r.kind == timeline.ACTUAL for r in tl.rows)

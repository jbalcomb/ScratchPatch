"""Budget jobs: SnapShot and Exceptions parsing, the month reconcile, the balance timeline and ThisWeek.
No network or Google needed."""

from datetime import date

import pytest

from taskdeck.core import StepError
from taskdeck.jobs import budget
from taskdeck.jobs.budget import data, exceptions, reconcile, snapshot, thisweek, timeline

H = ["Account", "Periodicity", "Day of Period", "Monthly", "Weekly", "Annually", "Rounded"]
# The budget sheet's SnapShot tab as the Sheets API returns it: a totals row, Income section, Expense section.
SNAPSHOT = [
    ["Expense totals", "", "", "", "$1,156.97"], [],
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
LINKEDIN_ROW = 10  # struck through: cancelled


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
    assert bills["Rent"].status == "paid in cash (not in the bank data), due 9/1"
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
    tl = timeline.build(items, OCT)  # Friday 10/16 is the last day of bank data
    assert (tl.start, tl.through, tl.end) == (date(2026, 10, 1), date(2026, 10, 16), date(2026, 11, 30))
    actual = [r for r in tl.rows if r.kind == timeline.ACTUAL]
    # Rent (cash, due 10/1) is assumed paid: Cash starts at what covered it, so it isn't negative.
    assert [r.category for r in tl.rows[:2]] == ["Rent", "Phone Payment"] and tl.rows[0].kind == timeline.ASSUMED
    assert tl.rows[0].balances == {"checking": 0, "bills": 20000, "savings": 500, "cash": 0}
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

    negative = tl.first_negative()
    assert set(negative) == {"bills", "cash"} and negative["bills"].date == date(2026, 10, 18)
    assert negative["cash"].date == date(2026, 11, 1)
    text = timeline.as_text(tl)
    assert "move at least" in text and "take out at least $350.00 in cash" in text
    rows = timeline.as_rows(tl)
    assert rows[0][-6:] == ["Daily", "Bills", "Save", "Cash", "Total", "Kind"]


def test_income_without_a_day_is_not_projected_as_spending():
    items = snapshot.parse([["Income", *H], ["Tips", "Daily", "Weekly", "-", "$400.00", "$100.00"], ["Expense", *H],
                            ["Rent", "Cash", "Monthly", "1", "$450.00"]])
    tl = timeline.build(items, OCT)
    assert not any(r.category == "Tips" for r in tl.rows) and "Tips: income with no Day of Period" in tl.notes[0]


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


def test_jobs_register_and_share_settings():
    reconcile_job, timeline_job, week_job = budget.PLUGINS
    assert timeline_job.settings_id == week_job.settings_id == reconcile_job.id
    assert week_job.tab == "ThisWeek"
    problems = reconcile_job.check_ready(reconcile_job.resolve_settings({}))
    assert [p.message for p in problems] == ["Set “Google service account key (JSON)” in this job's settings."]


def test_month_input_is_checked(monkeypatch, items):
    report = budget.PLUGIN.run({"month": "October"}, settings={})
    assert not report.ok and "2026-10" in report.error


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
    data._answered.clear()


FEED = {"accounts": {"bills": {"balance": 642, "as_of": "2026-10-19"}}, "transactions": []}


def test_service_url_uses_the_address_that_answers_and_says_so(monkeypatch):
    answering(monkeypatch, {"https://ws.ts.net:8443": FEED})
    assert data.service_url("http://ip:8092") == "https://ws.ts.net:8443"
    note, service, sheet, excepted = budget.PLUGIN.check_ready_online({"budget_url": "http://ip:8092"})
    assert not note.blocking and note.settings == {"budget_url": "https://ws.ts.net:8443"}
    assert note.message.startswith("Budget service address updated to https://ws.ts.net:8443")
    assert service.ok and service.message == ("Budget service at https://ws.ts.net:8443: 1 accounts, bank data "
                                              "through 10/19/2026.")
    assert sheet.ok and sheet.message == "Budget sheet: the SnapShot tab lists 1 income and 8 expense lines (1 cancelled)."
    assert excepted.ok and excepted.message == "Exceptions tab: 2 charges set aside or disputed."
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
    problem, sheet, excepted = budget.PLUGIN.check_ready_online({"budget_url": "http://ip:8092"})
    assert problem.blocking and "http://ip:8092, https://ws.ts.net" in problem.message
    assert sheet.ok and excepted.ok  # the sheet is checked (and reported) even when the service isn't there


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

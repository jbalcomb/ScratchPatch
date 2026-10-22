import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget  # noqa: E402

# Made-up rows in the exact layout of a Bank of America checking export (CRLF, summary block first).
EXPORT = (
    'Description,,Summary Amt.\r\n'
    'Beginning balance as of 01/01/2026,,"100.00"\r\n'
    'Total credits,,"1,250.00"\r\n'
    'Total debits,,"-60.00"\r\n'
    'Ending balance as of 01/05/2026,,"1,290.00"\r\n'
    '\r\n'
    'Date,Description,Amount,Running Bal.\r\n'
    '01/01/2026,Beginning balance as of 01/01/2026,,"100.00"\r\n'
    '01/02/2026,"SUNOCO 0408780500 01/01 PURCHASE MADISON OH","-20.00","80.00"\r\n'
    '01/02/2026,"SUNOCO 0408780500 01/01 PURCHASE MADISON OH","-20.00","60.00"\r\n'
    '01/03/2026,"STRIPE DES:Instacart ID:ST-AAAA INDN:TEST CO ID:123 CCD","1,250.00","1,310.00"\r\n'
    '01/05/2026,"crownbill.com 01/04 PURCHASE Cyprus 00","-20.00","1,290.00"\r\n'
)

RULES = "# test rules\nSUNOCO = Gas\nINSTACART = Income\nGIANT EAGLE #4 = Groceries\n"


def args_for(tmp_path, llm_url=None, **kw):
    rules = tmp_path / "rules.txt"
    if not rules.exists():
        rules.write_text(RULES)
    ns = dict(db=tmp_path / "b.db", rules=rules, suggestions=tmp_path / "sugg.txt", llm_url=llm_url,
              folder=tmp_path / "drop", interval=0, once=True)
    ns.update(kw)
    return type("Args", (), ns)()


def rows(db_path):
    return sqlite3.connect(db_path).execute(
        "SELECT date, description, amount, balance, category, account FROM transactions ORDER BY id").fetchall()


def test_parse_skips_summary_and_beginning_balance(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text(EXPORT, newline="")
    parsed = budget.parse_bofa_csv(f)
    assert len(parsed) == 4
    assert parsed[0] == {"date": "2026-01-02", "description": "SUNOCO 0408780500 01/01 PURCHASE MADISON OH",
                         "amount": -2000, "balance": 8000}
    assert parsed[2]["amount"] == 125000


def test_rejects_unknown_format(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("Posted Date,Payee,Amount\n01/02/2026,X,1.00\n")
    try:
        budget.parse_bofa_csv(f)
    except ValueError as e:
        assert "header" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_overlapping_redrop_adds_nothing_but_keeps_identical_same_day_purchases(tmp_path):
    args = args_for(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "a.csv").write_text(EXPORT, newline="")
    budget.cmd_watch(args)
    (drop / "b.csv").write_text(EXPORT, newline="")
    budget.cmd_watch(args)

    stored = rows(args.db)
    assert len(stored) == 4  # both $20 Sunoco purchases kept, no duplicates from the re-drop
    assert [r[4] for r in stored] == ["Gas", "Gas", "Income", None]
    assert {r[5] for r in stored} == {"checking"}
    assert sorted(p.name.split("-", 2)[-1] for p in (drop / "done").iterdir()) == ["a.csv", "b.csv"]


def test_subfolder_is_the_account_and_bad_files_go_to_failed(tmp_path):
    args = args_for(tmp_path)
    (tmp_path / "drop" / "savings").mkdir(parents=True)
    (tmp_path / "drop" / "savings" / "s.csv").write_text(EXPORT, newline="")
    (tmp_path / "drop" / "junk.csv").write_text("not,a,bank,file\n")
    budget.cmd_watch(args)
    assert {r[5] for r in rows(args.db)} == {"savings"}
    assert len(list((tmp_path / "drop" / "failed").iterdir())) == 1


def test_rule_patterns_may_contain_hash(tmp_path):
    rules = budget.load_rules(args_for(tmp_path).rules)
    assert budget.match(rules, "GIANT EAGLE #4 01/03 PURCHASE NORTH MADISON OH") == "Groceries"


def test_editing_rules_then_categorize_applies_to_old_rows(tmp_path):
    args = args_for(tmp_path)
    f = tmp_path / "x.csv"
    f.write_text(EXPORT, newline="")
    args.files, args.account = [f], "checking"
    budget.cmd_ingest(args)
    assert rows(args.db)[3][4] is None

    args.rules.write_text(RULES + "CROWNBILL = Subscriptions\n")
    budget.cmd_categorize(args)
    assert rows(args.db)[3][4] == "Subscriptions"


def test_merchant_is_a_usable_prefix():
    for desc, want in [
        ("WAL-MART #3608 02/14 PURCHASE MADISON OH", "WAL-MART"),
        ("SUNOCO 0408780500 01/01 PURCHASE MADISON OH", "SUNOCO"),
        ("Online Banking transfer to SAV 0235 Confirmation# 1234567890", "Online Banking transfer to SAV"),
        ("STRIPE DES:Instacart ID:ST-AAAA INDN:X", "STRIPE DES:Instacart"),
    ]:
        assert budget.merchant(desc) == want
        assert want.upper() in desc.upper()


def test_suggestions_without_llm_list_unmatched_once(tmp_path):
    args = args_for(tmp_path)
    f = tmp_path / "x.csv"
    f.write_text(EXPORT, newline="")
    args.files, args.account = [f], "checking"
    budget.cmd_ingest(args)
    budget.cmd_ingest(args)  # second run must not repeat the suggestion
    text = args.suggestions.read_text()
    assert text.count("CROWNBILL.COM = ?") == 1
    assert "SUNOCO" not in text
    # suggestions are never applied on their own
    assert rows(args.db)[3][4] is None


def test_suggestions_use_llm_category(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"choices": [{"message": {"content": "Subscriptions\n"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        args = args_for(tmp_path, llm_url=f"http://127.0.0.1:{server.server_port}")
        f = tmp_path / "x.csv"
        f.write_text(EXPORT, newline="")
        args.files, args.account = [f], "checking"
        budget.cmd_ingest(args)
    finally:
        server.shutdown()
    assert "CROWNBILL.COM = Subscriptions" in args.suggestions.read_text()
    assert "crownbill.com" in seen[0]["messages"][0]["content"]
    assert "Gas" in seen[0]["messages"][0]["content"]  # existing categories offered for reuse

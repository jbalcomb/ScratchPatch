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
    ns = dict(db=tmp_path / "b.db", rules=rules, suggestions=tmp_path / "sugg.txt", llm_url=llm_url, llm_key=None, llm_model=None,
              folder=tmp_path / "drop", interval=0, once=True, force=False, n=None, upto=None)
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
    archived = (tmp_path / "archive" / "checking").iterdir()
    assert sorted(p.name.split("-", 2)[-1] for p in archived) == ["a.csv", "b.csv"]
    assert not list(drop.glob("*.csv"))


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
            seen.append(self.headers.get("Authorization"))
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
        args = args_for(tmp_path, llm_url=f"http://127.0.0.1:{server.server_port}", llm_key="sekrit")
        f = tmp_path / "x.csv"
        f.write_text(EXPORT, newline="")
        args.files, args.account = [f], "checking"
        budget.cmd_ingest(args)
    finally:
        server.shutdown()
    assert "CROWNBILL.COM = Subscriptions" in args.suggestions.read_text()
    assert "crownbill.com" in seen[0]["messages"][0]["content"]
    assert "Gas" in seen[0]["messages"][0]["content"]  # existing categories offered for reuse
    assert seen[1] == "Bearer sekrit"


# ---------- balance check, upload log, undo / rebuild ----------

def export(*txns):
    """A BofA-layout export of (MM/DD/YYYY, description, amount, running balance) rows."""
    body = "".join(f'{d},"{desc}","{a:.2f}","{b:.2f}"\r\n' for d, desc, a, b in txns)
    return 'Description,,Summary Amt.\r\n\r\nDate,Description,Amount,Running Bal.\r\n' + body


# Carries on from EXPORT's closing balance of 1,290.00, sharing its last row.
LATER = export(("01/05/2026", "crownbill.com 01/04 PURCHASE Cyprus 00", -20.00, 1290.00),
               ("01/06/2026", "SUNOCO 1 01/06 PURCHASE MADISON OH", -30.00, 1260.00))
# Carries on from 1,290.00 with nothing in common.
NEXT = export(("01/06/2026", "SUNOCO 1 01/06 PURCHASE MADISON OH", -30.00, 1260.00))
# A savings account's file: its balances don't continue checking's.
OTHER = export(("01/06/2026", "Interest Earned", 0.05, 500.05))


def write(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text, newline="")
    return f


def stored_import(tmp_path, text, name="f.csv", account="checking", **kw):
    db = budget.connect(tmp_path / "b.db")
    try:
        return budget.import_file(db, write(tmp_path, name, text), account, **kw)
    finally:
        db.close()


def test_balance_check_accepts_continuing_and_overlapping_files(tmp_path):
    assert stored_import(tmp_path, EXPORT)["new"] == 4
    assert stored_import(tmp_path, EXPORT)["same_as"] == 1  # identical re-upload: noted, adds nothing
    assert stored_import(tmp_path, NEXT)["new"] == 1
    assert stored_import(tmp_path, OTHER, account="savings")["new"] == 1  # first file for an account


def test_balance_check_refuses_another_accounts_file(tmp_path):
    stored_import(tmp_path, EXPORT)
    for text, why in [(OTHER, "starts from $500.00, but checking's last stored balance (2026-01-05) is $1,290.00"),
                      (export(("01/03/2026", "Interest Earned", 0.05, 500.05)), "none of its transactions match"),
                      (export(("12/30/2025", "Interest Earned", 0.05, 99.00)), "ends at $99.00")]:
        try:
            stored_import(tmp_path, text)
        except budget.BalanceMismatch as e:
            assert why in str(e)
        else:
            raise AssertionError(f"expected a mismatch: {why}")
    assert len(rows(tmp_path / "b.db")) == 4
    assert not (tmp_path / "archive" / "checking").exists() or len(list((tmp_path / "archive" / "checking").iterdir())) == 1
    assert stored_import(tmp_path, OTHER, force=True)["new"] == 1


def test_another_accounts_file_is_refused_even_into_an_empty_account(tmp_path):
    stored_import(tmp_path, EXPORT, account="savings")
    try:
        stored_import(tmp_path, EXPORT, name="20261025-143433-20261025-143427-save_stmt.csv")
    except budget.BalanceMismatch as e:
        assert "4 of its 4 transactions are already stored under savings" in str(e)
    else:
        raise AssertionError("expected a mismatch")
    imp = stored_import(tmp_path, EXPORT, name="20261025-143433-20261025-143427-save_stmt.csv", account="savings")
    archived = sorted(p.name for p in (tmp_path / "archive" / "savings").iterdir())
    assert archived[-1].endswith("-save_stmt.csv") and archived[-1].count("2026") == 1
    assert imp["same_as"] == 1


def test_watcher_puts_a_mismatched_file_in_failed(tmp_path):
    args = args_for(tmp_path)
    (tmp_path / "drop").mkdir()
    write(tmp_path / "drop", "a.csv", EXPORT)
    budget.cmd_watch(args)
    write(tmp_path / "drop", "save_stmt.csv", OTHER)
    budget.cmd_watch(args)
    assert len(rows(args.db)) == 4
    assert [p.name.endswith("save_stmt.csv") for p in (tmp_path / "drop" / "failed").iterdir()] == [True]


def test_undo_keeps_rows_a_later_upload_also_had_and_redo_restores(tmp_path):
    args = args_for(tmp_path)
    stored_import(tmp_path, EXPORT)          # 1
    stored_import(tmp_path, LATER)           # 2: shares EXPORT's last row
    assert len(rows(args.db)) == 5

    args.n = 1
    budget.cmd_undo(args)
    assert [r[1] for r in rows(args.db)] == ["crownbill.com 01/04 PURCHASE Cyprus 00",
                                             "SUNOCO 1 01/06 PURCHASE MADISON OH"]
    budget.cmd_redo(args)
    assert len(rows(args.db)) == 5
    assert [r[4] for r in rows(args.db)].count("Gas") == 3  # recategorized after the rebuild

    args.n = None
    budget.cmd_undo(args)                    # the latest: #2
    assert len(rows(args.db)) == 4


def test_rebuild_upto_last_good_upload_and_backup(tmp_path):
    args = args_for(tmp_path)
    stored_import(tmp_path, EXPORT)
    stored_import(tmp_path, NEXT)
    stored_import(tmp_path, OTHER, force=True)  # the bad one
    args.upto = 2
    budget.cmd_rebuild(args)
    assert len(rows(args.db)) == 5
    undone = sqlite3.connect(args.db).execute("SELECT id FROM imports WHERE undone_at IS NOT NULL").fetchall()
    assert undone == [(3,)]
    assert len(list((tmp_path / "backups").glob("budget-*.db"))) == 1


def test_rebuild_refuses_when_an_archived_file_is_missing(tmp_path):
    args = args_for(tmp_path)
    stored_import(tmp_path, EXPORT)
    for f in (tmp_path / "archive" / "checking").iterdir():
        f.unlink()
    try:
        budget.cmd_rebuild(args)
    except SystemExit as e:
        assert "#1" in str(e)
    else:
        raise AssertionError("expected a refusal")
    assert len(rows(args.db)) == 4  # nothing was emptied


def test_rows_from_before_the_upload_log_are_adopted(tmp_path):
    done = tmp_path / "drop" / "savings" / "done"
    done.mkdir(parents=True)
    write(done, "20261025-143433-save.csv", EXPORT)
    old = sqlite3.connect(tmp_path / "b.db")
    old.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, account TEXT NOT NULL, date TEXT NOT NULL,"
                " description TEXT NOT NULL, amount INTEGER NOT NULL, balance INTEGER, category TEXT,"
                " source_file TEXT, added_at TEXT NOT NULL, UNIQUE (account, date, description, amount, balance))")
    old.execute("INSERT INTO transactions (account, date, description, amount, balance, source_file, added_at)"
                " VALUES ('savings', '2026-01-02', 'X', -100, 900, 'save.csv', '2026-10-25T14:34:33')")
    old.commit()
    old.close()

    db = budget.connect(tmp_path / "b.db")
    assert db.execute("SELECT id, account, file FROM imports").fetchall() == [
        (1, "savings", "drop/savings/done/20261025-143433-save.csv")]
    assert db.execute("SELECT import_id FROM transactions").fetchall() == [(1,)]
    budget.rebuild(db)  # the adopted file replays
    assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4

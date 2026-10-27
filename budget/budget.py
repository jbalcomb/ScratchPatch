"""Budget drop folder: bank CSV exports -> SQLite, categorized by a hand-edited rules file.

    python budget/budget.py watch               # watch the drop folder, ingest new exports
    python budget/budget.py ingest FILE...      # ingest specific files (--force skips the balance check)
    python budget/budget.py categorize          # re-apply rules.txt to every row, refresh suggestions
    python budget/budget.py summary [YYYY-MM]   # totals per category
    python budget/budget.py imports             # every upload, with its number
    python budget/budget.py undo [N]            # take back upload N (default: the latest)
    python budget/budget.py redo N              # put an undone upload back
    python budget/budget.py rebuild [--upto N]  # rebuild the store from the archived files

Standard library only. See budget/README.md.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("BUDGET_HOME", Path.home() / "budget"))
DEFAULT_DB = HOME / "budget.db"
DEFAULT_DROP = HOME / "drop"
DEFAULT_RULES = HERE / "rules.txt"
DEFAULT_SUGGESTIONS = HERE / "rules_suggested.txt"
DEFAULT_ACCOUNT = "checking"

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    account     TEXT NOT NULL,
    date        TEXT NOT NULL,      -- YYYY-MM-DD
    description TEXT NOT NULL,
    amount      INTEGER NOT NULL,   -- cents; negative = money out
    balance     INTEGER,            -- running balance in cents, as the bank reports it
    category    TEXT,               -- NULL until a rule matches
    source_file TEXT,
    added_at    TEXT NOT NULL,
    import_id   INTEGER,            -- the upload that added it (imports.id)
    -- The running balance makes two identical same-day purchases distinct,
    -- and makes the same transaction identical across overlapping exports.
    UNIQUE (account, date, description, amount, balance)
);
-- One row per uploaded file. The file itself is kept (file, relative to the store's folder), so the store
-- can always be rebuilt from the uploads that aren't undone.
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    account     TEXT NOT NULL,
    file        TEXT NOT NULL,
    sha256      TEXT,
    rows        INTEGER,
    new_rows    INTEGER,
    imported_at TEXT NOT NULL,
    undone_at   TEXT
);
"""

# The watcher and the upload page run in one process; one import at a time.
LOCK = threading.Lock()


class BalanceMismatch(ValueError):
    """The file doesn't continue the account's stored running balance: likely the wrong account."""


# ---------- parsing ----------

def cents(text):
    text = text.strip().replace(",", "").replace("$", "")
    if not text:
        return None
    return round(float(text) * 100)


def parse_bofa_csv(path):
    """Parse a Bank of America account export (summary block, blank line, then the transaction table)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header = ["Date", "Description", "Amount", "Running Bal."]
    for i, row in enumerate(rows):
        if [c.strip() for c in row[:4]] == header:
            break
    else:
        raise ValueError(f"{path}: no 'Date,Description,Amount,Running Bal.' header; not a Bank of America export?")

    out = []
    for row in rows[i + 1:]:
        if not any(c.strip() for c in row):
            continue
        date, desc, amount, balance = (row + ["", "", "", ""])[:4]
        if not amount.strip():  # "Beginning balance as of ..." line
            continue
        out.append({
            "date": datetime.strptime(date.strip(), "%m/%d/%Y").strftime("%Y-%m-%d"),
            "description": " ".join(desc.split()),
            "amount": cents(amount),
            "balance": cents(balance),
        })
    return out


# ---------- store ----------

def connect(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30)
    db.executescript(SCHEMA)
    if "import_id" not in [c[1] for c in db.execute("PRAGMA table_info(transactions)")]:
        db.execute("ALTER TABLE transactions ADD COLUMN import_id INTEGER")
    adopt_legacy_rows(db, Path(db_path).parent)
    return db


def adopt_legacy_rows(db, base):
    """Give rows stored before the imports table existed an import each, pointing at their archived file."""
    groups = db.execute("SELECT account, source_file, COUNT(*), MIN(added_at) FROM transactions"
                        " WHERE import_id IS NULL GROUP BY 1, 2 ORDER BY MIN(id)").fetchall()
    for account, source_file, n, added_at in groups:
        found = sorted(p for p in (base / "drop").rglob(f"*-{source_file}") if p.parent.name == "done")
        file = str(found[0].relative_to(base)) if found else ""
        cur = db.execute("INSERT INTO imports (account, file, rows, new_rows, imported_at) VALUES (?, ?, ?, ?, ?)",
                         (account, file, n, n, added_at))
        db.execute("UPDATE transactions SET import_id = ? WHERE import_id IS NULL AND account = ? AND source_file = ?",
                   (cur.lastrowid, account, source_file))
    db.commit()


def insert(db, account, rows, source_file, import_id, added_at):
    before = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO transactions"
        " (account, date, description, amount, balance, source_file, added_at, import_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(account, r["date"], r["description"], r["amount"], r["balance"], source_file, added_at, import_id)
         for r in rows],
    )
    return db.total_changes - before


def money(c):
    return f"${c / 100:,.2f}"


def balance_problem(db, account, rows):
    """Why this file can't belong to `account`, judged by the bank's running balance, or None if it fits.

    A file whose transactions are already stored under another account doesn't fit, even an empty one.
    A file that shares transactions with this account's stored ones fits. Otherwise it must carry on from where
    the stored balance leaves off (or lead into where it starts).
    """
    lo, hi = rows[0]["date"], rows[-1]["date"]
    elsewhere = {}
    for (acct, *key) in db.execute("SELECT account, date, description, amount, balance FROM transactions"
                                   " WHERE account != ? AND date BETWEEN ? AND ?", (account, lo, hi)):
        elsewhere[tuple(key)] = acct
    found = [elsewhere[k] for k in ((r["date"], r["description"], r["amount"], r["balance"]) for r in rows)
             if k in elsewhere]
    if found:
        other = max(set(found), key=found.count)
        return f"{found.count(other)} of its {len(rows)} transactions are already stored under {other}."
    smin, smax = db.execute("SELECT MIN(date), MAX(date) FROM transactions WHERE account = ?", (account,)).fetchone()
    if smin is None or rows[0]["balance"] is None:
        return None
    stored = set(db.execute("SELECT date, description, amount, balance FROM transactions"
                            " WHERE account = ? AND date BETWEEN ? AND ?", (account, lo, hi)))
    if any((r["date"], r["description"], r["amount"], r["balance"]) in stored for r in rows):
        return None
    hint = " Wrong account, or are days missing between the two downloads?"
    if lo >= smax:
        last = db.execute("SELECT balance FROM transactions WHERE account = ? ORDER BY date DESC, id DESC LIMIT 1",
                          (account,)).fetchone()[0]
        opening = rows[0]["balance"] - rows[0]["amount"]
        if opening == last:
            return None
        return f"it starts from {money(opening)}, but {account}'s last stored balance ({smax}) is {money(last)}." + hint
    if hi <= smin:
        bal, amt = db.execute("SELECT balance, amount FROM transactions WHERE account = ? ORDER BY date, id LIMIT 1",
                              (account,)).fetchone()
        if rows[-1]["balance"] == bal - amt:
            return None
        return (f"it ends at {money(rows[-1]['balance'])}, but {account}'s stored history ({smin}) "
                f"starts from {money(bal - amt)}." + hint)
    return f"none of its transactions match {account}'s stored ones for the same dates ({lo} to {hi}). Wrong account?"


def import_file(db, path, account, force=False, name=None, move=False):
    """Check, archive and store one export. Returns the imports row as a dict.

    Raises ValueError for a file that isn't a bank export, BalanceMismatch (unless force) for one that doesn't
    fit the account. Nothing is stored or archived in either case.
    """
    path = Path(path)
    rows = parse_bofa_csv(path)
    if not rows:
        raise ValueError("no transactions in the file")
    if not force:
        problem = balance_problem(db, account, rows)
        if problem:
            raise BalanceMismatch(f"doesn't look like {account}: {problem}")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    same = db.execute("SELECT id FROM imports WHERE account = ? AND sha256 = ? AND undone_at IS NULL",
                      (account, digest)).fetchone()

    base = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
    now = datetime.now()
    name = re.sub(r"^(x*\d{8}-\d{6}-)+", "", name or path.name)  # drop timestamps from earlier archiving
    dest = base / "archive" / account / f"{now:%Y%m%d-%H%M%S}-{name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    while dest.exists():
        dest = dest.with_name("x" + dest.name)
    shutil.move(str(path), dest) if move else shutil.copyfile(path, dest)

    stamp = now.isoformat(timespec="seconds")
    cur = db.execute("INSERT INTO imports (account, file, sha256, rows, imported_at) VALUES (?, ?, ?, ?, ?)",
                     (account, str(dest.relative_to(base)), digest, len(rows), stamp))
    new = insert(db, account, rows, dest.name, cur.lastrowid, stamp)
    db.execute("UPDATE imports SET new_rows = ? WHERE id = ?", (new, cur.lastrowid))
    db.commit()
    return {"id": cur.lastrowid, "account": account, "rows": len(rows), "new": new,
            "same_as": same[0] if same else None}


def describe(imp):
    text = (f"upload #{imp['id']}: {imp['rows']} rows, {imp['new']} new "
            f"({imp['rows'] - imp['new']} already stored) -> {imp['account']}")
    if imp["same_as"]:
        text += f"; identical to upload #{imp['same_as']}"
    return text


def backup(db):
    """Copy the store to backups/ before a rebuild; keeps the newest 10.

    Read through a second connection, so the copy is the last committed state: from before the undo/redo
    that `db` has pending.
    """
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    folder = path.parent / "backups"
    folder.mkdir(exist_ok=True)
    dest = folder / f"budget-{datetime.now():%Y%m%d-%H%M%S}.db"
    while dest.exists():
        dest = dest.with_name("x" + dest.name)
    src, out = sqlite3.connect(path, timeout=30), sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()
    for old in sorted(folder.glob("budget-*.db"))[:-10]:
        old.unlink()
    return dest


def rebuild(db):
    """Empty the store and replay every upload that isn't undone, in upload order. No balance checks:
    each file was accepted when it was first uploaded."""
    base = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
    imports = db.execute("SELECT id, account, file, imported_at FROM imports WHERE undone_at IS NULL ORDER BY id"
                         ).fetchall()
    missing = [f"#{i} ({f or 'no file recorded'})" for i, _, f, _ in imports if not f or not (base / f).exists()]
    if missing:
        raise SystemExit("can't rebuild, archived files are missing for: " + ", ".join(missing)
                         + ". Undo those uploads first.")
    saved = backup(db)
    db.execute("DELETE FROM transactions")
    for i, account, file, stamp in imports:
        new = insert(db, account, parse_bofa_csv(base / file), Path(file).name, i, stamp)
        db.execute("UPDATE imports SET new_rows = ? WHERE id = ?", (new, i))
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"rebuilt from {len(imports)} uploads: {total} transactions (previous store saved as {saved})")


# ---------- rules ----------

def load_rules(path):
    """Lines of `PATTERN = Category`. Case-insensitive substring match; first match wins.

    Lines starting with # are comments (patterns may contain #, as in 'GIANT EAGLE #4').
    """
    rules = []
    if not Path(path).exists():
        return rules
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{n}: expected 'PATTERN = Category', got {line!r}")
        pattern, category = (s.strip() for s in line.split("=", 1))
        rules.append((pattern.upper(), category))
    return rules


def match(rules, description):
    d = description.upper()
    for pattern, category in rules:
        if pattern in d:
            return category
    return None


def categorize(db, rules):
    """Re-apply the rules to every row, so edits to rules.txt take effect. Returns the uncategorized count."""
    rows = db.execute("SELECT id, description FROM transactions").fetchall()
    db.executemany("UPDATE transactions SET category = ? WHERE id = ?",
                   [(match(rules, d), i) for i, d in rows])
    db.commit()
    return db.execute("SELECT COUNT(*) FROM transactions WHERE category IS NULL").fetchone()[0]


# ---------- suggestions for unmatched transactions ----------

def merchant(description):
    """A short leading piece of the description that makes a usable rule pattern.

    'WAL-MART #3608 02/14 PURCHASE MADISON OH' -> 'WAL-MART'. Always a prefix, so it matches its own row.
    """
    head = re.split(r"\s\d\d/\d\d\s|\d{4,}|#| ID:", description)[0].strip(" -*")
    return head or description.split(" ")[0]


def load_suggested(path):
    if not Path(path).exists():
        return set()
    return {line.split("=", 1)[0].strip().upper()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")}


def ask_llm(url, merchant_name, examples, categories, api_key=None, model=None):
    """One category for a merchant, from an OpenAI-compatible chat endpoint (llama.cpp's llama-server)."""
    prompt = (
        "Pick a budget category for this bank transaction merchant.\n"
        f"Existing categories: {', '.join(sorted(categories)) or '(none yet)'}\n"
        "Reuse an existing category when one fits; otherwise invent a short one.\n"
        f"Merchant: {merchant_name}\n"
        "Example transactions:\n" + "\n".join(examples) + "\n"
        "Answer with the category name only."
    )
    body = json.dumps({
        **({"model": model} if model else {}),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 400,  # room for a thinking model, if the switch below is ignored
        "chat_template_kwargs": {"enable_thinking": False},  # llama.cpp: skip reasoning for a one-word answer
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:  # llama-server --api-key
        headers["Authorization"] = f"Bearer {api_key}"
    # Accept either the server root or the full endpoint URL.
    endpoint = url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/v1/chat/completions"
    req = urllib.request.Request(endpoint, body, headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = json.load(resp)["choices"][0]["message"]["content"]
    return text.strip().splitlines()[0].strip(" .\"'*") if text.strip() else "?"


def suggest(db, rules, suggestions_path, llm_url=None, llm_key=None, llm_model=None):
    """Append a suggested rule to rules_suggested.txt for each unmatched merchant not already there.

    Nothing is applied: James copies the lines he agrees with into rules.txt.
    """
    rows = db.execute("SELECT description, amount FROM transactions WHERE category IS NULL ORDER BY date").fetchall()
    groups = {}
    for desc, amount in rows:
        groups.setdefault(merchant(desc).upper(), []).append(f"{desc}  {amount / 100:.2f}")
    already = load_suggested(suggestions_path)
    categories = {c for _, c in rules}
    lines = []
    for name, examples in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if name in already:
            continue
        category = "?"
        if llm_url:
            try:
                category = ask_llm(llm_url, name, examples[:5], categories, llm_key, llm_model)
            except Exception as e:  # the suggestion is optional; keep going without it
                print(f"  LLM failed for {name}: {e}", file=sys.stderr)
        lines += [f"# {len(examples)} txns, e.g. {examples[0]}", f"{name} = {category}"]
    if lines:
        new = not Path(suggestions_path).exists()
        with open(suggestions_path, "a", encoding="utf-8") as f:
            if new:
                f.write("# Suggested rules for unmatched transactions. Copy the ones you agree with into rules.txt\n"
                        "# (fix the category or shorten the pattern as needed), then run: budget.py categorize\n")
            f.write(f"\n# added {datetime.now():%Y-%m-%d %H:%M}\n" + "\n".join(lines) + "\n")
    return len(lines) // 2


# ---------- commands ----------

def refresh(db, args):
    rules = load_rules(args.rules)
    left = categorize(db, rules)
    total = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"categorized {total - left}/{total}; {left} unmatched")
    if left:
        n = suggest(db, rules, args.suggestions, args.llm_url, args.llm_key, args.llm_model)
        if n:
            print(f"{n} new rule suggestions in {args.suggestions}")


def account_for(path, drop):
    """Files in drop/<name>/ belong to account <name>; files directly in drop/ to 'checking'."""
    parent = Path(path).parent.resolve()
    return DEFAULT_ACCOUNT if parent == Path(drop).resolve() else parent.name


def cmd_ingest(args):
    db = connect(args.db)
    for f in args.files:
        try:
            with LOCK:
                print(f"{Path(f).name}: {describe(import_file(db, f, args.account, force=args.force))}")
        except BalanceMismatch as e:
            raise SystemExit(f"{Path(f).name}: not stored, {e}\n  to store it anyway, add --force")
    refresh(db, args)


def cmd_categorize(args):
    refresh(connect(args.db), args)


def cmd_watch(args):
    drop = Path(args.folder)
    drop.mkdir(parents=True, exist_ok=True)
    print(f"watching {drop} (Ctrl+C to stop)")
    sizes = {}
    while True:
        for path in sorted(drop.rglob("*.csv")):
            if path.parent.name in ("done", "failed"):
                continue
            size = path.stat().st_size
            if not args.once and sizes.get(path) != size:  # still downloading, or just appeared: wait one more poll
                sizes[path] = size
                continue
            sizes.pop(path, None)
            account = account_for(path, drop)
            db = connect(args.db)
            try:
                with LOCK:
                    imp = import_file(db, path, account, move=True)  # moved into archive/<account>/
                print(f"{path.name}: {describe(imp)}")
                refresh(db, args)
            except Exception as e:
                print(f"{path.name}: FAILED: {e}", file=sys.stderr)
                if isinstance(e, BalanceMismatch):
                    print(f"  to load it anyway: budget.py ingest --force --account {account} FILE", file=sys.stderr)
                failed = path.parent / "failed"
                failed.mkdir(exist_ok=True)
                shutil.move(str(path), failed / f"{datetime.now():%Y%m%d-%H%M%S}-{path.name}")
            finally:
                db.close()
        if args.once:
            return
        time.sleep(args.interval)


def cmd_imports(args):
    db = connect(args.db)
    for i, account, file, rows, new, at, undone in db.execute(
            "SELECT id, account, file, rows, new_rows, imported_at, undone_at FROM imports ORDER BY id"):
        state = f"UNDONE {undone[:16]}" if undone else ""
        print(f"#{i:<4} {at[:16]}  {account:<9} {rows:>5} rows {new:>5} new  {Path(file).name}  {state}")


def cmd_undo(args):
    db = connect(args.db)
    with LOCK:
        if args.n is None:
            row = db.execute("SELECT MAX(id) FROM imports WHERE undone_at IS NULL").fetchone()
            args.n = row[0]
        row = db.execute("SELECT account, file FROM imports WHERE id = ? AND undone_at IS NULL", (args.n,)).fetchone()
        if not row:
            raise SystemExit(f"no upload #{args.n} to undo (see: budget.py imports)")
        db.execute("UPDATE imports SET undone_at = ? WHERE id = ?",
                   (datetime.now().isoformat(timespec="seconds"), args.n))
        print(f"undid upload #{args.n} ({row[0]}, {Path(row[1]).name})")
        rebuild(db)
    refresh(db, args)


def cmd_redo(args):
    db = connect(args.db)
    with LOCK:
        if not db.execute("UPDATE imports SET undone_at = NULL WHERE id = ? AND undone_at IS NOT NULL",
                          (args.n,)).rowcount:
            raise SystemExit(f"upload #{args.n} isn't undone (see: budget.py imports)")
        print(f"restored upload #{args.n}")
        rebuild(db)
    refresh(db, args)


def cmd_rebuild(args):
    db = connect(args.db)
    with LOCK:
        if args.upto is not None:
            n = db.execute("UPDATE imports SET undone_at = ? WHERE id > ? AND undone_at IS NULL",
                           (datetime.now().isoformat(timespec="seconds"), args.upto)).rowcount
            print(f"keeping uploads up to #{args.upto}; undid {n} later ones (budget.py redo N brings one back)")
        rebuild(db)
    refresh(db, args)


def cmd_summary(args):
    db = connect(args.db)
    month = args.month or db.execute("SELECT substr(MAX(date), 1, 7) FROM transactions").fetchone()[0]
    rows = db.execute(
        "SELECT COALESCE(category, '(uncategorized)'), SUM(amount), COUNT(*) FROM transactions"
        " WHERE date LIKE ? GROUP BY 1 ORDER BY SUM(amount)", (f"{month}-%",)).fetchall()
    print(f"{month}")
    for category, total, n in rows:
        print(f"  {category:<28} {total / 100:>10.2f}  ({n})")
    print(f"  {'net':<28} {sum(r[1] for r in rows) / 100:>10.2f}")


def build_parser():
    p = argparse.ArgumentParser(description="Budget drop folder")
    p.add_argument("--db", default=os.environ.get("BUDGET_DB", DEFAULT_DB))
    p.add_argument("--rules", default=os.environ.get("BUDGET_RULES", DEFAULT_RULES))
    p.add_argument("--suggestions", default=os.environ.get("BUDGET_SUGGESTIONS", DEFAULT_SUGGESTIONS))
    # BUDGET_LLM_* wins; the shared LLM_* names in ~/.config/secrets.env are the fallback.
    p.add_argument("--llm-url", default=os.environ.get("BUDGET_LLM_URL") or os.environ.get("LLM_URL"),
                   help="llama.cpp server, e.g. http://localhost:8080 (omit to skip LLM suggestions)")
    p.add_argument("--llm-key", default=os.environ.get("BUDGET_LLM_KEY") or os.environ.get("LLM_API_KEY"),
                   help="the --api-key llama-server was started with (prefer the LLM_API_KEY variable)")
    p.add_argument("--llm-model", default=os.environ.get("BUDGET_LLM_MODEL") or os.environ.get("LLM_MODEL"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("watch")
    s.add_argument("--folder", default=os.environ.get("BUDGET_DROP", DEFAULT_DROP))
    s.add_argument("--interval", type=float, default=5)
    s.add_argument("--once", action="store_true", help="process what is there now, then exit")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("ingest")
    s.add_argument("files", nargs="+")
    s.add_argument("--account", default=DEFAULT_ACCOUNT)
    s.add_argument("--force", action="store_true", help="store it even if the running balance doesn't fit")
    s.set_defaults(func=cmd_ingest)

    sub.add_parser("categorize").set_defaults(func=cmd_categorize)

    s = sub.add_parser("summary")
    s.add_argument("month", nargs="?", help="YYYY-MM (default: latest month in the store)")
    s.set_defaults(func=cmd_summary)

    sub.add_parser("imports").set_defaults(func=cmd_imports)
    s = sub.add_parser("undo")
    s.add_argument("n", nargs="?", type=int, help="upload number (default: the latest)")
    s.set_defaults(func=cmd_undo)
    s = sub.add_parser("redo")
    s.add_argument("n", type=int)
    s.set_defaults(func=cmd_redo)
    s = sub.add_parser("rebuild")
    s.add_argument("--upto", type=int, help="keep uploads up to this number (the last known good one)")
    s.set_defaults(func=cmd_rebuild)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

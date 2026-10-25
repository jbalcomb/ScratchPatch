"""Budget drop folder: bank CSV exports -> SQLite, categorized by a hand-edited rules file.

    python budget/budget.py watch               # watch the drop folder, ingest new exports
    python budget/budget.py ingest FILE...      # ingest specific files
    python budget/budget.py categorize          # re-apply rules.txt to every row, refresh suggestions
    python budget/budget.py summary [YYYY-MM]   # totals per category

Standard library only. See budget/README.md.
"""

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import sys
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
    -- The running balance makes two identical same-day purchases distinct,
    -- and makes the same transaction identical across overlapping exports.
    UNIQUE (account, date, description, amount, balance)
);
"""


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
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    return db


def insert(db, account, rows, source_file):
    now = datetime.now().isoformat(timespec="seconds")
    before = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO transactions (account, date, description, amount, balance, source_file, added_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(account, r["date"], r["description"], r["amount"], r["balance"], source_file, now) for r in rows],
    )
    db.commit()
    return db.total_changes - before


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

def ingest_file(db, path, account, args):
    rows = parse_bofa_csv(path)
    added = insert(db, account, rows, Path(path).name)
    print(f"{Path(path).name}: {len(rows)} rows, {added} new ({len(rows) - added} already stored) -> {account}")
    return added


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
        ingest_file(db, f, args.account, args)
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
            db = connect(args.db)
            try:
                ingest_file(db, path, account_for(path, drop), args)
                refresh(db, args)
                dest = path.parent / "done"
            except Exception as e:
                print(f"{path.name}: FAILED: {e}", file=sys.stderr)
                dest = path.parent / "failed"
            finally:
                db.close()
            dest.mkdir(exist_ok=True)
            shutil.move(str(path), dest / f"{datetime.now():%Y%m%d-%H%M%S}-{path.name}")
        if args.once:
            return
        time.sleep(args.interval)


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


def main(argv=None):
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
    s.set_defaults(func=cmd_ingest)

    sub.add_parser("categorize").set_defaults(func=cmd_categorize)

    s = sub.add_parser("summary")
    s.add_argument("month", nargs="?", help="YYYY-MM (default: latest month in the store)")
    s.set_defaults(func=cmd_summary)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

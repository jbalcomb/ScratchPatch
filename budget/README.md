# Budget drop folder

Drop a Bank of America CSV export into a folder; its transactions land in a SQLite file, categorized by
`rules.txt`, which you edit by hand. Python 3.10+, standard library only: nothing to install.

Part of [PERSONAL_STACK_PLAN.md](../PERSONAL_STACK_PLAN.md) (first task).

## Run it

```bash
python budget/budget.py watch          # leave running; Ctrl+C to stop
```

Then, on Bank of America's site: account → **Download transactions** → **Microsoft Excel format** (it's a
`.csv`). Save it into the drop folder. Within a few seconds the watcher prints how many rows were new, files the
CSV under `drop/done/`, and categorizes everything.

```bash
python budget/budget.py summary            # totals per category for the latest month
python budget/budget.py summary 2026-03    # a specific month
```

The summary covers all accounts together, so transfers between your own accounts net to zero.

Default locations (all under `~/budget/` unless `BUDGET_HOME` is set):

| What | Where | Override |
|---|---|---|
| Drop folder | `~/budget/drop/` | `--folder` or `BUDGET_DROP` |
| Store | `~/budget/budget.db` (table `transactions`) | `--db` or `BUDGET_DB` |
| Rules | `budget/rules.txt` in this repo | `--rules` or `BUDGET_RULES` |
| Suggestions | `budget/rules_suggested.txt` (not committed) | `--suggestions` or `BUDGET_SUGGESTIONS` |

**Accounts.** A file directly in `drop/` is `checking`. Put other accounts' exports in a subfolder named after
the account, e.g. `drop/savings/` or `drop/bills/`.

**Overlapping exports are fine.** Download "this year so far" every time if you like: a transaction already in
the store is skipped (matched on account, date, description, amount and running balance, so two identical
purchases on the same day still both count).

A file the script can't read goes to `drop/failed/` with the reason printed.

## Categories

`rules.txt` lines look like `SUNOCO = Gas`: if the pattern appears anywhere in the description (any case), the
transaction gets that category. The first matching line wins, so put specific patterns above general ones.
After editing:

```bash
python budget/budget.py categorize     # re-applies rules.txt to every stored row
```

Transactions no rule matches are grouped by merchant and written to `rules_suggested.txt`, one suggested line
each. They are **never applied automatically**: copy the lines you agree with into `rules.txt` (fix the category
or shorten the pattern first if needed) and run `categorize`.

### Let the local model suggest categories

Without a model, suggestions read `MERCHANT = ?`. Point it at your llama.cpp server (`llama-server`, which
speaks the OpenAI-style `/v1/chat/completions` API) to get a category filled in:

```bash
BUDGET_LLM_URL=http://192.168.1.50:8080 python budget/budget.py watch
# or: python budget/budget.py --llm-url http://192.168.1.50:8080 categorize
```

Each merchant is asked about once; it's offered your existing categories so it reuses them.

## Tests

```bash
python -m pytest budget/tests
```

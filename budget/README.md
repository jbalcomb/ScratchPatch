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

Without a model, suggestions read `MERCHANT = ?`. Point it at llama.cpp's `llama-server` (OpenAI-style
`/v1/chat/completions`) and it fills in a category. Each merchant is asked about once, and it's offered your
existing categories so it reuses them.

| Variable | Value |
|---|---|
| `BUDGET_LLM_URL` | `http://localhost:8080` on the AI Workstation itself |
| `BUDGET_LLM_KEY` | the `--api-key` llama-server was started with (sent as `Authorization: Bearer ...`) |

Keep the key out of the repo and out of chat: put it in `~/.config/secrets.env` (below).

## Run it on the AI Workstation

It has the battery backup and runs llama.cpp, so the key never leaves that machine.

1. Clone the repo to `~/ScratchPatch` (or edit `WorkingDirectory` in `budget-watch.service`).
2. Create the secrets file, readable only by you:

   ```bash
   mkdir -p ~/.config && touch ~/.config/secrets.env && chmod 600 ~/.config/secrets.env
   nano ~/.config/secrets.env
   ```

   ```
   BUDGET_LLM_URL=http://localhost:8080
   BUDGET_LLM_KEY=your-llama-server-key
   ```

   Other projects on this machine can read the same file (one `KEY=value` per line).
3. Install and start the service, and keep it running when you're logged out:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp budget/budget-watch.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now budget-watch
   loginctl enable-linger "$USER"
   journalctl --user -u budget-watch -f      # watch it work
   ```

To run a command by hand with the same settings: `set -a; . ~/.config/secrets.env; set +a`, then
`python3 budget/budget.py categorize`.

## Tests

```bash
python -m pytest budget/tests
```

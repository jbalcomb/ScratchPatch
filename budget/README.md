# Budget drop folder

Upload a Bank of America CSV export from any device at home (or drop it into a folder); its transactions land in a SQLite file, categorized by
`rules.txt`, which you edit by hand. Python 3.10+, standard library only: nothing to install.

Part of [PERSONAL_STACK_PLAN.md](../PERSONAL_STACK_PLAN.md) (first task).

## Run it

```bash
python budget/budget.py watch          # watch the drop folder; Ctrl+C to stop
python budget/web.py                   # or: the upload page on port 8090, plus the watcher
```

On Bank of America's site: account → **Download transactions** → **Microsoft Excel format** (it's a `.csv`).
Then either open **http://100.83.88.59:8090** (the AI Workstation over Tailscale) on your phone or any computer, pick the account and drop the file, or
save it straight into the drop folder. Within a few seconds the watcher stores the new rows, files the CSV under
`drop/done/`, and categorizes everything.

The page checks the file is a Bank of America export before saving it. It has no login, so keep it on the
home network or Tailscale; don't forward the port to the internet.

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

**Accounts.** A file directly in `drop/` is `checking`; `drop/bills/` and `drop/savings/` are those accounts.
(The upload page does this for you.)

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

1. Clone the repo to `~/ScratchPatch` (or edit `WorkingDirectory` in `budget.service`).
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
3. Install and start the service (upload page + watcher), and keep it running when you're logged out:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp budget/budget.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now budget
   loginctl enable-linger "$USER"
   journalctl --user -u budget -f      # watch it work
   ```

   Then open http://100.83.88.59:8090 on the phone (Tailscale on) and add it to the home screen.
   If `ufw` is on: `sudo ufw allow 8090/tcp`.

### From the phone

The upload page is the everyday path. For anything else (`summary`, `categorize`, editing `rules.txt`), use
Tailscale SSH: run `sudo tailscale set --ssh` once on the workstation, then connect from an SSH app on the phone
(Termius or JuiceSSH) to `100.83.88.59` with your Linux username. No keys or passwords to manage.

To run a command by hand with the same settings: `set -a; . ~/.config/secrets.env; set +a`, then
`python3 budget/budget.py categorize`.

## Tests

```bash
python -m pytest budget/tests
```

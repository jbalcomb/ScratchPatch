# Budget drop folder

Upload a Bank of America CSV export from any device at home (or drop it into a folder); its transactions land in a SQLite file, categorized by
`rules.txt`, which you edit by hand. Python 3.10+, standard library only: nothing to install.

Part of [PERSONAL_STACK_PLAN.md](../PERSONAL_STACK_PLAN.md) (first task).

## Run it

```bash
python budget/budget.py watch          # watch the drop folder; Ctrl+C to stop
python budget/web.py                   # or: the upload page on port 8092, plus the watcher
```

On Bank of America's site: account → **Download transactions** → **Microsoft Excel format** (it's a `.csv`).
Then either open the upload page on your phone or any computer (the address is set up below), pick the account and drop the file, or
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
| Suggestions | `budget/rules_suggested.txt`; in Docker, `~/budget/rules_suggested.txt` | `--suggestions` or `BUDGET_SUGGESTIONS` |

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
| `LLM_URL` | llama-server's address, root or full `/v1/chat/completions` URL, e.g. `http://localhost:11435` |
| `LLM_API_KEY` | the `--api-key` llama-server was started with (sent as `Authorization: Bearer ...`) |
| `LLM_MODEL` | optional model name |

`BUDGET_LLM_URL` / `BUDGET_LLM_KEY` / `BUDGET_LLM_MODEL` override these for this project only.

Keep the key out of the repo and out of chat: put it in `~/.config/secrets.env` (below).

## Run it on the AI Workstation (Docker)

It has the battery backup and runs llama.cpp, so the key never leaves that machine. The page and the watcher run
in one container; your data stays in `~/budget` on the workstation, outside the container.

1. Clone the repo, and create the data folder and the secrets file (readable only by you):

   ```bash
   git clone -b ccr-860724ba-1ahp18 https://github.com/jbalcomb/ScratchPatch.git ~/ScratchPatch   # branch until merged
   mkdir -p ~/budget ~/.config && touch ~/.config/secrets.env && chmod 600 ~/.config/secrets.env
   nano ~/.config/secrets.env
   ```

   ```
   LLM_URL=http://localhost:11435
   LLM_API_KEY=your-llama-server-key
   LLM_MODEL=your-model-name
   ```

   Other projects on this machine can read the same file. Write it as plain `KEY=value`: no spaces around `=`
   and no quotes (Docker keeps them as part of the value; watch for curly quotes pasted from a phone or doc).
2. Start it. It restarts on its own after a crash or reboot:

   ```bash
   cd ~/ScratchPatch/budget
   docker compose up -d --build
   docker compose logs -f              # watch it work; Ctrl+C stops watching, not the service
   ```

   If `id -u` isn't `1000`, start it with `UID=$(id -u) GID=$(id -g) docker compose up -d --build` so the files
   it writes in `~/budget` stay yours.
3. Give it an HTTPS address with **Tailscale Serve**, so only your own devices can reach it:

   ```bash
   sudo tailscale serve status         # see whether something already uses https:443
   sudo tailscale serve --bg 8092      # or, if 443 is taken: sudo tailscale serve --bg --https=8443 8092
   ```

   It prints the address, like `https://ai-workstation.tail1234.ts.net`. The very first visit can fail with
   `ERR_SSL_PROTOCOL_ERROR` for up to a minute while Tailscale fetches the certificate; reload. If it says HTTPS isn't enabled, turn on
   MagicDNS and HTTPS Certificates in the Tailscale admin console (DNS page) and run it again. The setting
   survives reboots. On the phone, open that address and use Chrome's menu, "Add to Home screen".

The page only listens on the workstation's own `localhost:8092`; Tailscale Serve is the way in, so there is no
firewall port to open.

**After pulling new code:** `git pull && docker compose up -d --build`. Rules edits need no rebuild.

### Commands by hand

```bash
cd ~/ScratchPatch/budget
docker compose exec budget python budget.py summary
docker compose exec budget python budget.py categorize     # after editing rules.txt
```

### From the phone

The upload page is the everyday path. For the commands above, use Tailscale SSH: run `sudo tailscale set --ssh`
once on the workstation, then connect from an SSH app on the phone (Termius or JuiceSSH) to `100.83.88.59` with
your Linux username. No keys or passwords to manage.

## Tests

```bash
python -m pytest budget/tests
```

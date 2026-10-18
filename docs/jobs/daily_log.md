# Daily Log to Sheet

Paste the day's delivery log, tap **Parse log**, check the preview, tap **Save to sheet**. The local AI model
on the AI Workstation only extracts values. All arithmetic happens in code
(`src/taskdeck/jobs/daily_log/record.py`). The full spec is in [daily_log_PRD.md](daily_log_PRD.md).

## One-time setup

### 1. Sheet headers
Row 1 must already contain these headers. The job never creates or fixes them, and it refuses to save if any
header is wrong:

| B | C | D | E | F | G | H | I | J | K | L | M | N | O |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Departure Time | Departure Mileage | Online Time | Offline Time | Arrival Time | Arrival Mileage | Batch Count | Order Count | Earnings | Online Hours | Onclock Hours | **Gas At Mileage** | **Post-Gas Mileage** | **Miles Driven** |

When last checked, the sheet only had B–L. **Add M1, N1 and O1 before the first save.** A1 (the date column) is not checked.

### 2. Google service account (current approach: key stored on the phone)
1. In Google Cloud Console, create a project (or use an existing one) and enable the **Google Sheets API**.
2. *IAM & Admin → Service accounts → Create*. No roles are needed.
3. Open the account → *Keys → Add key → JSON*. A `.json` file downloads.
4. Open the spreadsheet → **Share** → add the service account's `client_email` (in the JSON) as **Editor**.
5. Get the `.json` file onto the phone (e.g. Drive or a USB cable).

### 3. Job settings on the phone
Open the job, tap the **tune** icon (top right):

| Setting | Value |
|---|---|
| AI model URL | the llama.cpp endpoint on the AI Workstation, e.g. `http://192.168.1.50:8080/v1/chat/completions` at home, or its Tailscale name/IP when away. `localhost` will **not** work from the phone. |
| AI model name | whatever llama.cpp expects (`local` is fine for a single-model server) |
| Sheet ID | pre-filled with the Instacart sheet |
| Tab name | `Sheet1` |
| Google service account key | tap **Import file…** and pick the `.json`, or paste its contents |

llama.cpp must listen on the network, not just loopback: start it with `--host 0.0.0.0` (or the
Tailscale IP).

## Security note

With the current setup, the service-account key is stored as plain JSON in TaskDeck's private app storage on
the phone. Anyone who can read that storage (a rooted phone, a backup) can edit any sheet shared with that
service account. Share only this one sheet with it. The planned switches:

1. **Relay on the AI Workstation**: the phone sends the finished record to a small service there, which holds
   the key, checks the headers and appends the row. The key leaves the phone.
2. **Google sign-in on the phone (OAuth)**: no service account at all.

Both go in `jobs/daily_log/sheets.py` as another `SheetBackend` (`header_row()` + `append(row)`). The job, the
preview and the header check don't change.

## How it maps to the PRD

| PRD | Where |
|---|---|
| `parse(text)`: no side effects | the job's pipeline: require → model configured → ask AI model → calculate → preview |
| `commit(record)`: the only side effect | `DailyLog.commit` → `sheets.save_record` (header check, then append) |
| Column map: the only source of layout truth | `record.COLUMNS` (header, record field, preview label, optional flag) |
| Stale preview can't be saved / no double save | the host's job screen: editing any input discards the preview; the Save button is disabled while saving and after success |
| Adding a note type | prompt key in `extract.py`, calculation in `record.build_record`, one `Column` in `record.COLUMNS` |
| Acceptance tests | `tests/test_daily_log.py` (§8 example, no gas, two gas stops, midnight, missing fields, headers missing/swapped, model errors) |

## Open questions (from the PRD, still open)

- Gas math beyond miles driven (gallons, cost, MPG)?
- Will Batch Count be written into the log, or stay blank?
- Warn when saving a date that already has a row? (not checked today)

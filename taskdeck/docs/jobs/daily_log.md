# Daily Log to Sheet

Paste the day's delivery log, tap **Parse log**, check the preview, tap **Save to sheet**. An AI model only
extracts values: the AI Workstation's llama.cpp first, and **Gemini as a fallback** when the workstation is
offline or fails. The phone itself writes the row to the sheet. All arithmetic happens in code
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
| AI Workstation URL | the llama.cpp endpoint, e.g. `http://192.168.1.50:8080/v1/chat/completions` at home, or its Tailscale name/IP when away. `localhost` will **not** work from the phone. |
| AI Workstation model name | whatever llama.cpp expects (`local` is fine for a single-model server) |
| AI Workstation API key | the `--api-key` llama-server was started with (sent as `Authorization: Bearer …`); blank if none |
| Gemini API key (fallback) | create one at [aistudio.google.com](https://aistudio.google.com) → *Get API key*, then tap **Paste**. Pasting the whole curl example AI Studio shows also works; the key is pulled out of it. Leave blank for no fallback. |
| Gemini model(s) | `gemini-flash-latest` by default (resolved to `gemini-3.8-flash` on 2026-10-18); comma-separated list allowed. `gemini-flash-lite-latest` is always tried last. |
| Fallback URL | Gemini's OpenAI-compatible endpoint by default; any OpenAI-compatible chat API works |
| Sheet ID | pre-filled with the Instacart sheet |
| Tab name | `Sheet1` |
| Google service account key | tap **Import file…** and pick the `.json`, or paste its contents |

llama.cpp must listen on the network, not just loopback: start it with `--host 0.0.0.0` (or the
Tailscale IP).

## Which model parses

1. The phone checks (3-second connect) whether the AI Workstation is reachable. If it is, the log goes there.
2. If the workstation is unreachable or returns an error, and a Gemini key is set, the log goes to Gemini.
3. The preview title says which one answered ("Row for 10/16/2026 · parsed by Gemini").
4. If both fail, the error names each failure, and the pasted text is kept.

Gemini models are tried in order: the configured model(s), then always `gemini-flash-lite-latest`. **The free
tier has a per-model daily limit; on 2026-10-19 it was only 20 requests/day for `gemini-3.8-flash`**, so a second
model doubles the allowance. Flash-Lite parsed the PRD sample correctly in about 1 s.

* A daily-quota error (HTTP 429 with a long `retryDelay`) moves straight to the next model, and the message says
  when it resets. A short `retryDelay` (≤ 20 s) is waited out once; busy errors (5xx) are retried after 2 s and 5 s.
* Each Gemini request waits up to 30 s. Gemini sometimes *hangs* instead of answering (seen when the quota was
  used up), so a stall moves on to the next model; only the last model is retried once.
* **Settings → Test AI models** sends one tiny request to each configured model and shows the result and time
  (each test counts toward the daily limit).

Tested on 2026-10-18 against Gemini's free tier: the PRD §8 sample produces exactly the expected row, and a
log with two gas stops and a post-midnight arrival is correct. The first test showed Gemini reading
"Start time" as the online time when there was no "Online at" line, so the prompt now spells out which label
maps to which key.

On Gemini's **free tier**, Google may use what you send to improve its products. The log contains times,
earnings and mileage. Enabling billing on the AI Studio project (Google AI Pro includes monthly Cloud credit
that can cover this) makes it paid usage, which isn't used that way.

## Security note

The service-account key, the Gemini key and the AI Workstation key are kept in TaskDeck's **credentials
vault** (see [../CREDENTIALS.md](../CREDENTIALS.md)): Android's encrypted storage, whose key lives in the
phone's keystore and is only released after your fingerprint or phone PIN. They are not in `state.json`.
Still, the service-account key can edit any sheet shared with it: share only this one sheet with it, and
delete the downloaded `.json` from the phone after importing it.

The AI Workstation only parses; it never touches the sheet. The planned next step is **Google sign-in on the
phone (OAuth)**, which removes the service-account key. It goes in `jobs/daily_log/sheets.py` as another
`SheetBackend` (`rows()`, `write()`, `backup()`). The job, the preview and the header check don't change.

## One row per date, no gaps

* **Saving** reads the tab first and writes the new row directly under the last filled row (never
  Google's "append", which guesses where the table is and put a row above the headers when A1 was blank).
  * Date already in the sheet with the same values: nothing is written ("Already in the sheet").
  * Date already there with different values: the differences are listed and a **Replace row N** button
    overwrites that row. Nothing is replaced without that tap.
  * Date there more than once: the save is refused until the sheet is reconciled.
* **Reconcile Daily Log sheet** (a second job, using the same settings) checks the tab and previews the fixes:
  removes blank rows, keeps one row per date (identical copies: the first; differing copies: the most
  complete, listing what differs), rewrites dates typed as text in another form (e.g. 2026-10-18) as M/D/YYYY, and sorts by date. Rows without a readable
  date are kept at the end and pointed out.
* **Empty cells**: "-" means the value is known to be missing, which is fine. An empty calculated column
  (Online Hours, Onclock Hours, Miles Driven) gets "-" when one of its inputs is "-", or its calculated
  value when all inputs are there (same formulas as parsing; `record.DERIVED`). An empty calculated column
  whose input is empty isn't reported, but the empty input is: every other empty cell (except the gas
  columns, where empty means no gas stop) is listed under "check by hand" as one compact line per row (`Row 2 (9/3) Missing: Dep time, Dep mi, ...`, at most two phone lines; short names from `Column.short`). **Apply fixes** first copies the
  tab to a `Backup <date time>` tab, refuses if the sheet changed since the preview, then rewrites rows 2
  onward. Formulas are kept.
* Opening **Daily Log to Sheet** shows a "needs tidying" heads-up with a button to the reconcile job when
  the sheet has any of these problems.

## How it maps to the PRD

| PRD | Where |
|---|---|
| `parse(text)`: no side effects | the job's pipeline: require → model configured → ask AI model → calculate → preview |
| `commit(record)`: the only side effect | `DailyLog.commit` → `sheets.save_record` (header check, one row per date, write below the last filled row) |
| Column map: the only source of layout truth | `record.COLUMNS` (header, record field, preview label, optional flag) |
| Stale preview can't be saved / no double save | the host's job screen: editing any input discards the preview; the Save button is disabled while saving and after success |
| Adding a note type | prompt key in `extract.py`, calculation in `record.build_record`, one `Column` in `record.COLUMNS` |
| Acceptance tests | `tests/test_daily_log.py` (§8 example, no gas, two gas stops, midnight, missing fields, headers missing/swapped, model errors) |

## Open questions (from the PRD, still open)

- Gas math beyond miles driven (gallons, cost, MPG)?
- Will Batch Count be written into the log, or stay blank?

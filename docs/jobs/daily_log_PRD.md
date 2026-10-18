# PRD: “Daily Log → Google Sheet” plug-in

**Status:** Draft v1, 2026-10-18
**Owner:** James
**Audience:** The Claude Code session building the mobile app and its plug-in system.

---

## 1. Summary

The mobile app lets James pick a **job** and kick it off from his phone. Jobs are plug-ins. This document specifies the first plug-in.

The plug-in takes a free-text daily log pasted from his phone, uses a **local AI model** to extract structured data points, calculates derived values in code, shows a preview, and on confirmation **appends one row to a Google Sheet**.

It was first prototyped as a standalone self-hosted web app called DataPhone. The prototype (`app.py`, `index.html`, `requirements.txt`) is the reference implementation of the plug-in logic. This PRD describes the behavior it must have, so the plug-in can be rebuilt inside the host app.

## 2. Background

- The log was originally described as an “employee time log”. The real data is a **delivery/gig-work log**: the target sheet is titled “Instacart” and tracks departure/arrival, online/offline time, batches, orders, earnings, and mileage.
- The log is written by hand as a block for the start of the day, free-form notes, then a block for the end of the day.
- **Infrastructure already in place:** an Ubuntu 25 machine (“the AI Workstation”) running **llama.cpp**, which exposes an OpenAI-style `/v1/chat/completions` endpoint. Everything is self-hosted. The phone reaches it over home Wi-Fi, or remotely via Tailscale.
- The original plan was a self-hosted mobile web app. James has since decided on a “proper mobile app” with plug-in jobs. The platform choice (native vs. installable web app) is left to the builder (see Open Questions).

## 3. Goals and non-goals

**Goals**
- Paste text, get a correct row in the sheet, in a few taps on a phone.
- Keep the AI’s role narrow: extract values. All arithmetic happens in code.
- Never write to a sheet whose layout isn’t what the plug-in expects.
- Make it easy to add new note types later without redesigning.

**Non-goals (for now)**
- Pulling logs automatically from email or any other source. Input is paste only.
- Editing or deleting existing sheet rows.
- Any note type other than gas and mileage.
- Cloud-hosted AI. The model is local.

## 4. Host ↔ plug-in contract (assumptions to reconcile)

The host app is being designed separately, so treat this section as a proposal. The minimum the plug-in needs from the host:

| Plug-in provides | Notes |
|---|---|
| `id`, `name`, `description` | Shown in the job picker. |
| Input definition | One multiline text field (the pasted log). |
| Settings | LLM URL, LLM model, Sheet ID, tab name, path to Google credentials. See §10 and §9. |
| `parse(text) → record` | **No side effects.** Calls the local model, normalizes, calculates. Returns the record shown in the preview. |
| `commit(record)` | **The only step with side effects.** Validates the sheet, then appends the row. |

Why two steps: model output can be wrong, so a human confirms before anything is written.

## 5. User flow

1. Select the job.
2. Paste the log into the text box.
3. Tap **Parse log**. A progress state is shown while the model runs.
4. Review the preview: every sheet column, in order, with its value. Missing values show “—”. Anything missing (except gas fields) is listed in a plain sentence, e.g. “Not found: Batch count.”
5. Tap **Save to sheet**. The button is disabled while saving and after success (prevents double rows). It reads “Saved to sheet” on success.
6. **Clear** empties the log and resets the preview.

Rules:
- The preview is read-only. To fix a wrong value, edit the pasted text and parse again.
- Editing the text after a parse discards the preview, so a stale preview can never be saved.
- Errors appear inline in plain language and never lose the pasted text.

## 6. Input format

Free text with three parts: a start-of-day block, notes, and an end-of-day block. The notes can contain anything. Today only gas stops matter. Real sample (the `…` lines stand for notes):

```
Friday October 16th
Starting mileage 52 mi
Start time 10:38
Online at 11:00
…
Got gas at 32 miles
New miles 540 miles
…
Offline at 2146
13 orders for $187.82
Arrival time 2209
Ending mileage? 303
```

Formatting is informal: times may be `10:38` or `2146`, labels vary, and there is no year.

**Important: mileage values are fuel range shown on the dash, not odometer readings.** “Got gas at 32 miles / New miles 540 miles” means the range was 32 when gas was bought and 540 after filling.

## 7. Extraction spec

### 7.1 What the model extracts

The model returns **only a JSON object**. Missing values are `null`.

| Key | Type | Notes |
|---|---|---|
| `month` | number 1–12 | From the date line. |
| `day` | number | From the date line. |
| `departure_time` | `"HH:MM"` 24-hour | “Start time”. |
| `departure_mileage` | number | “Starting mileage”. |
| `online_time` | `"HH:MM"` 24-hour | |
| `offline_time` | `"HH:MM"` 24-hour | |
| `arrival_time` | `"HH:MM"` 24-hour | |
| `arrival_mileage` | number | “Ending mileage”. |
| `batches` | integer | Not present in the sample. Leave `null` if absent. |
| `orders` | integer | |
| `earnings` | number | No currency symbol. |
| `gas_fills` | list of `{at, after}` | One entry per fill-up. `at` is the range when gas was bought, `after` is the range after filling. |

System prompt used by the prototype (use as the starting point and tune against real logs):

```
You extract data from a delivery driver's daily log. Reply with ONLY a JSON object, no other text.
Keys (use null when a value is missing):
  month: number 1-12
  day: number
  departure_time: "HH:MM" 24-hour
  departure_mileage: number
  online_time: "HH:MM" 24-hour
  offline_time: "HH:MM" 24-hour
  arrival_time: "HH:MM" 24-hour
  arrival_mileage: number
  batches: integer
  orders: integer
  earnings: number, no currency symbol
  gas_fills: list of {"at": number, "after": number}, one per fill-up
Notes: times like 2146 mean 21:46. Mileage values are fuel range shown on the dash, not odometer readings.
For a fill-up, "at" is the range when gas was bought and "after" is the range after filling.
```

Call settings: `temperature: 0`, `response_format: {"type": "json_object"}`. Be tolerant of stray text around the JSON (extract the first `{…}` block).

### 7.2 Normalization and calculations (in code, never in the model)

- **Times:** accept `HH:MM` or `HHMM` (3–4 digits); output `HH:MM`. Invalid becomes `null`.
- **Numbers:** strip `$` and commas. Whole numbers are integers.
- **Date:** the log has no year. Use the current year; if that puts the date more than 7 days in the future, use the previous year. Output `MM/DD/YYYY`.
- **Online Hours** = `offline_time − online_time`, in hours, rounded to 2 decimals.
- **Onclock Hours** = `arrival_time − departure_time`, in hours, rounded to 2 decimals.
- For both hours values, if the difference is negative the span crossed midnight: add 24.
- **Miles Driven** (range burned), handling any number of fill-ups:
  ```
  total = 0; prev = departure_mileage
  for each fill: total += prev - fill.at; prev = fill.after
  miles_driven = total + prev - arrival_mileage
  ```
  With no gas stop this reduces to `departure_mileage − arrival_mileage`. If either end mileage is missing, the result is `null`.
- **Gas columns:** with one fill, the plain numbers. With several, comma-joined (`"32, 40"`) in the same cell.

## 8. Worked example (acceptance test)

Input: the sample in §6, run on 2026-10-18.

Expected record:

| Field | Value |
|---|---|
| date | 10/16/2026 |
| departure_time / mileage | 10:38 / 52 |
| online_time | 11:00 |
| offline_time | 21:46 |
| arrival_time / mileage | 22:09 / 303 |
| batches | *(blank)* |
| orders | 13 |
| earnings | 187.82 |
| online_hours | 10.77 |
| onclock_hours | 11.52 |
| gas_at / gas_after | 32 / 540 |
| miles_driven | 257 |

Hand-check: (52 − 32) + (540 − 303) = 20 + 237 = 257.

## 9. Google Sheet integration

**Target:** spreadsheet “Instacart”, ID `1RemIJ0pmrN-zj58cpeHd4Jas2ix2ss3aTef14_zEkec`, tab `Sheet1`. Both are settings, not hard-coded.

### 9.1 Column map

A single map is the **only** source of truth for the layout. It links column number → required header → record field. It is used both to validate the sheet and to build the row.

| Col | Letter | Required header | Record field |
|---|---|---|---|
| 1 | A | *(not checked, currently blank)* | `date` |
| 2 | B | Departure Time | `departure_time` |
| 3 | C | Departure Mileage | `departure_mileage` |
| 4 | D | Online Time | `online_time` |
| 5 | E | Offline Time | `offline_time` |
| 6 | F | Arrival Time | `arrival_time` |
| 7 | G | Arrival Mileage | `arrival_mileage` |
| 8 | H | Batch Count | `batches` |
| 9 | I | Order Count | `orders` |
| 10 | J | Earnings | `earnings` |
| 11 | K | Online Hours | `online_hours` |
| 12 | L | Onclock Hours | `onclock_hours` |
| 13 | M | Gas At Mileage | `gas_at` |
| 14 | N | Post-Gas Mileage | `gas_after` |
| 15 | O | Miles Driven | `miles_driven` |

A header of “not checked” (None) means that column’s header isn’t validated.

### 9.2 Header validation (hard requirement)

- The headers **must already exist** in row 1. **The plug-in must never create, rename, or fix headers.** This was an explicit decision by James.
- Before every write, read row 1 and compare each checked column’s header to the map (exact match after trimming surrounding whitespace). A correct name in the wrong column is a failure.
- On any mismatch, **write nothing** and return an error naming each bad cell, e.g.:
  `Sheet headers don’t match: M1 should be “Gas At Mileage” but is “”`
- The host may also run this check when the job opens, so a bad sheet is caught before James does any work (nice to have). The check before every write is required.

**Prerequisite for James:** when last checked, the sheet had headers only in B–L. He must add **M1 `Gas At Mileage`, N1 `Post-Gas Mileage`, O1 `Miles Driven`** before the first save.

### 9.3 Writing the row

- Build the row by column number from the map (`max(column)` cells; columns with no value are empty strings).
- Append to the end of the data using USER_ENTERED so dates and times are parsed by Sheets as dates and times.
- `null` fields are written as empty cells.

### 9.4 Authentication

The prototype uses a Google **service account**: its JSON key sits on the AI Workstation and the sheet is shared with the service account’s email as Editor. The builder may choose a different approach if the host app already has Google auth.

## 10. Local model integration

- Endpoint: OpenAI-style chat completions, default `http://localhost:8080/v1/chat/completions` (llama.cpp). The URL and model name are settings.
- Timeout generous enough for a local model (the prototype uses 120 s).
- If the model call fails or returns no usable JSON, show a clear error and keep the pasted text so the user can retry.

## 11. UI requirements (mobile)

- Mobile-first, one-handed use: large touch targets (about 52 px tall buttons), text input at least 16 px so iOS doesn’t zoom on focus.
- Respect system light/dark mode and safe-area insets.
- Status and errors are announced to assistive tech (live region).
- Currency shown as `$187.82` in the preview only; the sheet receives the plain number.

## 12. Security

- The prototype has **no login**. It assumes the app is only reachable on the home network or over Tailscale. The host app should either keep that assumption or add authentication.
- Google credentials stay on the AI Workstation and are never sent to the phone.

## 13. Extensibility

James wants this designed to be flexible for more note types later. Today the only note type is **gas and mileage**.

Adding a note type should only require three small changes: add a field to the extraction schema/prompt, add any needed calculation, and add one line to the column map (plus the new header in the sheet). No change to the flow.

## 14. Acceptance criteria

1. The §8 example produces exactly the expected record and row.
2. A log with no gas stop gives `miles_driven = departure_mileage − arrival_mileage` and blank gas cells.
3. A log with two gas stops gives the correct miles and comma-joined gas cells.
4. A log where offline or arrival time is after midnight gives positive hours.
5. A log missing batches, or any other field, parses without error and the preview lists the missing fields.
6. If the sheet’s headers are missing, misnamed, or swapped, **no row is written** and the error names the exact cells.
7. Saving cannot happen twice for one parse, and a stale preview cannot be saved after the text is edited.
8. If the model or Google is unreachable, the user sees a clear error and keeps their text.
9. Unit tests cover the calculation and normalization functions. These work without the model.

## 15. Open questions

1. **Native app or installable web app?** James said “proper mobile app”. Earlier he also agreed to plan for a self-hosted web app. The builder should confirm the choice with James.
2. **Gas math.** James said the gas math was about “what the miles were and then what they…” (the sentence was cut off). The prototype computes miles driven only. Ask whether he also wants gallons, cost, or MPG, which would need new fields and columns.
3. **Batch count** isn’t in the sample log. Confirm whether he will start writing it into the log or whether it stays blank.
4. **Duplicate days.** Should saving a date that already has a row warn him? The prototype does not check.

## 16. Reference prototype

Files in `dataphone/`:

- `app.py`: FastAPI backend with the model call, normalization, calculations, the column map, header validation, and the sheet append. The calculation functions can be lifted directly.
- `index.html`: the single-page mobile UI (paste, preview, save).
- `requirements.txt`: `fastapi`, `uvicorn`, `gspread>=6`, `requests`.

Tested so far: the calculations against the §8 example, and header validation (correct, swapped, and missing headers) with a mocked sheet. **Not yet tested** against the real local model or the live sheet.

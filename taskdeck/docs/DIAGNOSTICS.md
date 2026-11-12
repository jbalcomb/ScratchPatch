# Diagnostics: task traces

Every background task (e.g. Extract Archive) writes a detailed trace for analysis afterwards. There is no
screen for it in the app; it's for working out what went wrong or why something was slow.

## Where they are

* **On the phone (the record):** `diagnostics/` next to TaskDeck's `state.json`, one `.jsonl` file per task,
  named `YYYYMMDD-HHMMSS-<job>.jsonl`. Written line by line as the task runs (flushed on every event, synced to
  storage every few seconds), so it survives the app being killed. The newest 20 are kept.
* **In Google Drive (a copy):** My Drive → **TaskDeck diagnostics**. Uploaded when a task ends, every 2 minutes
  while tasks run, and when TaskDeck starts (catching up after a crash or kill). A growing file replaces its
  earlier copy, and next to each trace goes `<name>.summary.txt`: the report below, readable at a glance (a long
  trace has thousands of lines). Needs Google Drive connected (Settings → Accounts); otherwise the files wait on the phone.
  Ignores the jobs' Wi-Fi-only setting (traces are small).

## What's in them

One JSON object per line, each with `t` (Unix time), `s` (seconds since the task started) and `ev`:

| ev | What |
|---|---|
| `start` | task title, job, app version, Python, platform |
| `archive` / `unpacked` | the archive (format, size, files, bytes); how long unpacking took |
| `phases` / `phase` | the stages, and when each one starts |
| `http` | **every request** the task makes: method, host, path (no query string), status or error, bytes sent and received, seconds |
| `file` | one file uploaded: path in the archive, bytes, seconds (or `skipped`: already in Drive) |
| `uploads` | how many small and large files there are to upload, and how many go at once to start with |
| `workers` | every adjustment of how many small files upload at once (`limit`, `was`, `reason`, median seconds per file, files per minute) |
| `throttle` | Google asked to slow down (429 / 403 rate limit) or was busy (5xx): HTTP status and the shared pause in seconds |
| `heartbeat` | every 30 s: progress and bytes moved since the last heartbeat (a stall shows as heartbeats with `moved: 0`) |
| `note` | pauses: "Paused: no connection", "Waiting for Wi-Fi", "No progress for N minutes" (shown in the app after a minute without progress), and when they clear |
| `app` | TaskDeck went to the background / came back |
| `network` | Wi-Fi / mobile data changes, and how many open connections were cut (`connections_cut`): on a change, requests in flight on the old network are ended at once and retried, instead of hanging until their timeout |
| `service` | the background service started (or failed) |
| `cancel_requested`, `end` | Cancel tapped; how it ended (status, message, Python traceback on failure) |

No file contents, passwords, tokens or query strings are recorded.

## Reading one

```
python tools/trace_report.py TRACE.jsonl [--minute]
```

(the same report as the `.summary.txt` files; the code is `taskdeck.core.trace_report`)

prints how it ended, where the time went, upload speed (per request and per file), the slowest and failed
requests, stalls, and the timeline of pauses, app and network changes.

## How uploads to Google Drive are paced

Measured from traces: Google takes about 3-5 s to create each file however small, and large files move at
the connection's speed. So (`taskdeck/shared/drive_uploads.py`):

* **Two lanes.** Files up to 5 MB go up in one request each, several at once. Larger files use resumable
  uploads in 8 MB pieces (after a dropped connection only the current piece is sent again), 2 at once: they
  are limited by bandwidth, which more parallel uploads don't add to, and in their own lane they never hold up
  the small ones.
* **How many small files at once adjusts itself.** It starts at 4. Every 30 s: one more if things went well,
  half as many if Google asked to slow down or each file took more than twice as long as the best seen;
  at most 20. Each change is a `workers` event in the trace.
* **Slow-down replies pause every upload together**, for 1, 2, 4 ... up to 64 s (with some randomness), as
  long as Google keeps saying so. Being rate-limited never fails the job; Cancel still works during a pause.
* **Time left** is the longer of: small files left × seconds per file ÷ uploads at once, and bytes of large
  files left ÷ measured speed.

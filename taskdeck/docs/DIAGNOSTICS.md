# Diagnostics: task traces

Every background task (e.g. Extract Archive) writes a detailed trace for analysis afterwards. There is no
screen for it in the app; it's for working out what went wrong or why something was slow.

## Where they are

* **On the phone (the record):** `diagnostics/` next to TaskDeck's `state.json`, one `.jsonl` file per task,
  named `YYYYMMDD-HHMMSS-<job>.jsonl`. Written line by line as the task runs (flushed on every event, synced to
  storage every few seconds), so it survives the app being killed. The newest 20 are kept.
* **In Google Drive (a copy):** My Drive → **TaskDeck diagnostics**. Uploaded when a task ends, every 2 minutes
  while tasks run, and when TaskDeck starts (catching up after a crash or kill). A growing file replaces its
  earlier copy. Needs Google Drive connected (Settings → Accounts); otherwise the files wait on the phone.
  Ignores the jobs' Wi-Fi-only setting (traces are small).

## What's in them

One JSON object per line, each with `t` (Unix time), `s` (seconds since the task started) and `ev`:

| ev | What |
|---|---|
| `start` | task title, job, app version, Python, platform |
| `archive` / `unpacked` | the archive (format, size, files, bytes); how long unpacking took |
| `phases` / `phase` | the stages, and when each one starts |
| `http` | **every request** the task makes: method, host, path (no query string), status or error, bytes sent and received, seconds |
| `file` | one file uploaded: path in the archive, bytes, seconds |
| `heartbeat` | every 30 s: progress and bytes moved since the last heartbeat (a stall shows as heartbeats with `moved: 0`) |
| `note` | pauses: "Paused: no connection", "Waiting for Wi-Fi", and when they clear |
| `app` | TaskDeck went to the background / came back |
| `network` | Wi-Fi / mobile data changes |
| `service` | the background service started (or failed) |
| `cancel_requested`, `end` | Cancel tapped; how it ended (status, message, Python traceback on failure) |

No file contents, passwords, tokens or query strings are recorded.

## Reading one

```
python tools/trace_report.py TRACE.jsonl [--minute]
```

prints how it ended, where the time went, upload speed (per request and per file), the slowest and failed
requests, stalls, and the timeline of pauses, app and network changes.

# Discord Capture

Pulls messages from Discord servers into a local SQLite file. It does a one-shot historic
backfill, then scheduled runs that only fetch what's new.

Pure Python 3.10+ standard library: no dependencies to install.

## Usage

```powershell
$env:DISCORD_BOT_TOKEN = "..."            # or set it permanently: setx DISCORD_BOT_TOKEN "..."
$env:PYTHONPATH = "src"

python -m discord_capture channels        # what can the bot see?
python -m discord_capture backfill -v     # everything, all the way back; safe to stop and rerun
python -m discord_capture sync            # only new messages, for the scheduler
python -m discord_capture status          # per-channel counts and backfill progress
```

Options: `--guild ID` / `--channel ID` (repeatable) narrow the scope, `--db PATH` picks the file
(default `data/discord.sqlite3`, git-ignored), `--min-interval S` sets the floor between requests
(default 1 s), and `--max-pages N` caps each channel's backfill per run.

Tests: `python -m unittest discover -s tests` with `PYTHONPATH=src` (pytest also works).

## How it works

* **Auth:** a bot token (`Authorization: Bot ...`). The bot has to be invited to the server.
* **Discovery:** `GET /users/@me/guilds`, then `/guilds/{id}/channels`, `/guilds/{id}/threads/active`
  and `/channels/{id}/threads/archived/public` for every channel that can hold threads. Threads
  are stored as channels whose `parent_id` is the channel they live in.
* **Messages:** `GET /channels/{id}/messages?limit=100`, paged with `before=` (backfill) and
  `after=` (sync). Message IDs are snowflakes, so ordering by ID is ordering by time.
  `sync_state` records where each channel stands, and every page is committed as it lands.
* **Storage:** SQLite with a few useful columns (author, timestamp, content, reply target) plus
  the full JSON of every object in `raw`. Nothing is thrown away while we work out which fields
  matter. Columns can be added later from `raw` with `json_extract`.

### Rate limiting

Discord enforces per-route buckets (reported in `X-RateLimit-*` headers), 50 requests/s
globally, and an IP ban from Cloudflare after 10,000 invalid (401/403/429) requests in
10 minutes. The client stays far inside all of these:

* It sends at most one request per second (`--min-interval`) no matter what the headers allow.
* It stops using a bucket when one request is left, waits until it resets, then adds 0.5 s.
* On a 429 it waits Discord's `retry_after` × 1.5 + 1 s.
* On 5xx and network errors it backs off exponentially with jitter (capped at 5 min, 6 tries).
* A 401 stops the run immediately.
* A 403 on a channel records the error and skips that channel, without retrying.
* After 50 invalid requests in a run it aborts, roughly 200 times below the ban threshold.

At 1 request/s, a backfill pulls about 360,000 messages an hour.

## Scheduling (Windows)

```powershell
schtasks /Create /TN "Discord Capture sync" /SC HOURLY /TR ^
  "cmd /c cd /d C:\develai\ScratchPatch\discord-capture && set PYTHONPATH=src && python -m discord_capture sync >> data\sync.log 2>&1"
```

Then open the task in Task Scheduler and set "If the task is already running: Do not start a new
instance". That keeps a slow sync, or the backfill, from overlapping with the next run.
`DISCORD_BOT_TOKEN` has to be set for the account the task runs as (`setx` does that).

## Open questions

1. **Bot or user account?** This assumes a bot. Automating a normal user account ("self-bot")
   is against Discord's Terms of Service and gets accounts banned. If you can't add a bot to
   the server, you can still use Discord's own data export (Settings → Data & Privacy), but that
   only covers your own messages.
2. **Who controls the server?** Adding a bot needs the *Manage Server* permission. The bot needs
   *View Channel* and *Read Message History* on every channel to be captured.
3. **Message Content intent.** Without this privileged intent, bots get an empty `content` for
   messages that don't mention them, and that applies to REST too. Turn it on under Developer
   Portal → Bot → Privileged Gateway Intents. Bots in fewer than 100 servers don't need review.
4. **Which servers and channels?** Everything the bot can see, or a fixed list? Should private
   threads be included? Those need *Manage Threads* and aren't fetched yet.
5. **Attachments.** Only metadata and the CDN URL are stored for now. The URLs are signed and
   expire, so if you want the files they should be downloaded during the capture.
6. **Edits, deletions and reactions.** Polling with `after=` only sees new messages. An edit to
   an old message, a deletion or a change in reaction counts is missed. If any of that matters,
   the answer is a small always-on gateway listener alongside this, or a periodic re-scan of
   recent days.
7. **How often to sync, and where it runs.** Hourly from this PC means it only runs while the
   PC is on. Every run currently repeats discovery, about 3 requests per server plus 1 per
   thread-capable channel, which is fine for a few servers.
8. **What's it for?** Search, archiving, feeding an LLM or analytics? The answer decides
   whether we add SQLite FTS5 full-text search, an export format, or a move to Postgres later.

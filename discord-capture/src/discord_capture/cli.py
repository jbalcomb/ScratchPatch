"""Command line: `python -m discord_capture <command>`.

  channels   list the servers and channels the bot can see
  backfill   fetch everything, oldest history included (resumable)
  sync       fetch only what's new since the last run (for the scheduler)
  status     show per-channel progress from the database

The bot token comes from the DISCORD_BOT_TOKEN environment variable or, failing
that, from a file (default ~/.config/discord-capture/token), which is what the
scheduled task and cron job use.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from .capture import capture_channel, discover
from .client import AuthError, Client, TooManyInvalidRequests
from .store import Store

DEFAULT_TOKEN_FILE = Path.home() / ".config" / "discord-capture" / "token"

CHANNEL_TYPES = {0: "text", 2: "voice", 4: "category", 5: "announcement", 10: "news-thread",
                 11: "thread", 12: "private-thread", 13: "stage", 15: "forum", 16: "media"}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="discord_capture")
    parser.add_argument("command", choices=["channels", "backfill", "sync", "status"])
    parser.add_argument("--db", default="data/discord.sqlite3", help="SQLite file (default: %(default)s)")
    parser.add_argument("--guild", action="append", help="limit to this server ID (repeatable)")
    parser.add_argument("--channel", action="append", help="limit to this channel ID (repeatable)")
    parser.add_argument("--min-interval", type=float, default=1.0,
                        help="seconds between requests, at least (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, help="backfill: stop each channel after this many pages")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE,
                        help="read the bot token from here if DISCORD_BOT_TOKEN is unset (default: %(default)s)")
    parser.add_argument("--log-file", help="append the log here instead of printing it")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose or args.log_file else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s",
                        filename=args.log_file, encoding="utf-8")
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    try:
        if args.command == "status":
            for row in store.summary():
                done = "done" if row["backfill_done"] else ("-" if row["updated_at"] is None else "partial")
                print(f"{row['id']:>20}  {CHANNEL_TYPES.get(row['type'], row['type']):<14} "
                      f"{row['messages']:>8}  {done:<8} #{row['name']}  {row['last_error'] or ''}")
            return 0

        token = _token(args.token_file)
        if not token:
            _fail(f"No token: set DISCORD_BOT_TOKEN or put it in {args.token_file}")
            return 2
        lock = _lock(Path(args.db).with_suffix(".lock"))
        if lock is None:
            logging.warning("another capture is already running against %s; not starting", args.db)
            return 0
        client = Client(token, min_interval=args.min_interval)
        try:
            return _run(args, client, store)
        except AuthError as error:
            _fail(f"Discord rejected the token: {error}")
            return 3
        except TooManyInvalidRequests as error:
            _fail(str(error))
            return 4
        except Exception:
            logging.exception("capture failed")
            raise
        finally:
            logging.info("%d requests, %d invalid", client.requests, client.invalid)
            lock.close()
    finally:
        store.close()


def _token(path):
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token and path.is_file():
        token = path.read_text(encoding="utf-8-sig")  # tolerate a BOM from Notepad
    return (token or "").strip()


def _lock(path):
    """Hold an OS lock on `path` for the rest of the run, so a scheduled sync and a
    manual backfill never hit the API at the same time. The OS drops the lock if
    the process dies. Returns the open file, or None if another run holds it."""
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _fail(message):
    # Under pythonw (the Windows scheduled task) there is no stderr; the log file has it.
    logging.error(message)
    if sys.stderr:
        print(message, file=sys.stderr)


def _run(args, client, store):
    channels = discover(client, store, set(args.guild) if args.guild else None)
    if args.channel:
        channels = [(g, c) for g, c in channels if c["id"] in args.channel]

    if args.command == "channels":
        for gid, c in channels:
            print(f"{gid:>20}  {c['id']:>20}  {CHANNEL_TYPES.get(c['type'], c['type']):<14} #{c.get('name')}")
        return 0

    total = 0
    for gid, c in channels:
        total += capture_channel(client, store, gid, c["id"],
                                 backfill=args.command == "backfill", max_pages=args.max_pages)
    result = f"{total} messages saved from {len(channels)} channels ({client.requests} requests)"
    logging.info(result)
    print(result)
    return 0

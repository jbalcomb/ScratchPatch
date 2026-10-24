"""Command line: `python -m discord_capture <command>`.

  channels   list the servers and channels the bot can see
  backfill   fetch everything, oldest history included (resumable)
  sync       fetch only what's new since the last run (for the scheduler)
  status     show per-channel progress from the database

The bot token is read from the DISCORD_BOT_TOKEN environment variable.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from .capture import capture_channel, discover
from .client import AuthError, Client, TooManyInvalidRequests
from .store import Store

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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    try:
        if args.command == "status":
            for row in store.summary():
                done = "done" if row["backfill_done"] else ("-" if row["updated_at"] is None else "partial")
                print(f"{row['id']:>20}  {CHANNEL_TYPES.get(row['type'], row['type']):<14} "
                      f"{row['messages']:>8}  {done:<8} #{row['name']}  {row['last_error'] or ''}")
            return 0

        token = os.environ.get("DISCORD_BOT_TOKEN")
        if not token:
            print("Set DISCORD_BOT_TOKEN to the bot's token.", file=sys.stderr)
            return 2
        client = Client(token, min_interval=args.min_interval)
        try:
            return _run(args, client, store)
        except AuthError as error:
            print(f"Discord rejected the token: {error}", file=sys.stderr)
            return 3
        except TooManyInvalidRequests as error:
            print(error, file=sys.stderr)
            return 4
        finally:
            logging.info("%d requests, %d invalid", client.requests, client.invalid)
    finally:
        store.close()


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
    print(f"{total} messages saved from {len(channels)} channels ({client.requests} requests)")
    return 0

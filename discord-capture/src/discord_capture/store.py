"""SQLite store. Every object keeps Discord's full JSON in `raw`, so nothing is
lost while we decide which fields deserve columns of their own."""

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    id INTEGER PRIMARY KEY,
    name TEXT,
    raw TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER,
    parent_id INTEGER,           -- category for channels, parent channel for threads
    type INTEGER,                -- https://discord.com/developers/docs/resources/channel#channel-object-channel-types
    name TEXT,
    raw TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    global_name TEXT,
    bot INTEGER,
    raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,      -- snowflake; ordering by id is ordering by time
    channel_id INTEGER NOT NULL,
    guild_id INTEGER,
    author_id INTEGER,
    created_at TEXT,
    edited_at TEXT,
    type INTEGER,
    content TEXT,
    reply_to_id INTEGER,
    raw TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_channel ON messages (channel_id, id);
CREATE INDEX IF NOT EXISTS messages_by_author ON messages (author_id);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    filename TEXT,
    content_type TEXT,
    size INTEGER,
    url TEXT,                    -- signed CDN URL; expires, so download promptly if we want the file
    local_path TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    channel_id INTEGER PRIMARY KEY,
    newest_id INTEGER,           -- forward sync continues after this
    oldest_id INTEGER,           -- backfill continues before this
    backfill_done INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(value):
    return int(value) if value is not None else None


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def commit(self):
        self.db.commit()

    def upsert_guild(self, guild):
        self.db.execute(
            "INSERT INTO guilds (id, name, raw, seen_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET name=excluded.name, raw=excluded.raw, seen_at=excluded.seen_at",
            (_id(guild["id"]), guild.get("name"), json.dumps(guild), now()),
        )

    def upsert_channel(self, channel):
        self.db.execute(
            "INSERT INTO channels (id, guild_id, parent_id, type, name, raw, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET guild_id=excluded.guild_id, parent_id=excluded.parent_id, "
            "type=excluded.type, name=excluded.name, raw=excluded.raw, seen_at=excluded.seen_at",
            (
                _id(channel["id"]),
                _id(channel.get("guild_id")),
                _id(channel.get("parent_id")),
                channel.get("type"),
                channel.get("name"),
                json.dumps(channel),
                now(),
            ),
        )

    def save_messages(self, guild_id, messages):
        fetched = now()
        for m in messages:
            author = m.get("author") or {}
            if author.get("id"):
                self.db.execute(
                    "INSERT INTO users (id, username, global_name, bot, raw) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET username=excluded.username, "
                    "global_name=excluded.global_name, raw=excluded.raw",
                    (_id(author["id"]), author.get("username"), author.get("global_name"),
                     int(bool(author.get("bot"))), json.dumps(author)),
                )
            reference = m.get("message_reference") or {}
            # Re-fetching a message (e.g. after an edit) replaces the stored copy.
            self.db.execute(
                "INSERT OR REPLACE INTO messages (id, channel_id, guild_id, author_id, created_at, edited_at, "
                "type, content, reply_to_id, raw, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _id(m["id"]),
                    _id(m["channel_id"]),
                    _id(guild_id),
                    _id(author.get("id")),
                    m.get("timestamp"),
                    m.get("edited_timestamp"),
                    m.get("type"),
                    m.get("content"),
                    _id(reference.get("message_id")),
                    json.dumps(m),
                    fetched,
                ),
            )
            for a in m.get("attachments") or []:
                self.db.execute(
                    "INSERT INTO attachments (id, message_id, filename, content_type, size, url) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET url=excluded.url",
                    (_id(a["id"]), _id(m["id"]), a.get("filename"), a.get("content_type"), a.get("size"), a.get("url")),
                )

    def state(self, channel_id):
        row = self.db.execute("SELECT * FROM sync_state WHERE channel_id = ?", (channel_id,)).fetchone()
        if row is None:
            return {"newest_id": None, "oldest_id": None, "backfill_done": 0, "last_error": None}
        return dict(row)

    def set_state(self, channel_id, **fields):
        merged = {**self.state(channel_id), **fields}
        self.db.execute(
            "INSERT INTO sync_state (channel_id, newest_id, oldest_id, backfill_done, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (channel_id) DO UPDATE SET newest_id=excluded.newest_id, "
            "oldest_id=excluded.oldest_id, backfill_done=excluded.backfill_done, "
            "last_error=excluded.last_error, updated_at=excluded.updated_at",
            (channel_id, merged["newest_id"], merged["oldest_id"], merged["backfill_done"],
             merged["last_error"], now()),
        )

    def summary(self):
        return self.db.execute(
            "SELECT c.id, c.name, c.type, c.guild_id, g.name AS guild, s.backfill_done, s.last_error, s.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.channel_id = c.id) AS messages, "
            "(SELECT MAX(created_at) FROM messages m WHERE m.channel_id = c.id) AS newest_at "
            "FROM channels c LEFT JOIN sync_state s ON s.channel_id = c.id LEFT JOIN guilds g ON g.id = c.guild_id "
            "ORDER BY c.guild_id, c.name"
        ).fetchall()

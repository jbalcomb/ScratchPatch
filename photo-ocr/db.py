"""The service's SQLite store: the six tables of PRD.md (Data model), plus a few small ones the phases need.

Amounts are integer cents, dates YYYY-MM-DD, times ISO 8601 local time.
"""
import json
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending, parsed, approved, failed
    bucket        TEXT,                             -- auto, review, retake (NULL while pending)
    merchant      TEXT,                             -- normalized (as approved), else as read
    merchant_raw  TEXT,                             -- as the engine read it
    purchase_date TEXT,
    total         INTEGER,
    tax           INTEGER,
    location      TEXT,
    source        TEXT NOT NULL DEFAULT 'in-store', -- in-store, instacart, other
    engine        TEXT,                             -- the engine whose reading the fields come from
    checks        TEXT,                             -- JSON [{name, ok, detail}]
    confidence    TEXT,                             -- JSON {field: 0..1}
    disagreements TEXT,                             -- JSON {field: {engine: value}}
    error         TEXT,
    note          TEXT,
    order_text    TEXT,                             -- online orders: the confirmation text it was read from
    approved_at   TEXT
);
CREATE TABLE IF NOT EXISTS images (
    id              INTEGER PRIMARY KEY,
    receipt_id      INTEGER REFERENCES receipts(id) ON DELETE SET NULL,
    product_id      INTEGER REFERENCES products(id) ON DELETE SET NULL,
    position        INTEGER,                        -- order within the receipt, from 1
    immich_asset_id TEXT UNIQUE,
    album           TEXT,                           -- receipts, products
    via             TEXT,                           -- capture, immich, syncthing, picker
    source_path     TEXT,                           -- original file name or path
    cache_path      TEXT,                           -- local copy, if any
    added_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_items (
    id            INTEGER PRIMARY KEY,
    receipt_id    INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    raw_text      TEXT NOT NULL,
    quantity      REAL,
    unit_price    INTEGER,
    line_total    INTEGER,
    product_id    INTEGER REFERENCES products(id) ON DELETE SET NULL,
    match         TEXT,                             -- alias, manual, skip (not a product); NULL = unmatched
    confidence    REAL,
    suggested_product_id INTEGER,
    suggested_by  TEXT,                             -- text, model
    suggestion_score REAL
);
CREATE TABLE IF NOT EXISTS engine_runs (
    id          INTEGER PRIMARY KEY,
    receipt_id  INTEGER REFERENCES receipts(id) ON DELETE CASCADE,
    product_id  INTEGER REFERENCES products(id) ON DELETE CASCADE,
    engine      TEXT NOT NULL,
    raw_output  TEXT,
    parsed_json TEXT,
    ocr_json    TEXT,
    error       TEXT,
    seconds     REAL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY,
    name       TEXT,
    brand      TEXT,
    size       TEXT,
    status     TEXT NOT NULL DEFAULT 'ready',       -- pending (being read from its photo), ready
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_aliases (
    norm_text  TEXT PRIMARY KEY,                    -- normalized receipt text
    raw_text   TEXT NOT NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,  -- NULL = "not a product" (bag fee, ...)
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (                -- every hand edit, so the labeled set grows
    id         INTEGER PRIMARY KEY,
    receipt_id INTEGER REFERENCES receipts(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    before     TEXT,
    after      TEXT,
    at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stores (                     -- known stores: approved receipts' merchants
    norm_name TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    location  TEXT
);
CREATE TABLE IF NOT EXISTS bank_txns (
    id          INTEGER PRIMARY KEY,
    account     TEXT,
    date        TEXT NOT NULL,
    description TEXT NOT NULL,
    amount      INTEGER NOT NULL,                   -- cents; negative = money out
    balance     INTEGER,
    origin      TEXT,                               -- csv:<file>, budget
    UNIQUE (account, date, description, amount, balance)
);
CREATE TABLE IF NOT EXISTS reconcile_matches (
    receipt_id INTEGER PRIMARY KEY REFERENCES receipts(id) ON DELETE CASCADE,
    bank_id    INTEGER UNIQUE REFERENCES bank_txns(id) ON DELETE CASCADE,
    method     TEXT NOT NULL,                       -- auto, manual
    at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS picker_sessions (
    id         TEXT PRIMARY KEY,
    album      TEXT NOT NULL,
    picker_uri TEXT,
    state      TEXT NOT NULL,                       -- waiting, importing, done, error
    imported   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS images_receipt ON images(receipt_id, position);
CREATE INDEX IF NOT EXISTS items_receipt ON receipt_items(receipt_id, position);
CREATE INDEX IF NOT EXISTS items_product ON receipt_items(product_id);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


class Store:
    """One SQLite file shared by the web threads and the worker: one connection, one lock."""

    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def q(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, *args):
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def x(self, sql, *args):
        """Execute; returns lastrowid."""
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def tx(self):
        """with store.tx(): several statements, all or nothing."""
        return _Tx(self)

    def get(self, key, default=None):
        row = self.one("SELECT value FROM kv WHERE key = ?", key)
        return json.loads(row["value"]) if row else default

    def put(self, key, value):
        self.x("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               key, json.dumps(value))


class _Tx:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        self.store.conn.execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(self, exc_type, *_):
        try:
            self.store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store.lock.release()


def loads(value, default=None):
    return json.loads(value) if value else default

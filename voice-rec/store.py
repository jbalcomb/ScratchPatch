"""Postgres: one row per recording, audio in a bytea column, plus everything learned about it.

Status flow (``status``):

    queued → transcribing → transcribed → archiving → archived
                         ↘ failed (after MAX_ATTEMPTS tries)

``archive_requested`` is set by the phone (or AUTO_ARCHIVE); the worker only archives requested rows.
Rows left in ``transcribing``/``archiving`` by a crash go back a step when the worker starts.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MAX_ATTEMPTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id                uuid PRIMARY KEY,
    recorded_at       timestamptz NOT NULL,
    received_at       timestamptz NOT NULL DEFAULT now(),
    transcribed_at    timestamptz,
    archived_at       timestamptz,
    duration_s        real,
    mime              text NOT NULL,
    size_bytes        integer NOT NULL,
    sha256            text NOT NULL,
    audio             bytea NOT NULL,
    status            text NOT NULL DEFAULT 'queued',
    archive_requested boolean NOT NULL DEFAULT false,
    attempts          integer NOT NULL DEFAULT 0,
    error             text,
    language          text,
    transcript        text,
    segments          jsonb,
    model             text,
    title             text,
    category          text,
    tags              text[] NOT NULL DEFAULT '{}',
    summary           text,
    actions           jsonb NOT NULL DEFAULT '[]',
    entities          jsonb NOT NULL DEFAULT '[]',
    note_path         text,
    mobile_data       text NOT NULL DEFAULT '',
    search            tsvector GENERATED ALWAYS AS (
                          to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' ||
                                                 coalesce(transcript, ''))) STORED
);
CREATE INDEX IF NOT EXISTS recordings_status ON recordings (status);
CREATE INDEX IF NOT EXISTS recordings_recorded_at ON recordings (recorded_at);
CREATE INDEX IF NOT EXISTS recordings_search ON recordings USING gin (search);
"""

# Everything but the audio, for the API and listings.
PUBLIC = ("id, recorded_at, received_at, transcribed_at, archived_at, duration_s, mime, size_bytes, sha256, status, "
          "archive_requested, attempts, error, language, transcript, model, title, category, tags, summary, "
          "actions, entities, note_path")


class Conflict(Exception):
    """Same id, different audio."""


@dataclass
class Job:
    id: str
    audio: bytes
    mime: str


def _clean(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            v = v.isoformat(timespec="seconds")
        elif isinstance(v, uuid.UUID):
            v = str(v)
        out[k] = v
    return out


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA)

    def recover(self) -> int:
        """Rows a crashed worker was in the middle of go back a step."""
        with self.connect() as conn:
            a = conn.execute("UPDATE recordings SET status = 'queued' WHERE status = 'transcribing'").rowcount
            b = conn.execute("UPDATE recordings SET status = 'transcribed' WHERE status = 'archiving'").rowcount
        return a + b

    # ---- from the phone -------------------------------------------------------------------------

    def add(self, rec_id: str, audio: bytes, mime: str, recorded_at: str, duration: float | None,
            mobile_data: str = "", archive: bool = False) -> tuple[dict[str, Any], bool]:
        """Store a new recording. Returns (row, created). Sending the same audio again is fine."""
        digest = hashlib.sha256(audio).hexdigest()
        with self.connect() as conn:
            row = conn.execute(
                """INSERT INTO recordings (id, recorded_at, duration_s, mime, size_bytes, sha256, audio, mobile_data,
                                          archive_requested)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING RETURNING """ + PUBLIC,
                (rec_id, recorded_at, duration, mime, len(audio), digest, audio, mobile_data, archive)).fetchone()
            if row:
                return _clean(row), True
            existing = conn.execute("SELECT sha256 FROM recordings WHERE id = %s", (rec_id,)).fetchone()
        if existing and existing["sha256"] != digest:
            raise Conflict(rec_id)
        return self.get(rec_id), False

    def get(self, rec_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return _clean(conn.execute(f"SELECT {PUBLIC} FROM recordings WHERE id = %s", (rec_id,)).fetchone())

    def audio(self, rec_id: str) -> tuple[bytes, str] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT audio, mime FROM recordings WHERE id = %s", (rec_id,)).fetchone()
        return (bytes(row["audio"]), row["mime"]) if row else None

    def list(self, limit: int = 200, query: str = "") -> list[dict[str, Any]]:
        """Oldest first (ascending by recorded time); ``query`` is full-text search."""
        with self.connect() as conn:
            if query:
                rows = conn.execute(
                    f"SELECT {PUBLIC} FROM recordings WHERE search @@ websearch_to_tsquery('english', %s) "
                    "ORDER BY recorded_at LIMIT %s", (query, limit)).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM (SELECT {PUBLIC} FROM recordings ORDER BY recorded_at DESC LIMIT %s) t "
                    "ORDER BY recorded_at", (limit,)).fetchall()
        return [_clean(r) for r in rows]

    def request_archive(self, rec_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute("UPDATE recordings SET archive_requested = true, "
                         "error = CASE WHEN status = 'transcribed' THEN NULL ELSE error END, "
                         "attempts = CASE WHEN status = 'transcribed' THEN 0 ELSE attempts END WHERE id = %s",
                         (rec_id,))
        return self.get(rec_id)

    def retry(self, rec_id: str) -> None:
        """Start over from transcription (e.g. after changing the model)."""
        with self.connect() as conn:
            conn.execute("UPDATE recordings SET status = 'queued', attempts = 0, error = NULL WHERE id = %s",
                         (rec_id,))

    def rearchive(self, rec_id: str | None = None) -> int:
        """Archive again (e.g. after changing categories or the Obsidian layout); None = every archived one."""
        sql = ("UPDATE recordings SET status = 'transcribed', archive_requested = true, attempts = 0, error = NULL "
               "WHERE status = 'archived'")
        with self.connect() as conn:
            if rec_id:
                return conn.execute(sql + " AND id = %s", (rec_id,)).rowcount
            return conn.execute(sql).rowcount

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, count(*) AS n FROM recordings GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---- the worker -------------------------------------------------------------------------------

    def claim(self, auto_archive: bool = False) -> tuple[str, Job] | None:
        """The next piece of work: ("transcribe", job) first, then ("archive", job). Oldest first."""
        with self.connect() as conn:
            row = conn.execute(
                """UPDATE recordings SET status = 'transcribing', attempts = attempts + 1
                   WHERE id = (SELECT id FROM recordings WHERE status = 'queued'
                               ORDER BY recorded_at FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING id, audio, mime""").fetchone()
            if row:
                return "transcribe", Job(str(row["id"]), bytes(row["audio"]), row["mime"])
            row = conn.execute(
                """UPDATE recordings SET status = 'archiving', attempts = attempts + 1
                   WHERE id = (SELECT id FROM recordings WHERE status = 'transcribed'
                                 AND (archive_requested OR %s) AND attempts < %s
                               ORDER BY recorded_at FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING id, audio, mime""", (auto_archive, MAX_ATTEMPTS)).fetchone()
            if row:
                return "archive", Job(str(row["id"]), bytes(row["audio"]), row["mime"])
        return None

    def transcribed(self, rec_id: str, text: str, language: str | None, segments: list, model: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE recordings SET status = 'transcribed', transcribed_at = now(), transcript = %s,
                          language = %s, segments = %s, model = %s, attempts = 0, error = NULL WHERE id = %s""",
                (text, language, Jsonb(segments), model, rec_id))

    def organized(self, rec_id: str, info: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE recordings SET title = %s, category = %s, tags = %s, summary = %s, actions = %s,
                          entities = %s WHERE id = %s""",
                (info.get("title"), info.get("category"), list(info.get("tags") or []), info.get("summary"),
                 Jsonb(info.get("actions") or []), Jsonb(info.get("entities") or []), rec_id))

    def archived(self, rec_id: str, note_path: str | None, error: str | None = None) -> None:
        """Done; ``error`` keeps a non-fatal problem visible (e.g. the knowledge graph was down)."""
        with self.connect() as conn:
            conn.execute("""UPDATE recordings SET status = 'archived', archived_at = now(), note_path = %s,
                                   attempts = 0, error = %s WHERE id = %s""", (note_path, error, rec_id))

    def failed(self, rec_id: str, stage: str, error: str) -> None:
        """A step went wrong: try again later, up to MAX_ATTEMPTS, then mark it failed."""
        back = "queued" if stage == "transcribe" else "transcribed"
        with self.connect() as conn:
            conn.execute(
                """UPDATE recordings SET error = %s,
                          status = CASE WHEN attempts >= %s AND %s = 'transcribe' THEN 'failed' ELSE %s END
                   WHERE id = %s""", (f"{stage}: {error}"[:2000], MAX_ATTEMPTS, stage, back, rec_id))

    def full(self, rec_id: str) -> dict[str, Any] | None:
        """Everything but the audio, plus the segments: for writing the note."""
        with self.connect() as conn:
            return _clean(conn.execute(f"SELECT {PUBLIC}, segments, mobile_data FROM recordings WHERE id = %s",
                                       (rec_id,)).fetchone())

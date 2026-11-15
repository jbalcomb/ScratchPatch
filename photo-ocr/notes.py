"""Handwritten notes and cork boards (NOTES-PRD.md), phase 1: capture, archive, read, review, measure.

Notes live apart from receipts and products: in Postgres (the server voice-rec runs, a database of its own),
originals and all. The service has them when ``NOTES_DATABASE_URL`` is set.

    notes            one handwritten note (one or more pages) or one photo set of a cork board
    note_photos      the original photos, full size (bytea), in order
    note_readings    every reading by a model: the prompt sent, the reply, the transcript, the time taken,
                     and, once the note is approved, how far it was from the approved transcript
    note_corrections every hand edit

A reading is Markdown that keeps the layout (arrows, boxes, emphasis, crossed-out words) and marks what the
model couldn't read as [word?]. Approving compares every reading with the approved transcript: word and
character error rates, and how many [?] marks were right anyway (``measure``).
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path

import parsing

log = logging.getLogger("photo-ocr.notes")

KINDS = ("handwritten", "board")

NOTE_PROMPT = """These photos are the pages of one handwritten note, in order. The writing may be sideways or upside
down in a photo: read it the right way up.

Write out everything written on it as Markdown that keeps its layout:
- One line per written line, in reading order. Large writing, like a title, as a heading (# ...).
- Arrows as → between what they connect (A → B); an arrow pointing at something: → something.
- Words inside a drawn box: a line [box], then the words in the box as a list.
- Underlined, circled or starred words in **bold**.
- Crossed-out or scribbled-over writing as ~~text~~, or ~~?~~ if it can't be read.
- A word you can't read with confidence: your best reading in brackets with a question mark, like [DEV?].
  Never guess without marking it.
- A drawing: [drawing: a few words on what it shows].
- Before each page after the first, a line: --- page N ---

Reply with the Markdown only."""

BOARD_PROMPT = """These photos show one cork board with cards, notes and pages pinned to it (several photos are parts
of the same board). The writing may be sideways or upside down in a photo: read it the right way up.

Write out every item on the board as Markdown, one section per item, from the top left across and down:
## <where it is: top left, top middle, ..., bottom right> - <a few words naming the item>
then what is written on it, keeping its layout:
- One line per written line. Arrows as → between what they connect. Words inside a drawn box: a line [box],
  then the words as a list. Underlined, circled or starred words in **bold**.
- Crossed-out writing as ~~text~~, or ~~?~~ if it can't be read.
- A word you can't read with confidence: your best reading in brackets with a question mark, like [DEV?].
  Never guess without marking it.
- A picture or drawing: [picture: a few words on what it shows]. String or lines joining items: say which.

Reply with the Markdown only."""

PROMPTS = {"handwritten": NOTE_PROMPT, "board": BOARD_PROMPT}

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('handwritten', 'board')),
    board        TEXT,                    -- which cork board
    event        TEXT,                    -- where it was written, if anywhere in particular
    written_on   DATE,                    -- asked at capture; else the first photo's date
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending / read / approved / failed
    error        TEXT,
    transcript   TEXT,                    -- the latest reading, then as approved
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at      TIMESTAMPTZ,
    approved_at  TIMESTAMPTZ,
    search       TSVECTOR GENERATED ALWAYS AS (to_tsvector('english',
                     coalesce(transcript, '') || ' ' || coalesce(event, '') || ' ' || coalesce(board, ''))) STORED
);
CREATE INDEX IF NOT EXISTS notes_search ON notes USING GIN (search);
CREATE TABLE IF NOT EXISTS note_photos (
    id         BIGSERIAL PRIMARY KEY,
    note_id    BIGINT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    position   INT NOT NULL,
    filename   TEXT,
    mime       TEXT NOT NULL,
    data       BYTEA NOT NULL,            -- the original, full size
    bytes      INT NOT NULL,
    width      INT,
    height     INT,
    taken_at   TIMESTAMP,
    sha256     TEXT NOT NULL UNIQUE,
    turn       INT NOT NULL DEFAULT 0     -- degrees clockwise to make the writing upright (set on the review page)
);
CREATE INDEX IF NOT EXISTS note_photos_note ON note_photos(note_id, position);
CREATE TABLE IF NOT EXISTS note_readings (
    id             BIGSERIAL PRIMARY KEY,
    note_id        BIGINT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    engine         TEXT NOT NULL,
    prompt         TEXT,
    prompt_version TEXT,
    raw            TEXT,
    transcript     TEXT,
    error          TEXT,
    seconds        REAL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- once the note is approved: this reading against the approved transcript (see measure)
    words          INT,
    wer            REAL,
    cer            REAL,
    uncertain      INT,
    uncertain_right INT,
    measured_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS note_readings_note ON note_readings(note_id);
CREATE TABLE IF NOT EXISTS note_corrections (
    id       BIGSERIAL PRIMARY KEY,
    note_id  BIGINT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    field    TEXT NOT NULL,
    before   TEXT,
    after    TEXT,
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class NotFound(LookupError):
    pass


# ---------------------------------------------------------------- measuring a reading

UNCERTAIN = re.compile(r"\[([^\[\]\n]+?)\?\]")
MARKUP = re.compile(r"\[(box|drawing|picture)\b[^\]]*\]|---\s*page\s*\d+\s*---|~~\?~~|[#*_`>|]+", re.I)


def words(text: str) -> list[str]:
    """The words of a transcript, for comparing: layout marks dropped, [word?] read as word, case ignored."""
    text = UNCERTAIN.sub(r"\1", text or "")
    text = MARKUP.sub(" ", text).replace("~~", " ").replace("→", " ").replace("->", " ")
    return re.findall(r"[\w'’&@.+/-]*\w", text.casefold())


def _edits(a: list, b: list) -> int:
    """Levenshtein distance between two sequences (words or characters)."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def measure(reading: str, truth: str) -> dict:
    """A reading against the approved transcript: word error rate (words changed, added or dropped, over the
    approved word count), character error rate, and the [word?] marks: how many there were, and how many of
    those words are in the approved transcript anyway (an honest doubt, not a mistake)."""
    got, want = words(reading), words(truth)
    n = len(want)
    chars_got, chars_want = " ".join(got), " ".join(want)
    marked = [m.casefold() for m in UNCERTAIN.findall(reading or "")]
    truth_words = set(want)
    return {"words": n,
            "wer": round(_edits(got, want) / n, 4) if n else (0.0 if not got else 1.0),
            "cer": round(_edits(chars_got, chars_want) / len(chars_want), 4) if chars_want else (0.0 if not chars_got else 1.0),
            "uncertain": len(marked),
            "uncertain_right": sum(1 for m in marked if all(w in truth_words for w in words(m)))}


def clean_reply(text: str | None) -> str:
    """The Markdown out of a model's reply: no thinking, no code fence around it."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n?```", text, re.S)
    return (fenced.group(1) if fenced else text).strip()


def read_photos(engine: dict, paths: list[Path], kind: str) -> dict:
    """One reading of a note's photos by a vision engine; never raises (the error is in the result)."""
    prompt = PROMPTS[kind]
    run = {"engine": engine["name"], "prompt": prompt, "prompt_version": parsing.prompt_version(prompt),
           "raw": None, "transcript": None, "error": None, "seconds": None}
    t0 = time.monotonic()
    try:
        if engine.get("kind") != "vlm":
            raise ValueError(f"{engine['name']} isn't a vision engine (kind vlm)")
        content = [parsing.image_part(p, engine.get("max_side")) for p in paths] + [{"type": "text", "text": prompt}]
        run["raw"], _ = parsing.chat(engine, content)
        run["transcript"] = clean_reply(run["raw"])
        if not run["transcript"]:
            raise ValueError("the model returned nothing")
    except Exception as e:  # noqa: BLE001
        run["error"] = f"{type(e).__name__}: {e}"
    run["seconds"] = round(time.monotonic() - t0, 2)
    return run


def server_down(run: dict) -> bool:
    return run["transcript"] is None and bool(re.match(r"(URLError|TimeoutError|ConnectionError|ConnectionRefusedError|"
                                                       r"RemoteDisconnected)\b", run["error"] or ""))


# ---------------------------------------------------------------- photos

def photo_facts(data: bytes) -> dict:
    """Size and the date the camera wrote (EXIF), when Pillow can tell."""
    out = {"width": None, "height": None, "taken_at": None}
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as im:
            exif = im.getexif()
            stamp = exif.get(0x0132) or exif.get_ifd(0x8769).get(0x9003)  # DateTime / DateTimeOriginal
            im = ImageOps.exif_transpose(im)
            out["width"], out["height"] = im.size
        if stamp:
            out["taken_at"] = datetime.strptime(str(stamp).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - not a photo Pillow knows, or no date: keep what we have
        pass
    return out


def mime_of(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    return {".png": "image/png", ".webp": "image/webp", ".heic": "image/heic"}.get(ext, "image/jpeg")


# ---------------------------------------------------------------- the store

class Notes:
    """Notes in Postgres. ``dsn`` like postgresql://voicerec:...@127.0.0.1:5433/notes."""

    SIZES = (400, 800, 1200, 1600)

    def __init__(self, dsn: str, cache: Path, engines: list[dict]):
        import psycopg  # only needed when notes are set up
        from psycopg.rows import dict_row
        self._psycopg, self._rows = psycopg, dict_row
        self.dsn, self.cache, self.engines = dsn, Path(cache), engines
        self.cache.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self._conn() as c:
            c.execute(SCHEMA)

    def _conn(self):
        return self._psycopg.connect(self.dsn, row_factory=self._rows, autocommit=True)

    def q(self, sql, *args):
        with self._conn() as c:
            return c.execute(sql, args).fetchall()

    def one(self, sql, *args):
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def ping(self) -> str | None:
        """None when the database answers, else why not."""
        try:
            self.q("SELECT 1")
            return None
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"

    # ---- capture

    def create(self, kind, photos, event=None, written_on=None, board=None) -> int:
        """photos: [(filename, bytes)] in order -> note id. The originals are kept as they came."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if not photos:
            raise ValueError("no photos")
        if kind == "board" and not (board or "").strip():
            raise ValueError("which board? (board)")
        if written_on:
            try:
                written_on = date.fromisoformat(str(written_on)).isoformat()
            except ValueError:
                raise ValueError("written_on must be YYYY-MM-DD") from None
        facts = [photo_facts(data) for _, data in photos]
        if not written_on:
            first = next((f["taken_at"] for f in facts if f["taken_at"]), None)
            written_on = (first.date() if first else date.today()).isoformat()
        with self._conn() as c, c.transaction():
            for filename, data in photos:
                dup = c.execute("SELECT note_id FROM note_photos WHERE sha256 = %s",
                                (hashlib.sha256(data).hexdigest(),)).fetchone()
                if dup:
                    raise ValueError(f"{filename} is already stored (note {dup['note_id']})")
            nid = c.execute("INSERT INTO notes (kind, board, event, written_on) VALUES (%s, %s, %s, %s) RETURNING id",
                            (kind, (board or "").strip() or None, (event or "").strip() or None, written_on)).fetchone()["id"]
            for n, ((filename, data), f) in enumerate(zip(photos, facts), 1):
                c.execute("INSERT INTO note_photos (note_id, position, filename, mime, data, bytes, width, height, taken_at, "
                          "sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                          (nid, n, filename, mime_of(filename), data, len(data), f["width"], f["height"], f["taken_at"],
                           hashlib.sha256(data).hexdigest()))
        return nid

    # ---- reading

    def photo_paths(self, nid) -> list[Path]:
        """The note's originals as files for the model (a cache: the originals stay in Postgres)."""
        out = []
        for p in self.q("SELECT id, mime, sha256, turn FROM note_photos WHERE note_id = %s ORDER BY position", nid):
            if p["turn"]:  # turned upright by hand: the model gets it upright (full size, JPEG)
                path = self.cache / f"{p['sha256']}-turn{p['turn']}.jpg"
                if not path.exists():
                    self._write_turned(p["id"], p["turn"], path)
            else:
                path = self.cache / f"{p['sha256']}{'.png' if p['mime'] == 'image/png' else '.jpg'}"
                if not path.exists():
                    data = self.one("SELECT data FROM note_photos WHERE id = %s", p["id"])["data"]
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(bytes(data))
                    tmp.replace(path)
            out.append(path)
        return out

    def _image(self, photo_id, turn=0, width=None):
        """A stored photo as a Pillow image: upright by EXIF, then turned ``turn`` degrees clockwise, shrunk."""
        from PIL import Image, ImageOps
        data = bytes(self.one("SELECT data FROM note_photos WHERE id = %s", photo_id)["data"])
        im = Image.open(io.BytesIO(data))
        if width and im.format == "JPEG":
            im.draft("RGB", (width, width))
        im = ImageOps.exif_transpose(im)
        if turn:
            im = im.rotate(-turn, expand=True)
        if width and im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        return im.convert("RGB")

    def _write_turned(self, photo_id, turn, path, width=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        self._image(photo_id, turn, width).save(tmp, "JPEG", quality=90 if not width else 82)
        tmp.replace(path)

    def turn(self, nid, position, degrees) -> dict:
        """Set how far a photo is turned (clockwise, 0/90/180/270) to make its writing upright."""
        if degrees not in (0, 90, 180, 270):
            raise ValueError("turn must be 0, 90, 180 or 270")
        p = self.one("SELECT id FROM note_photos WHERE note_id = %s AND position = %s", nid, position)
        if not p:
            raise NotFound(f"note {nid} has no photo {position}")
        self.q("UPDATE note_photos SET turn = %s WHERE id = %s RETURNING id", degrees, p["id"])
        return self.get(nid)

    def read_pending(self, limit=5) -> int:
        """Background work: read notes waiting to be read with the first engine. Returns how many were read."""
        if not self.engines:
            return 0
        engine, done = self.engines[0], 0
        with self.lock:
            for n in self.q("SELECT id, kind FROM notes WHERE status = 'pending' ORDER BY id LIMIT %s", limit):
                run = read_photos(engine, self.photo_paths(n["id"]), n["kind"])
                if server_down(run):
                    log.warning("note %s stays pending, the model server didn't answer: %s", n["id"], run["error"])
                    break
                self.q("INSERT INTO note_readings (note_id, engine, prompt, prompt_version, raw, transcript, error, seconds) "
                       "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id", n["id"], run["engine"], run["prompt"],
                       run["prompt_version"], run["raw"], run["transcript"], run["error"], run["seconds"])
                if run["error"]:
                    self.q("UPDATE notes SET status = 'failed', error = %s RETURNING id", run["error"])
                    log.warning("note %s: %s", n["id"], run["error"])
                else:
                    self.q("UPDATE notes SET status = 'read', transcript = %s, error = NULL, read_at = now() "
                           "WHERE id = %s RETURNING id", run["transcript"], n["id"])
                    log.info("note %s read by %s in %.0f s: %d words, %d marked unsure", n["id"], run["engine"],
                             run["seconds"], len(words(run["transcript"])), len(UNCERTAIN.findall(run["transcript"])))
                done += 1
        return done

    def reparse(self, nid):
        self._row(nid)
        self.q("UPDATE notes SET status = 'pending', error = NULL, approved_at = NULL WHERE id = %s RETURNING id", nid)

    # ---- review

    def _row(self, nid):
        row = self.one("SELECT * FROM notes WHERE id = %s", nid)
        if not row:
            raise NotFound(f"no note {nid}")
        return row

    @staticmethod
    def _summary(r) -> dict:
        text = r["transcript"] or ""
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if words(ln)), "")
        return {"id": r["id"], "kind": r["kind"], "board": r["board"], "event": r["event"],
                "written_on": r["written_on"].isoformat() if r["written_on"] else None, "status": r["status"],
                "error": r["error"], "first_line": first[:120], "uncertain": len(UNCERTAIN.findall(text)),
                "created_at": r["created_at"].isoformat(timespec="seconds"), "url": f"/notes/{r['id']}"}

    def list(self, status=None, q=None, limit=50) -> list[dict]:
        sql, args = "SELECT * FROM notes WHERE TRUE", []
        if status:
            sql, args = sql + " AND status = %s", args + [status]
        if q:
            sql, args = sql + " AND search @@ websearch_to_tsquery('english', %s)", args + [q]
        rows = self.q(sql + " ORDER BY id DESC LIMIT %s", *args, int(limit))
        counts = {r["note_id"]: r["n"] for r in self.q(
            "SELECT note_id, COUNT(*) AS n FROM note_photos WHERE note_id = ANY(%s) GROUP BY note_id", [r["id"] for r in rows])}
        return [dict(self._summary(r), photos=counts.get(r["id"], 0)) for r in rows]

    def get(self, nid) -> dict:
        r = self._row(nid)
        out = self._summary(r)
        out["transcript"] = r["transcript"]
        out["photos"] = [{"position": p["position"], "width": p["width"], "height": p["height"], "bytes": p["bytes"],
                          "turn": p["turn"], "url": f"/api/notes/{nid}/photos/{p['position']}"}
                         for p in self.q("SELECT position, width, height, bytes, turn FROM note_photos WHERE note_id = %s "
                                         "ORDER BY position", nid)]
        out["runs"] = [dict(x, created_at=x["created_at"].isoformat(timespec="seconds")) for x in self.q(
            "SELECT id, engine, prompt_version, seconds, error, created_at, wer, cer FROM note_readings "
            "WHERE note_id = %s ORDER BY id DESC LIMIT 4", nid)]
        return out

    def runs(self, nid) -> list[dict]:
        self._row(nid)
        return [dict(x, created_at=x["created_at"].isoformat(timespec="seconds"),
                     measured_at=x["measured_at"].isoformat(timespec="seconds") if x["measured_at"] else None)
                for x in self.q("SELECT * FROM note_readings WHERE note_id = %s ORDER BY id DESC", nid)]

    def edit(self, nid, data: dict) -> dict:
        """Hand edits from the review page, each kept as a correction; approve: the transcript is final, and every
        reading of the note is measured against it."""
        r = self._row(nid)
        changes = {}
        if "transcript" in data:
            changes["transcript"] = str(data["transcript"] or "").replace("\r\n", "\n").strip() or None
        for f in ("event", "board"):
            if f in data:
                changes[f] = str(data[f] or "").strip() or None
        if "written_on" in data:
            try:
                changes["written_on"] = date.fromisoformat(str(data["written_on"])) if data["written_on"] else None
            except ValueError:
                raise ValueError("written_on must be YYYY-MM-DD") from None
        with self._conn() as c, c.transaction():
            for f, v in changes.items():
                if v != r[f]:
                    c.execute("INSERT INTO note_corrections (note_id, field, before, after) VALUES (%s, %s, %s, %s)",
                              (nid, f, None if r[f] is None else str(r[f]), None if v is None else str(v)))
                    c.execute(f"UPDATE notes SET {f} = %s WHERE id = %s", (v, nid))
            if data.get("approve"):
                truth = changes.get("transcript", r["transcript"])
                if not truth:
                    raise ValueError("nothing to approve: the transcript is empty")
                c.execute("UPDATE notes SET status = 'approved', approved_at = now() WHERE id = %s", (nid,))
                for run in c.execute("SELECT id, transcript FROM note_readings WHERE note_id = %s AND transcript IS NOT NULL",
                                     (nid,)).fetchall():
                    m = measure(run["transcript"], truth)
                    c.execute("UPDATE note_readings SET words = %s, wer = %s, cer = %s, uncertain = %s, uncertain_right = %s, "
                              "measured_at = now() WHERE id = %s",
                              (m["words"], m["wer"], m["cer"], m["uncertain"], m["uncertain_right"], run["id"]))
        if data.get("approve"):
            last = self.one("SELECT wer, cer FROM note_readings WHERE note_id = %s AND measured_at IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1", nid)
            if last:
                log.info("note %s approved: the reading was %.0f%% words off (%.0f%% characters)", nid,
                         100 * last["wer"], 100 * last["cer"])
        return self.get(nid)

    def delete(self, nid):
        self._row(nid)
        self.q("DELETE FROM notes WHERE id = %s RETURNING id", nid)

    # ---- photos for pages

    def photo(self, nid, position, width=None) -> tuple[bytes, str]:
        """An original photo, or with ``width`` a copy at most that wide (turned upright by EXIF), cached."""
        p = self.one("SELECT id, mime, sha256, turn FROM note_photos WHERE note_id = %s AND position = %s", nid, position)
        if not p:
            raise NotFound(f"note {nid} has no photo {position}")
        if not width:  # the original, exactly as it came
            return bytes(self.one("SELECT data FROM note_photos WHERE id = %s", p["id"])["data"]), p["mime"]
        w = next((s for s in self.SIZES if s >= int(width)), self.SIZES[-1])
        sized = self.cache / "sized" / f"{p['sha256']}-{w}-turn{p['turn']}.jpg"
        if not sized.exists():
            self._write_turned(p["id"], p["turn"], sized, w)
        return sized.read_bytes(), "image/jpeg"

    # ---- how well notes are read

    def accuracy(self) -> dict:
        """Per prompt version and engine, over approved notes: readings measured, mean word and character error
        rates, [?] marks and how many of them were right anyway."""
        def group(key):
            rows = self.q(f"SELECT {key} AS k, COUNT(*) AS readings, AVG(wer) AS wer, AVG(cer) AS cer, "
                          "SUM(words) AS words, SUM(uncertain) AS uncertain, SUM(uncertain_right) AS uncertain_right "
                          f"FROM note_readings WHERE measured_at IS NOT NULL GROUP BY {key} ORDER BY {key}")
            return {r["k"] or "not recorded": {"readings": r["readings"], "wer": round(float(r["wer"]), 4),
                                               "cer": round(float(r["cer"]), 4), "words": int(r["words"] or 0),
                                               "uncertain": int(r["uncertain"] or 0),
                                               "uncertain_right": int(r["uncertain_right"] or 0)} for r in rows}
        worst = self.q("SELECT r.note_id, r.engine, r.prompt_version, r.wer, r.cer, n.kind FROM note_readings r "
                       "JOIN notes n ON n.id = r.note_id WHERE r.measured_at IS NOT NULL ORDER BY r.wer DESC LIMIT 10")
        return {"by_prompt": group("prompt_version"), "by_engine": group("engine"),
                "worst": [dict(w, url=f"/notes/{w['note_id']}") for w in worst]}

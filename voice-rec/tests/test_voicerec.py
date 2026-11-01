"""Server tests against a real Postgres: set VOICE_REC_TEST_DSN (each test gets a fresh schema).

    VOICE_REC_TEST_DSN=postgresql://postgres@127.0.0.1:5432/postgres python -m pytest voice-rec/tests
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graph  # noqa: E402
import obsidian  # noqa: E402
import organize  # noqa: E402
import voicerec  # noqa: E402
from store import Store  # noqa: E402
from transcribe import Transcript, WhisperRemote  # noqa: E402

DSN = os.environ.get("VOICE_REC_TEST_DSN", "")
needs_db = pytest.mark.skipif(not DSN, reason="set VOICE_REC_TEST_DSN to a Postgres you can create schemas in")


# ---- stubs --------------------------------------------------------------------------------------


class Recorder(BaseHTTPRequestHandler):
    """Records requests; answers like an LLM, a webhook or Neo4j depending on the path."""

    requests = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Recorder.requests.append((self.path, dict(self.headers), body))
        if self.path.endswith("/chat/completions"):
            content = json.dumps({"title": "Call the dentist", "category": "todo", "tags": ["Health", "#errands"],
                                  "summary": "Book a checkup.", "actions": ["Call Dr. Lee"],
                                  "entities": [{"name": "Dr. Lee", "type": "person"},
                                               {"name": "Cleveland", "type": "city"}]})
            reply = {"choices": [{"message": {"content": "Sure! " + content}}]}
        elif self.path.endswith("/audio/transcriptions"):
            reply = {"text": " hello there ", "language": "en", "segments": [{"start": 0, "end": 1.5, "text": " hello there"}]}
        elif "/tx/commit" in self.path:
            reply = {"results": [], "errors": []}
        else:
            reply = {"ok": True}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub():
    Recorder.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def store():
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in DSN else "?"
    s = Store(f"{DSN}{sep}options=-csearch_path%3D{schema}")
    s.migrate()
    yield s
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def fake_transcriber(audio, mime):
    return Transcript(f"note with {len(audio)} bytes, call the dentist", "en",
                      [{"start": 0.0, "end": 2.0, "text": "call the dentist"}], "fake")


def serve_api(store, worker, token="tok"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), voicerec.make_handler(store, worker, token, "fake"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def call(method, url, body=None, headers=None, token="tok"):
    hdrs = {"Authorization": f"Bearer {token}", **(headers or {})}
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if resp.headers.get("Content-Type") == "application/json" else raw)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# ---- the whole path, phone to vault ------------------------------------------------------------


@needs_db
def test_upload_transcribe_archive(store, stub, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    env = {"OBSIDIAN_VAULT": str(vault), "LLM_URL": stub, "GRAPH_KIND": "neo4j", "GRAPH_URL": stub,
           "GRAPH_PASSWORD": "pw"}
    worker = voicerec.Worker(store, fake_transcriber, env)
    server, base = serve_api(store, worker)
    try:
        rid = str(uuid.uuid4())
        audio = b"\x00m4a" * 100
        hdr = {"Content-Type": "audio/mp4", "X-Recorded-At": "2026-11-01T18:00:00+00:00", "X-Duration": "12.5"}

        assert call("PUT", f"{base}/api/recordings/{rid}", audio, hdr, token="bad")[0] == 401
        status, rec = call("PUT", f"{base}/api/recordings/{rid}", audio, hdr)
        assert status == 201 and rec["status"] == "queued"
        assert call("PUT", f"{base}/api/recordings/{rid}", audio, hdr)[0] == 200  # sent twice: fine
        assert call("PUT", f"{base}/api/recordings/{rid}", b"other", hdr)[0] == 409

        assert worker.run_once()
        status, rec = call("GET", f"{base}/api/recordings/{rid}")
        assert rec["status"] == "transcribed" and "call the dentist" in rec["transcript"] and rec["transcribed_at"]
        assert not worker.run_once()  # nothing to archive until the phone asks

        assert call("POST", f"{base}/api/recordings/{rid}/archive", b"{}")[0] == 202
        assert worker.run_once()
        status, rec = call("GET", f"{base}/api/recordings/{rid}")
        assert rec["status"] == "archived", rec
        assert rec["title"] == "Call the dentist" and rec["category"] == "Todo"
        assert rec["tags"] == ["health", "errands"]
        assert rec["entities"][1] == {"name": "Cleveland", "type": "topic"}

        note = vault / rec["note_path"]
        text = note.read_text()
        assert note.parent.name == "Todo" and note.name.endswith("Call the dentist.md")
        assert "- [ ] Call Dr. Lee" in text and "[[Dr. Lee]]" in text and 'people: ["[[Dr. Lee]]"]' in text
        assert "![[Voice Notes/_audio/" in text
        assert list((vault / "Voice Notes" / "_audio").iterdir())[0].read_bytes() == audio

        neo = [r for r in Recorder.requests if "/tx/commit" in r[0]]
        assert neo and neo[0][0] == "/db/neo4j/tx/commit"
        statements = json.loads(neo[0][2])["statements"]
        assert statements[0]["parameters"]["id"] == rid and statements[0]["parameters"]["actions"] == ["Call Dr. Lee"]

        status, raw = call("GET", f"{base}/api/recordings/{rid}/audio")
        assert raw == audio
        status, page = call("GET", f"{base}/?token=tok", token="")
        assert status == 200 and b"Call the dentist" in page
        assert call("GET", f"{base}/api/recordings?q=dentist")[1]["recordings"][0]["id"] == rid

        # Your own notes under the marker survive re-archiving.
        note.write_text(text + "remember insurance card\n")
        assert store.rearchive(rid) == 1
        assert worker.run_once()
        assert "remember insurance card" in (vault / store.get(rid)["note_path"]).read_text()
    finally:
        server.shutdown()


@needs_db
def test_list_is_oldest_first_and_failures_retry(store):
    calls = []

    def flaky(audio, mime):
        calls.append(1)
        raise RuntimeError("GPU out of memory")

    for when in ("2026-11-02T08:00:00+00:00", "2026-11-01T08:00:00+00:00"):
        store.add(str(uuid.uuid4()), when.encode(), "audio/mp4", when, 3)
    assert [r["recorded_at"][:10] for r in store.list()] == ["2026-11-01", "2026-11-02"]

    worker = voicerec.Worker(store, flaky, {})
    for _ in range(6):
        worker.run_once()
    rows = store.list()
    assert all(r["status"] == "failed" and "GPU out of memory" in r["error"] for r in rows)
    assert not worker.run_once() and len(calls) == 6

    store.retry(rows[0]["id"])
    worker.transcriber = fake_transcriber
    assert worker.run_once() and store.get(rows[0]["id"])["status"] == "transcribed"


@needs_db
def test_crash_recovery_and_auto_archive(store):
    rid = str(uuid.uuid4())
    store.add(rid, b"x", "audio/mp4", "2026-11-01T08:00:00+00:00", 1)
    assert store.claim()[0] == "transcribe"
    assert store.recover() == 1 and store.get(rid)["status"] == "queued"
    worker = voicerec.Worker(store, fake_transcriber, {"AUTO_ARCHIVE": "1"})
    worker.run_once()
    worker.run_once()
    rec = store.get(rid)
    assert rec["status"] == "archived" and rec["category"] == "Inbox"
    assert "no LLM_URL" in rec["error"]  # archived anyway, with the reason kept


# ---- pieces without a database -----------------------------------------------------------------


def test_remote_transcriber(stub):
    t = WhisperRemote(stub)("x" and b"audio", "audio/mp4")
    path, headers, body = Recorder.requests[-1]
    assert path == "/v1/audio/transcriptions" and b'filename="note.m4a"' in body
    assert t.text == "hello there" and t.language == "en" and t.segments[0]["end"] == 1.5


def test_organize_fallback_and_cleaning():
    info, problem = organize.organize("buy milk eggs and bread on the way home from work today", {})
    assert info["title"] == "buy milk eggs and bread on the" and info["category"] == "Inbox" and "LLM_URL" in problem
    cleaned = organize.clean({"category": "IDEAS", "tags": ["A b", "a-b"], "entities": [{"name": ""}]},
                             ["Ideas", "Inbox"], "x")
    assert cleaned["category"] == "Ideas" and cleaned["tags"] == ["a-b"] and cleaned["entities"] == []
    assert organize.chat_url("http://h:1/v1") == "http://h:1/v1/chat/completions"


def test_categories_file():
    cats = organize.categories()
    assert "Ideas" in cats and cats.count("Inbox") == 1


def test_obsidian_names_and_moves(tmp_path):
    rec = {"id": "3f2a9c1b-0000-0000-0000-000000000000", "recorded_at": "2026-11-01T18:00:00+00:00",
           "title": 'What: a/b "test"?', "category": "Ideas", "transcript": "hi", "mime": "audio/mp4"}
    first = obsidian.write(rec, b"aa", tmp_path)
    assert "/" not in first.name and ":" not in first.name and first.parts[1] == "Ideas"
    moved = obsidian.write({**rec, "category": "Work", "note_path": first.as_posix()}, b"aa", tmp_path)
    assert moved.parts[1] == "Work" and not (tmp_path / first).exists()
    with pytest.raises(FileNotFoundError):
        obsidian.write(rec, b"", tmp_path / "missing")


def test_graph_webhook_and_off(stub):
    rec = {"id": "x", "recorded_at": "2026-11-01T18:00:00+00:00", "tags": ["a"]}
    assert graph.send(rec, {}) == ""
    assert graph.send(rec, {"GRAPH_KIND": "webhook", "GRAPH_URL": stub + "/ingest", "GRAPH_PASSWORD": "t"})
    path, headers, body = Recorder.requests[-1]
    assert path == "/ingest" and headers["Authorization"] == "Bearer t" and json.loads(body)["type"] == "voice-note"
    with pytest.raises(ValueError):
        graph.send(rec, {"GRAPH_KIND": "mystery", "GRAPH_URL": stub})

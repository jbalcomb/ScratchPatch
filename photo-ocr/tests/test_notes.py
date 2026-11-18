"""Handwritten notes and cork boards (NOTES-PRD.md, phase 1) against a real Postgres: a fresh database per test.

Set NOTES_TEST_DSN to a server you may create databases on (default postgresql://postgres@127.0.0.1:5432/postgres);
without one these tests are skipped."""
import io
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

import pytest

import notes
import web
from fakes import FakeModel, png
from test_service import form, make_service

ADMIN = os.environ.get("NOTES_TEST_DSN", "postgresql://postgres@127.0.0.1:5432/postgres")


def jpeg(w=40, h=20, shade=200, taken="2026:10:24 14:01:08"):
    from PIL import Image
    im = Image.new("RGB", (w, h), (shade, shade, shade))
    exif = Image.Exif()
    exif[0x0132] = taken
    b = io.BytesIO()
    im.save(b, "JPEG", exif=exif)
    return b.getvalue()


@pytest.fixture
def dsn():
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(ADMIN, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"no Postgres for the notes tests ({e})")
    name = "notes_test_" + uuid.uuid4().hex[:10]
    admin.execute(f"CREATE DATABASE {name}")
    yield ADMIN.rsplit("/", 1)[0] + "/" + name
    admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
    admin.close()


@pytest.fixture
def model():
    m = FakeModel()
    yield m
    m.close()


@pytest.fixture
def api(tmp_path, model, dsn):
    svc = make_service(tmp_path, model, NOTES_DATABASE_URL=dsn)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(web.App(svc, None)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(method, path, body=None, ctype=None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data, method=method,
                                     headers={"Content-Type": ctype or "application/json"} if data is not None else {})
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers["Content-Type"] == "application/json" else
                                     (raw, resp.headers["Content-Type"]))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    call.svc = svc
    yield call
    httpd.shutdown()


def send_note(api, *photos, **fields):
    body, ctype = form([("photo", f"p{i}.jpg", data) for i, data in enumerate(photos, 1)], **fields)
    return api("POST", "/api/notes", body, ctype)


# ------------------------------------------------------------ measuring

def test_words_ignore_layout_and_keep_unsure_words():
    text = "# Pi:\n**need** for OpenClaw\nClaude Code → Open Code\n[box]\n- Docker\n~~?~~ ~~old~~ [DEV?]\n--- page 2 ---\n[drawing: a cat]"
    assert notes.words(text) == ["pi", "need", "for", "openclaw", "claude", "code", "open", "code", "docker", "old", "dev"]


def test_measure_a_reading_against_the_approved_transcript():
    truth = "Claude Code → Open Code → Pi\nUltimate Pi: DEV"
    assert notes.measure(truth, truth) == {"words": 8, "wer": 0.0, "cer": 0.0, "uncertain": 0, "uncertain_right": 0}
    m = notes.measure("Claude Code → Opan Code → Pi\nUltimate Pi: [DEV?] [Task?]", truth)
    assert (m["words"], m["wer"], m["uncertain"], m["uncertain_right"]) == (8, 0.25, 2, 1)  # Opan, Task; DEV was right
    assert 0 < m["cer"] < m["wer"]
    assert notes.measure("", "") == {"words": 0, "wer": 0.0, "cer": 0.0, "uncertain": 0, "uncertain_right": 0}


def test_the_markdown_comes_out_of_a_fenced_reply():
    assert notes.clean_reply("<think>hm</think>\n```markdown\n# A\nb\n```") == "# A\nb"
    assert notes.clean_reply("# A") == "# A"


# ------------------------------------------------------------ capture, read, review

def test_a_note_is_archived_read_reviewed_and_measured(api, model):
    status, made = send_note(api, jpeg(shade=10), jpeg(shade=20), kind="handwritten", event="AI meetup")
    assert status == 201 and made["status"] == "pending" and made["url"] == "/notes/1"
    n = api("GET", "/api/notes/1")[1]
    assert (n["kind"], n["event"], n["written_on"], len(n["photos"])) == ("handwritten", "AI meetup", "2026-10-24", 2)
    stored = api.svc.notes.one("SELECT data, bytes, width FROM note_photos WHERE note_id = 1 AND position = 1")
    assert bytes(stored["data"]) == jpeg(shade=10) and stored["width"] == 40  # the original, as it came

    assert api.svc.work_once()["notes"] == 1
    request = json.loads(model.requests[-1][2])
    content = request["messages"][0]["content"]
    assert sum(c["type"] == "image_url" for c in content) == 2 and "response_format" not in request
    assert "[DEV?]" in content[-1]["text"] and "--- page N ---" in content[-1]["text"]
    n = api("GET", "/api/notes/1")[1]
    assert n["status"] == "read" and n["transcript"] == model.note_reply and n["uncertain"] == 1
    assert n["first_line"] == "Pi:" and n["runs"][0]["prompt_version"] == notes.parsing.prompt_version(notes.NOTE_PROMPT)

    fixed = model.note_reply.replace("[DEV?]", "DEV").replace("Docker", "Docker\n- Tailscale")
    status, n = api("POST", "/api/notes/1", {"transcript": fixed, "event": "AI meetup, Cleveland", "approve": True})
    assert status == 200 and n["status"] == "approved" and n["transcript"] == fixed
    [run] = api("GET", "/api/notes/1/runs")[1]["runs"]
    assert (run["words"], run["uncertain"], run["uncertain_right"]) == (15, 1, 1) and run["wer"] == round(1 / 15, 4)
    fields = {r["field"] for r in api.svc.notes.q("SELECT field FROM note_corrections WHERE note_id = 1")}
    assert fields == {"transcript", "event"}

    acc = api("GET", "/api/notes/accuracy")[1]
    version = run["prompt_version"]
    assert acc["by_prompt"][version]["readings"] == 1 and acc["by_engine"]["vl-a"]["words"] == 15
    assert acc["worst"][0]["url"] == "/notes/1"
    assert api("GET", "/api/notes?q=openclaw")[1]["notes"][0]["id"] == 1  # full-text search
    assert api("GET", "/api/notes?q=receipt")[1]["notes"] == []


def test_a_cork_board_needs_its_name_and_gets_the_board_prompt(api, model):
    assert send_note(api, jpeg(), kind="board")[1] == {"error": "which board? (board)"}
    assert send_note(api, jpeg(), kind="board", board="Job search", written_on="2026-11-01")[0] == 201
    api.svc.work_once()
    n = api("GET", "/api/notes/1")[1]
    assert (n["kind"], n["board"], n["written_on"], n["transcript"]) == ("board", "Job search", "2026-11-01", model.board_reply)
    assert "one cork board" in json.loads(model.requests[-1][2])["messages"][0]["content"][-1]["text"]


def test_bad_input(api):
    assert send_note(api, jpeg(), kind="poster")[0] == 400
    assert send_note(api, jpeg(), written_on="Nov 1")[1] == {"error": "written_on must be YYYY-MM-DD"}
    assert send_note(api, jpeg(shade=5))[0] == 201
    assert send_note(api, jpeg(shade=5))[1] == {"error": "p1.jpg is already stored (note 1)"}
    assert api("GET", "/api/notes/9")[0] == 404
    assert api("POST", "/api/notes/1", {"transcript": " ", "approve": True})[1] == {
        "error": "nothing to approve: the transcript is empty"}


def test_a_model_server_that_is_down_leaves_the_note_pending(api, model):
    send_note(api, jpeg())
    model.close()
    assert api.svc.work_once()["notes"] == 0
    assert api("GET", "/api/notes/1")[1]["status"] == "pending"


def test_turning_a_photo_upright_for_the_page_and_the_next_reading(api, model):
    send_note(api, jpeg(w=40, h=20))
    status, n = api("POST", "/api/notes/1/photos/1", {"turn": 90})
    assert status == 200 and n["photos"][0]["turn"] == 90
    from PIL import Image
    (raw, ctype) = api("GET", "/api/notes/1/photos/1?w=400")[1]
    assert ctype == "image/jpeg" and Image.open(io.BytesIO(raw)).size == (20, 40)  # turned
    (raw, ctype) = api("GET", "/api/notes/1/photos/1")[1]
    assert raw == jpeg(w=40, h=20)  # the original is never changed
    assert api("POST", "/api/notes/1/photos/1", {"turn": 45})[1] == {"error": "turn must be 0, 90, 180 or 270"}
    [path] = api.svc.notes.photo_paths(1)
    assert Image.open(path).size == (20, 40) and path.name.endswith("-turn90.jpg")


def test_read_again_and_delete(api, model):
    send_note(api, jpeg())
    api.svc.work_once()
    model.note_reply = "second reading"
    assert api("POST", "/api/notes/1/reparse")[1] == {"status": "pending"}
    api.svc.work_once()
    n = api("GET", "/api/notes/1")[1]
    assert n["transcript"] == "second reading" and len(n["runs"]) == 2
    assert api("DELETE", "/api/notes/1")[1] == {"ok": True}
    assert api("GET", "/api/notes/1")[0] == 404
    assert api.svc.notes.q("SELECT COUNT(*) AS n FROM note_photos")[0]["n"] == 0


def test_pages_and_health(api):
    for path in ("/notes", "/notes/1"):
        status, (page, _ctype) = api("GET", path)
        assert status == 200 and b"<script>" in page
    assert api("GET", "/api/health")[1]["notes"] == "ok"


def test_without_a_database_notes_say_how_to_set_them_up(tmp_path, model):
    svc = make_service(tmp_path, model)
    assert svc.notes is None and svc.read_notes() == 0
    with pytest.raises(ValueError, match="NOTES_DATABASE_URL"):
        svc.require_notes()

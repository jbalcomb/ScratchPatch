import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from taskdeck.jobs.voice_rec import PLUGIN
from taskdeck.jobs.voice_rec.client import Client, ServerError, base_url
from taskdeck.jobs.voice_rec.library import (
    ARCHIVE, ARCHIVED, ARCHIVING, QUEUED, RECORDED, TRANSCRIBE, TRANSCRIBED, Library, clock,
)
from taskdeck.jobs.voice_rec.sync import recover_orphans, sync_once


class FakeServer:
    """Stands in for the workstation: transcribes on demand (``finish``)."""

    def __init__(self):
        self.recs = {}
        self.uploads = []
        self.archive_calls = []
        self.down = False

    def _up(self):
        if self.down:
            raise ServerError("The AI Workstation isn't reachable (test)", None)

    configured = True

    def upload(self, rec, audio):
        self._up()
        self.uploads.append(rec.id)
        self.recs.setdefault(rec.id, {"status": "queued", "audio": audio})
        return {"status": "queued"}

    def get(self, rec_id):
        self._up()
        r = self.recs.get(rec_id)
        return None if r is None else {k: v for k, v in r.items() if k != "audio"}

    def archive(self, rec_id, changes=None):
        self._up()
        self.archive_calls.append(rec_id)
        self.recs[rec_id]["status"] = "archiving"
        return {"status": "archiving"}

    def finish_transcribing(self, rec_id, text="buy milk and call the dentist"):
        self.recs[rec_id].update(status="transcribed", transcript=text, transcribed_at="2026-11-01T18:05:00+00:00")

    def finish_archiving(self, rec_id):
        self.recs[rec_id].update(status="archived", archived_at="2026-11-01T18:06:00+00:00", title="Errands",
                                 category="Todo", tags=["errands"])


@pytest.fixture
def lib(tmp_path):
    return Library(tmp_path / "voice")


def record(lib, when, mobile=None, data=b"audio"):
    rec_id, path = lib.new_path()
    path.write_bytes(data)
    return lib.add(rec_id, path, when, 12.4, mobile)


def test_history_is_ascending_by_recorded_time(lib):
    b = record(lib, "2026-11-01T10:00:00+00:00")
    a = record(lib, "2026-11-01T09:00:00+00:00")
    c = record(lib, "2026-11-02T08:00:00+00:00")
    assert [r.id for r in lib.all()] == [a.id, b.id, c.id]


def test_full_flow_on_wifi(lib):
    server = FakeServer()
    rec = record(lib, "2026-11-01T18:00:00+00:00")

    res = sync_once(lib, server, on_wifi=True)
    assert res.uploaded == 1 and res.busy == 1
    assert lib.get(rec.id).status == QUEUED and lib.get(rec.id).uploaded_at

    server.finish_transcribing(rec.id)
    res = sync_once(lib, server, on_wifi=True)
    got = lib.get(rec.id)
    assert res.transcribed == 1
    assert got.transcript == "buy milk and call the dentist"
    assert got.transcribed_at == "2026-11-01T18:05:00+00:00"
    assert got.status == ARCHIVING and server.archive_calls == [rec.id]

    server.finish_archiving(rec.id)
    res = sync_once(lib, server, on_wifi=True)
    got = lib.get(rec.id)
    assert res.archived == 1 and got.status == ARCHIVED
    assert (got.title, got.category, got.tags) == ("Errands", "Todo", ["errands"])
    assert lib.path(got).exists()  # keep_audio defaults to on
    assert lib.pending() == []


def test_wifi_only_by_default_and_override_per_recording(lib):
    server = FakeServer()
    plain = record(lib, "2026-11-01T09:00:00+00:00")
    urgent = record(lib, "2026-11-01T10:00:00+00:00", mobile={TRANSCRIBE: True})

    res = sync_once(lib, server, on_wifi=False)
    assert server.uploads == [urgent.id]
    assert res.waiting_wifi == 1 and lib.get(plain.id).status == RECORDED

    # The override was for transcribing only: archiving still waits for Wi-Fi.
    server.finish_transcribing(urgent.id)
    res = sync_once(lib, server, on_wifi=False)
    assert lib.get(urgent.id).status == TRANSCRIBED and server.archive_calls == []
    assert res.waiting_wifi == 2

    lib.set_mobile_data(urgent.id, ARCHIVE, True)
    sync_once(lib, server, on_wifi=False)
    assert server.archive_calls == [urgent.id]


def test_unreachable_stops_the_pass(lib):
    server = FakeServer()
    server.down = True
    record(lib, "2026-11-01T09:00:00+00:00")
    record(lib, "2026-11-01T10:00:00+00:00")
    res = sync_once(lib, server, on_wifi=True)
    assert "isn't reachable" in res.unreachable
    assert all(r.status == RECORDED for r in lib.all())


def test_server_that_lost_a_recording_gets_it_again(lib):
    server = FakeServer()
    rec = record(lib, "2026-11-01T09:00:00+00:00")
    sync_once(lib, server, on_wifi=True)
    server.recs.clear()
    sync_once(lib, server, on_wifi=True)
    assert lib.get(rec.id).status == RECORDED and "sent again" in lib.get(rec.id).error
    sync_once(lib, server, on_wifi=True)
    assert lib.get(rec.id).status == QUEUED and lib.get(rec.id).error == ""


def test_manual_archive_and_dropping_audio(lib):
    server = FakeServer()
    rec = record(lib, "2026-11-01T09:00:00+00:00")
    sync_once(lib, server, on_wifi=True, auto_archive=False)
    server.finish_transcribing(rec.id)
    sync_once(lib, server, on_wifi=True, auto_archive=False)
    assert lib.get(rec.id).status == TRANSCRIBED and server.archive_calls == []

    sync_once(lib, server, on_wifi=True, auto_archive=False, archive_now={rec.id})
    server.finish_archiving(rec.id)
    sync_once(lib, server, on_wifi=True, auto_archive=False, keep_audio=False)
    got = lib.get(rec.id)
    assert got.status == ARCHIVED and got.file == "" and not list(lib.audio_dir.iterdir())


def test_failed_transcription_is_shown(lib):
    server = FakeServer()
    rec = record(lib, "2026-11-01T09:00:00+00:00")
    sync_once(lib, server, on_wifi=True)
    server.recs[rec.id].update(status="failed", error="no speech found")
    sync_once(lib, server, on_wifi=True)
    assert "no speech found" in lib.get(rec.id).error


def test_orphaned_audio_is_recovered(lib):
    _, path = lib.new_path()
    path.write_bytes(b"x" * 10)
    _, empty = lib.new_path()
    empty.write_bytes(b"")
    assert recover_orphans(lib) == 1
    assert [r.file for r in lib.all()] == [path.name] and not empty.exists()


def test_not_configured():
    res = sync_once(Library.__new__(Library), Client(""), on_wifi=True)
    assert "workstation address" in res.unreachable


def test_helpers():
    assert base_url("ai-box:8093/") == "http://ai-box:8093"
    assert clock(65) == "1:05" and clock(3725) == "1:02:05"


def test_plugin_pipeline_lists_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKDECK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)
    report = PLUGIN.run({})
    assert report.ok and "0 recordings" in report.result.text


# ---- the HTTP client against a stub server -----------------------------------------------------


class Stub(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Stub.seen.append(("PUT", self.path, dict(self.headers), body))
        if self.headers.get("Authorization") != "Bearer secret":
            return self._reply(401, {"error": "unauthorized"})
        self._reply(201, {"status": "queued"})

    def do_GET(self):
        if self.path.endswith("/missing"):
            return self._reply(404, {"error": "not found"})
        self._reply(200, {"status": "transcribed", "transcript": "hi"})


@pytest.fixture
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_client_upload_and_get(stub, lib):
    rec = record(lib, "2026-11-01T09:00:00+00:00", mobile={TRANSCRIBE: True}, data=b"m4a-bytes")
    assert Client(stub, "secret").upload(rec, b"m4a-bytes") == {"status": "queued"}
    method, path, headers, body = Stub.seen[-1]
    assert path == f"/api/recordings/{rec.id}" and body == b"m4a-bytes"
    assert headers["X-Recorded-At"] == "2026-11-01T09:00:00+00:00" and headers["X-Mobile-Data"] == "transcribe"
    with pytest.raises(ServerError, match="access token"):
        Client(stub, "wrong").upload(rec, b"m4a-bytes")
    assert Client(stub).get("missing") is None
    assert Client(stub).get("abc")["transcript"] == "hi"


def test_client_unreachable():
    with pytest.raises(ServerError) as exc:
        Client("http://127.0.0.1:9", timeout=2).get("x")
    assert exc.value.unreachable


# ---- the screen, in the real app with a fake page ----------------------------------------------


def test_screen_opens_and_lists_history(tmp_path):
    from test_extract_archive import make_app

    from taskdeck.jobs.voice_rec.screen import VoiceRecScreen, VoiceSync

    app = make_app(tmp_path)
    sync = VoiceSync.for_app(app, PLUGIN)
    assert sync is VoiceSync.for_app(app, PLUGIN)  # one per app, started by on_app_start
    assert sync.on_network in app.network.listeners
    record(sync.lib, "2026-11-01T09:00:00+00:00")
    record(sync.lib, "2026-11-01T08:00:00+00:00")

    app.open_job("voice_rec")
    screen = app.stack[-1]
    assert isinstance(screen, VoiceRecScreen)
    cards = screen.history_box.controls
    assert len(cards) == 2
    assert sync.default_mobile_data() == {TRANSCRIBE: False, ARCHIVE: False}  # Wi-Fi only by default

    screen.details(sync.lib.all()[0].id)  # builds the dialog without errors
    assert app.page.dialogs


def test_screen_recording_state_and_closing(tmp_path):
    from test_extract_archive import make_app

    from taskdeck.jobs.voice_rec.screen import VoiceSync

    app = make_app(tmp_path)
    app.open_job("voice_rec")
    screen = app.stack[-1]
    rec = screen.recorder
    rec.rec_id, rec.started_at, rec._t0 = "x", "2026-11-01T09:00:00+00:00", 0.0  # pretend it's recording
    screen.render()
    assert len(screen.record_box.controls) == 4  # state, timer, buttons, mobile-data switch
    rec._reset()

    sync = VoiceSync.for_app(app, PLUGIN)
    assert screen._on_sync in sync.listeners
    app.back()
    sync._notify()  # a closed screen unsubscribes instead of redrawing
    assert screen._on_sync not in sync.listeners


# ---- microphone permission and crash breadcrumbs ----------------------------------------------


class FakeRecorderService:
    def __init__(self):
        self.calls = []

    async def has_permission(self):
        raise AssertionError("the recorder plugin's own permission call must not be used")

    async def start_recording(self, output_path=None, configuration=None):
        # Flet maps a path that isn't an existing file into the app's assets folder (ENOENT on the phone).
        from pathlib import Path
        assert Path(output_path).is_absolute() and Path(output_path).exists(), "the file must exist first"
        self.calls.append("start")
        self.configuration = configuration
        open(output_path, "wb").write(b"audio")
        return True

    async def stop_recording(self):
        self.calls.append("stop")
        return None


def fake_permissions(monkeypatch, statuses):
    """flet_permission_handler stand-in: get_status / request answer from ``statuses`` in turn."""
    import enum
    import sys
    import types

    class Status(enum.Enum):
        GRANTED, DENIED, PERMANENTLY_DENIED = "granted", "denied", "permanentlyDenied"

    answers = iter(statuses)

    class Handler:
        async def get_status(self, p):
            return Status[next(answers)]

        async def request(self, p):
            return Status[next(answers)]

    mod = types.SimpleNamespace(Permission=types.SimpleNamespace(MICROPHONE="mic"), PermissionHandler=Handler,
                                PermissionStatus=Status)
    monkeypatch.setitem(sys.modules, "flet_permission_handler", mod)
    from taskdeck.jobs.voice_rec import screen
    monkeypatch.setattr(screen, "is_android", lambda: True)
    monkeypatch.setattr(screen, "recorder_supported", lambda: True)


def run(coro):
    import asyncio

    return asyncio.run(coro)


# These build the recorder's real configuration objects. flet-audio-recorder is a TaskDeck dependency
# (pyproject.toml) and CI installs it; a test run without it skips them and says why.
needs_recorder = pytest.mark.skipif(importlib.util.find_spec("flet_audio_recorder") is None,
                                    reason='needs flet-audio-recorder: pip install "flet-audio-recorder==1.0.4"')


@needs_recorder
def test_fresh_permission_grant_doesnt_start_recording(monkeypatch, tmp_path):
    from taskdeck.core import JsonStore
    from taskdeck.jobs.voice_rec.screen import MIC_BLOCKED, MIC_JUST_ALLOWED, Recorder

    store = JsonStore(tmp_path / "state.json")
    rec = Recorder(Library(tmp_path / "v"), store)
    rec.service = FakeRecorderService()

    fake_permissions(monkeypatch, ["DENIED", "GRANTED"])  # not yet allowed → Android asks → allowed
    assert run(rec.start()) == MIC_JUST_ALLOWED
    assert rec.service.calls == [] and not rec.active
    assert store.get("voice_rec:native_step") == {}  # the prompt finished: no breadcrumb left

    fake_permissions(monkeypatch, ["GRANTED"])  # the next tap records
    assert run(rec.start()) == "" and rec.active and rec.service.calls == ["start"]
    assert run(rec.stop({})) is not None

    fake_permissions(monkeypatch, ["DENIED", "PERMANENTLY_DENIED"])
    assert run(rec.start()) == MIC_BLOCKED


def test_after_a_crash_voice_notes_reopens_and_says_where(tmp_path):
    import time

    from test_extract_archive import make_app

    from taskdeck.core import JsonStore
    from taskdeck.jobs.voice_rec.screen import VoiceRecScreen

    store = JsonStore(tmp_path / "state.json")
    store.set("voice_rec:native_step", {"step": "asking for microphone permission", "at": time.time()})
    app = make_app(tmp_path)  # the same state file: TaskDeck starting again after it closed
    screen = app.stack[-1]
    assert isinstance(screen, VoiceRecScreen)
    assert screen.crash["step"] == "asking for microphone permission"
    texts = [c.controls[1].value for c in screen.record_box.controls if isinstance(c, type(screen.status_box.controls[0]))
             and len(c.controls) == 2 and hasattr(c.controls[1], "value")]
    assert any("while asking for microphone permission" in t for t in texts)
    assert app.store.get("voice_rec:native_step") == {}  # shown once

    app2 = make_app(tmp_path)  # a normal start afterwards stays on Home
    assert not app2.stack


@needs_recorder
def test_recorder_settings_step_down_after_a_crash_while_starting(monkeypatch, tmp_path):
    from taskdeck.core import JsonStore
    from taskdeck.jobs.voice_rec.screen import BREADCRUMB, PROFILES, Recorder

    store = JsonStore(tmp_path / "state.json")
    rec = Recorder(Library(tmp_path / "v"), store)
    rec.service = FakeRecorderService()
    fake_permissions(monkeypatch, ["GRANTED"] * 10)

    assert run(rec.start()) == ""
    android = rec.service.configuration.android_configuration
    assert android.use_legacy and not android.manage_bluetooth  # safest settings first
    assert rec.service.configuration.channels == 1 and rec.path.suffix == ".m4a"
    run(rec.stop({}))

    for expected in range(1, len(PROFILES)):
        # TaskDeck "closes" while starting: the breadcrumb is left behind with the settings in use...
        store.set(BREADCRUMB, {"step": "starting the recording", "at": 0, "profile": rec.profile_index})
        crumb = rec.last_crash()
        assert rec.profile_index == expected and crumb["next_profile"] == PROFILES[expected]["name"]
    store.set(BREADCRUMB, {"step": "starting the recording", "at": 0, "profile": rec.profile_index})
    rec.last_crash()
    assert rec.profile_index == len(PROFILES) - 1  # stays on the last one

    assert run(rec.start()) == "" and rec.path.suffix == ".wav"
    saved = run(rec.stop({}))
    from taskdeck.jobs.voice_rec.library import mime_of
    assert mime_of(saved.file) == "audio/wav"


@needs_recorder
def test_a_recorder_error_is_a_message_not_a_crash(monkeypatch, tmp_path):
    from taskdeck.core import JsonStore
    from taskdeck.jobs.voice_rec.screen import BREADCRUMB, Recorder

    class Failing(FakeRecorderService):
        async def start_recording(self, output_path=None, configuration=None):
            raise RuntimeError("PlatformException(record, ... open failed: ENOENT ...)")

    store = JsonStore(tmp_path / "state.json")
    lib = Library(tmp_path / "v")
    rec = Recorder(lib, store)
    rec.service = Failing()
    fake_permissions(monkeypatch, ["GRANTED"])
    problem = run(rec.start())
    assert problem.startswith("The recorder didn't start:") and "ENOENT" in problem
    assert store.get(BREADCRUMB) == {}  # not mistaken for a crash, so the settings don't step down
    assert rec.last_crash() is None and rec.profile_index == 0
    assert not rec.active and list(lib.audio_dir.iterdir()) == []  # no empty file left behind

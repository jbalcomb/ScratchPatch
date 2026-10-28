"""Diagnostics traces, their Drive copy, the per-request network scope, and Wi-Fi only."""

import http.server as http_server
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskdeck.core import Cancelled, JsonStore, TaskManager
from taskdeck.core.tasks import DONE, FAILED
from taskdeck.core.trace import Trace, TraceStore
from taskdeck.shared import gdrive, http
from taskdeck.ui.diagnostics import FOLDER, DiagnosticsSync
from taskdeck.ui.network import WAITING, NetworkMonitor


from taskdeck.core import trace_report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import trace_report as trace_report_cli  # noqa: E402

from test_gdrive import FIX, FakeDrive, client, needs_engine  # noqa: E402


def events(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


# ---- traces ----------------------------------------------------------------------------------------

def test_trace_file_and_heartbeat(tmp_path):
    beats = iter(range(100))
    t = Trace(tmp_path / "a.jsonl", heartbeat=lambda: {"n": next(beats)}, interval=0.02)
    t.event("hello", x=1)
    time.sleep(0.1)
    t.close()
    t.event("after close")  # ignored
    ev = events(tmp_path / "a.jsonl")
    assert ev[0]["ev"] == "hello" and ev[0]["x"] == 1 and "t" in ev[0] and "s" in ev[0]
    assert sum(e["ev"] == "heartbeat" for e in ev) >= 2 and ev[-1]["ev"] != "after close"


def test_trace_store_keeps_the_newest(tmp_path):
    store = TraceStore(tmp_path / "d", keep=3)
    for i in range(5):
        store.new(f"job {i}/x").close()
    names = [p.name for p in store.files()]
    assert len(names) == 3 and names[-1].endswith("job-4-x.jsonl")


def test_task_manager_traces(tmp_path):
    tm = TaskManager(start_thread=lambda fn: fn(), traces=TraceStore(tmp_path))

    def work(t):
        t.set_phases(("Unpacking", 10), ("Uploading", 10))
        t.advance(0, 5)
        t.advance(1, 3)
        t.set_note("Paused")
        t.set_note("")
        return "ok"
    t = tm.start("Extracting a.7z", "extract_archive", work)
    assert t.status == DONE
    kinds = [e["ev"] for e in events(t.trace.path)]
    assert kinds == ["start", "phases", "phase", "phase", "note", "note", "end"]
    end = events(t.trace.path)[-1]
    assert end["status"] == DONE and end["message"] == "ok"

    def broken(t):
        raise ValueError("boom")
    t = tm.start("x", "j", broken)
    end = events(t.trace.path)[-1]
    assert t.status == FAILED and "ValueError: boom" in end["traceback"]


# ---- the per-thread network scope -----------------------------------------------------------------

@pytest.fixture
def server():
    class Handler(http_server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}' if self.path != "/missing" else b'{"error": "nope"}'
            self.send_response(200 if self.path != "/missing" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(308)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_scope_records_and_gates_requests(server):
    seen, gated = [], []
    with http.scope(trace=lambda ev, **f: seen.append((ev, f)), gate=gated.append):
        assert http.request("GET", f"{server}/a?q=secret") == {"ok": True}
        assert http.send("PUT", f"{server}/up", b"x" * 1000).status == 308
        with pytest.raises(http.HttpError):
            http.request("GET", f"{server}/missing")
        with pytest.raises(http.HttpError):
            http.request("GET", "http://127.0.0.1:9/", timeout=2)
    http.request("GET", f"{server}/b")  # outside the scope: not recorded
    assert len(gated) == 4
    rec = [f for _, f in seen]
    assert [r.get("status") for r in rec] == [200, 308, 404, None]
    assert rec[0]["path"] == "/a" and "secret" not in json.dumps(rec)  # no query strings
    assert rec[1]["sent"] == 1000 and "error" in rec[3]

    sent = []
    with http.scope(trace=lambda ev, **f: seen.append((ev, f))):
        assert http.send("PUT", f"{server}/up", gdrive.Piece(b"y" * 600_000, sent.append),
                         {"Content-Length": "600000"}).status == 308
    assert sent == [262144, 524288, 600000] and seen[-1][1]["sent"] == 600_000  # reported as it goes out

    def refuse(url):
        raise Cancelled("no")
    with http.scope(gate=refuse), pytest.raises(Cancelled):
        http.request("GET", f"{server}/a")


# ---- Wi-Fi only -------------------------------------------------------------------------------------

def monitor(tmp_path, wifi_only=True):
    app = SimpleNamespace(tasks=TaskManager(), job_settings=lambda sid: {"wifi_only": wifi_only})
    return NetworkMonitor(app)


def test_wait_for_wifi(tmp_path):
    net = monitor(tmp_path)
    assert net.on_wifi  # unknown: nothing held back
    net.set_types(["mobile", "vpn"])
    notes = []
    threading.Timer(0.1, net.set_types, args=(["wifi", "vpn"],)).start()
    net.wait_for_wifi(lambda: False, notes.append)
    assert notes == [WAITING, ""]

    net.set_types(["mobile"])
    with pytest.raises(Cancelled):
        net.wait_for_wifi(lambda: True, notes.append)


def test_scope_only_gates_wifi_only_jobs(tmp_path, monkeypatch):
    calls = []
    with http.scope(trace=lambda ev, **f: calls.append(f)):
        off = monitor(tmp_path, wifi_only=False)
        off.set_types(["mobile"])
        with off.scope("j", lambda: True, lambda n: None), pytest.raises(http.HttpError):
            http.send("GET", "http://127.0.0.1:9/")  # not Wi-Fi only: goes out on mobile data (refused here)
        on = monitor(tmp_path)
        on.set_types(["mobile"])
        with on.scope("j", lambda: True, lambda n: None), pytest.raises(Cancelled):
            http.send("GET", "http://127.0.0.1:9/")  # waits for Wi-Fi; cancelled before connecting
    assert len(calls) == 1


# ---- the Drive copy ------------------------------------------------------------------------------------

def sync_app(tmp_path, fake):
    google = SimpleNamespace(connected=True, locked=False, drive=lambda: client(fake))
    return SimpleNamespace(google=google, store=JsonStore(tmp_path / "state.json"), temp_dir=tmp_path / "tmp",
                           tasks=TaskManager())


def test_sync_uploads_new_and_grown_traces(tmp_path):
    fake = FakeDrive()
    store = TraceStore(tmp_path / "diag")
    a = store.new("a")
    a.event("one")
    app = sync_app(tmp_path, fake)
    sync = DiagnosticsSync(app, store)
    assert sync.sync() == 1
    folder = next(i for i, it in fake.items.items() if it["name"] == FOLDER)
    tree = fake.tree(folder)
    summary = a.path.stem + ".summary.txt"
    assert sorted(tree) == sorted([a.path.name, summary]) and b"Task:" in tree[summary]
    assert sync.sync() == 0  # nothing changed

    a.event("two")  # still running: grows
    b = store.new("b")
    b.event("x")
    assert sync.sync() == 2
    tree = fake.tree(folder)
    assert len(tree) == 4 and tree[a.path.name].count(b"\n") == 2  # replaced, not duplicated
    assert not (tmp_path / "tmp" / "diagnostics").exists()

    app.google.connected = False
    b.event("y")
    assert sync.sync() == 0  # not connected: later


def test_sync_survives_errors(tmp_path):
    fake = FakeDrive()
    fake.send = lambda *a, **k: (_ for _ in ()).throw(http.HttpError("Can't reach www.googleapis.com"))
    store = TraceStore(tmp_path / "diag")
    store.new("a").event("x")
    sync = DiagnosticsSync(sync_app(tmp_path, fake), store)
    assert sync.sync() == 0 and "Can't reach" in sync.last_error


# ---- a real extraction, traced, and the report ------------------------------------------------------------

@needs_engine
def test_extract_to_drive_trace_and_report(tmp_path):
    from taskdeck.jobs.extract_archive import logic

    fake = FakeDrive()
    fake.faults = [("POST", "network")] * 3
    tm = TaskManager(start_thread=lambda fn: fn(), traces=TraceStore(tmp_path / "diag"),
                     around=lambda t: http.scope(trace=t.trace))
    drive = gdrive.DriveClient(lambda: "AT", send=fake.send, sleep=lambda s: None, chunk=256 * 1024)
    t = tm.start("Extracting data-encrypted.7z to Google Drive", "extract_archive", lambda t: logic.extract_to_drive(
        t, FIX / "data-encrypted.7z", "Secret1", drive, gdrive.DriveLocation(), "Pics", tmp_path / "tmp"))
    assert t.status == DONE, t.message
    ev = events(t.trace.path)
    kinds = [e["ev"] for e in ev]
    assert kinds[0] == "start" and kinds[-1] == "end"
    assert {"archive", "unpacked", "file", "note"} <= set(kinds)
    assert sum(e["ev"] == "file" for e in ev) == 3
    text = trace_report.report(trace_report.load(t.trace.path))
    assert "Ended: done" in text and "Files uploaded: 3" in text and "Timeline:" in text
    assert trace_report_cli.main([str(t.trace.path), "--minute"]) == 0


def test_report_without_end_and_with_stall(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [{"t": 0, "s": 0, "ev": "start", "title": "X", "job": "j"},
             {"t": 30, "s": 30, "ev": "heartbeat", "done": 10, "total": 100, "moved": 10, "phase": "Uploading"},
             {"t": 60, "s": 60, "ev": "heartbeat", "done": 10, "total": 100, "moved": 0, "phase": "Uploading"},
             {"t": 90, "s": 90, "ev": "heartbeat", "done": 10, "total": 100, "moved": 0, "phase": "Uploading"}]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n{broken")
    text = trace_report.report(trace_report.load(p))
    assert "No end recorded" in text and "STALL" in text and "corrupt" in text


def test_network_change_cuts_open_connections():
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()  # accepts, never answers: like a connection left on the old network
    port = listener.getsockname()[1]
    errors = []
    started = time.monotonic()
    worker = threading.Thread(target=lambda: errors.append(
        pytest.raises(http.HttpError, http.request, "GET", f"http://127.0.0.1:{port}/", timeout=30)))
    worker.start()
    for _ in range(100):
        if http.abort_connections():
            break
        time.sleep(0.02)
    worker.join(5)
    listener.close()
    assert errors and time.monotonic() - started < 5  # failed at once, not after 30 s

    net = monitor(None)
    cut = []
    real = http.abort_connections
    try:
        http.abort_connections = lambda: cut.append(1) or 0
        net.set_types(["mobile"])  # first look: nothing to cut
        net.set_types(["wifi"])
        net.set_types(["wifi"])  # no change
    finally:
        http.abort_connections = real
    assert cut == [1]


def test_stall_note(monkeypatch):
    tm = TaskManager(start_thread=lambda fn: None)
    t = tm.start("x", "j", lambda t: "")
    t.report(10, 100)
    now = time.time()
    t.watch(now + 30)
    assert t.note == ""
    t.watch(now + 125)
    assert t.note == "No progress for 2 minutes. Still trying…"
    t.report(20, 100)  # moving again
    assert t.note == ""
    t.set_note("Paused: no connection.")
    t.watch(time.time() + 300)
    assert t.note == "Paused: no connection."  # an explained pause isn't overwritten


def test_report_counts_skipped_files_and_sync_survives_a_report_error(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    lines = [{"t": 0, "s": 0, "ev": "start", "title": "X", "job": "j"},
             {"t": 1, "s": 1, "ev": "file", "path": "a.png", "bytes": 10, "skipped": True},
             {"t": 2, "s": 2, "ev": "file", "path": "b.png", "bytes": 2000, "secs": 2.5}]
    p.write_text("\n".join(json.dumps(x) for x in lines))
    text = trace_report.report(trace_report.load(p))
    assert "Files skipped (already in Drive): 1" in text and "Files uploaded: 1" in text and "2.5 s each" in text

    fake = FakeDrive()
    store = TraceStore(tmp_path / "diag")
    a = store.new("a")
    a.event("one")
    sync = DiagnosticsSync(sync_app(tmp_path, fake), store)
    monkeypatch.setattr(trace_report, "report", lambda *a, **k: 1 / 0)
    assert sync.sync() == 1
    a.event("two")
    assert sync.sync() == 1
    folder = next(i for i, it in fake.items.items() if it["name"] == FOLDER)
    tree = fake.tree(folder)
    assert len(tree) == 2  # one trace (replaced, not copied) and its summary
    assert b"Couldn't summarize" in tree[a.path.stem + ".summary.txt"]


def test_connections_are_kept_open_and_renewed_when_stale():
    connections = []

    class Handler(http_server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive

        def setup(self):
            super().setup()
            connections.append(1)

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/x?a=1"
    try:
        for _ in range(5):
            assert http.send("GET", url).body == b"ok"
        assert len(connections) == 1  # one handshake for all five
        http.abort_connections()  # e.g. the server dropped the idle connection
        assert http.send("GET", url).status == 200  # renewed transparently
        assert len(connections) == 2
    finally:
        srv.shutdown()


def test_listeners_run_off_the_work_thread():
    tm = TaskManager(start_thread=lambda fn: threading.Thread(target=fn).start())
    tm.notify_in_background = True
    seen, work_thread = [], []

    def slow_listener(task):
        seen.append((threading.get_ident(), task.status, task.done))
        time.sleep(0.05)  # e.g. a slow screen redraw
    tm.listeners.append(slow_listener)

    def work(t):
        work_thread.append(threading.get_ident())
        started = time.monotonic()
        for i in range(100):
            t.report(i, 100)
        assert time.monotonic() - started < 0.5  # not held up by the listener
        return "ok"
    t = tm.start("x", "j", work)
    for _ in range(200):
        if seen and seen[-1][1] == DONE:
            break
        time.sleep(0.02)
    assert seen[-1][1] == DONE  # the end is always delivered
    assert work_thread[0] not in {ident for ident, _, _ in seen}
    assert len(seen) < 100  # coalesced


def test_request_timing_breakdown(server):
    seen = []
    with http.scope(trace=lambda ev, **f: seen.append(f)):
        http.send("GET", f"{server}/a")
        http.send("PUT", f"{server}/up", gdrive.Piece(b"z" * 300_000, lambda n: time.sleep(0.01)),
                  {"Content-Length": "300000"})
    first, second = seen
    assert first["reused"] is False and "connect" in first and {"send", "wait", "read"} <= set(first)
    assert second["callbacks"] >= 0.01
    lines = [{"t": 0, "s": 0, "ev": "start"}] + [{"t": i, "s": i, "ev": "http", **f} for i, f in enumerate(seen, 1)]
    text = trace_report.report(lines)
    assert "Where a request's time goes" in text and "2 new connections, 0 reused" in text  # HTTP/1.0 test server

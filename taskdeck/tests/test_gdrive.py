"""Google sign-in (device flow), the Drive client, and extracting to Google Drive — against fakes."""

import json
import re
import urllib.parse
from pathlib import Path

import pytest

from taskdeck.core import JsonStore, TaskManager
from taskdeck.core.tasks import CANCELLED, DONE, FAILED
from taskdeck.shared import archive, gdrive, google_oauth, http
from taskdeck.shared.gdrive import DriveLocation

FIX = Path(__file__).parent / "fixtures" / "archives"
CONTENT = {"docs/readme.txt": b"hello archive\n", "docs/notes/a.txt": b"note a\n", "big.txt": b"x" * 3000}
needs_engine = pytest.mark.skipif(not archive.available(), reason="7-Zip engine not built (set TASKDECK_7Z_DIR)")


def resp(status, body=None, headers=None):
    raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    return http.Response(status, headers or {}, raw)


# ---- sign-in ----------------------------------------------------------------------------------

def id_token(email):
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def test_device_flow(monkeypatch):
    answers = [
        resp(200, {"device_code": "DEV", "user_code": "BQKD-WXTL", "verification_url": "https://www.google.com/device",
                   "expires_in": 1800, "interval": 5}),
        resp(428, {"error": "authorization_pending"}),
        resp(403, {"error": "slow_down"}),
        resp(200, {"access_token": "AT", "expires_in": 3600, "refresh_token": "RT", "id_token": id_token("jim@x.com")}),
    ]
    sent, slept = [], []
    monkeypatch.setattr(http, "send", lambda m, url, body, headers, timeout: (sent.append(
        (url, urllib.parse.parse_qs(body.decode()))), answers.pop(0))[1])
    code = google_oauth.start("CID")
    assert code.user_code == "BQKD-WXTL" and code.interval == 5
    assert sent[0][1]["scope"] == ["openid email https://www.googleapis.com/auth/drive.file"]
    tokens = google_oauth.wait("CID", "SECRET", code, sleep=slept.append)
    assert (tokens.refresh_token, tokens.email) == ("RT", "jim@x.com")
    assert slept == [5, 10]  # slow_down adds 5 s
    assert sent[-1][1]["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]


@pytest.mark.parametrize("error,text", [("access_denied", "declined"), ("invalid_client", "Client ID"),
                                        ("invalid_scope", "invalid_scope")])
def test_device_flow_errors(monkeypatch, error, text):
    code = google_oauth.DeviceCode("DEV", "X", "u", 9e18, 0)
    monkeypatch.setattr(http, "send", lambda *a: resp(400, {"error": error}))
    with pytest.raises(google_oauth.OAuthError, match=text):
        google_oauth.wait("CID", "S", code, sleep=lambda s: None)


def test_device_flow_cancel(monkeypatch):
    code = google_oauth.DeviceCode("DEV", "X", "u", 9e18, 0)
    assert google_oauth.wait("CID", "S", code, cancelled=lambda: True) is None


def test_token_source_refreshes_once(monkeypatch):
    calls = []
    monkeypatch.setattr(http, "send", lambda m, url, body, h, t: (calls.append(body), resp(200, {"access_token": "AT",
                                                                                               "expires_in": 3600}))[1])
    src = google_oauth.TokenSource("CID", "S", "RT")
    assert src.token() == src.token() == "AT" and len(calls) == 1
    src.invalidate()
    src.token()
    assert len(calls) == 2


# ---- a fake Google Drive ----------------------------------------------------------------------

class FakeDrive:
    """Just enough of Drive v3 + resumable uploads, with fault injection."""

    def __init__(self):
        self.items = {}  # id -> {name, parent, folder, data}
        self.sessions = {}
        self.faults = []  # (method, "network" | status) applied to the next matching request
        self.next_id = 0
        self.puts = 0

    def _new_id(self):
        self.next_id += 1
        return f"id{self.next_id}"

    def add_folder(self, name, parent="root"):
        fid = self._new_id()
        self.items[fid] = {"name": name, "parent": parent, "folder": True, "data": b""}
        return fid

    def tree(self, root):
        """{path: bytes} of files under root, like files_under() on disk."""
        out = {}

        def walk(fid, prefix):
            for cid, item in self.items.items():
                if item["parent"] == fid:
                    path = f"{prefix}{item['name']}"
                    if item["folder"]:
                        walk(cid, path + "/")
                    else:
                        out[path] = item["data"]
        walk(root, "")
        return out

    def send(self, method, url, body=None, headers=None, timeout=60):
        for i, (m, fault) in enumerate(self.faults):
            if m == method:
                del self.faults[i]
                if fault == "network":
                    raise http.HttpError("Can't reach www.googleapis.com: connection reset")
                return resp(fault, {"error": {"message": "fault", "errors": [{"reason": "backendError"}]}})
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        if u.netloc == "upload.test":  # an upload session
            return self._put(u.path.rsplit("/", 1)[1], body, headers)
        if method == "GET" and u.path.endswith("/files"):
            parent = re.search(r"'([^']+)' in parents", q["q"][0]).group(1)
            only_folders = "mimeType='application/vnd.google-apps.folder'" in q["q"][0]
            files = [{"id": i, "name": it["name"], "mimeType": gdrive.FOLDER_MIME if it["folder"] else "text/plain"}
                     for i, it in self.items.items() if it["parent"] == parent and (it["folder"] or not only_folders)]
            return resp(200, {"files": sorted(files, key=lambda f: f["name"])})
        if method == "POST" and u.path.endswith("/drive/v3/files") and "uploadType" not in q:
            meta = json.loads(body)
            if meta["parents"][0] != "root" and meta["parents"][0] not in self.items:
                return resp(404, {"error": {"message": "File not found"}})
            return resp(200, {"id": self.add_folder(meta["name"], meta["parents"][0]), "name": meta["name"]})
        if "uploadType" in q:
            sid = f"s{len(self.sessions) + 1}"
            meta = json.loads(body or b"{}")
            replace = u.path.rsplit("/", 1)[1] if method == "PATCH" else None
            self.sessions[sid] = {"meta": meta, "replace": replace, "data": bytearray(),
                                  "size": int(headers["X-Upload-Content-Length"])}
            return resp(200, headers={"location": f"https://upload.test/session/{sid}"})
        return resp(400, {"error": {"message": f"unexpected {method} {url}"}})

    def _put(self, sid, body, headers):
        self.puts += 1
        s = self.sessions[sid]
        rng = headers["Content-Range"]
        if not rng.startswith("bytes */") or s["size"] == 0:
            if not rng.startswith("bytes */"):
                start = int(rng.split()[1].split("-")[0])
                assert start == len(s["data"]), "uploads must continue where Google says"
                s["data"] += body
        if len(s["data"]) >= s["size"]:
            if s["replace"]:
                self.items[s["replace"]]["data"] = bytes(s["data"])
                return resp(200, {"id": s["replace"]})
            fid = self._new_id()
            self.items[fid] = {"name": s["meta"]["name"], "parent": s["meta"]["parents"][0], "folder": False,
                               "data": bytes(s["data"])}
            return resp(201, {"id": fid})
        headers = {"range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
        return resp(308, headers=headers)


def client(fake, chunk=256 * 1024):
    return gdrive.DriveClient(lambda: "AT", send=fake.send, sleep=lambda s: None, chunk=chunk)


# ---- Drive client ----------------------------------------------------------------------------------

def test_folders(tmp_path):
    fake = FakeDrive()
    d = client(fake)
    a = d.create_folder("Archives")
    d.create_folder("Old", a.id)
    assert [f.name for f in d.list_folders()] == ["Archives"]
    assert [f.name for f in d.list_folders(a.id)] == ["Old"]
    assert d.children(a.id) == ({"Old": "id2"}, {})


def test_upload_in_pieces_with_progress(tmp_path):
    fake = FakeDrive()
    data = bytes(range(256)) * 4096  # 1 MiB -> 4 pieces of 256 KiB
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    seen = []
    fid = client(fake).upload(f, "root", progress=lambda n: seen.append(n) and False)
    assert fake.items[fid]["data"] == data and fake.puts == 4
    assert seen == [262144, 524288, 786432, 1048576]


def test_upload_resumes_after_network_drop_and_server_error(tmp_path):
    fake = FakeDrive()
    fake.faults = [("PUT", "network"), ("PUT", 503)]
    f = tmp_path / "big.bin"
    f.write_bytes(b"y" * 600_000)
    fid = client(fake).upload(f, "root")
    assert fake.items[fid]["data"] == b"y" * 600_000


def test_upload_replace_and_empty_file(tmp_path):
    fake = FakeDrive()
    d = client(fake)
    f = tmp_path / "a.txt"
    f.write_bytes(b"one")
    fid = d.upload(f, "root")
    f.write_bytes(b"two!")
    assert d.upload(f, "root", replace=fid) == fid and fake.items[fid]["data"] == b"two!"
    e = tmp_path / "empty"
    e.write_bytes(b"")
    assert fake.items[d.upload(e, "root")]["data"] == b""


def test_upload_cancel_and_errors(tmp_path):
    fake = FakeDrive()
    f = tmp_path / "big.bin"
    f.write_bytes(b"z" * 600_000)
    with pytest.raises(gdrive.UploadCancelled):
        client(fake).upload(f, "root", progress=lambda n: True)
    fake.faults = [("POST", 401)]
    with pytest.raises(gdrive.DriveError, match="sign-in expired"):
        client(fake).upload(f, "root")
    full = FakeDrive()
    full.send = lambda *a, **k: resp(403, {"error": {"message": "The user's Drive storage quota has been exceeded.",
                                                      "errors": [{"reason": "storageQuotaExceeded"}]}})
    with pytest.raises(gdrive.DriveError, match="Drive is full"):
        client(full).create_folder("x")


def test_upload_waits_through_long_network_loss(tmp_path):
    """Switching apps cuts the network: with keep_waiting the upload pauses, then carries on."""
    fake = FakeDrive()
    fake.faults = [("PUT", "network")] * 40  # the piece and many status checks fail
    f = tmp_path / "big.bin"
    f.write_bytes(b"w" * 600_000)
    states, sleeps = [], []
    d = gdrive.DriveClient(lambda: "AT", send=fake.send, sleep=sleeps.append, chunk=256 * 1024,
                           keep_waiting=lambda: True, on_offline=states.append)
    fid = d.upload(f, "root")
    assert fake.items[fid]["data"] == b"w" * 600_000
    assert states == [True, False] and max(sleeps) == gdrive.OFFLINE_RETRY_MAX
    # without it, the old behaviour: give up after a few tries
    fake.faults = [("PUT", "network")] * 40
    with pytest.raises(gdrive.DriveError, match="Lost the connection"):
        client(fake).upload(f, "root")


def test_waiting_offline_stops_on_cancel_and_covers_api_calls(tmp_path):
    fake = FakeDrive()
    fake.faults = [("POST", "network")] * 10
    states, cancel = [], []
    d = gdrive.DriveClient(lambda: "AT", send=fake.send, sleep=lambda s: cancel.append(1),
                           keep_waiting=lambda: len(cancel) < 3, on_offline=states.append)
    with pytest.raises(gdrive.UploadCancelled):
        d.create_folder("x")
    assert states == [True]

    def token():  # refreshing the sign-in needs the network too
        if fake.faults:
            fake.faults.pop()
            raise http.HttpError("Can't reach oauth2.googleapis.com")
        return "AT"
    fake.faults = [("x", "network")] * 3
    d = gdrive.DriveClient(token, send=fake.send, sleep=lambda s: None, keep_waiting=lambda: True)
    assert d.create_folder("y").name == "y"


# ---- extracting to Drive ------------------------------------------------------------------------------

def run_inline():
    return TaskManager(start_thread=lambda fn: fn())


@needs_engine
def test_extract_to_drive(tmp_path):
    from taskdeck.jobs.extract_archive import logic

    fake = FakeDrive()
    old = fake.add_folder("Backups")
    tm = run_inline()
    t = tm.start("x", "extract_archive", lambda t: logic.extract_to_drive(
        t, FIX / "data-encrypted.7z", "Secret1", client(fake), DriveLocation(old, ("My Drive", "Backups")),
        "Pics", tmp_path / "tmp"))
    assert t.status == DONE, t.message
    assert t.message == "Uploaded 3 files (3.0 KB) to Google Drive › My Drive › Backups › Pics"
    pics = next(i for i, it in fake.items.items() if it["name"] == "Pics")
    assert fake.tree(pics) == CONTENT and t.result.folder_id == pics
    assert [(p.label, p.complete) for p in t.phases] == [("Unpacking", True), ("Uploading to Google Drive", True)]
    assert not any((tmp_path / "tmp").iterdir())  # temporary copy removed

    # again into the same folder: files replaced, folders reused (no duplicates)
    count = len(fake.items)
    t2 = tm.start("x", "extract_archive", lambda t: logic.extract_to_drive(
        t, FIX / "plain.7z", None, client(fake), DriveLocation(old, ("My Drive", "Backups")), "Pics", tmp_path / "tmp"))
    assert t2.status == DONE and len(fake.items) == count


@needs_engine
def test_extract_to_drive_cancel_and_failure(tmp_path):
    from taskdeck.jobs.extract_archive import logic

    fake = FakeDrive()
    tm = run_inline()

    def cancel_after_first_file(task):
        drive = client(fake)
        real = drive.upload

        def upload(*a, **k):
            fid = real(*a, **k)
            task.cancel_requested = True
            return fid
        drive.upload = upload
        return logic.extract_to_drive(task, FIX / "plain.7z", None, drive, DriveLocation(), "P", tmp_path / "tmp")

    t = tm.start("x", "j", cancel_after_first_file)
    assert t.status == CANCELLED and t.message == "Stopped. 1 file was uploaded to Google Drive › My Drive › P."

    paused = FakeDrive()
    paused.faults = [("PUT", "network")] * 6
    notes = []
    tm.listeners.append(lambda task: notes.append(task.note))
    t = tm.start("x", "j", lambda t: logic.extract_to_drive(t, FIX / "plain.7z", None, client(paused),
                                                           DriveLocation(), "Q", tmp_path / "tmp"))
    assert t.status == DONE and logic.PAUSED in notes and t.note == ""
    tm.listeners.clear()

    broken = FakeDrive()
    broken.faults = [("POST", 404)]  # the chosen folder disappeared
    t = tm.start("x", "j", lambda t: logic.extract_to_drive(t, FIX / "plain.7z", None, client(broken),
                                                           DriveLocation("gone", ("My Drive", "gone")), None,
                                                           tmp_path / "tmp"))
    assert t.status == FAILED and "not found" in t.message
    assert not any((tmp_path / "tmp").iterdir())


@needs_engine
def test_drive_problems(tmp_path):
    from taskdeck.jobs.extract_archive import logic

    s = logic.scan(FIX / "plain.7z")
    assert logic.problems(s, DriveLocation(), "x", "", drive_connected=False, temp_root=tmp_path) == [
        "Connect Google Drive first (Settings → Accounts)."]
    assert logic.problems(s, DriveLocation(), "x", "", drive_connected=True, temp_root=tmp_path) == []


# ---- the account in the app --------------------------------------------------------------------------

def make_app(tmp_path, backend=None):
    from taskdeck.ui.app import TaskDeckApp
    from test_extract_archive import FakePage

    store = JsonStore(tmp_path / "state.json")
    if backend is not None:
        store.set("vault:enabled", True)
    app = TaskDeckApp(FakePage(), store=store, vault_backend=backend)
    app.toasts = []
    app.toast = app.toasts.append
    return app


def test_account_settings_and_secrets(tmp_path):
    from test_vault import FakeKeystore

    import asyncio

    ks = FakeKeystore()
    app = make_app(tmp_path, ks)
    asyncio.run(app.unlock())
    app.google.save(client_id="cid.apps.googleusercontent.com", client_secret="S", refresh_token="RT", email="j@x")
    assert app.google.configured and app.google.connected and app.google.email == "j@x"
    assert ks.data == {"settings:account:google:client_secret": "S", "settings:account:google:refresh_token": "RT"}
    assert "RT" not in (tmp_path / "state.json").read_text()
    app.lock_credentials()
    assert app.google.locked and not app.google.connected


@needs_engine
def test_screen_extracts_to_drive(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    app.tasks._start = lambda fn: fn()
    fake = FakeDrive()
    app.google.save(client_id="c.apps.googleusercontent.com", client_secret="S", refresh_token="RT", email="j@x")
    monkeypatch.setattr(type(app.google), "drive", lambda self: client(fake))
    app.open_job("extract_archive")
    screen = app.stack[-1]
    screen.set_archive(FIX / "plain.7z")
    screen.set_destination(DriveLocation(), "plain")
    assert screen.destination_text() == "Google Drive › My Drive › plain/"
    assert screen.problems() == [] and not screen.extract_button.disabled
    screen.extract()
    assert screen.task.status == DONE, screen.task.message
    plain = next(i for i, it in fake.items.items() if it["name"] == "plain")
    assert fake.tree(plain) == CONTENT


def test_device_flow_survives_network_loss(monkeypatch):
    """Approving happens in a browser; meanwhile TaskDeck's network can drop (DNS: Errno 7)."""
    code = google_oauth.DeviceCode("DEV", "X", "u", 9e18, 0)
    answers = [http.HttpError("Can't reach oauth2.googleapis.com: [Errno 7] No address associated with hostname"),
               resp(503, b"unavailable"),
               resp(428, {"error": "authorization_pending"}),
               resp(200, {"access_token": "AT", "refresh_token": "RT", "id_token": id_token("j@x")})]

    def send(*a):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(http, "send", send)
    offline = []
    tokens = google_oauth.wait("CID", "S", code, sleep=lambda s: None, on_offline=offline.append)
    assert tokens.refresh_token == "RT" and offline == [True, True, False, False]

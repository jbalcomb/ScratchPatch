import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget  # noqa: E402
import web  # noqa: E402
from test_budget import EXPORT, args_for, rows  # noqa: E402


def serve(drop):
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(drop))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def post(url, data):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, method="POST")) as r:
            return r.status, json.load(r)["message"]
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)["message"]


def test_page_lists_accounts(tmp_path):
    server, base = serve(tmp_path / "drop")
    try:
        html = urllib.request.urlopen(base + "/").read().decode()
    finally:
        server.shutdown()
    assert all(f'value="{a}"' in html for a in web.ACCOUNTS)
    assert json.dumps(web.ACCOUNT_HINTS) in html  # file-name check that warns on a mismatched account
    assert set(web.ACCOUNT_HINTS) == set(web.ACCOUNTS)


def test_upload_lands_in_account_folder_and_watcher_ingests_it(tmp_path):
    drop = tmp_path / "drop"
    server, base = serve(drop)
    try:
        status, msg = post(base + "/upload?account=bills&name=../../evil%20name.csv", EXPORT.encode())
        status2, _ = post(base + "/upload?account=checking&name=daily.csv", EXPORT.encode())
    finally:
        server.shutdown()
    assert (status, status2) == (200, 200)
    assert "4 transactions" in msg
    saved = list((drop / "bills").glob("*.csv"))
    assert len(saved) == 1 and saved[0].name.endswith("-evil_name.csv")
    assert len(list(drop.glob("*.csv"))) == 1

    budget.cmd_watch(args_for(tmp_path))
    assert sorted({r[5] for r in rows(tmp_path / "b.db")}) == ["bills", "checking"]


def test_bad_uploads_are_refused_and_not_saved(tmp_path):
    drop = tmp_path / "drop"
    server, base = serve(drop)
    try:
        bad_file = post(base + "/upload?account=checking&name=x.csv", b"Posted Date,Payee\n1,2\n")
        bad_account = post(base + "/upload?account=../etc&name=x.csv", EXPORT.encode())
    finally:
        server.shutdown()
    assert bad_file[0] == 400 and "header" in bad_file[1]
    assert bad_account[0] == 400
    assert not any(p.is_file() for p in drop.rglob("*"))

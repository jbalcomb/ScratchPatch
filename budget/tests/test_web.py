import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web  # noqa: E402
from test_budget import EXPORT, OTHER, args_for, rows  # noqa: E402


def serve(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(args_for(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def post(url, data):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, method="POST")) as r:
            return r.status, json.load(r)["message"]
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)["message"]


def test_page_lists_accounts(tmp_path):
    server, base = serve(tmp_path)
    try:
        html = urllib.request.urlopen(base + "/").read().decode()
    finally:
        server.shutdown()
    assert all(f'value="{a}"' in html for a in web.ACCOUNTS)
    assert json.dumps(web.ACCOUNT_HINTS) in html  # file-name check that warns on a mismatched account
    assert set(web.ACCOUNT_HINTS) == set(web.ACCOUNTS)


def test_upload_is_stored_and_archived_right_away(tmp_path):
    server, base = serve(tmp_path)
    try:
        status, msg = post(base + "/upload?account=bills&name=../../evil%20name.csv", EXPORT.encode())
        status2, _ = post(base + "/upload?account=checking&name=daily.csv", OTHER.encode())
    finally:
        server.shutdown()
    assert (status, status2) == (200, 200)
    assert msg == "stored as upload #1: 4 rows, 4 new (0 already stored) -> bills"
    saved = list((tmp_path / "archive" / "bills").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("-evil_name.csv")
    assert sorted({r[5] for r in rows(tmp_path / "b.db")}) == ["bills", "checking"]


def test_mismatched_balance_needs_force(tmp_path):
    server, base = serve(tmp_path)
    try:
        post(base + "/upload?account=checking&name=daily.csv", EXPORT.encode())
        refused = post(base + "/upload?account=checking&name=save.csv", OTHER.encode())
        forced = post(base + "/upload?account=checking&name=save.csv&force=1", OTHER.encode())
    finally:
        server.shutdown()
    assert refused[0] == 409 and "doesn't look like checking" in refused[1]
    assert forced[0] == 200
    assert len(rows(tmp_path / "b.db")) == 5


def test_bad_uploads_are_refused_and_not_saved(tmp_path):
    server, base = serve(tmp_path)
    try:
        bad_file = post(base + "/upload?account=checking&name=x.csv", b"Posted Date,Payee\n1,2\n")
        bad_account = post(base + "/upload?account=../etc&name=x.csv", EXPORT.encode())
    finally:
        server.shutdown()
    assert bad_file[0] == 400 and "header" in bad_file[1]
    assert bad_account[0] == 400
    assert not (tmp_path / "archive").exists()


def test_page_script_parses(tmp_path):
    """A syntax error in the page's script silently disables uploading; catch it here (needs node)."""
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not installed")
    server, base = serve(tmp_path)
    try:
        html = urllib.request.urlopen(base + "/").read().decode()
    finally:
        server.shutdown()
    script = tmp_path / "page.js"
    script.write_text(re.search(r"<script>(.*)</script>", html, re.S).group(1))
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

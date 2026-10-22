"""Upload page for the budget drop folder, plus the watcher, in one process.

    python budget/web.py                 # http://<this machine>:8090
    python budget/web.py --port 8091

An upload is checked (it must parse as a Bank of America export), then saved into the drop folder,
where the watcher picks it up within a few seconds. No login: keep it on the home network / Tailscale.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import budget  # noqa: E402

MAX_BYTES = 10 * 1024 * 1024
ACCOUNTS = ["checking", "bills", "savings"]

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Budget Upload</title>
<style>
  :root { --bg: #f6f6f4; --fg: #1d1d1b; --muted: #6b6b66; --line: #cfcfc8; --accent: #2f6f4f; --bad: #a33; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #161615; --fg: #ececea; --muted: #9a9a94; --line: #3a3a37; --accent: #6fbf8f; --bad: #e77; }
  }
  body { background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 24px 16px; }
  main { max-width: 520px; margin: 0 auto; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  p { color: var(--muted); margin: 0 0 20px; }
  label { display: block; font-weight: 600; margin-bottom: 6px; }
  select { font: inherit; padding: 8px; width: 100%; margin-bottom: 16px; background: var(--bg); color: var(--fg);
           border: 1px solid var(--line); border-radius: 6px; }
  #drop { border: 2px dashed var(--line); border-radius: 10px; padding: 36px 16px; text-align: center; cursor: pointer; }
  #drop.over { border-color: var(--accent); }
  #log { margin-top: 20px; padding: 0; list-style: none; }
  #log li { padding: 8px 0; border-top: 1px solid var(--line); }
  .ok { color: var(--accent); } .bad { color: var(--bad); }
</style></head>
<body><main>
  <h1>Budget upload</h1>
  <p>Bank of America: Download transactions &rarr; Microsoft Excel format. Re-uploading overlapping dates is fine.</p>
  <label for="account">Account</label>
  <select id="account">__OPTIONS__</select>
  <div id="drop">Drop CSV files here, or tap to choose
    <input id="file" type="file" accept=".csv,text/csv" multiple hidden></div>
  <ul id="log"></ul>
</main>
<script>
const drop = document.getElementById('drop'), input = document.getElementById('file');
const log = document.getElementById('log'), account = document.getElementById('account');
try { account.value = localStorage.getItem('account') || account.value; } catch (e) {}
account.onchange = () => { try { localStorage.setItem('account', account.value); } catch (e) {} };
drop.onclick = () => input.click();
input.onchange = () => send(input.files);
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); send(e.dataTransfer.files); };
async function send(files) {
  for (const f of files) {
    const li = document.createElement('li');
    li.textContent = f.name + ': uploading...';
    log.prepend(li);
    try {
      const r = await fetch('upload?account=' + encodeURIComponent(account.value) +
                            '&name=' + encodeURIComponent(f.name), { method: 'POST', body: f });
      const j = await r.json();
      li.className = r.ok ? 'ok' : 'bad';
      li.textContent = f.name + ': ' + j.message;
    } catch (e) {
      li.className = 'bad';
      li.textContent = f.name + ': ' + e;
    }
  }
  input.value = '';
}
</script></body></html>
"""


def make_handler(drop):
    drop = Path(drop)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def message(self, status, text):
            self.reply(status, json.dumps({"message": text}))

        def do_GET(self):
            if urlparse(self.path).path != "/":
                return self.reply(404, "not found", "text/plain")
            options = "".join(f'<option value="{a}">{a}</option>' for a in ACCOUNTS)
            self.reply(200, PAGE.replace("__OPTIONS__", options), "text/html; charset=utf-8")

        def do_POST(self):
            url = urlparse(self.path)
            if url.path != "/upload":
                return self.message(404, "not found")
            q = parse_qs(url.query)
            account = q.get("account", [""])[0]
            if account not in ACCOUNTS:
                return self.message(400, f"unknown account {account!r}")
            size = int(self.headers.get("Content-Length") or 0)
            if not 0 < size <= MAX_BYTES:
                return self.message(400, "empty file, or larger than 10 MB")
            data = self.rfile.read(size)

            name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(q.get("name", ["upload"])[0]).stem)[:60]
            folder = drop if account == budget.DEFAULT_ACCOUNT else drop / account
            folder.mkdir(parents=True, exist_ok=True)
            # Written as .tmp and renamed, so the watcher (which only looks at *.csv) never sees half a file.
            fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=folder)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                rows = budget.parse_bofa_csv(tmp)
            except Exception as e:
                os.unlink(tmp)
                return self.message(400, f"not saved: {e}".replace(tmp, "file"))
            dest = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.csv"
            os.replace(tmp, dest)
            print(f"upload: {dest.name} ({len(rows)} rows) -> {account}", flush=True)
            self.message(200, f"saved, {len(rows)} transactions; new ones are added to {account} within seconds")

        def log_message(self, *a):
            pass

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="Budget upload page + drop folder watcher")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("BUDGET_PORT", 8090)))
    p.add_argument("--no-watch", action="store_true", help="serve the page only (another process watches)")
    args, rest = p.parse_known_args(argv)
    drop = os.environ.get("BUDGET_DROP", budget.DEFAULT_DROP)

    if not args.no_watch:
        def watch():
            try:
                budget.main(rest + ["watch", "--folder", str(drop)])
            except BaseException:
                traceback.print_exc()
            finally:  # never keep taking uploads that nothing will process; systemd restarts us
                print("watcher stopped; exiting", file=sys.stderr, flush=True)
                os._exit(1)
        threading.Thread(target=watch, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(drop))
    print(f"upload page on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

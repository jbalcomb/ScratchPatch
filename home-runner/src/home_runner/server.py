"""HTTP API. Every endpoint except /health needs `Authorization: Bearer <key>`.

  GET  /health               {"ok": true}, for "is it up?" checks
  GET  /jobs                 the jobs that can be run, with their parameters
  GET  /runs?limit=20        recent runs, newest first (add &all=1 to include quick lookups)
  POST /runs                 {"job": "...", "params": {...}}: 202 queued, or 200 with a quick job's output
  GET  /runs/<id>?tail=40    one run, with the end of its log
  POST /runs/<id>/cancel     cancel a queued run or stop a running one
"""

import hmac
import json
import logging
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .registry import BadRequest

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
RUN = re.compile(r"/runs/(\d+)")
RUN_CANCEL = re.compile(r"/runs/(\d+)/cancel")


def make_server(runner, key, host="127.0.0.1", port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "home-runner/0.1"

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            if method == "GET" and path == "/health":
                return self._send(200, {"ok": True, "service": "home-runner"})
            if not self._authorized():
                time.sleep(1)  # makes guessing the key slow
                return self._send(401, {"error": "missing or wrong key"})
            try:
                if method == "GET" and path == "/jobs":
                    return self._send(200, [job.describe() for job in runner.jobs.values()])
                if method == "GET" and path == "/runs":
                    return self._send(200, runner.list(_int(query, "limit", 20, 200), "all" in query))
                if method == "POST" and path == "/runs":
                    body = self._body()
                    name = body.get("job")
                    if name not in runner.jobs:
                        return self._send(404, {"error": f"no job named {name!r}"})
                    run = runner.submit(name, body.get("params") or {})
                    return self._send(202 if run["status"] == "queued" else 200, run)
                if match := RUN_CANCEL.fullmatch(path):
                    if method == "POST":
                        return self._found(runner.cancel(int(match[1])))
                elif match := RUN.fullmatch(path):
                    if method == "GET":
                        return self._found(runner.get(int(match[1]), _int(query, "tail", 40, 500)))
                return self._send(404, {"error": f"no endpoint {method} {path}"})
            except BadRequest as error:
                return self._send(400, {"error": str(error)})
            except Exception:
                log.exception("%s %s failed", method, path)
                return self._send(500, {"error": "internal error; see the runner's log"})

        def _authorized(self):
            header = self.headers.get("Authorization", "")
            given = header[7:] if header.startswith("Bearer ") else ""
            return hmac.compare_digest(given.encode(), key.encode())

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise BadRequest("request too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise BadRequest("body must be JSON") from None
            if not isinstance(body, dict):
                raise BadRequest("body must be a JSON object")
            return body

        def _found(self, run):
            return self._send(200, run) if run else self._send(404, {"error": "no such run"})

        def _send(self, status, data):
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

    return ThreadingHTTPServer((host, port), Handler)


def _int(query, name, default, ceiling):
    try:
        return max(0, min(ceiling, int(query.get(name, [default])[0])))
    except ValueError:
        return default

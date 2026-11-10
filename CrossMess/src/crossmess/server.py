"""Gateway HTTP API (Phase A). Put `tailscale serve` in front; it listens on localhost.

  GET  /health          {"ok": true, "ruleset": ...}, no key needed
  GET  /gateway-key     the public key agents verify envelopes with, and the ruleset, no key needed
  POST /messages        `Authorization: Bearer <sender secret>`, body = a signed submission
                        202 admitted and published · 200 duplicate id, not published again
                        400 invalid · 401 unknown sender · 413 too large · 422 content rejected
                        429 rate limited · 502 broker unreachable (safe to retry)

Every outcome is appended to audit.jsonl without the message body: clipboard text often holds
things that should not sit in a log.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from crossmess.envelope import Invalid, parse_time
from crossmess.gateway import admit
from crossmess.ntfy import PublishFailed, Publisher, envelope_bytes
from crossmess.screen import Classifier, Rejected, Ruleset
from crossmess.store import Senders, public_key_text

log = logging.getLogger(__name__)

MAX_BODY = 2 * 1024 * 1024


class RateLimit:
    """A token bucket per sender: `burst` messages at once, refilled at burst per `period_s`."""

    def __init__(self, burst: int = 30, period_s: float = 60, clock=time.monotonic):
        self.burst, self.rate, self.clock = burst, burst / period_s, clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, sender: str) -> bool:
        now = self.clock()
        tokens, then = self._buckets.get(sender, (self.burst, now))
        tokens = min(self.burst, tokens + (now - then) * self.rate)
        if tokens < 1:
            self._buckets[sender] = (tokens, now)
            return False
        self._buckets[sender] = (tokens - 1, now)
        return True


class Service:
    def __init__(
        self,
        rules: Ruleset,
        gw_key: Ed25519PrivateKey,
        senders: Senders,
        publish: Publisher,
        data_dir: Path,
        *,
        rate: RateLimit | None = None,
        max_envelope: int = 4096,
        classifier: Classifier | None = None,
    ):
        self.rules, self.gw_key, self.senders, self.publish = rules, gw_key, senders, publish
        self.rate = rate or RateLimit()
        self.max_envelope = max_envelope
        self.classifier = classifier
        self.audit_path = data_dir / "audit.jsonl"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # (sender, id) -> (expires, response). Agents drop replays too; this only spares the broker.
        self._done: dict[tuple[str, str], tuple[datetime, dict]] = {}

    def describe(self) -> dict:
        return {
            "public_key": public_key_text(self.gw_key),
            "ruleset": self.rules.digest,
            "ruleset_version": self.rules.version,
        }

    def submit(self, bearer: str, submission: dict) -> tuple[int, dict]:
        token = self.senders.token_for(bearer) if bearer else None
        if token is None:
            return 401, {"error": "unknown sender"}
        now = datetime.now(timezone.utc)
        entry = {"sender": token.sender, "channel": submission.get("channel"), "id": submission.get("id")}

        with self._lock:
            allowed = self.rate.allow(token.sender)
            self._done = {k: v for k, v in self._done.items() if v[0] > now}
            earlier = self._done.get((token.sender, str(submission.get("id"))))
        if not allowed:
            return self._audit(429, {"error": "rate limited; slow down"}, entry, "rate_limited")
        if earlier:
            return 200, {**earlier[1], "duplicate": True}

        try:
            env = admit(submission, token, self.rules, self.gw_key, now=now, classifier=self.classifier)
        except Invalid as error:
            return self._audit(400, {"error": str(error)}, entry, "invalid")
        except Rejected as error:
            return self._audit(422, {"error": str(error)}, entry, "rejected")

        size = len(envelope_bytes(env))
        if size > self.max_envelope:
            error = f"envelope is {size} bytes; the broker takes {self.max_envelope}"
            return self._audit(413, {"error": error}, entry, "too_large")
        try:
            self.publish(env)
        except PublishFailed as error:
            log.warning("%s", error)
            return self._audit(502, {"error": "broker unreachable; retry"}, entry, "publish_failed")

        response = {"id": env.id, "channel": env.channel, "labels": list(env.labels)}
        with self._lock:
            self._done[(token.sender, env.id)] = (parse_time(env.expires, "expires"), response)
        entry |= {"labels": list(env.labels), "orig_sha256": env.orig_sha256}
        return self._audit(202, response, entry, "admitted")

    def _audit(self, status: int, response: dict, entry: dict, result: str) -> tuple[int, dict]:
        line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "result": result, **entry}
        if "error" in response:
            line["reason"] = response["error"]
        with self._lock, self.audit_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return status, response


def make_server(service: Service, host: str = "127.0.0.1", port: int = 8766, auth_delay: float = 1.0):
    class Handler(BaseHTTPRequestHandler):
        server_version = "courier-gateway/0.1"

        def do_GET(self):
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/health":
                return self._send(200, {"ok": True, "service": "courier-gateway", "ruleset": service.rules.digest})
            if path == "/gateway-key":
                return self._send(200, service.describe())
            return self._send(404, {"error": f"no endpoint GET {path}"})

        def do_POST(self):
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path != "/messages":
                return self._send(404, {"error": f"no endpoint POST {path}"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                return self._send(400, {"error": "body must be JSON"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "body must be a JSON object"})
            header = self.headers.get("Authorization", "")
            try:
                status, response = service.submit(header[7:] if header.startswith("Bearer ") else "", body)
            except Exception:
                log.exception("POST /messages failed")
                return self._send(500, {"error": "internal error; see the gateway's log"})
            if status == 401:
                time.sleep(auth_delay)  # makes guessing a secret slow
            return self._send(status, response)

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

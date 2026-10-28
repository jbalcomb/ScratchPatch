"""Minimal JSON-over-HTTP helpers built on urllib.

The standard library keeps the Android build free of compiled dependencies. On
Android, Python has no system CA bundle, so HTTPS verification uses ``certifi``
when it is installed.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator


class HttpError(Exception):
    """A request failed. ``status`` is the HTTP status, or None if the server was unreachable."""

    def __init__(self, message: str, status: int | None = None, body: str = "", timed_out: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.timed_out = timed_out  # connected, but no (complete) answer within the timeout


# ---- per-thread scope: who may use the network, and who records what it did ------------------------
#
# A job's worker thread runs inside ``scope(gate=..., trace=...)``:
# * gate(url) is called before every request. It may block (e.g. "waiting for Wi-Fi") or raise
#   (e.g. Cancelled) — the job's Wi-Fi-only setting.
# * trace(event, **fields) records every request: method, host, path, status, bytes, seconds,
#   or the error (diagnostics, see taskdeck.core.trace).

_scope = threading.local()


@contextmanager
def scope(gate: Callable[[str], None] | None = None,
          trace: Callable[..., None] | None = None) -> Iterator[None]:
    prev = (getattr(_scope, "gate", None), getattr(_scope, "trace", None))
    _scope.gate = gate or prev[0]
    _scope.trace = trace or prev[1]
    try:
        yield
    finally:
        _scope.gate, _scope.trace = prev


def current_scope() -> dict[str, Any]:
    """This thread's scope, to carry into worker threads: ``with scope(**current_scope()):``."""
    return {"gate": getattr(_scope, "gate", None), "trace": getattr(_scope, "trace", None)}


def _enter(url: str) -> float:
    gate = getattr(_scope, "gate", None)
    if gate:
        gate(url)
    return time.monotonic()


def _record(method: str, url: str, started: float, status: int | None = None, sent: int = 0,
            received: int = 0, error: str = "", **timing: Any) -> None:
    trace = getattr(_scope, "trace", None)
    if not trace:
        return
    parts = urllib.parse.urlsplit(url)
    fields: dict[str, Any] = {"method": method, "host": parts.netloc, "path": parts.path,
                              "secs": round(time.monotonic() - started, 3), "sent": sent, "received": received}
    if status is not None:
        fields["status"] = status
    if error:
        fields["error"] = error
    fields.update(timing)
    try:
        trace("http", **fields)
    except Exception:  # diagnostics must never break a request
        pass


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


class _Response(http.client.HTTPResponse):
    """Skip every interim 1xx response, not just 100 Continue.

    Python's http.client only skips 100, so a ``103 Early Hints`` (which Google's
    front ends can send) is mistaken for the final response and surfaces as
    "HTTP 103". Browsers ignore it; so do we.
    """

    def _read_status(self):
        while True:
            version, status, reason = super()._read_status()
            if 100 <= status < 200 and status != 101:
                http.client._read_headers(self.fp)  # discard the interim response's headers
                continue
            return version, status, reason


# Open sockets, so a network change can cut them (abort_connections): a request in flight when the
# phone moves between mobile data and Wi-Fi otherwise hangs on the old network until its timeout.
_live: "weakref.WeakSet[socket.socket]" = weakref.WeakSet()
_live_lock = threading.Lock()


def abort_connections() -> int:
    """Shut down every open connection; requests using them fail at once (and are retried by their
    callers on the new network). Returns how many were cut."""
    with _live_lock:
        socks = list(_live)
    count = 0
    for sock in socks:
        try:
            sock.shutdown(socket.SHUT_RDWR)
            count += 1
        except OSError:
            pass
    return count


class _Tracked:
    def connect(self):
        super().connect()
        with _live_lock:
            _live.add(self.sock)


class _HTTPConnection(_Tracked, http.client.HTTPConnection):
    response_class = _Response


class _HTTPSConnection(_Tracked, http.client.HTTPSConnection):
    response_class = _Response


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


_OPENER = urllib.request.build_opener(_HTTPHandler(), _HTTPSHandler(context=_SSL))


def _network_error(exc: Exception, url: str, timeout: float) -> HttpError:
    reason = getattr(exc, "reason", exc)
    host = urllib.parse.urlsplit(url).netloc or url
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
        return HttpError(f"{host} didn't answer within {timeout:g} s", timed_out=True)
    return HttpError(f"Can't reach {host}: {reason}")


def request(method: str, url: str, *, json_body: Any = None, form: dict[str, str] | None = None,
            headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
    """Send a request and decode a JSON response (or return text if it isn't JSON)."""
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    started = _enter(url)
    sent = len(data or b"")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
            _record(method, url, started, resp.status, sent, len(raw_bytes))
            raw = raw_bytes.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read() or b""
        _record(method, url, started, exc.code, sent, len(body_bytes))
        body = body_bytes.decode("utf-8", errors="replace")
        raise HttpError(f"HTTP {exc.code}: {_error_detail(body) or exc.reason}", exc.code, body) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:  # incl. a cut connection
        err = _network_error(exc, url, timeout)
        _record(method, url, started, sent=sent, error=str(err))
        raise err from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw




@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8", errors="replace") or "null")
        except json.JSONDecodeError:
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


# Connections kept open per thread and host (HTTP keep-alive). A new HTTPS connection costs a TLS
# handshake, 1-2 s on a phone; uploading thousands of small files to Drive was mostly handshakes.
_pool = threading.local()
_STALE = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _pooled(parts: urllib.parse.SplitResult, timeout: float) -> http.client.HTTPConnection:
    conns = _pool.__dict__.setdefault("conns", {})
    key = (parts.scheme, parts.hostname, parts.port)
    conn = conns.get(key)
    if conn is None:
        if parts.scheme == "https":
            conn = _HTTPSConnection(parts.hostname, parts.port, timeout=timeout, context=_SSL)
        else:
            conn = _HTTPConnection(parts.hostname, parts.port, timeout=timeout)
        conns[key] = conn
    conn.timeout = timeout
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
    return conn


def _drop(parts: urllib.parse.SplitResult) -> None:
    conn = getattr(_pool, "conns", {}).pop((parts.scheme, parts.hostname, parts.port), None)
    if conn is not None:
        conn.close()


def send(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
         timeout: float = 60) -> Response:
    """One request; any HTTP status comes back as a Response (no raising, no redirects followed).
    Network failures raise HttpError, like :func:`request`. The connection is kept open for the
    thread's next request to the same host.

    ``body`` is bytes or a sized file-like (read as it is sent, e.g. gdrive.Piece)."""
    parts = urllib.parse.urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    started = _enter(url)
    sent = len(body) if body is not None else 0
    for attempt in (1, 2):
        conn = _pooled(parts, timeout)
        reused = conn.sock is not None
        timing: dict[str, Any] = {"reused": reused}
        try:
            t0 = time.monotonic()
            if not reused:
                conn.connect()  # DNS, TCP and (for https) the TLS handshake
                timing["connect"] = round(time.monotonic() - t0, 3)
            t1 = time.monotonic()
            conn.request(method, target, body=body, headers=headers or {})
            t2 = time.monotonic()
            resp = conn.getresponse()
            t3 = time.monotonic()
            data = resp.read()
            t4 = time.monotonic()
            timing.update(send=round(t2 - t1, 3), wait=round(t3 - t2, 3), read=round(t4 - t3, 3))
            if getattr(body, "callback_secs", 0):  # time spent in progress updates while sending
                timing["callbacks"] = round(body.callback_secs, 3)
        except (OSError, http.client.HTTPException) as exc:  # incl. a cut connection
            _drop(parts)
            if reused and attempt == 1 and isinstance(exc, _STALE):
                # The server closed the idle connection before this request reached it: try a new one.
                if hasattr(body, "seek"):
                    body.seek(0)
                continue
            err = _network_error(exc, url, timeout)
            _record(method, url, started, sent=sent, error=str(err), **timing)
            raise err from None
        if resp.will_close:
            _drop(parts)
        out = Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
        _record(method, url, started, out.status, sent, len(out.body), **timing)
        return out
    raise AssertionError("unreachable")


def reachable(url: str, timeout: float = 3) -> bool:
    """Quick TCP connect to the URL's host and port: is anything listening there?"""
    parts = urllib.parse.urlsplit(url.strip())
    if not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def post_json(url: str, body: Any, **kw: Any) -> Any:
    return request("POST", url, json_body=body, **kw)


def get_json(url: str, **kw: Any) -> Any:
    return request("GET", url, **kw)


def _error_detail(body: str) -> str:
    """Pull the human-readable message out of common JSON error shapes."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip()[:200]
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, str):
        return str(data.get("error_description") or err)
    return str(data.get("detail") or data)[:200] if isinstance(data, dict) else str(data)[:200]

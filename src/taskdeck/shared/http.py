"""Minimal JSON-over-HTTP helpers built on urllib.

The standard library keeps the Android build free of compiled dependencies. On
Android, Python has no system CA bundle, so HTTPS verification uses ``certifi``
when it is installed.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HttpError(Exception):
    """A request failed. ``status`` is the HTTP status, or None if the server was unreachable."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


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
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HttpError(f"HTTP {exc.code}: {_error_detail(body) or exc.reason}", exc.code, body) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HttpError(f"Can't reach {urllib.parse.urlsplit(url).netloc or url}: {reason}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


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
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, str):
        return str(data.get("error_description") or err)
    return str(data.get("detail") or data)[:200] if isinstance(data, dict) else str(data)[:200]

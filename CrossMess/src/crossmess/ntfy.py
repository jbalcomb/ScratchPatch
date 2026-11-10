"""Publish admitted envelopes to a self-hosted ntfy server, one topic per channel (Phase A broker).

The envelope JSON is the ntfy message text. ntfy keeps messages up to its `message-size-limit`
(4 KB by default) as text; the gateway refuses larger envelopes rather than let ntfy turn them
into attachments.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from crossmess.envelope import Envelope

Publisher = Callable[[Envelope], None]


class PublishFailed(RuntimeError):
    pass


def envelope_bytes(env: Envelope) -> bytes:
    return json.dumps(env.to_json(), separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def publisher(base_url: str, token: str | None = None, timeout: float = 10) -> Publisher:
    base = base_url.rstrip("/")

    def publish(env: Envelope) -> None:
        request = urllib.request.Request(f"{base}/{env.channel}", data=envelope_bytes(env), method="POST")
        request.add_header("Content-Type", "text/plain; charset=utf-8")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
        except (urllib.error.URLError, OSError) as error:
            raise PublishFailed(f"ntfy publish to {env.channel!r} failed: {error}") from error

    return publish

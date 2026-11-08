"""Sending side: read a credentials file, sign a submission and post it to the gateway."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from courier.envelope import make_submission
from courier.store import private_key_from

# What a sender should be told for each gateway refusal (see server.py).
REASONS = {
    400: "the gateway refused it (channel outside this sender's scope, or a malformed message)",
    401: "the gateway doesn't know this sender (revoked, or the wrong credentials file)",
    413: "too long for the broker (about 3 KB of text)",
    422: "screening rejected the content",
    429: "sending too fast; wait a minute",
    502: "the gateway can't reach the broker right now; retry later",
}


class SendFailed(Exception):
    def __init__(self, message: str, status: int | None = None, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail or {}


@dataclass(frozen=True)
class Credentials:
    sender: str
    secret: str
    private_key: str
    gateway: str
    channels: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path, gateway: str | None = None) -> "Credentials":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        url = (gateway or data.get("gateway") or "").rstrip("/")
        if not url:
            raise ValueError(f"{path} names no gateway; pass --gateway")
        return cls(data["sender"], data["secret"], data["private_key"], url, tuple(data.get("channels") or ()))


def send(creds: Credentials, channel: str, text: str, *, content_type: str = "text/plain",
         mode: str = "fanout", ttl_s: int = 600, timeout: float = 30) -> dict:
    """Post one message. Returns the gateway's reply ({"id", "labels", ...}); raises SendFailed."""
    submission = make_submission(private_key_from(creds.private_key), channel, text,
                                 content_type=content_type, mode=mode, ttl_s=ttl_s)
    request = urllib.request.Request(f"{creds.gateway}/messages", data=json.dumps(submission).encode("utf-8"),
                                     method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {creds.secret}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read())
        except ValueError:
            detail = {}
        reason = REASONS.get(error.code, f"the gateway answered {error.code}")
        if detail.get("error"):
            reason += f": {detail['error']}"
        raise SendFailed(reason, error.code, detail) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SendFailed(f"can't reach the gateway at {creds.gateway}: {getattr(error, 'reason', error)}") from None

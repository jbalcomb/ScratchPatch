"""Sending through Courier (CrossMess/ in this repo): sign a submission and post it to the gateway.

The submission matches Courier's envelope format: the same fields, the same canonical JSON (sorted
keys, no whitespace, UTF-8) and an Ed25519 signature over it, made here in pure Python.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from taskdeck.core import StepError
from taskdeck.shared import ed25519
from taskdeck.shared.http import HttpError, request

CHANNEL = re.compile(r"^[a-z0-9-]{1,48}$")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PASTE_PREFIX = "courier-sender:"


def parse_credentials(text: str) -> dict[str, str]:
    """The sender credentials, pasted as the .sender.json file or as its one-line paste string."""
    text = (text or "").strip()
    if not text:
        raise StepError("Paste the sender credentials into this job's settings.")
    try:
        if text.startswith(PASTE_PREFIX):
            text = base64.urlsafe_b64decode(text[len(PASTE_PREFIX):] + "==").decode("utf-8")
        creds = json.loads(text)
        seed = base64.b64decode(creds["private_key"], validate=True)
        if len(seed) != 32 or not creds["secret"] or not creds["sender"]:
            raise ValueError
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise StepError("The sender credentials aren't readable. On the gateway machine run "
                        "`python -m crossmess add-sender NAME ... --paste-string` and paste the line it prints.") from None
    return {"sender": creds["sender"], "secret": creds["secret"], "private_key": creds["private_key"],
            "gateway": creds.get("gateway") or ""}


def gateway_url(settings: dict[str, Any], creds: dict[str, str] | None = None) -> str:
    url = (settings.get("gateway_url") or "").strip() or (creds or {}).get("gateway", "")
    if not url:
        raise StepError("Set the Gateway URL in this job's settings (the install script prints it).")
    return url.rstrip("/")


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def new_id(ms: int, rand: bytes) -> str:
    n = (ms << 80) | int.from_bytes(rand, "big")
    return "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_submission(creds: dict[str, str], channel: str, body: str, *, ttl_s: int = 600,
                     now: datetime | None = None, randbytes=os.urandom) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    fields = {
        "v": 1,
        "id": new_id(int(now.timestamp() * 1000), randbytes(10)),
        "channel": channel,
        "mode": "fanout",
        "created": stamp(now),
        "expires": stamp(now + timedelta(seconds=ttl_s)),
        "nonce": base64.b64encode(randbytes(16)).decode("ascii"),
        "content_type": "text/plain",
        "body": body,
    }
    seed = base64.b64decode(creds["private_key"])
    signature = ed25519.sign(seed, canonical(fields))
    return {**fields, "sender_sig": base64.b64encode(signature).decode("ascii")}


GATEWAY_ERRORS = {
    401: "The gateway doesn't know this sender: it was removed, or the credentials are wrong.",
    413: "Too long for the broker (about 3 KB of text in Phase A).",
    429: "Sending too fast; the gateway allows 30 messages a minute per sender. Try again shortly.",
    502: "The gateway couldn't reach its broker (ntfy). Try again; check the Courier tasks on the gateway machine.",
}


def post(settings: dict[str, Any], creds: dict[str, str], submission: Any, timeout: float = 20) -> Any:
    url = gateway_url(settings, creds) + "/messages"
    try:
        return request("POST", url, json_body=submission, headers={"Authorization": f"Bearer {creds['secret']}"},
                       timeout=timeout)
    except HttpError as exc:
        if exc.status in GATEWAY_ERRORS:
            raise StepError(GATEWAY_ERRORS[exc.status]) from None
        if exc.status == 422:
            raise StepError(f"Screening rejected it: {_detail(exc)}") from None
        if exc.status == 400:
            raise StepError(f"The gateway refused it: {_detail(exc)}") from None
        if exc.status is None:
            raise StepError(f"{exc}. Is the gateway machine on, and Tailscale connected on this phone?") from None
        raise StepError(str(exc)) from None


def _detail(exc: HttpError) -> str:
    try:
        return json.loads(exc.body)["error"]
    except (ValueError, KeyError, TypeError):
        return str(exc)


def test_connection(settings: dict[str, Any]) -> str:
    """Settings action. An empty submission is refused with 400 once the secret is accepted (401 if
    not), so this checks the URL and the credentials without sending anything."""
    try:
        creds = parse_credentials(settings.get("credentials") or "")
        post(settings, creds, {}, timeout=10)
    except StepError as exc:
        if str(exc).startswith("The gateway refused it"):
            return f"Connected as {creds['sender']}. The gateway accepts these credentials."
        return str(exc)
    return "Connected, but the gateway accepted an empty message, which it shouldn't. Check its version."

"""The Courier envelope: one JSON object, signed by its sender and again by the gateway.

The sender signs the submission fields. The gateway screens the body, adds the fields only it
may set (origin, labels, screen, orig_sha256), and signs everything but its own signature.
Device agents trust only the gateway signature.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

VERSION = 1
SUBMISSION_FIELDS = ("v", "id", "channel", "mode", "created", "expires", "nonce", "content_type", "body")

ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class Invalid(ValueError):
    """The envelope is malformed, unauthorized, forged, expired or replayed."""


@dataclass(frozen=True)
class Envelope:
    v: int
    id: str
    channel: str
    mode: str
    created: str
    expires: str
    nonce: str
    content_type: str
    body: str
    orig_sha256: str = ""
    origin: dict[str, str] = field(default_factory=dict)  # {"kind", "sender"}
    labels: tuple[str, ...] = ()
    screen: dict[str, str] = field(default_factory=dict)  # {"ruleset", "version"}
    sender_sig: str = ""
    gw_sig: str = ""

    @classmethod
    def from_json(cls, data: dict, allowed: tuple[str, ...] | None = None) -> Envelope:
        names = tuple(f.name for f in fields(cls))
        allowed = allowed or names
        extra = set(data) - set(allowed)
        if extra:
            raise Invalid(f"fields not allowed here: {', '.join(sorted(extra))}")
        missing = [k for k in SUBMISSION_FIELDS if k not in data]
        if missing:
            raise Invalid(f"missing fields: {', '.join(missing)}")
        for k in SUBMISSION_FIELDS + ("orig_sha256", "sender_sig", "gw_sig"):
            if k in data and not isinstance(data[k], int if k == "v" else str):
                raise Invalid(f"{k} has the wrong type")
        for k in ("origin", "screen"):
            if not isinstance(data.get(k, {}), dict):
                raise Invalid(f"{k} has the wrong type")
        labels = data.get("labels", [])
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise Invalid("labels has the wrong type")
        kwargs = dict(data)
        if "labels" in kwargs:
            kwargs["labels"] = tuple(kwargs["labels"])
        return cls(**kwargs)

    def to_json(self) -> dict:
        data = asdict(self)
        data["labels"] = list(self.labels)
        return data

    def submission_bytes(self) -> bytes:
        return canonical({k: getattr(self, k) for k in SUBMISSION_FIELDS})

    def signed_bytes(self) -> bytes:
        return canonical({**self.to_json(), "gw_sig": ""})


def canonical(obj: dict) -> bytes:
    """Sorted keys, no whitespace, UTF-8. For Courier's ASCII keys, strings and small integers
    this matches RFC 8785 (JCS), so a non-Python client can produce the same bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(key: Ed25519PrivateKey, data: bytes) -> str:
    return base64.b64encode(key.sign(data)).decode("ascii")


def verify(key: Ed25519PublicKey, sig: str, data: bytes) -> bool:
    try:
        key.verify(base64.b64decode(sig, validate=True), data)
    except (InvalidSignature, binascii.Error, ValueError):
        return False
    return True


def new_id(now_ms: int | None = None) -> str:
    """A ULID: 48 bits of milliseconds then 80 random bits, so IDs sort by creation time."""
    ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    n = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str, name: str) -> datetime:
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise Invalid(f"{name} is not an RFC 3339 time") from None
    if t.tzinfo is None:
        raise Invalid(f"{name} has no time zone")
    return t


def make_submission(
    key: Ed25519PrivateKey,
    channel: str,
    body: str,
    *,
    content_type: str = "text/plain",
    mode: str = "fanout",
    ttl_s: int = 600,
    now: datetime | None = None,
) -> dict:
    """What a sender POSTs to the gateway: the submission fields plus its signature."""
    now = now or datetime.now(timezone.utc)
    env = Envelope(
        v=VERSION,
        id=new_id(int(now.timestamp() * 1000)),
        channel=channel,
        mode=mode,
        created=stamp(now),
        expires=stamp(now + timedelta(seconds=ttl_s)),
        nonce=base64.b64encode(os.urandom(16)).decode("ascii"),
        content_type=content_type,
        body=body,
    )
    return {**{k: getattr(env, k) for k in SUBMISSION_FIELDS}, "sender_sig": sign(key, env.submission_bytes())}

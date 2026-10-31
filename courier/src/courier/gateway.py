"""Gateway admission: check a submission against its sender's token, screen it, sign the result.

The HTTP and MCP front ends both end here. Nothing in the request can set origin, labels or the
ruleset: origin comes from the token, the rest from screening.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from courier.envelope import SUBMISSION_FIELDS, ULID, VERSION, Envelope, Invalid, parse_time, sign, verify
from courier.screen import Classifier, Ruleset, screen

ORIGIN_KINDS = frozenset({"human", "script", "agent", "web"})


@dataclass(frozen=True)
class Token:
    """What the gateway knows about one sender credential."""

    sender: str
    kind: str  # one of ORIGIN_KINDS
    channels: frozenset[str]
    public_key: Ed25519PublicKey

    def __post_init__(self):
        if self.kind not in ORIGIN_KINDS:
            raise ValueError(f"unknown origin kind {self.kind!r}")


def check_submission(env: Envelope, token: Token, rules: Ruleset, now: datetime) -> None:
    if env.v != VERSION:
        raise Invalid(f"unsupported envelope version {env.v}")
    if not ULID.match(env.id):
        raise Invalid("id is not a ULID")
    if not rules.channel.match(env.channel):
        raise Invalid("channel name is not allowed")
    if env.channel not in token.channels:
        raise Invalid(f"this sender may not post to {env.channel!r}")
    if env.mode not in rules.modes:
        raise Invalid(f"mode {env.mode!r} is not allowed")
    try:
        nonce = base64.b64decode(env.nonce, validate=True)
    except (binascii.Error, ValueError):
        raise Invalid("nonce is not base64") from None
    if len(nonce) != 16:
        raise Invalid("nonce must be 16 bytes")

    created = parse_time(env.created, "created")
    expires = parse_time(env.expires, "expires")
    limits = rules.limits
    if (created - now).total_seconds() > limits["max_skew_s"]:
        raise Invalid("created is in the future")
    if expires <= now or expires <= created:
        raise Invalid("already expired")
    if (expires - created).total_seconds() > limits["max_lifetime_s"]:
        raise Invalid(f"lifetime is over {limits['max_lifetime_s']} s")

    if not verify(token.public_key, env.sender_sig, env.submission_bytes()):
        raise Invalid("sender signature does not verify")


def admit(
    submission: dict,
    token: Token,
    rules: Ruleset,
    gw_key: Ed25519PrivateKey,
    *,
    now: datetime | None = None,
    classifier: Classifier | None = None,
) -> Envelope:
    """Raises Invalid or Rejected, or returns the signed envelope to publish on the channel."""
    now = now or datetime.now(timezone.utc)
    env = Envelope.from_json(submission, allowed=SUBMISSION_FIELDS + ("sender_sig",))
    check_submission(env, token, rules, now)

    screened = screen(env.body, env.content_type, rules, origin=token.kind, classifier=classifier)
    env = replace(
        env,
        body=screened.body,
        orig_sha256=hashlib.sha256(env.body.encode("utf-8")).hexdigest(),
        origin={"kind": token.kind, "sender": token.sender},
        labels=tuple(sorted(screened.labels)),
        screen={"ruleset": screened.ruleset, "version": screened.version},
    )
    return replace(env, gw_sig=sign(gw_key, env.signed_bytes()))

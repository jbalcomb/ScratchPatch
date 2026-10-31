"""Device-side receipt: verify the gateway's signature, refuse stale or replayed envelopes, and
screen the body again with this device's own ruleset before any topping or action sees it.

A compromised gateway or broker can therefore withhold messages but cannot deliver text that
skipped screening, and labels it stamped can only be added to, never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from courier.envelope import Envelope, Invalid, parse_time, verify
from courier.screen import Ruleset, screen


@dataclass(frozen=True)
class Delivery:
    envelope: Envelope
    body: str
    labels: frozenset[str]


class Seen:
    """IDs already delivered, kept until their envelope would have expired anyway."""

    def __init__(self):
        self._until: dict[str, datetime] = {}

    def check_and_add(self, env_id: str, expires: datetime, now: datetime) -> None:
        self._until = {k: t for k, t in self._until.items() if t > now}
        if env_id in self._until:
            raise Invalid("envelope already delivered")
        self._until[env_id] = expires


def receive(
    data: dict,
    gateway_keys: Iterable[Ed25519PublicKey],
    rules: Ruleset,
    seen: Seen,
    *,
    accept_rulesets: frozenset[str] | None = None,
    now: datetime | None = None,
) -> Delivery:
    now = now or datetime.now(timezone.utc)
    env = Envelope.from_json(data)
    if not any(verify(k, env.gw_sig, env.signed_bytes()) for k in gateway_keys):
        raise Invalid("gateway signature does not verify")
    if accept_rulesets is not None and env.screen.get("ruleset") not in accept_rulesets:
        raise Invalid("screened by a ruleset this device does not accept")
    expires = parse_time(env.expires, "expires")
    if expires <= now:
        raise Invalid("envelope has expired")
    seen.check_and_add(env.id, expires, now)

    screened = screen(env.body, env.content_type, rules)
    return Delivery(env, screened.body, frozenset(env.labels) | screened.labels)

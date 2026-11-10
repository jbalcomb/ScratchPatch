"""The device agent: subscribe to ntfy, verify and re-screen each envelope, run the subscription's
toppings, and perform at most the action tier its subscription allows.

Labels about where a message came from or what it held (secret, origin_*, url_*, ai_*) stay with
it whatever the toppings do. Guards about the body's shape (multiline, shell_chars, long) look at
the body actually about to be delivered, so a single-line topping can make a message copyable.
Nothing in a message can raise the tier: the subscription alone sets it.

device.toml (in the config folder):

    ntfy = "https://<machine>.<tailnet>.ts.net:2586"
    token_file = "ntfy-device.token"          # relative to the config folder
    gateway_keys = ["<base64 public key>"]
    accept_rulesets = []                      # pin screening rulesets; empty accepts any

    [[subscriptions]]
    channel = "clip"
    action = "clipboard"                      # inbox | notify | clipboard | open-url
    clear_after = 90                          # seconds; 0 keeps it
    allow_multiline = false
    allow_shell_chars = false
    toppings = [{ filter = "drop-labels", labels = ["secret", "ai_injection"] }, { mutate = "single-line" }]
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from crossmess.agent import Delivery, Seen, native_text, receive
from crossmess.envelope import Invalid
from crossmess.screen import Rejected, Ruleset, screen
from crossmess.store import public_key_from

log = logging.getLogger(__name__)

ACTIONS = ("inbox", "notify", "clipboard", "open-url")
SHAPE_LABELS = frozenset({"multiline", "shell_chars", "long"})
TEXT_TYPES = frozenset({"text/plain", "text/uri-list"})
NOTIFY_CHARS = 240


class ConfigError(ValueError):
    pass


Topping = Callable[[Delivery], "Delivery | None"]


# --- Toppings: pure functions, message -> message or None (dropped) ----------------------------

def _labels(spec: dict, key: str) -> frozenset[str]:
    value = spec.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"topping {spec} needs {key} = [\"...\"]")
    return frozenset(value)


def _drop_labels(spec: dict) -> Topping:
    labels = _labels(spec, "labels")
    return lambda d: None if d.labels & labels else d


def _allow_origins(spec: dict) -> Topping:
    kinds = _labels(spec, "origins")
    return lambda d: d if d.envelope.origin.get("kind") in kinds else None


def _allow_senders(spec: dict) -> Topping:
    senders = _labels(spec, "senders")
    return lambda d: d if d.envelope.origin.get("sender") in senders else None


def _single_line(spec: dict) -> Topping:
    return lambda d: replace(d, body=d.body.split("\n", 1)[0])


def _trim(spec: dict) -> Topping:
    return lambda d: replace(d, body=d.body.strip())


def _max_length(spec: dict) -> Topping:
    chars = spec.get("chars")
    if not isinstance(chars, int) or chars < 1:
        raise ConfigError("max-length needs chars = <positive number>")
    return lambda d: replace(d, body=d.body[:chars])


FILTERS = {"drop-labels": _drop_labels, "allow-origins": _allow_origins, "allow-senders": _allow_senders}
MUTATIONS = {"single-line": _single_line, "trim": _trim, "max-length": _max_length}


def build_topping(spec: dict) -> tuple[Topping, bool]:
    """Returns the topping and whether it can change the body."""
    if not isinstance(spec, dict):
        raise ConfigError(f"a topping must be a table, not {spec!r}")
    if "filter" in spec and spec["filter"] in FILTERS:
        return FILTERS[spec["filter"]](spec), False
    if "mutate" in spec and spec["mutate"] in MUTATIONS:
        return MUTATIONS[spec["mutate"]](spec), True
    known = ", ".join([f"filter={n}" for n in FILTERS] + [f"mutate={n}" for n in MUTATIONS])
    raise ConfigError(f"unknown topping {spec}; known: {known}")


# --- Configuration ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Subscription:
    channel: str
    action: str
    toppings: tuple[Topping, ...] = ()
    clear_after: int = 90
    allow_multiline: bool = False
    allow_shell_chars: bool = False
    allow_secret: bool = False


@dataclass(frozen=True)
class DeviceConfig:
    ntfy: str
    token: str
    gateway_keys: tuple[Ed25519PublicKey, ...]
    accept_rulesets: frozenset[str] | None
    subscriptions: dict[str, Subscription]

    @classmethod
    def load(cls, path: Path) -> DeviceConfig:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"{path} not found; create it with: python -m crossmess device-init") from None
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{path}: {error}") from None
        try:
            token_path = path.parent / data.get("token_file", "ntfy-device.token")
            token = token_path.read_text(encoding="utf-8").strip()
            keys = tuple(public_key_from(k) for k in data["gateway_keys"])
            if not keys:
                raise ConfigError("gateway_keys is empty")
            rulesets = frozenset(data.get("accept_rulesets") or ()) or None
            subscriptions = {}
            for entry in data["subscriptions"]:
                sub = _subscription(entry)
                if sub.channel in subscriptions:
                    raise ConfigError(f"channel {sub.channel!r} is subscribed twice")
                subscriptions[sub.channel] = sub
            return cls(data["ntfy"].rstrip("/"), token, keys, rulesets, subscriptions)
        except KeyError as error:
            raise ConfigError(f"{path}: missing {error}") from None
        except (OSError, ValueError, TypeError) as error:
            if isinstance(error, ConfigError):
                raise
            raise ConfigError(f"{path}: {error}") from None


def _subscription(entry: dict) -> Subscription:
    channel, action = entry["channel"], entry.get("action", "inbox")
    if not re.fullmatch(r"[a-z0-9-]{1,48}", channel):
        raise ConfigError(f"bad channel name {channel!r}")
    if action not in ACTIONS:
        raise ConfigError(f"channel {channel!r}: action must be one of {', '.join(ACTIONS)}")
    known = {"channel", "action", "toppings", "clear_after", "allow_multiline", "allow_shell_chars", "allow_secret"}
    if unknown := set(entry) - known:
        raise ConfigError(f"channel {channel!r}: unknown settings {', '.join(sorted(unknown))}")
    toppings = tuple(build_topping(spec)[0] for spec in entry.get("toppings", []))
    return Subscription(
        channel, action, toppings, int(entry.get("clear_after", 90)),
        bool(entry.get("allow_multiline", False)), bool(entry.get("allow_shell_chars", False)),
        bool(entry.get("allow_secret", False)),
    )


# --- The agent ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    result: str  # done | refused | dropped | invalid | rejected | ignored | failed
    channel: str = ""
    id: str = ""
    sender: str = ""
    action: str = ""
    labels: tuple[str, ...] = ()
    reason: str = ""


def clipboard_refusal(sub: Subscription, d: Delivery, shape: frozenset[str]) -> str | None:
    if d.envelope.content_type not in TEXT_TYPES:
        return "only text is copied"
    if "ai_injection" in d.labels:
        return "the classifier flagged it as an injection"
    if "secret" in d.labels and not sub.allow_secret:
        return "it looks like a secret"
    if "multiline" in shape and not sub.allow_multiline:
        return "it has more than one line"
    if "shell_chars" in shape and not sub.allow_shell_chars:
        return "it has shell characters"
    return None


URL_REFUSALS = {
    "url_not_https": "the link is not https",
    "url_idn": "the link's host uses non-ASCII or punycode",
    "url_mixed_script": "the link's host mixes scripts",
    "url_userinfo": "the link hides its host behind an @",
    "ai_injection": "the classifier flagged it as an injection",
    "ai_suspicious": "the classifier flagged it",
    "secret": "it looks like a secret",
}


def url_refusal(d: Delivery) -> str | None:
    if d.envelope.content_type not in TEXT_TYPES or not re.fullmatch(r"https://\S+", d.body):
        return "it is not a single https link"
    for label, reason in URL_REFUSALS.items():
        if label in d.labels:
            return reason
    return None


class DeviceAgent:
    def __init__(self, config: DeviceConfig, rules: Ruleset, data_dir: Path, clipboard, notifier, *,
                 opener: Callable[[str], object] = webbrowser.open, platform: str = sys.platform,
                 timer: Callable[..., threading.Timer] = threading.Timer):
        self.config, self.rules, self.clipboard, self.notifier = config, rules, clipboard, notifier
        self.opener, self.platform, self.timer = opener, platform, timer
        self.seen = Seen()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_path = data_dir / "inbox.jsonl"
        self.log_path = data_dir / "device.jsonl"
        self._clear_timer: threading.Timer | None = None

    def handle(self, topic: str, text: str) -> Outcome:
        outcome = self._handle(topic, text)
        self._record(self.log_path, {**outcome.__dict__, "labels": list(outcome.labels)})
        return outcome

    def _handle(self, topic: str, text: str) -> Outcome:
        sub = self.config.subscriptions.get(topic)
        if sub is None:
            return Outcome("ignored", topic, reason="not subscribed")
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError:
            return Outcome("invalid", topic, reason="not a Courier envelope")
        if data.get("channel") != topic:
            return Outcome("invalid", topic, str(data.get("id", "")), reason="channel does not match the topic")
        try:
            d = receive(data, self.config.gateway_keys, self.rules, self.seen,
                        accept_rulesets=self.config.accept_rulesets)
        except Invalid as error:
            return Outcome("invalid", topic, str(data.get("id", "")), reason=str(error))
        except Rejected as error:
            return Outcome("rejected", topic, str(data.get("id", "")), reason=str(error))

        base = dict(channel=topic, id=d.envelope.id, sender=d.envelope.origin.get("sender", ""), action=sub.action)
        for topping in sub.toppings:
            result = topping(d)
            if result is None:
                return Outcome("dropped", labels=tuple(sorted(d.labels)), reason="a topping dropped it", **base)
            d = result
        # Mutations are followed by the fixed stages again; what they find is added, never removed.
        try:
            again = screen(d.body, d.envelope.content_type, self.rules)
        except Rejected as error:
            return Outcome("rejected", labels=tuple(sorted(d.labels)), reason=str(error), **base)
        d = replace(d, body=again.body, labels=d.labels | again.labels)
        shape = again.labels & SHAPE_LABELS
        labels = tuple(sorted(d.labels))

        try:
            refusal = self._act(sub, d, shape)
        except Exception as error:  # an OS tool failing is reported, not fatal
            log.warning("%s on %s failed: %s", sub.action, topic, error)
            return Outcome("failed", labels=labels, reason=str(error), **base)
        if refusal:
            self._tell(f"Courier: not {'copied' if sub.action == 'clipboard' else 'opened'} ({topic})",
                       f"{refusal}. From {base['sender']}.")
            return Outcome("refused", labels=labels, reason=refusal, **base)
        return Outcome("done", labels=labels, **base)

    def _act(self, sub: Subscription, d: Delivery, shape: frozenset[str]) -> str | None:
        if sub.action == "inbox":
            self._inbox(d)
        elif sub.action == "notify":
            self._inbox(d)
            body = "(hidden: it looks like a secret)" if "secret" in d.labels else d.body
            if d.envelope.content_type not in TEXT_TYPES:
                body = f"({d.envelope.content_type}; in the inbox)"
            if len(body) > NOTIFY_CHARS:
                body = body[:NOTIFY_CHARS - 3] + "..."
            self.notifier.notify(f"{d.envelope.channel} from {d.envelope.origin.get('sender', '?')}", body)
        elif sub.action == "clipboard":
            if refusal := clipboard_refusal(sub, d, shape):
                return refusal
            self._copy(native_text(d.body, self.platform), sub.clear_after)
        elif sub.action == "open-url":
            if refusal := url_refusal(d):
                return refusal
            self.opener(d.body)
        return None

    def _copy(self, text: str, clear_after: int) -> None:
        if self._clear_timer:
            self._clear_timer.cancel()
        self.clipboard.set(text)
        if clear_after > 0:
            # Clear only what we put there: if you copied something since, leave it.
            def clear():
                try:
                    if self.clipboard.get() == text:
                        self.clipboard.clear()
                except Exception as error:
                    log.warning("clearing the clipboard failed: %s", error)

            self._clear_timer = self.timer(clear_after, clear)
            self._clear_timer.daemon = True
            self._clear_timer.start()

    def _inbox(self, d: Delivery) -> None:
        entry = {"channel": d.envelope.channel, "id": d.envelope.id, "sender": d.envelope.origin.get("sender"),
                 "content_type": d.envelope.content_type, "labels": sorted(d.labels)}
        # A secret is not written to disk in clear, even on its own device.
        entry["body"] = None if "secret" in d.labels else d.body
        self._record(self.inbox_path, entry)

    def _tell(self, title: str, body: str) -> None:
        try:
            self.notifier.notify(title, body)
        except Exception as error:
            log.warning("notification failed: %s", error)

    def _record(self, path: Path, entry: dict) -> None:
        line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


# --- Subscribing to ntfy ------------------------------------------------------------------------

def subscribe(config: DeviceConfig, agent: DeviceAgent, state_path: Path, *,
              stop: threading.Event | None = None, read_timeout: float = 120, max_backoff: float = 60) -> None:
    """Stream every subscribed topic, resuming after the last ntfy message seen, until `stop`."""
    stop = stop or threading.Event()
    try:
        since = json.loads(state_path.read_text(encoding="utf-8"))["since"]
    except (OSError, ValueError, KeyError):
        since = str(int(time.time()))  # first run: only what arrives from now on
    topics = ",".join(sorted(config.subscriptions))
    backoff = 1.0
    while not stop.is_set():
        request = urllib.request.Request(f"{config.ntfy}/{topics}/json?since={since}")
        request.add_header("Authorization", f"Bearer {config.token}")
        try:
            with urllib.request.urlopen(request, timeout=read_timeout) as response:
                log.info("subscribed to %s at %s", topics, config.ntfy)
                backoff = 1.0
                for raw in response:
                    if stop.is_set():
                        return
                    event = json.loads(raw)
                    if event.get("event") != "message":
                        continue
                    outcome = agent.handle(event.get("topic", ""), event.get("message", ""))
                    log.info("%s %s %s %s", outcome.result, outcome.channel, outcome.id, outcome.reason)
                    since = event["id"]
                    state_path.write_text(json.dumps({"since": since}), encoding="utf-8")
        except urllib.error.HTTPError as error:
            log.error("ntfy refused the subscription: %s %s (check the device token)", error.code, error.reason)
            backoff = max_backoff
        except (urllib.error.URLError, OSError, ValueError) as error:
            log.warning("ntfy connection lost: %s; retrying in %.0f s", error, backoff)
        if stop.wait(backoff):
            return
        backoff = min(max_backoff, backoff * 2)

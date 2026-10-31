"""Fixed screening: normalize, inspect and label a message body before anything acts on it.

Each stage is a pure function of (body, ruleset) that returns the body and the labels it adds.
The pipeline only ever unions those labels, so no stage, including the optional AI classifier
that runs last, can leave a message less restricted than the stages before it did. The gateway
runs this on submission and every device agent runs it again on receipt.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Callable


class Rejected(ValueError):
    """The message must not be delivered at all."""


@dataclass(frozen=True)
class Ruleset:
    digest: str  # "sha256:<hex>" of the ruleset file's bytes, with LF line endings
    version: str
    limits: dict[str, int]
    content_types: frozenset[str]
    modes: frozenset[str]
    channel: re.Pattern[str]
    shell_chars: frozenset[str]
    bare_schemes: frozenset[str]
    secrets: tuple[tuple[str, re.Pattern[str]], ...]

    @classmethod
    def load(cls, path: Path | None = None) -> Ruleset:
        if path is None:
            raw = resources.files("courier").joinpath("ruleset.toml").read_bytes()
        else:
            raw = path.read_bytes()
        # LF is canonical, as in git's object store: a CRLF checkout on Windows hashes the same.
        raw = raw.replace(b"\r\n", b"\n")
        data = tomllib.loads(raw.decode("utf-8"))
        return cls(
            digest="sha256:" + hashlib.sha256(raw).hexdigest(),
            version=data["version"],
            limits=dict(data["limits"]),
            content_types=frozenset(data["envelope"]["content_types"]),
            modes=frozenset(data["envelope"]["modes"]),
            channel=re.compile(data["envelope"]["channel"]),
            shell_chars=frozenset(data["inspect"]["shell_chars"]),
            bare_schemes=frozenset(data["inspect"]["bare_schemes"]),
            secrets=tuple((s["name"], re.compile(s["pattern"])) for s in data["secrets"]),
        )


@dataclass(frozen=True)
class Screened:
    body: str
    labels: frozenset[str]
    ruleset: str
    version: str


# Terminal escape sequences: CSI (colours, cursor moves, line erase), OSC (titles, hyperlinks),
# and the two-byte forms. Removed before control characters so their parameters go with them.
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")

# Direction overrides and isolates (Trojan Source) and the implicit direction marks.
BIDI = frozenset(map(chr, [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]))

# Characters that render as nothing. Tags (U+E0000-E007F) and the variation selector supplement
# can smuggle a hidden ASCII message past a person to a model; FE00-FE0F stay, emoji need them.
INVISIBLE = frozenset(
    map(chr, [0x00AD, 0x180E, 0x200B, 0x200C, 0x200D, *range(0x2060, 0x2065), 0xFEFF])
)


def _invisible(c: str) -> bool:
    o = ord(c)
    return c in INVISIBLE or 0xE0000 <= o <= 0xE007F or 0xE0100 <= o <= 0xE01EF


def _control(c: str) -> bool:
    o = ord(c)
    return (o < 0x20 and c not in "\t\n") or 0x7F <= o <= 0x9F


def normalize(body: str, rules: Ruleset) -> tuple[str, frozenset[str]]:
    labels = set()
    out = ANSI.sub("", body)
    if out != body:
        labels.add("ansi_removed")
    # A lone CR returns the cursor and lets later text overwrite what a person saw.
    if re.search(r"\r(?!\n)", out):
        labels.add("cr_normalized")
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    kept = []
    for c in out:
        if c in BIDI:
            labels.add("bidi_removed")
        elif _invisible(c):
            labels.add("invisible_removed")
        elif _control(c):
            labels.add("control_removed")
        else:
            kept.append(c)
    out = unicodedata.normalize("NFC", "".join(kept))
    # A trailing newline runs the command the moment it is pasted into a terminal.
    if "\n" in out[len(out.rstrip()):]:
        out = out.rstrip()
        labels.add("trailing_newline_removed")
    return out, frozenset(labels)


URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,31}):(//)?[^\s<>\"']+")


def _script(c: str) -> str:
    return unicodedata.name(c, "UNKNOWN").split(" ")[0]


def _idn_labels(host: str) -> list[str]:
    decoded = []
    for part in host.split("."):
        if part.startswith("xn--"):
            try:
                part = part[4:].encode("ascii").decode("punycode")
            except (UnicodeError, ValueError):
                pass
        decoded.append(part)
    return decoded


def _url_labels(url: str, scheme: str) -> set[str]:
    labels = {"has_url"}
    if scheme != "https":
        labels.add("url_not_https")
    netloc = url.split("//", 1)[1].split("/", 1)[0] if "//" in url else ""
    # https://bank.example@evil.example shows one host and goes to the other.
    if "@" in netloc:
        labels.add("url_userinfo")
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    if not host.isascii() or any(p.startswith("xn--") for p in host.split(".")):
        labels.add("url_idn")
    for part in _idn_labels(host):
        if len({_script(c) for c in part if c.isalpha()}) > 1:
            labels.add("url_mixed_script")
    return labels


def inspect(body: str, rules: Ruleset) -> tuple[str, frozenset[str]]:
    labels = set()
    if "\n" in body:
        labels.add("multiline")
    if any(c in rules.shell_chars for c in body):
        labels.add("shell_chars")
    if len(body) > rules.limits["long_chars"]:
        labels.add("long")
    # Browsers drop tabs inside a URL, so java<TAB>script: still runs.
    for m in URL.finditer(body.replace("\t", "")):
        scheme = m.group(1).lower()
        if m.group(2) or scheme in rules.bare_schemes:
            labels |= _url_labels(m.group(0), scheme)
    return body, frozenset(labels)


def scan_secrets(body: str, rules: Ruleset) -> tuple[str, frozenset[str]]:
    found = any(pattern.search(body) for _, pattern in rules.secrets)
    return body, frozenset({"secret"} if found else ())


TEXT_STAGES = (normalize, inspect, scan_secrets)

# The classifier is handed the normalized body as quoted data and must answer one of these words.
# "clean" adds nothing: a model can never vouch a message past the fixed stages.
AI_LABELS = {
    "clean": frozenset(),
    "suspicious": frozenset({"ai_suspicious"}),
    "injection": frozenset({"ai_injection"}),
}

Classifier = Callable[[str], str]


def classify(body: str, classifier: Classifier) -> frozenset[str]:
    try:
        verdict = classifier(body)
    except Exception:
        return frozenset({"ai_unavailable"})
    if not isinstance(verdict, str):
        return frozenset({"ai_suspicious"})
    # Anything off the list, such as a verdict the message talked the model into, counts against it.
    return AI_LABELS.get(verdict.strip().lower(), frozenset({"ai_suspicious"}))


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _check_png(body: str, rules: Ruleset) -> None:
    try:
        data = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        raise Rejected("image body is not valid base64") from None
    if len(data) > rules.limits["image_bytes"]:
        raise Rejected(f"image is {len(data)} bytes; the limit is {rules.limits['image_bytes']}")
    if not data.startswith(PNG_MAGIC):
        raise Rejected("image body is not a PNG")


def _check_uri_list(body: str) -> None:
    for line in body.split("\n"):
        if line.startswith("#"):
            continue
        if not re.fullmatch(r"(?i)[a-z][a-z0-9+.-]*:\S+", line):
            raise Rejected("text/uri-list holds a line that is not a URI")


def screen(
    body: str,
    content_type: str,
    rules: Ruleset,
    *,
    origin: str | None = None,
    classifier: Classifier | None = None,
) -> Screened:
    """Screen one body. Raises Rejected, or returns the body to deliver and its labels."""
    if content_type not in rules.content_types:
        raise Rejected(f"content type {content_type!r} is not allowed")
    if content_type == "image/png":
        # Phase B: decode and re-encode images so hidden chunks and malformed data never travel.
        _check_png(body, rules)
        return Screened(body, frozenset(), rules.digest, rules.version)

    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError:
        raise Rejected("body is not valid UTF-8") from None
    if size > rules.limits["text_bytes"]:
        raise Rejected(f"body is {size} bytes; the limit is {rules.limits['text_bytes']}")

    labels: frozenset[str] = frozenset()
    for stage in TEXT_STAGES:
        body, added = stage(body, rules)
        labels |= added
    if not body.strip():
        raise Rejected("body is empty once screened")
    if content_type == "text/uri-list":
        _check_uri_list(body)
    if origin and origin != "human":
        labels |= {f"origin_{origin}"}
    if classifier is not None:
        labels |= classify(body, classifier)
    return Screened(body, labels, rules.digest, rules.version)

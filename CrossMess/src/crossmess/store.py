"""Keys and sender records on disk.

  gateway.key     the gateway's Ed25519 private key (base64 of the 32 raw bytes), mode 0600
  senders.json    one record per sender: kind, channels, public key, SHA-256 of its bearer secret

A sender's credentials file (its private key and bearer secret) is written once by `add` and
handed to the sender; the gateway keeps only the public half and the secret's hash.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from crossmess.gateway import ORIGIN_KINDS, Token

CONFIG_DIR = Path.home() / ".config" / "courier"
NAME = r"[a-z0-9][a-z0-9_-]{0,47}"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def private_key_from(text: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(text))


def public_key_from(text: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(text))


def public_key_text(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return b64(public.public_bytes_raw())


def load_gateway_key(path: Path, create: bool = True) -> Ed25519PrivateKey:
    if not path.is_file():
        if not create:
            raise FileNotFoundError(path)
        write_private(path, b64(Ed25519PrivateKey.generate().private_bytes_raw()) + "\n")
    return private_key_from(path.read_text(encoding="utf-8").strip())


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class Senders:
    """senders.json, re-read whenever it changes on disk, so add/remove needs no restart."""

    def __init__(self, path: Path):
        self.path = path
        self._stamp = None
        self._records: dict[str, dict] = {}
        self._by_secret: dict[str, str] = {}

    def _refresh(self) -> None:
        stamp = self.path.stat().st_mtime_ns if self.path.is_file() else None
        if stamp == self._stamp:
            return
        self._stamp = stamp
        self._records = json.loads(self.path.read_text(encoding="utf-8"))["senders"] if stamp else {}
        self._by_secret = {r["secret_sha256"]: name for name, r in self._records.items()}

    def records(self) -> dict[str, dict]:
        self._refresh()
        return dict(self._records)

    def token_for(self, bearer: str) -> Token | None:
        self._refresh()
        name = self._by_secret.get(secret_hash(bearer))
        if name is None:
            return None
        r = self._records[name]
        return Token(name, r["kind"], frozenset(r["channels"]), public_key_from(r["public_key"]))

    def add(self, name: str, kind: str, channels: list[str]) -> dict:
        """Create a sender and return its credentials; they are not stored here."""
        if not re.fullmatch(NAME, name):
            raise ValueError(f"sender name must match {NAME}")
        if kind not in ORIGIN_KINDS:
            raise ValueError(f"kind must be one of {', '.join(sorted(ORIGIN_KINDS))}")
        records = self.records()
        if name in records:
            raise ValueError(f"sender {name!r} already exists")
        key = Ed25519PrivateKey.generate()
        secret = secrets.token_urlsafe(32)
        records[name] = {
            "kind": kind,
            "channels": sorted(set(channels)),
            "public_key": public_key_text(key),
            "secret_sha256": secret_hash(secret),
        }
        self._save(records)
        return {"sender": name, "secret": secret, "private_key": b64(key.private_bytes_raw())}

    def remove(self, name: str) -> bool:
        records = self.records()
        if records.pop(name, None) is None:
            return False
        self._save(records)
        return True

    def _save(self, records: dict) -> None:
        write_private(self.path, json.dumps({"senders": records}, indent=2, sort_keys=True) + "\n")
        self._stamp = None

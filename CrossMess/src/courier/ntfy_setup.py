"""Write the ntfy server config Courier needs, so both installers share one tested implementation.

ntfy is the Phase A broker. Everything is private: anonymous access is denied, the gateway
holds a write-only token, devices hold a read-only token, and the web app is off. Users,
access rules and tokens are declared in server.yml, so setup needs no interactive prompts.
Rerunning keeps both tokens; the users' passwords are random and never used (tokens are).
"""

from __future__ import annotations

import secrets
import string
import subprocess
from pathlib import Path
from typing import Callable

from courier.store import write_private

GATEWAY_USER = "courier-gateway"
DEVICE_USER = "courier-device"
TOKEN_ALPHABET = string.ascii_lowercase + string.digits

Hasher = Callable[[str], str]


def new_token() -> str:
    """ntfy tokens are "tk_" plus 29 characters, 32 in all."""
    return "tk_" + "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(29))


def ntfy_hasher(ntfy_exe: str) -> Hasher:
    """bcrypt a password with `ntfy user hash`, which asks for it twice on stdin."""

    def hash_password(password: str) -> str:
        result = subprocess.run(
            [ntfy_exe, "user", "hash"], input=f"{password}\n{password}\n",
            capture_output=True, text=True, timeout=60, check=True,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("$2")]
        if not lines:
            raise RuntimeError(f"ntfy user hash printed no bcrypt hash: {result.stdout!r} {result.stderr!r}")
        return lines[-1]

    return hash_password


def _token(path: Path) -> str:
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token.startswith("tk_") and len(token) == 32:
            return token
    token = new_token()
    write_private(path, token + "\n")
    return token


def write_config(config_dir: Path, hasher: Hasher, listen: str = "127.0.0.1:2586") -> Path:
    """Write <config>/ntfy/server.yml and the two token files; return the server.yml path."""
    ntfy_dir = config_dir / "ntfy"
    ntfy_dir.mkdir(parents=True, exist_ok=True)
    gateway_token = _token(config_dir / "ntfy-gateway.token")
    device_token = _token(config_dir / "ntfy-device.token")
    gateway_hash = hasher(secrets.token_urlsafe(24))
    device_hash = hasher(secrets.token_urlsafe(24))

    # Forward slashes: ntfy reads them on Windows too, and YAML needs no escaping.
    d = ntfy_dir.resolve().as_posix()
    text = f"""\
# Written by `python -m courier ntfy-setup`. Rerun that instead of editing: it keeps the tokens.
listen-http: "{listen}"
behind-proxy: true
web-root: "disable"
enable-login: false
enable-signup: false

# Offline devices catch up on anything up to a day old, Courier's longest message lifetime.
cache-file: "{d}/cache.db"
cache-duration: "24h"

# The gateway publishes from this machine; keep ntfy's per-visitor rate limit off it.
visitor-request-limit-exempt-hosts: "127.0.0.1,::1"

auth-file: "{d}/user.db"
auth-default-access: "deny-all"
auth-users:
  - "{GATEWAY_USER}:{gateway_hash}:user"
  - "{DEVICE_USER}:{device_hash}:user"
auth-access:
  - "{GATEWAY_USER}:*:write-only"
  - "{DEVICE_USER}:*:read-only"
auth-tokens:
  - "{GATEWAY_USER}:{gateway_token}:Courier gateway"
  - "{DEVICE_USER}:{device_token}:Courier devices"
"""
    path = ntfy_dir / "server.yml"
    write_private(path, text)
    return path

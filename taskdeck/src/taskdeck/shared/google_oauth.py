"""Signing in as the user with Google's device flow ("enter this code at google.com/device").

Works without a browser inside the app or any native code: TaskDeck shows a code, the user
approves on any device, TaskDeck polls until Google hands over tokens. Needs an OAuth client
of type "TVs and Limited Input devices" (see docs/GOOGLE_DRIVE_SETUP.md).
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from . import http

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPES = f"openid email {DRIVE_FILE}"

FRIENDLY = {
    "invalid_scope": "Google doesn't allow Drive access for this kind of sign-in (invalid_scope). "
                     "See Troubleshooting in docs/GOOGLE_DRIVE_SETUP.md.",
    "invalid_client": "Google doesn't recognise the Client ID or secret, or the client isn't of type "
                      "“TVs and Limited Input devices”. Check them in Settings → Accounts.",
    "unauthorized_client": "This OAuth client can't use the device sign-in. Create one of type "
                           "“TVs and Limited Input devices”.",
    "access_denied": "Sign-in was declined on the approval page. If you didn't decline, publish the consent "
                     "screen (docs/GOOGLE_DRIVE_SETUP.md, step 2) and try again.",
    "expired_token": "The code expired before it was approved. Connect again.",
    "invalid_grant": "Google no longer accepts this sign-in (revoked or expired). Connect again.",
}


class OAuthError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(FRIENDLY.get(code) or f"Google sign-in failed: {code}" + (f" ({detail})" if detail else ""))


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    expires_at: float
    interval: float


@dataclass
class Tokens:
    access_token: str
    expires_at: float
    refresh_token: str = ""
    email: str = ""


def _post(url: str, form: dict[str, str], timeout: float = 20) -> tuple[int, dict]:
    resp = http.send("POST", url, urllib.parse.urlencode(form).encode(),
                     {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, timeout)
    data = resp.json()
    return resp.status, data if isinstance(data, dict) else {}


def _raise(data: dict, status: int) -> None:
    raise OAuthError(str(data.get("error") or f"HTTP {status}"), str(data.get("error_description") or ""))


def start(client_id: str, scope: str = SCOPES) -> DeviceCode:
    status, data = _post(DEVICE_CODE_URL, {"client_id": client_id, "scope": scope})
    if status != 200 or "device_code" not in data:
        _raise(data, status)
    return DeviceCode(data["device_code"], data["user_code"],
                      data.get("verification_url") or data.get("verification_uri") or "https://www.google.com/device",
                      time.time() + float(data.get("expires_in", 1800)), float(data.get("interval", 5)))


def email_from_id_token(id_token: str) -> str:
    """The signed-in address, read from the ID token (display only; no signature check needed)."""
    try:
        payload = id_token.split(".")[1]
        return str(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", ""))
    except (IndexError, ValueError):
        return ""


def wait(client_id: str, client_secret: str, code: DeviceCode, cancelled: Callable[[], bool] = lambda: False,
         sleep: Callable[[float], None] = time.sleep) -> Tokens | None:
    """Poll until the user approves. None if cancelled. Raises OAuthError on refusal/expiry."""
    interval = code.interval
    while time.time() < code.expires_at:
        if cancelled():
            return None
        status, data = _post(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret,
                                         "device_code": code.device_code,
                                         "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if status == 200 and "access_token" in data:
            return Tokens(data["access_token"], time.time() + float(data.get("expires_in", 3600)),
                          data.get("refresh_token", ""), email_from_id_token(data.get("id_token", "")))
        error = data.get("error")
        if error == "authorization_pending":
            pass
        elif error == "slow_down":
            interval += 5
        else:
            _raise(data, status)
        sleep(interval)
    raise OAuthError("expired_token")


def refresh(client_id: str, client_secret: str, refresh_token: str) -> Tokens:
    status, data = _post(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret,
                                     "refresh_token": refresh_token, "grant_type": "refresh_token"})
    if status != 200 or "access_token" not in data:
        _raise(data, status)
    return Tokens(data["access_token"], time.time() + float(data.get("expires_in", 3600)), refresh_token)


def revoke(token: str) -> None:
    """Best effort: Google forgets the grant (the app forgets the token either way)."""
    try:
        _post(REVOKE_URL, {"token": token})
    except http.HttpError:
        pass


class TokenSource:
    """Hands out a valid access token, refreshing it shortly before it expires (thread-safe)."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self._tokens: Tokens | None = None
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._tokens is None or self._tokens.expires_at - 60 < time.time():
                self._tokens = refresh(self.client_id, self.client_secret, self.refresh_token)
            return self._tokens.access_token

    def invalidate(self) -> None:
        with self._lock:
            self._tokens = None

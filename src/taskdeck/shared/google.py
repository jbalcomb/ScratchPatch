"""Google service-account auth and a small Sheets values client.

Pure Python (``rsa`` + ``pyasn1``) so it runs inside the Android build without
compiled wheels. Only what jobs need today: read a range, append or write rows.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .http import HttpError, get_json, post_json, request

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"


class GoogleError(Exception):
    """User-facing failure talking to Google."""


@dataclass(frozen=True)
class ServiceAccountKey:
    client_email: str
    private_key_pem: str
    private_key_id: str = ""
    token_uri: str = "https://oauth2.googleapis.com/token"

    @classmethod
    def from_json(cls, text: str) -> "ServiceAccountKey":
        """Read a service-account key, tolerating how pasted/imported text gets mangled on phones:
        extra text before or after the JSON (e.g. copied from a chat message), a byte-order mark,
        "smart" quotes, or real line breaks inside the private key."""
        info = _parse_key_json(text)
        if info.get("type") != "service_account":
            raise GoogleError("That JSON isn't a service account key (its \"type\" should be \"service_account\").")
        missing = [k for k in ("client_email", "private_key") if not info.get(k)]
        if missing:
            raise GoogleError(f"The service account key is missing: {', '.join(missing)}")
        return cls(info["client_email"], info["private_key"], info.get("private_key_id", ""),
                   info.get("token_uri") or cls.token_uri)

    def check(self) -> None:
        """Raise GoogleError if the private key can't be loaded (e.g. truncated). No network."""
        try:
            _load_private_key(self.private_key_pem)
        except Exception:
            raise GoogleError("The private key inside the service account key is damaged or incomplete. "
                              "Import the original .json file again.") from None


_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


def _parse_key_json(text: str) -> dict:
    raw = (text or "").strip().lstrip("\ufeff")
    start = raw.find("{")
    if start < 0:
        preview = raw[:30].replace("\n", " ")
        raise GoogleError(f"The service account key isn't JSON: got {len(raw)} characters starting with "
                          f"\"{preview}\" instead of {{. Import the .json key file (Import file…).")
    error = None
    for candidate in (raw[start:], raw[start:].translate(_SMART_QUOTES)):
        try:
            info, _ = json.JSONDecoder(strict=False).raw_decode(candidate)  # ignores trailing text
        except json.JSONDecodeError as exc:
            error = exc
            continue
        if isinstance(info, dict):
            return info
    raise GoogleError(f"The service account key isn't valid JSON ({error.msg}; character {error.pos + start} "
                      f"of {len(raw)}). It may be cut off. Import the original .json key file (Import file…).")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_private_key(pem: str):
    """Load an RSA key from PKCS#8 (what Google issues) or PKCS#1 PEM."""
    import rsa
    from pyasn1.codec.der import decoder

    if "BEGIN RSA PRIVATE KEY" in pem:
        return rsa.PrivateKey.load_pkcs1(pem.encode("ascii"))
    body = "".join(line for line in pem.strip().splitlines() if "-----" not in line)
    der = base64.b64decode(body)
    # PrivateKeyInfo ::= SEQUENCE { version INTEGER, algorithm AlgorithmIdentifier, privateKey OCTET STRING }
    info, _ = decoder.decode(der)
    return rsa.PrivateKey.load_pkcs1(bytes(info[2]), format="DER")


def make_assertion(key: ServiceAccountKey, scope: str, now: float | None = None) -> str:
    """A signed RS256 JWT for the OAuth 2.0 JWT-bearer grant."""
    import rsa

    now = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if key.private_key_id:
        header["kid"] = key.private_key_id
    claims = {"iss": key.client_email, "scope": scope, "aud": key.token_uri, "iat": now, "exp": now + 3600}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    try:
        private_key = _load_private_key(key.private_key_pem)
    except Exception:
        raise GoogleError("Couldn't read the private key in the service account key file.") from None
    signature = rsa.sign(signing_input.encode("ascii"), private_key, "SHA-256")
    return f"{signing_input}.{_b64url(signature)}"


class ServiceAccountCredentials:
    """Fetches and caches an access token for a service account."""

    def __init__(self, key: ServiceAccountKey, scope: str = SHEETS_SCOPE):
        self.key = key
        self.scope = scope
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            try:
                data = request("POST", self.key.token_uri, form={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": make_assertion(self.key, self.scope),
                }, timeout=30)
            except HttpError as exc:
                raise GoogleError(f"Google sign-in failed ({exc}). Check the service account key.") from None
            if not isinstance(data, dict) or "access_token" not in data:
                raise GoogleError("Google sign-in returned no access token.")
            self._token = data["access_token"]
            self._expires = time.time() + float(data.get("expires_in", 3600))
            return self._token


def a1_range(tab: str, cells: str) -> str:
    """Quote a sheet name for A1 notation: Sheet1 -> 'Sheet1'!A1."""
    return "'" + tab.replace("'", "''") + "'!" + cells


class SheetsClient:
    def __init__(self, credentials: ServiceAccountCredentials, spreadsheet_id: str, timeout: float = 30):
        self.credentials = credentials
        self.spreadsheet_id = spreadsheet_id
        self.timeout = timeout

    def _url(self, rng: str, suffix: str = "", **params: str) -> str:
        url = f"{SHEETS_API}/{urllib.parse.quote(self.spreadsheet_id)}/values/{urllib.parse.quote(rng, safe='')}{suffix}"
        return url + ("?" + urllib.parse.urlencode(params) if params else "")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.token()}"}

    def get_values(self, rng: str) -> list[list[Any]]:
        try:
            data = get_json(self._url(rng), headers=self._headers(), timeout=self.timeout)
        except HttpError as exc:
            raise self._explain(exc) from None
        return data.get("values", []) if isinstance(data, dict) else []

    def append_row(self, rng: str, row: list[Any]) -> None:
        url = self._url(rng, ":append", valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS")
        try:
            post_json(url, {"values": [row]}, headers=self._headers(), timeout=self.timeout)
        except HttpError as exc:
            raise self._explain(exc) from None

    def write_row(self, rng: str, row: list[Any]) -> None:
        """Overwrite exactly the cells in ``rng`` with one row (no table guessing, unlike append)."""
        url = self._url(rng, valueInputOption="USER_ENTERED")
        try:
            request("PUT", url, json_body={"range": rng, "majorDimension": "ROWS", "values": [row]},
                    headers=self._headers(), timeout=self.timeout)
        except HttpError as exc:
            raise self._explain(exc) from None

    def _explain(self, exc: HttpError) -> GoogleError:
        if exc.status == 403:
            return GoogleError(f"Google Sheets refused access. Share the sheet with "
                               f"{self.credentials.key.client_email} as Editor. ({exc})")
        if exc.status == 404:
            return GoogleError(f"Spreadsheet not found. Check the Sheet ID. ({exc})")
        if exc.status == 400 and "parse range" in exc.body.lower():
            return GoogleError(f"Tab not found in the spreadsheet. Check the tab name. ({exc})")
        return GoogleError(f"Google Sheets: {exc}")

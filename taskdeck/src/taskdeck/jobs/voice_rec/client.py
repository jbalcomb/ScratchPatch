"""Talks to the voice-rec server on the AI Workstation (see voice-rec/README.md at the repo root).

    PUT  /api/recordings/<id>          audio bytes; X-Recorded-At / X-Duration headers. Idempotent.
    GET  /api/recordings/<id>          status, transcript, timestamps, title, category, tags
    POST /api/recordings/<id>/archive  ask the workstation to file it (Postgres, Obsidian, knowledge graph)
    GET  /health
"""

from __future__ import annotations

import hashlib
from typing import Any

from taskdeck.core import StepError
from taskdeck.shared import http, tailscale

from .library import Recording, mime_of


class ServerError(StepError):
    """The server answered with an error (``status``), or wasn't reachable (``status`` None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def unreachable(self) -> bool:
        return self.status is None


def base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


class Client:
    def __init__(self, url: str, token: str = "", timeout: float = 120):
        self.url = base_url(url)
        self.token = (token or "").strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", **(extra or {})}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _send(self, method: str, path: str, body: bytes | None = None,
              headers: dict[str, str] | None = None, timeout: float | None = None) -> http.Response:
        if not self.configured:
            raise ServerError("Set the workstation address in this job's settings.", 0)
        url = self.url + path
        try:
            return http.send(method, url, body=body, headers=self._headers(headers), timeout=timeout or self.timeout)
        except http.HttpError as exc:
            why = tailscale.why_unreachable(url)
            raise ServerError(f"The AI Workstation isn't reachable ({why or exc})", None) from None

    @staticmethod
    def _check(resp: http.Response) -> dict[str, Any]:
        data = resp.json()
        if resp.ok:
            return data if isinstance(data, dict) else {}
        detail = data.get("error") if isinstance(data, dict) else ""
        if resp.status == 401:
            detail = "the access token is missing or wrong; check it in this job's settings"
        raise ServerError(f"Workstation: HTTP {resp.status}" + (f": {detail}" if detail else ""), resp.status)

    def health(self) -> dict[str, Any]:
        return self._check(self._send("GET", "/health", timeout=10))

    def upload(self, rec: Recording, audio: bytes) -> dict[str, Any]:
        headers = {
            "Content-Type": mime_of(rec.file),
            "Content-Length": str(len(audio)),
            "X-Recorded-At": rec.recorded_at,
            "X-Duration": str(rec.duration),
            "X-Sha256": hashlib.sha256(audio).hexdigest(),
            "X-Mobile-Data": ",".join(s for s, ok in rec.mobile_data.items() if ok),
        }
        return self._check(self._send("PUT", f"/api/recordings/{rec.id}", body=audio, headers=headers))

    def get(self, rec_id: str) -> dict[str, Any] | None:
        """None when the workstation doesn't have it (e.g. its database was reset)."""
        resp = self._send("GET", f"/api/recordings/{rec_id}", timeout=30)
        if resp.status == 404:
            return None
        return self._check(resp)

    def archive(self, rec_id: str, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        import json

        body = json.dumps(changes or {}).encode("utf-8")
        return self._check(self._send("POST", f"/api/recordings/{rec_id}/archive", body=body,
                                      headers={"Content-Type": "application/json"}, timeout=30))


def test_connection(url: str, token: str) -> str:
    client = Client(url, token)
    if not client.configured:
        return "Set the workstation address first."
    try:
        info = client.health()
    except ServerError as exc:
        return f"✗ {exc}"
    parts = [f"✓ Connected to {client.url}"]
    if info.get("transcriber"):
        parts.append(f"transcriber: {info['transcriber']}")
    if "queued" in info:
        parts.append(f"{info['queued']} waiting to be transcribed")
    return " · ".join(parts)

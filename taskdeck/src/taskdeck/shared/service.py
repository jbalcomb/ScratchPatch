"""A JSON service on the AI Workstation, reached over Tailscale at the address in a job setting.

Shared by the jobs that talk to such services (Photo OCR, PDF Parser): the request, with the service's
``{"error": ...}`` or a network failure turned into a StepError that says why (e.g. the phone isn't on
Tailscale); the checks of the address as entered; and GET /api/health.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from taskdeck.core import Problem, StepError

from . import http, tailscale


class Service:
    def __init__(self, name: str, example_url: str, setting: str = "service_url"):
        self.name = name                # as the user knows it: "photo OCR service"
        self.example_url = example_url  # shown when the address is malformed
        self.setting = setting          # the job setting that holds the address

    def base_url(self, settings: dict[str, Any]) -> str:
        return str(settings.get(self.setting) or "").strip().rstrip("/")

    def _error(self, exc: http.HttpError, url: str) -> str:
        if exc.status is None:
            why = tailscale.why_unreachable(url)
            return f"Can't reach the {self.name}: {why}." if why else str(exc)
        try:
            error = json.loads(exc.body).get("error")
        except (ValueError, AttributeError):
            error = None
        return str(error) if error else f"the {self.name} answered: {exc}"

    def call(self, settings: dict[str, Any], method: str, path: str, *, params: dict[str, Any] | None = None,
             json_body: Any = None, fields: list[tuple[str, str]] = (), files: list[tuple[str, str, bytes]] = (),
             timeout: float = 30) -> dict[str, Any]:
        """One request; ``files`` makes it a multipart upload. -> the JSON object the service answered."""
        url = self.base_url(settings) + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        try:
            if files:
                data = http.post_multipart(url, list(fields), list(files), timeout=timeout)
            else:
                data = http.request(method, url, json_body=json_body, timeout=timeout)
        except http.HttpError as exc:
            raise StepError(self._error(exc, url)) from None
        if not isinstance(data, dict):
            raise StepError(f"{self.base_url(settings)} didn't answer like the {self.name}. Check the service URL.")
        return data

    def url_problems(self, settings: dict[str, Any], phone_itself: bool = True) -> list[Problem]:
        """The address as entered: missing, without http(s)://, or (``phone_itself``) pointing at the phone."""
        url = self.base_url(settings)
        if not url:
            return [Problem(f"Set the {self.name} URL in this job's settings (the tune icon at the top).")]
        if not url.startswith(("http://", "https://")):
            return [Problem(f"the {self.name} URL must start with https:// (e.g. {self.example_url}).")]
        if phone_itself and tailscale.host_of(url) in ("localhost", "127.0.0.1"):
            return [Problem(f"the {self.name} URL points at the phone itself (localhost). "
                            "Use the workstation's Tailscale address.")]
        return []

    def check_url(self, settings: dict[str, Any]) -> None:
        """For a VALIDATE step: StepError when the address can't work (localhost is only warned about when the
        job opens)."""
        problems = self.url_problems(settings, phone_itself=False)
        if problems:
            raise StepError(problems[0].message)

    def health(self, settings: dict[str, Any], timeout: float = 8) -> tuple[dict[str, Any] | None, list[Problem]]:
        """GET /api/health. -> (its answer, []) or (None, [the problem]) when it can't be reached or isn't ok."""
        try:
            answer = self.call(settings, "GET", "/api/health", timeout=timeout)
        except StepError as exc:
            return None, [Problem(str(exc), fix="recheck")]
        if answer.get("ok") is not True:
            return None, [Problem(f"the {self.name} isn't ready (its health check failed).", fix="recheck")]
        return answer, []

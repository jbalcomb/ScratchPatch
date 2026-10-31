"""The Receipts service's HTTP API (receipts/API.md), its settings and readiness checks, shared by the
Receipts jobs. Amounts from the service are integer cents."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from taskdeck.core import WIFI_ONLY, Field, JobPlugin, Problem, Stage, Step, StepError, step
from taskdeck.shared import http, tailscale

SETTINGS_ID = "receipts_capture"  # the job that stores the settings all Receipts jobs use
CATEGORY = "Receipts"
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

SETTINGS = (
    Field.text("service_url", "Receipts service URL", required=True,
               help="The workstation's Tailscale Serve address, e.g. https://ai-workstation.tail1234.ts.net:8443"),
    WIFI_ONLY,
)

# Receipt fields as the service names them (``suspect``), as the user knows them.
FIELD_NAMES = {"merchant": "store", "purchase_date": "date", "total": "total", "tax": "tax", "items": "items"}


def base_url(settings: dict[str, Any]) -> str:
    return str(settings.get("service_url") or "").strip().rstrip("/")


def page_url(settings: dict[str, Any], path: str) -> str:
    """A service page or a ``url`` the API returns (``/review/17``) as a full address."""
    return base_url(settings) + "/" + path.lstrip("/")


def dollars(cents: Any) -> str:
    if not isinstance(cents, int) or isinstance(cents, bool):
        return "—"
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def _error_message(exc: http.HttpError, url: str) -> str:
    if exc.status is None:
        why = tailscale.why_unreachable(url)
        return f"Can't reach the receipts service: {why}." if why else str(exc)
    try:
        error = json.loads(exc.body).get("error")
    except (ValueError, AttributeError):
        error = None
    return str(error) if error else f"The receipts service answered: {exc}"


def call(settings: dict[str, Any], method: str, path: str, *, params: dict[str, Any] | None = None,
         json_body: Any = None, fields: list[tuple[str, str]] = (), files: list[tuple[str, str, bytes]] = (),
         timeout: float = 30) -> dict[str, Any]:
    """One API request. The service's ``{"error": ...}`` (or a network failure) becomes a StepError."""
    url = base_url(settings) + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    try:
        if files:
            data = http.post_multipart(url, fields, files, timeout=timeout)
        else:
            data = http.request(method, url, json_body=json_body, timeout=timeout)
    except http.HttpError as exc:
        raise StepError(_error_message(exc, url)) from None
    if not isinstance(data, dict):
        raise StepError(f"{base_url(settings)} didn't answer like the receipts service. Check the service URL.")
    return data


def read_files(paths: list[str], extensions: tuple[str, ...], field: str = "photo") -> list[tuple[str, str, bytes]]:
    """The files to upload, in order, as multipart parts. StepError names a missing or unsupported file."""
    out = []
    for raw in paths:
        path = Path(raw).expanduser()
        if extensions and path.suffix.lower() not in extensions:
            raise StepError(f"{path.name} isn't a {', '.join(extensions)} file.")
        try:
            out.append((field, path.name, path.read_bytes()))
        except OSError as exc:
            raise StepError(f"Can't read {path}: {exc.strerror or exc}") from None
    return out


def fetch(key: str, path: str, label: str) -> Step:
    """An EXECUTE step that GETs ``path`` and stores the answer as ``ctx[key]``."""

    def run(ctx) -> None:
        ctx[key] = call(ctx.settings, "GET", path)

    return Step(name=label, fn=run, stage=Stage.EXECUTE)


@step(Stage.VALIDATE, name="check service URL")
def service_configured(ctx):
    problems = url_problems(ctx.settings, phone_itself=False)  # (check_ready warns about localhost)
    if problems:
        raise StepError(problems[0].message)


def url_problems(settings: dict[str, Any], phone_itself: bool = True) -> list[Problem]:
    url = base_url(settings)
    if not url:
        return [Problem("Set the receipts service URL in this job's settings (the tune icon at the top).")]
    if not url.startswith(("http://", "https://")):
        return [Problem("The receipts service URL must start with https:// "
                        "(e.g. https://ai-workstation.tail1234.ts.net:8443).")]
    if phone_itself and tailscale.host_of(url) in ("localhost", "127.0.0.1"):
        return [Problem("The receipts service URL points at the phone itself (localhost). "
                        "Use the workstation's Tailscale Serve address.")]
    return []


def health_problems(settings: dict[str, Any], uses_immich: bool = False) -> list[Problem]:
    """GET /api/health: is the service there, and (for photo uploads) can it reach Immich?"""
    try:
        health = call(settings, "GET", "/api/health", timeout=8)
    except StepError as exc:
        return [Problem(str(exc), fix="recheck")]
    if health.get("ok") is not True:
        return [Problem("The receipts service isn't ready (its health check failed).", fix="recheck")]
    if uses_immich and health.get("immich") is False:
        return [Problem("The receipts service can't reach Immich right now.", blocking=False, fix="recheck")]
    return []


class ReceiptsJob(JobPlugin):
    """What every Receipts job shares: the category, the settings and the readiness checks."""

    category = CATEGORY
    settings = SETTINGS
    settings_from = SETTINGS_ID
    uses_immich = False  # uploads photos, which the service stores in Immich

    def check_ready(self, settings):
        return super().check_ready(settings) + [p for p in url_problems(settings) if base_url(settings)]

    def check_ready_online(self, settings):
        return health_problems(settings, self.uses_immich)

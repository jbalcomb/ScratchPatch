"""The photo OCR service's HTTP API (photo-ocr/API.md), its settings and readiness checks, shared by the
Photo OCR jobs. Amounts from the service are integer cents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from taskdeck.core import WIFI_ONLY, Field, JobPlugin, Problem, Stage, Step, StepError, step
from taskdeck.shared import service

SERVICE = service.Service("photo OCR service", "https://ai-workstation.tail1234.ts.net:8443")
SETTINGS_ID = "photo_ocr_capture"  # the job that stores the settings all Photo OCR jobs use
CATEGORY = "Photo OCR"
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

SETTINGS = (
    Field.text("service_url", "Photo OCR service URL", required=True,
               help="The workstation's Tailscale Serve address, e.g. https://<workstation>.<tailnet>.ts.net:8444"),
    WIFI_ONLY,
)

# Receipt fields as the service names them (``suspect``), as the user knows them.
FIELD_NAMES = {"merchant": "store", "purchase_date": "date", "total": "total", "tax": "tax", "items": "items"}


def base_url(settings: dict[str, Any]) -> str:
    return SERVICE.base_url(settings)


def page_url(settings: dict[str, Any], path: str) -> str:
    """A service page or a ``url`` the API returns (``/review/17``) as a full address."""
    return base_url(settings) + "/" + path.lstrip("/")


def dollars(cents: Any) -> str:
    if not isinstance(cents, int) or isinstance(cents, bool):
        return "—"
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def call(settings: dict[str, Any], method: str, path: str, **kw: Any) -> dict[str, Any]:
    """One API request. The service's ``{"error": ...}`` (or a network failure) becomes a StepError."""
    return SERVICE.call(settings, method, path, **kw)


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
    SERVICE.check_url(ctx.settings)


def url_problems(settings: dict[str, Any], phone_itself: bool = True) -> list[Problem]:
    return SERVICE.url_problems(settings, phone_itself)


def health_problems(settings: dict[str, Any], uses_immich: bool = False) -> list[Problem]:
    """GET /api/health: is the service there, and (for photo uploads) can it reach Immich?"""
    health, problems = SERVICE.health(settings)
    if problems:
        return problems
    if uses_immich and health.get("immich") is False:
        return [Problem("the photo OCR service can't reach Immich right now.", blocking=False, fix="recheck")]
    return []


class PhotoOcrJob(JobPlugin):
    """What every Photo OCR job shares: the category, the settings and the readiness checks."""

    category = CATEGORY
    settings = SETTINGS
    settings_from = SETTINGS_ID
    uses_immich = False  # uploads photos, which the service stores in Immich

    def check_ready(self, settings):
        return super().check_ready(settings) + [p for p in url_problems(settings) if base_url(settings)]

    def check_ready_online(self, settings):
        return health_problems(settings, self.uses_immich)

"""Calls to Home Runner (home-runner/ in this repo), the job service on the workstation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from taskdeck.core import StepError
from taskdeck.shared.http import HttpError, request

ACTIVE = ("queued", "running")


def call(settings: dict[str, Any], method: str, path: str, body: Any = None, timeout: float = 20) -> Any:
    base = (settings.get("runner_url") or "").strip().rstrip("/")
    if not base:
        raise StepError("Set the Runner URL in Home Jobs settings (the tune icon at the top).")
    headers = {"Authorization": f"Bearer {(settings.get('runner_key') or '').strip()}"}
    try:
        return request(method, base + path, json_body=body, headers=headers, timeout=timeout)
    except HttpError as exc:
        if exc.status == 401:
            raise StepError("The workstation refused the runner key. Show it there with "
                            "`python -m home_runner key` and paste it into Home Jobs settings.") from None
        if exc.status is None:
            raise StepError(f"{exc}. Is the workstation on, and Tailscale connected on this phone?") from None
        raise StepError(str(exc)) from None


def run_quick(settings: dict[str, Any], job: str, params: dict[str, str] | None = None) -> Any:
    """Run a quick (read-only) job and return its parsed output."""
    run = call(settings, "POST", "/runs", {"job": job, "params": params or {}}, timeout=70)
    if run.get("status") != "succeeded":
        detail = run.get("last_line") or f"exit code {run.get('exit_code')}"
        raise StepError(f"{job} failed on the workstation: {detail}")
    return run.get("result", run.get("output"))


def start(settings: dict[str, Any], job: str, params: dict[str, str]) -> dict[str, Any]:
    return call(settings, "POST", "/runs", {"job": job, "params": params})


def test_connection(settings: dict[str, Any]) -> str:
    """Settings action: is the runner there, and does it accept the key?"""
    try:
        jobs = call(settings, "GET", "/jobs", timeout=10)
    except StepError as exc:
        return str(exc)
    return f"Connected. {len(jobs)} jobs available: " + ", ".join(j["title"] for j in jobs)


def ago(iso: str | None, now: datetime | None = None) -> str:
    if not iso:
        return ""
    seconds = int(((now or datetime.now(timezone.utc)) - datetime.fromisoformat(iso)).total_seconds())
    return duration(seconds) + " ago" if seconds >= 60 else "just now"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds >= 2 * 86400:
        return f"{seconds // 86400} days"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"

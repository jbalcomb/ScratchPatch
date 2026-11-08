"""PDF Parser: send a PDF to the PDF parser service on the AI Workstation (pdf-parser/README.md), wait while it
parses, and show what it found with links to the bundle. The heavy work runs there, not on the phone.
See docs/jobs/pdf_parser.md."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from taskdeck.core import WIFI_ONLY, Field, JobPlugin, Pipeline, Problem, Result, Stage, StepError, step
from taskdeck.shared import http, tailscale

DEFAULT_URL = "https://pdf-parser.tailc54221.ts.net"  # the Tailscale Service "pdf-parser" on the AI Workstation
MAX_MB = 200
UPLOAD_TIMEOUT = 300
POLL_SECONDS = 3
WAIT_SECONDS = 20 * 60  # then the result says it's still parsing and links the job page


def base_url(settings: dict[str, Any]) -> str:
    return str(settings.get("service_url") or "").strip().rstrip("/")


def _error(exc: http.HttpError, url: str) -> str:
    if exc.status is None:
        why = tailscale.why_unreachable(url)
        return f"Can't reach the PDF parser service: {why}." if why else str(exc)
    try:
        error = json.loads(exc.body).get("error")
    except (ValueError, AttributeError):
        error = None
    return str(error) if error else f"the PDF parser service answered: {exc}"


def call(settings: dict[str, Any], method: str, path: str, *, files=(), timeout: float = 30) -> dict[str, Any]:
    url = base_url(settings) + path
    try:
        data = (http.post_multipart(url, [], list(files), timeout=timeout) if files
                else http.request(method, url, timeout=timeout))
    except http.HttpError as exc:
        raise StepError(_error(exc, url)) from None
    if not isinstance(data, dict):
        raise StepError(f"{base_url(settings)} didn't answer like the PDF parser service. Check the service URL.")
    return data


@step(Stage.VALIDATE, name="check service URL")
def service_configured(ctx):
    url = base_url(ctx.settings)
    if not url:
        raise StepError("Set the PDF parser service URL in this job's settings (the tune icon at the top).")
    if not url.startswith(("http://", "https://")):
        raise StepError(f"the PDF parser service URL must start with https:// (e.g. {DEFAULT_URL}).")


@step(Stage.VALIDATE)
def read_pdf(ctx):
    path = Path(str(ctx["pdf"])).expanduser()
    if path.suffix.lower() != ".pdf":
        raise StepError(f"{path.name} isn't a .pdf file.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StepError(f"Can't read {path}: {exc.strerror or exc}") from None
    if not data.startswith(b"%PDF"):
        raise StepError(f"{path.name} doesn't look like a PDF.")
    if len(data) > MAX_MB * 2**20:
        raise StepError(f"{path.name} is {len(data) / 2**20:.0f} MB; the service takes at most {MAX_MB} MB.")
    ctx["upload"] = [("pdf", path.name, data)]


@step(Stage.EXECUTE, name="send PDF")
def send_pdf(ctx):
    ctx["job"] = call(ctx.settings, "POST", "/api/jobs", files=ctx["upload"], timeout=UPLOAD_TIMEOUT)


@step(Stage.EXECUTE, name="wait for parsing")
def wait_for_parsing(ctx):
    """Ask every POLL_SECONDS until the job is done or failed, up to WAIT_SECONDS; Cancel stops the wait (the
    service carries on)."""
    job = ctx["job"]
    deadline = time.monotonic() + WAIT_SECONDS
    while job.get("status") in ("queued", "running") and time.monotonic() < deadline:
        pause_until = time.monotonic() + POLL_SECONDS
        while time.monotonic() < pause_until:
            ctx.check_cancelled()
            time.sleep(0.2)
        job = call(ctx.settings, "GET", f"/api/jobs/{job['id']}")
    ctx["job"] = job


def _n(summary: dict, key: str) -> int:
    value = summary.get(key)
    return value if isinstance(value, int) else 0


@step(Stage.PRESENT)
def show_result(ctx):
    job, settings = ctx["job"], ctx.settings
    name = job.get("file") or "PDF"
    folder = ("Output folder", base_url(settings) + str(job.get("url") or ""))
    if job.get("status") == "failed":
        raise StepError(f"{name} couldn't be parsed: {job.get('error') or 'no reason given'}")
    if job.get("status") != "done":
        where = f" ({job['step']})" if job.get("step") else ""
        ctx.result = Result(title=f"{name}: still parsing{where}", rows=[("Job", str(job.get("id"))), folder],
                            text="The workstation is still working on it; the output folder fills in when it's done.")
        return
    s = job.get("summary") or {}
    pages, scanned = _n(s, "pages"), _n(s, "scanned_pages")
    kept, review = _n(s, "images_kept"), _n(s, "images_needs_review")
    rows = [("Pages", f"{pages}" + (f" ({scanned} scanned, OCR'd)" if scanned else "")),
            ("Text blocks", str(_n(s, "text_blocks"))),
            ("Tables", str(_n(s, "tables"))),
            ("Images kept", str(kept)),
            ("Needs review", str(review))]
    extra = [f"{_n(s, k)} {label}" for k, label in (("images_duplicate", "duplicate"), ("images_merged", "merged"),
                                                    ("images_small", "icons")) if _n(s, k)]
    if extra:
        rows.append(("Also", ", ".join(extra)))
    vision = s.get("vision") or {}
    if vision.get("requests"):
        rows.append(("Vision checks", f"{vision['requests']}" + (f" ({vision['failed']} failed)" if vision.get("failed") else "")))
    rows += [folder, ("Download (.zip)", base_url(settings) + str(job.get("zip") or ""))]
    warnings = []
    if review:
        warnings.append(f"{review} image{'s' if review != 1 else ''} need{'s' if review == 1 else ''} a look: "
                        f"see needs_review in manifest.json and the tries in images/attempts/.")
    if vision.get("stopped") and vision.get("stopped") != "vision disabled":
        warnings.append(f"Images weren't checked by the vision model: {vision['stopped']}.")
    ctx.result = Result(title=f"{name}: {pages} pages, {_n(s, 'tables')} tables, {kept} images kept"
                              + (f", {review} to review" if review else ""),
                        rows=rows, warning=" ".join(warnings) or None)


class PdfParser(JobPlugin):
    id = "pdf_parser"
    title = "PDF Parser"
    description = ("Send a PDF to the AI Workstation, which takes out its text, tables and images (with OCR and an "
                   "image check), and get a folder with all of it and a manifest.")
    category = "Tools"
    icon = "PICTURE_AS_PDF"
    tags = ("pdf", "parse", "extract", "text", "tables", "images", "ocr", "tesseract", "manifest", "document")
    fields = (Field.file("pdf", "PDF", required=True, extensions=(".pdf",),
                         help="Browse to the PDF. It's parsed on the workstation; the phone only sends it."),)
    settings = (
        Field.text("service_url", "PDF parser service URL", required=True, default=DEFAULT_URL,
                   help="The workstation's Tailscale Service address (pdf-parser/README.md)."),
        WIFI_ONLY,
    )
    run_label = "Parse PDF"

    def clean_settings(self, values):
        url = str(values.get("service_url") or "").strip().rstrip("/")
        if url and "://" not in url:
            url = "https://" + url
        return {**values, "service_url": url}

    def check_ready(self, settings):
        problems = super().check_ready(settings)
        url = base_url(settings)
        if url and tailscale.host_of(url) in ("localhost", "127.0.0.1"):
            problems.append(Problem("the PDF parser service URL points at the phone itself (localhost). "
                                    "Use the workstation's Tailscale address."))
        return problems

    def check_ready_online(self, settings):
        try:
            health = call(settings, "GET", "/api/health", timeout=8)
        except StepError as exc:
            return [Problem(str(exc), fix="recheck")]
        if health.get("ok") is not True:
            return [Problem("the PDF parser service isn't ready (its health check failed).", fix="recheck")]
        problems = []
        vision = health.get("vision") or {}
        if vision.get("state") == "unavailable":
            problems.append(Problem(f"The vision model isn't available ({vision.get('message') or 'no answer'}); "
                                    f"PDFs are still parsed, and images it would check go to review.",
                                    blocking=False, fix="recheck"))
        if health.get("tesseract") is False:
            problems.append(Problem("Tesseract isn't installed on the service: no OCR of scanned pages or images.",
                                    blocking=False, fix="recheck"))
        return problems

    def build_pipeline(self):
        return Pipeline([service_configured, read_pdf, send_pdf, wait_for_parsing, show_result])


PLUGIN = PdfParser()

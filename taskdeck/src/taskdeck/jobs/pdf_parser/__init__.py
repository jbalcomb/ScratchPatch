"""PDF Parser: send a PDF to the PDF parser service on the AI Workstation (pdf-parser/README.md), wait while it
parses, and show what it found with links to the bundle. The heavy work runs there, not on the phone.
See docs/jobs/pdf_parser.md."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from taskdeck.core import WIFI_ONLY, Field, JobPlugin, Pipeline, Problem, Result, Stage, StepError, step
from taskdeck.shared import service, steps

DEFAULT_URL = "https://pdf-parser.tailc54221.ts.net"  # the Tailscale Service "pdf-parser" on the AI Workstation
MAX_MB = 200
UPLOAD_TIMEOUT = 300
POLL_SECONDS = 3
WAIT_SECONDS = 20 * 60  # then the result says it's still parsing and links the job page


SERVICE = service.Service("PDF parser service", DEFAULT_URL)
base_url = SERVICE.base_url
call = SERVICE.call


@step(Stage.VALIDATE, name="check service URL")
def service_configured(ctx):
    SERVICE.check_url(ctx.settings)


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
    ctx["job"] = steps.wait_for_job(ctx, ctx["job"], lambda job: call(ctx.settings, "GET", f"/api/jobs/{job['id']}"),
                                    wait_seconds=WAIT_SECONDS, poll_seconds=POLL_SECONDS, label="Workstation")


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
    rows += [folder, ("Review", base_url(settings) + str(job.get("url") or "") + "review/"),
             ("Download (.zip)", base_url(settings) + str(job.get("zip") or ""))]
    warnings = []
    if review:
        warnings.append(f"{review} image{'s' if review != 1 else ''} need{'s' if review == 1 else ''} a look: "
                        f"see needs_review in manifest.json and the tries in images/attempts/.")
    if vision.get("stopped") and vision.get("stopped") != "vision disabled":
        warnings.append(f"Images weren't checked by the vision model: {vision['stopped']}.")
    ctx.result = Result(title=f"{name}: {pages} pages, {_n(s, 'tables')} tables, {kept} images kept"
                              + (f", {review} to review" if review else ""),
                        rows=rows, warning=" ".join(warnings) or None)


RECENT = 10  # jobs listed by PDF Parser jobs


def _when(iso: str | None) -> str:
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return ""
    return f"{d.day} {d:%b %H:%M}"


def job_line(job: dict) -> str:
    """What a job found, or where it is."""
    s = job.get("summary") or {}
    if job.get("status") == "done":
        review = _n(s, "images_needs_review")
        return (f"{_n(s, 'pages')} pages · {_n(s, 'tables')} tables · {_n(s, 'images_kept')} images kept"
                + (f" · {review} to review" if review else ""))
    if job.get("status") == "failed":
        return f"failed: {job.get('error') or 'no reason given'}"
    return str(job.get("status") or "") + (f": {job['step']}" if job.get("step") else "")


@step(Stage.EXECUTE, name="get jobs")
def get_jobs(ctx):
    ctx["jobs"] = call(ctx.settings, "GET", "/api/jobs").get("jobs") or []


@step(Stage.PRESENT)
def show_jobs(ctx):
    base, jobs = base_url(ctx.settings), ctx["jobs"][:RECENT]
    rows = []
    for job in jobs:
        lines = [job_line(job)]
        if job.get("status") == "done":
            lines += [f"Files: {base}{job.get('url')}", f"Review: {base}{job.get('url')}review/",
                      f"Download (.zip): {base}{job.get('zip')}"]
        rows.append((f"{job.get('file')}  {_when(job.get('created'))}".strip(), "\n".join(lines)))
    rows.append(("All jobs", base + "/"))
    ctx.result = Result(title=f"Latest {len(jobs)} PDF Parser job{'s' if len(jobs) != 1 else ''}" if jobs
                        else "No PDFs parsed yet", rows=rows)


class PdfParserJob(JobPlugin):
    """What the PDF Parser jobs share: the category, the service setting and the readiness checks."""

    category = "Tools"
    settings = (
        Field.text("service_url", "PDF parser service URL", required=True, default=DEFAULT_URL,
                   help="The workstation's Tailscale Service address (pdf-parser/README.md)."),
        WIFI_ONLY,
    )
    settings_from = "pdf_parser"

    def clean_settings(self, values):
        url = str(values.get("service_url") or "").strip().rstrip("/")
        if url and "://" not in url:
            url = "https://" + url
        return {**values, "service_url": url}

    def check_ready(self, settings):
        return super().check_ready(settings) + (SERVICE.url_problems(settings) if base_url(settings) else [])

    def check_ready_online(self, settings):
        health, problems = SERVICE.health(settings)
        if problems:
            return problems
        vision = health.get("vision") or {}
        if vision.get("state") == "unavailable":
            problems.append(Problem(f"The vision model isn't available ({vision.get('message') or 'no answer'}); "
                                    f"PDFs are still parsed, and images it would check go to review.",
                                    blocking=False, fix="recheck"))
        if health.get("tesseract") is False:
            problems.append(Problem("Tesseract isn't installed on the service: no OCR of scanned pages or images.",
                                    blocking=False, fix="recheck"))
        return problems


class PdfParser(PdfParserJob):
    id = "pdf_parser"
    title = "PDF Parser"
    description = ("Send a PDF to the AI Workstation, which takes out its text, tables and images (with OCR and an "
                   "image check), and get a folder with all of it and a manifest.")
    icon = "PICTURE_AS_PDF"
    tags = ("pdf", "parse", "extract", "text", "tables", "images", "ocr", "tesseract", "manifest", "document")
    fields = (Field.file("pdf", "PDF", required=True, extensions=(".pdf",),
                         help="Browse to the PDF. It's parsed on the workstation; the phone only sends it."),)
    run_label = "Parse PDF"

    def build_pipeline(self):
        return Pipeline([service_configured, read_pdf, send_pdf, wait_for_parsing, show_result])


class PdfParserJobs(PdfParserJob):
    id = "pdf_parser_jobs"
    title = "PDF Parser jobs"
    description = ("The latest PDFs parsed on the AI Workstation, with links to their files, their review page and "
                   "the .zip: to get back to a result you left.")
    icon = "HISTORY"
    tags = ("pdf", "parse", "jobs", "history", "results", "download", "zip", "review")
    run_label = "Show jobs"

    def build_pipeline(self):
        return Pipeline([service_configured, get_jobs, show_jobs])


PLUGIN = PdfParser()
JOBS = PdfParserJobs()
PLUGINS = (PLUGIN, JOBS)

"""Save a web page, as it looks, onto the storage machine: queued on that machine's chromers daemon
(github.com/jbalcomb/chromers-odyssey), whose own browser opens the page in a background tab and saves
it as MHTML (one file Chrome/Edge open exactly as the page was) and/or PDF, into a folder inside one of
the machine's save roots. The save finishes on the machine even if the phone goes offline."""

from __future__ import annotations

import json
import time
from typing import Any

from taskdeck.core import WIFI_ONLY, Field, JobPlugin, Pipeline, Problem, Result, Stage, StepError, step
from taskdeck.shared import http, steps, tailscale

WAIT_SECONDS = 120  # how long the job follows a save before leaving it to finish on the machine
POLL_SECONDS = 2

SETTINGS = (
    Field.text("daemon_url", "Storage machine", required=True,
               help="The chromers daemon on it: http://<its Tailscale address>:7700"),
    Field.text("token", "Token", secret=True, required=True,
               help="From that machine: scripts/token.sh new taskdeck (or ~/.config/chromers/token)"),
    Field.text("root", "Save root", default="web",
               help="A root name from the machine's CHROMERS_SAVE_ROOTS (default: web = ~/Archive/web)"),
    WIFI_ONLY,
)


def base_url(settings: dict[str, Any]) -> str:
    return str(settings.get("daemon_url") or "").strip().rstrip("/")


def call(settings: dict[str, Any], command: str, args: dict[str, Any] | None = None, timeout: float = 20) -> Any:
    """One chromers command (POST /api/commands/<name>); its ``{"ok": false, "error": ...}`` -> StepError."""
    url = f"{base_url(settings)}/api/commands/{command}"
    try:
        data = http.request("POST", url, json_body=args or {}, timeout=timeout,
                            headers={"Authorization": f"Bearer {str(settings.get('token') or '').strip()}"})
    except http.HttpError as exc:
        if exc.status is None:
            why = tailscale.why_unreachable(url)
            raise StepError(f"Can't reach the storage machine: {why}." if why else str(exc)) from None
        if exc.status == 401:
            raise StepError("The storage machine refused the token. Check it in this job's settings.") from None
        try:
            error = json.loads(exc.body).get("error")
        except (ValueError, AttributeError):
            error = None
        if str(error or "").startswith('unknown command "save.') or (exc.status == 404 and not error):
            raise StepError("The storage machine's chromers doesn't know how to save pages yet: update it "
                            "(the save.* commands, docs/jobs/save_page.md).") from None
        raise StepError(str(error) if error else f"The storage machine answered: {exc}") from None
    if not isinstance(data, dict) or "ok" not in data:
        raise StepError(f"{base_url(settings)} didn't answer like a chromers daemon. Check the address.")
    return data.get("result")


@step(Stage.EXECUTE, name="queue on the storage machine")
def queue(ctx):
    formats = [f for f, on in (("mhtml", ctx["mhtml"]), ("pdf", ctx["pdf"])) if on]
    if not formats:
        raise StepError("Pick at least one format: MHTML or PDF.")
    args = {"url": ctx["url"].strip(), "root": str(ctx.settings.get("root") or "web").strip(),
            "folder": (ctx.get("folder") or "").strip().strip("/"), "formats": formats}
    if (ctx.get("name") or "").strip():
        args["name"] = ctx["name"].strip()
    ctx["job"] = call(ctx.settings, "save.queue", args)


@step(Stage.EXECUTE, name="save the page")
def wait(ctx):
    job, deadline = ctx["job"], time.monotonic() + WAIT_SECONDS
    while job.get("status") in ("queued", "running") and time.monotonic() < deadline:
        ctx.check_cancelled()
        time.sleep(POLL_SECONDS)
        job = call(ctx.settings, "save.status", {"id": job["id"]})
    ctx["job"] = job


@step(Stage.PRESENT)
def show(ctx):
    job = ctx["job"]
    where = f"{ctx.settings.get('root') or 'web'}/{job.get('folder') or ''}".rstrip("/")
    if job.get("status") == "failed":
        raise StepError(f"The storage machine couldn't save the page: {job.get('error')}")
    rows = [("Page", job.get("title") or job.get("url", "")), ("Saved in", where)]
    if job.get("status") != "done":
        ctx.result = Result(title="Queued on the storage machine", rows=rows + [("Job", job["id"])],
                            text="Still saving there; it finishes on the machine even if the phone goes offline.")
        return
    for f in job.get("files", []):
        rows.append((f["path"].rsplit(".", 1)[-1].upper(), f"{f['path']} ({f['bytes'] / 1024:,.0f} KB)"))
    ctx.result = Result(title="Saved", rows=rows)


class SavePage(JobPlugin):
    id = "save_page"
    title = "Save web page"
    description = "Save a web page as it looks (MHTML and/or PDF) into a folder on the storage machine."
    category = "Tools"
    icon = "SAVE_ALT"
    tags = ("download", "save", "archive", "web", "page", "mhtml", "pdf", "chromers", "website")
    fields = (
        Field.text("url", "Page address", required=True, help="e.g. https://example.com/recipe"),
        Field.text("folder", "Folder", help="Inside the save root, e.g. recipes/soups (made if needed)"),
        Field.toggle("mhtml", "MHTML (opens in Chrome/Edge exactly as it looked)", default=True),
        Field.toggle("pdf", "PDF"),
        Field.text("name", "File name", help="Leave empty for the page title and date"),
    )
    settings = SETTINGS
    run_label = "Save"

    def check_ready(self, settings):
        problems = super().check_ready(settings)
        url = base_url(settings)
        if url and not url.startswith(("http://", "https://")):
            problems.append(Problem("The storage machine's address must start with http:// "
                                    "(e.g. http://100.83.88.59:7700)."))
        return problems

    def check_ready_online(self, settings):
        try:
            roots = call(settings, "save.roots", timeout=8)
        except StepError as exc:
            return [Problem(str(exc), fix="recheck")]
        names = [r.get("name") for r in roots or []]
        root = str(settings.get("root") or "web").strip()
        if root not in names:
            return [Problem(f"The storage machine has no save root “{root}” (it has: {', '.join(names) or 'none'}). "
                            "Change it in this job's settings.", fix="settings")]
        return []

    def build_pipeline(self):
        return Pipeline([steps.require("url"), queue, wait, show])


PLUGIN = SavePage()

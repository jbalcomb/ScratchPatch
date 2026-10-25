"""Home Jobs: what the workstation is running, has run, and each run's log."""

from datetime import datetime, timezone

from taskdeck.core import Field, JobPlugin, Pipeline, Problem, Result, Stage, StepError, step
from taskdeck.shared.http import reachable

from . import client
from .config import SETTINGS, SETTINGS_ACTIONS, SETTINGS_JOB, url_problems


@step(Stage.VALIDATE)
def check_run_number(ctx):
    raw = (ctx.get("run") or "").strip().lstrip("#")
    if raw and not raw.isdigit():
        raise StepError("Run number must be a number, like 12 (or blank for the recent list).")
    ctx["run_id"] = int(raw) if raw else None


@step(Stage.EXECUTE, name="ask the workstation")
def fetch(ctx):
    try:
        ctx["titles"] = {j["name"]: j["title"] for j in client.call(ctx.settings, "GET", "/jobs")}
    except (StepError, KeyError, TypeError):
        ctx["titles"] = {}
    if ctx["run_id"] is None:
        ctx["runs"] = client.call(ctx.settings, "GET", "/runs?limit=15")
    else:
        ctx["detail"] = client.call(ctx.settings, "GET", f"/runs/{ctx['run_id']}?tail=60")


@step(Stage.PRESENT)
def present(ctx):
    titles = ctx["titles"]
    if ctx["run_id"] is None:
        runs = ctx["runs"]
        ctx.result = Result(
            title="Recent runs on the workstation" if runs else "Nothing has run yet",
            rows=[(f"#{r['id']} {titles.get(r['job'], r['job'])}", summary(r)) for r in runs],
            text=None if not runs else "Enter a run number above and tap Refresh for its log.",
        )
        return
    run = ctx["detail"]
    rows = [("Job", titles.get(run["job"], run["job"])), ("Status", status_line(run))]
    if run.get("params"):
        rows.append(("Parameters", ", ".join(f"{k} {v}" for k, v in run["params"].items())))
    rows += [(label, f"{client.ago(run[key])} ({run[key][:16].replace('T', ' ')} UTC)")
             for label, key in (("Queued", "created_at"), ("Started", "started_at"), ("Finished", "finished_at"))
             if run.get(key)]
    if run.get("exit_code") is not None:
        rows.append(("Exit code", str(run["exit_code"])))
    ctx.result = Result(
        title=f"Run #{run['id']}",
        rows=rows,
        text="\n".join(run.get("log") or []) or "(no output yet)",
        # A payload makes the Cancel button appear, so only offer it while there's something to cancel.
        payload=run["id"] if run["status"] in client.ACTIVE else None,
    )


def summary(run, now=None):
    parts = [status_line(run, now)]
    if run.get("last_line") and run["status"] != "queued":
        parts.append(run["last_line"][-80:])
    return " · ".join(parts)


def status_line(run, now=None):
    now = now or datetime.now(timezone.utc)
    status = run["status"]
    if status == "queued":
        ahead = run.get("ahead")
        return "queued" + (f", {ahead} ahead" if ahead else "")
    if status == "running" and run.get("started_at"):
        return f"running for {client.duration((now - datetime.fromisoformat(run['started_at'])).total_seconds())}"
    return f"{status} {client.ago(run.get('finished_at'), now)}".strip()


class HomeJobs(JobPlugin):
    id = SETTINGS_JOB
    title = "Home Jobs"
    description = "See what the workstation is running for you, check a run's log, or cancel it."
    category = "Home"
    icon = "DNS_OUTLINED"
    tags = ("workstation", "runner", "status", "queue", "log", "cancel", "tailscale", "discord")
    fields = (Field.text("run", "Run number", help="Blank for the recent runs; a number for that run's log."),)
    settings = SETTINGS
    settings_actions = SETTINGS_ACTIONS
    run_label = "Refresh"
    commit_label = "Cancel run"
    committed_label = "Cancelled"

    def check_ready(self, settings):
        return super().check_ready(settings) + url_problems(settings)

    def check_ready_online(self, settings):
        return online_problems(settings)

    def build_pipeline(self):
        return Pipeline([check_run_number, fetch, present])

    def commit(self, payload, settings):
        run = client.call(settings, "POST", f"/runs/{int(payload)}/cancel")
        return f"Run #{run['id']} is {run['status']}." if run["status"] != "running" else f"Stopping run #{run['id']}."


def online_problems(settings):
    url = (settings.get("runner_url") or "").strip()
    if url and not reachable(url, 3):
        return [Problem("The workstation isn't answering. Is it on, and is Tailscale connected on this phone?",
                        fix="recheck")]
    return []


PLUGIN = HomeJobs()

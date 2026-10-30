"""Daily Log → Google Sheet: parse a pasted delivery log with the local AI model,
preview the row, and append it to the sheet on confirmation (see docs/jobs/daily_log.md)."""

from taskdeck.core import Field, JobPlugin, Pipeline, Problem, Result, SettingsAction, Stage, StepError, step
from taskdeck.shared.google import GoogleError
from taskdeck.shared import tailscale
from taskdeck.shared.http import HttpError, reachable
from taskdeck.shared import steps

from . import extract, reconcile, reconcile_job, sheets
from .config import SETTINGS
from .record import COLUMNS, build_record, display_value, header_problems, missing_fields


@step(Stage.VALIDATE)
def model_configured(ctx):
    if not any(e.configured for e in extract.endpoints(ctx.settings)):
        raise StepError("Set the AI Workstation URL or a Gemini API key in this job's settings (the tune icon at the top).")


@step(Stage.EXECUTE, name="ask AI model")
def extract_values(ctx):
    ctx["raw"], ctx["parsed_by"] = extract.ask_model(ctx["log"], ctx.settings)


@step(Stage.EXECUTE)
def calculate(ctx):
    ctx["record"] = build_record(ctx["raw"])


@step(Stage.PRESENT)
def preview(ctx):
    record = ctx["record"]
    missing = missing_fields(record)
    ctx.result = Result(
        title=(f"Row for {record['date']}" if record.get("date") else "Row preview")
              + (f" · parsed by {ctx.get('parsed_by')}" if ctx.get("parsed_by") else ""),
        rows=[(c.label, display_value(c.field, record.get(c.field))) for c in COLUMNS],
        warning=f"Not found: {', '.join(missing)}." if missing else None,
        payload=record,
    )


class DailyLog(JobPlugin):
    id = "daily_log"
    title = "Daily Log to Sheet"
    description = "Paste a delivery log; the local AI extracts it and a row is added to the Google Sheet."
    category = "Work"
    icon = "LOCAL_SHIPPING_OUTLINED"
    tags = ("instacart", "gig", "delivery", "mileage", "gas", "earnings", "google sheets", "log")
    fields = (Field.multiline("log", "Paste today's log", required=True, lines=12),)
    settings = SETTINGS
    settings_actions = (SettingsAction("Test AI models", extract.test_models, icon="NETWORK_CHECK"),)
    run_label = "Parse log"
    commit_label = "Save to sheet"
    committed_label = "Saved to sheet"

    def check_ready(self, settings):
        problems = super().check_ready(settings)
        url = (settings.get("llm_url") or "").strip()
        if not url and not (settings.get("fallback_api_key") or "").strip():
            problems.append(Problem("Set the AI Workstation URL or a Gemini API key, so the log can be parsed."))
        if url and not url.startswith(("http://", "https://")):
            problems.append(Problem("The AI Workstation URL must start with http:// (e.g. http://192.168.1.50:8080/v1/chat/completions)."))
        elif "localhost" in url or "127.0.0.1" in url:
            problems.append(Problem("The AI Workstation URL points at the phone itself (localhost). Use the workstation's "
                                    "network or Tailscale address.", blocking=not (settings.get("fallback_api_key") or "").strip()))
        return problems + sheets.setup_problems(settings)

    def check_ready_online(self, settings):
        """Sign in to Google and check the sheet (no AI quota used); see if the workstation answers."""
        problems = []
        try:
            values = sheets.make_backend(settings, timeout=10).rows()
            bad = header_problems(values[0] if values else [])
            if bad:
                problems.append(Problem("Sheet headers don’t match: " + "; ".join(bad) +
                                        ". Fix row 1 of the sheet, then check again.", fix="recheck"))
            else:
                plan = reconcile.plan(values)
                if plan.changed:
                    problems.append(Problem("The sheet needs tidying: " + " ".join(plan.changes), blocking=False,
                                            fix=f"job:{reconcile_job.PLUGIN.id}"))
        except StepError as exc:
            message = str(exc)
            offline = "Can't reach" in message or "didn't answer" in message
            problems.append(Problem(("Couldn't check the Google Sheet (no connection?): " if offline else "") + message,
                                    blocking=not offline, fix="recheck" if offline else "settings"))
        except (GoogleError, HttpError) as exc:
            problems.append(Problem(str(exc)))
        url = (settings.get("llm_url") or "").strip()
        if url and not reachable(url, 3):
            has_gemini = bool((settings.get("fallback_api_key") or "").strip())
            why = tailscale.why_unreachable(url)  # e.g. the phone isn't on Tailscale
            if why:
                text = f"The AI Workstation isn't reachable: {why}." + (" Until then Gemini parses the log."
                                                                         if has_gemini else "")
            else:
                text = "The AI Workstation isn't reachable right now" + (
                    ", so Gemini will parse the log." if has_gemini else ". Turn it on, or add a Gemini key.")
            problems.append(Problem(text, blocking=not has_gemini, fix="recheck"))
        return problems

    def build_pipeline(self):
        return Pipeline([steps.require("log"), model_configured, extract_values, calculate, preview])

    def commit(self, payload, settings):
        if not isinstance(payload, dict) or "date" not in payload:
            raise StepError("Nothing to save. Parse the log first.")
        return sheets.save_record(payload, sheets.make_backend(settings))


PLUGIN = DailyLog()
PLUGINS = (PLUGIN, reconcile_job.PLUGIN)

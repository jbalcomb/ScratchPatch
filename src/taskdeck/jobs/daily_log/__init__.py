"""Daily Log → Google Sheet: parse a pasted delivery log with the local AI model,
preview the row, and append it to the sheet on confirmation (see docs/jobs/daily_log.md)."""

from taskdeck.core import Field, JobPlugin, Pipeline, Problem, Result, SettingsAction, Stage, StepError, step
from taskdeck.shared.google import GoogleError, ServiceAccountKey
from taskdeck.shared.http import HttpError, reachable
from taskdeck.shared import steps

from taskdeck.shared.llm import GEMINI_URL

from . import extract, sheets
from .record import COLUMNS, build_record, display_value, header_problems, missing_fields

DEFAULT_SHEET_ID = "1RemIJ0pmrN-zj58cpeHd4Jas2ix2ss3aTef14_zEkec"


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
    settings = (
        Field.text("llm_url", "AI Workstation URL",
                   help="llama.cpp chat endpoint, tried first, e.g. "
                        "http://192.168.1.50:8080/v1/chat/completions or its Tailscale address"),
        Field.text("llm_model", "AI Workstation model name", default="local"),
        Field.text("fallback_api_key", "Gemini API key (fallback)", secret=True,
                   help="Used when the AI Workstation is offline. Paste the key from aistudio.google.com (pasting the whole "
                        "curl command it shows works too). Leave blank for no fallback."),
        Field.text("fallback_model", "Gemini model(s)", default="gemini-flash-latest",
                   help="Comma-separated, tried in order. gemini-flash-lite-latest is always tried last "
                        "(each model has its own free daily limit)."),
        Field.text("fallback_url", "Fallback URL", default=GEMINI_URL,
                   help="Any OpenAI-compatible chat-completions endpoint works here."),
        Field.text("sheet_id", "Sheet ID", default=DEFAULT_SHEET_ID, required=True),
        Field.text("sheet_tab", "Tab name", default="Sheet1"),
        Field.text("service_account_json", "Google service account key (JSON)", secret=True, importable=True, required=True,
                   help="Tap Paste after copying the whole .json key text, or Import the file. Share the sheet with the key's "
                        "client_email as Editor."),
    )
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
        key_text = (settings.get("service_account_json") or "").strip()
        if key_text:
            try:
                ServiceAccountKey.from_json(key_text).check()
            except GoogleError as exc:
                problems.append(Problem(str(exc)))
        return problems

    def check_ready_online(self, settings):
        """Sign in to Google and check the sheet's headers (no AI quota used); see if the workstation answers."""
        problems = []
        try:
            backend = sheets.make_backend(settings, timeout=10)
            bad = header_problems(backend.header_row())
            if bad:
                problems.append(Problem("Sheet headers don’t match: " + "; ".join(bad) +
                                        ". Fix row 1 of the sheet, then check again.", fix="recheck"))
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
            problems.append(Problem("The AI Workstation isn't reachable right now" +
                                    (", so Gemini will parse the log." if has_gemini else ". Turn it on, or add a Gemini key."),
                                    blocking=not has_gemini, fix="recheck" if has_gemini else "settings"))
        return problems

    def build_pipeline(self):
        return Pipeline([steps.require("log"), model_configured, extract_values, calculate, preview])

    def commit(self, payload, settings):
        if not isinstance(payload, dict) or "date" not in payload:
            raise StepError("Nothing to save. Parse the log first.")
        sheets.save_record(payload, sheets.make_backend(settings))
        return f"Saved row for {payload.get('date') or 'today'}"


PLUGIN = DailyLog()

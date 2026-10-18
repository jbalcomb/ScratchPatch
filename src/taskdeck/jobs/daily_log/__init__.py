"""Daily Log → Google Sheet: parse a pasted delivery log with the local AI model,
preview the row, and append it to the sheet on confirmation (see docs/jobs/daily_log.md)."""

from taskdeck.core import Field, JobPlugin, Pipeline, Result, Stage, StepError, step
from taskdeck.shared import steps

from taskdeck.shared.llm import GEMINI_URL

from . import extract, sheets
from .record import COLUMNS, build_record, display_value, missing_fields

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
                   help="Used when the AI Workstation is offline. Create one at aistudio.google.com. Leave blank for no fallback."),
        Field.text("fallback_model", "Gemini model", default="gemini-flash-latest"),
        Field.text("fallback_url", "Fallback URL", default=GEMINI_URL,
                   help="Any OpenAI-compatible chat-completions endpoint works here."),
        Field.text("sheet_id", "Sheet ID", default=DEFAULT_SHEET_ID),
        Field.text("sheet_tab", "Tab name", default="Sheet1"),
        Field.text("service_account_json", "Google service account key (JSON)", secret=True, importable=True,
                   help="Tap Paste after copying the whole .json key text, or Import the file. Share the sheet with the key's "
                        "client_email as Editor."),
    )
    run_label = "Parse log"
    commit_label = "Save to sheet"
    committed_label = "Saved to sheet"

    def build_pipeline(self):
        return Pipeline([steps.require("log"), model_configured, extract_values, calculate, preview])

    def commit(self, payload, settings):
        if not isinstance(payload, dict) or "date" not in payload:
            raise StepError("Nothing to save. Parse the log first.")
        sheets.save_record(payload, sheets.make_backend(settings))
        return f"Saved row for {payload.get('date') or 'today'}"


PLUGIN = DailyLog()

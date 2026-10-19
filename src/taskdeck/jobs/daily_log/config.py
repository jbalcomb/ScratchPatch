"""Settings shared by the Daily Log jobs (stored once, under the Daily Log job)."""

from taskdeck.core import Field
from taskdeck.shared.llm import GEMINI_URL

DEFAULT_SHEET_ID = "1RemIJ0pmrN-zj58cpeHd4Jas2ix2ss3aTef14_zEkec"

SETTINGS = (
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

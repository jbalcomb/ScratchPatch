"""Ask an AI model to extract raw values from the log: the AI Workstation first,
then the cloud fallback (Gemini) if the workstation can't be reached or fails."""

from __future__ import annotations

from typing import Any

from taskdeck.shared.llm import GEMINI_URL, ModelEndpoint, chat_json_with_fallback, ping

PROMPT = """You extract data from a delivery driver's daily log. Reply with ONLY a JSON object, no other text.
Keys (use null when a value is missing):
  month: number 1-12
  day: number
  departure_time: "HH:MM" 24-hour
  departure_mileage: number
  online_time: "HH:MM" 24-hour
  offline_time: "HH:MM" 24-hour
  arrival_time: "HH:MM" 24-hour
  arrival_mileage: number
  batches: integer
  orders: integer
  earnings: number, no currency symbol
  gas_fills: list of {"at": number, "after": number}, one per fill-up
Labels in the log:
  "Start time" = departure_time (leaving home). It is NOT online_time.
  "Online at" = online_time; "Offline at" = offline_time (the delivery app's on/off times).
  "Arrival time" = arrival_time (back home).
  "Starting mileage" = departure_mileage; "Ending mileage" = arrival_mileage.
  "N orders for $X" = orders N and earnings X.
Only fill a key from its own label; if that label is absent, use null.
Notes: times like 2146 mean 21:46. Mileage values are fuel range shown on the dash, not odometer readings.
For a fill-up, "at" is the range when gas was bought and "after" is the range after filling."""


# Tried after the configured Gemini model(s): it has its own free-tier quota (each model's
# daily limit is separate) and parses these logs correctly in about a second.
LAST_RESORT_MODEL = "gemini-flash-lite-latest"


def endpoints(settings: dict[str, Any]) -> list[ModelEndpoint]:
    s = {k: (v or "") if isinstance(v, str) or v is None else v for k, v in settings.items()}
    out = [ModelEndpoint("AI Workstation", s.get("llm_url", ""), s.get("llm_model") or "local",
                         api_key=s.get("llm_api_key", ""), timeout=120)]
    if (s.get("fallback_api_key") or "").strip():
        models = [m.strip() for m in (s.get("fallback_model") or "gemini-flash-latest").split(",") if m.strip()]
        if LAST_RESORT_MODEL not in models:
            models.append(LAST_RESORT_MODEL)
        for model in models:
            label = "Gemini" if model == models[0] else f"Gemini ({model.removeprefix('gemini-').removesuffix('-latest')})"
            out.append(ModelEndpoint(label, s.get("fallback_url") or GEMINI_URL, model,
                                     api_key=s["fallback_api_key"], timeout=30))
    return out


def ask_model(text: str, settings: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return (raw extraction, label of the model that answered)."""
    raw, used = chat_json_with_fallback(endpoints(settings), PROMPT, text)
    return raw, used.label


def test_models(settings: dict[str, Any]) -> str:
    """Settings-screen check: try each configured model with a tiny request."""
    eps = endpoints(settings)
    lines = [ping(e) for e in eps]  # one tiny request per model (counts toward free-tier quotas)
    if len(eps) == 1:
        lines.append("Gemini: not set up (no API key)")
    return "\n".join(lines)

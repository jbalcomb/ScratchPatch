"""Ask an AI model to extract raw values from the log: the AI Workstation first,
then the cloud fallback (Gemini) if the workstation can't be reached or fails."""

from __future__ import annotations

from typing import Any

from taskdeck.shared.llm import GEMINI_URL, ModelEndpoint, chat_json_with_fallback

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
Notes: times like 2146 mean 21:46. Mileage values are fuel range shown on the dash, not odometer readings.
For a fill-up, "at" is the range when gas was bought and "after" is the range after filling."""


def endpoints(settings: dict[str, Any]) -> list[ModelEndpoint]:
    s = {k: (v or "") if isinstance(v, str) or v is None else v for k, v in settings.items()}
    out = [ModelEndpoint("AI Workstation", s.get("llm_url", ""), s.get("llm_model") or "local", timeout=120)]
    if (s.get("fallback_api_key") or "").strip():
        out.append(ModelEndpoint("Gemini", s.get("fallback_url") or GEMINI_URL,
                                 s.get("fallback_model") or "gemini-flash-latest",
                                 api_key=s["fallback_api_key"], timeout=60))
    return out


def ask_model(text: str, settings: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return (raw extraction, label of the model that answered)."""
    raw, used = chat_json_with_fallback(endpoints(settings), PROMPT, text)
    return raw, used.label

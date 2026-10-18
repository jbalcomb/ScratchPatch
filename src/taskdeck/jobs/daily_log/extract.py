"""Ask the local model (llama.cpp, OpenAI-style API) to extract raw values from the log."""

from __future__ import annotations

import json
import re
from typing import Any

from taskdeck.core import StepError
from taskdeck.shared.http import HttpError, post_json

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

TIMEOUT = 120  # local models can be slow


def parse_model_json(content: str) -> dict[str, Any]:
    """Take the first {...} block from the reply, tolerating stray text around it."""
    match = re.search(r"\{.*\}", content or "", re.S)
    if not match:
        raise StepError("The AI model didn't return any data. Try parsing again.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise StepError("The AI model returned malformed data. Try parsing again.") from None
    if not isinstance(data, dict):
        raise StepError("The AI model returned data in an unexpected shape.")
    return data


def ask_model(text: str, url: str, model: str, timeout: float = TIMEOUT) -> dict[str, Any]:
    payload = {
        "model": model or "local",
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text},
        ],
    }
    try:
        resp = post_json(url, payload, timeout=timeout)
    except HttpError as exc:
        hint = " Is the AI Workstation on, and is this phone on home Wi-Fi or Tailscale?" if exc.status is None else ""
        raise StepError(f"AI model request failed: {exc}.{hint}") from None
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise StepError("The AI model's reply wasn't in the expected chat-completions format.") from None
    return parse_model_json(content)

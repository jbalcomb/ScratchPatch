"""Ask an OpenAI-compatible chat model for a JSON object, with ordered fallbacks.

Works with llama.cpp's server, Gemini's OpenAI-compatible endpoint
(``https://generativelanguage.googleapis.com/v1beta/openai/chat/completions``)
and anything else that speaks ``/v1/chat/completions``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from taskdeck.core import StepError

from .http import HttpError, post_json, reachable

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
RETRY_DELAYS = (2.0, 5.0)  # seconds before the 2nd and 3rd attempt on a busy/transient error


@dataclass(frozen=True)
class ModelEndpoint:
    label: str  # shown to the user, e.g. "AI Workstation" or "Gemini"
    url: str
    model: str
    api_key: str = ""
    timeout: float = 120

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())


_KEY_PATTERNS = (
    re.compile(r"x-goog-api-key\s*[:=]\s*['\"]?([A-Za-z0-9._\-]{20,})", re.I),
    re.compile(r"authorization\s*:\s*bearer\s+([A-Za-z0-9._\-]{20,})", re.I),
    re.compile(r"[?&]key=([A-Za-z0-9._\-]{20,})"),
)


def extract_api_key(text: str) -> str:
    """Accept a bare key, or pull it out of a pasted curl command, header line or URL."""
    text = (text or "").strip()
    for pattern in _KEY_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return text.strip("'\"")


def parse_json_object(content: str) -> dict[str, Any]:
    """Take the first {...} block from a reply, tolerating stray text around it."""
    match = re.search(r"\{.*\}", content or "", re.S)
    if not match:
        raise StepError("The AI model didn't return any data.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise StepError("The AI model returned malformed data.") from None
    if not isinstance(data, dict):
        raise StepError("The AI model returned data in an unexpected shape.")
    return data


def chat_json(endpoint: ModelEndpoint, system: str, user: str) -> dict[str, Any]:
    """One request to one endpoint. Raises StepError with a user-facing message."""
    payload = {
        "model": endpoint.model or "local",
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    key = extract_api_key(endpoint.api_key)
    headers = {"Authorization": f"Bearer {key}"} if key else None
    for attempt, delay in enumerate(RETRY_DELAYS + (None,)):
        try:
            resp = post_json(endpoint.url.strip(), payload, headers=headers, timeout=endpoint.timeout)
            break
        except HttpError as exc:
            # Busy / rate-limited / transient server errors are worth a short retry.
            if delay is not None and exc.status in (429, 500, 502, 503, 504):
                time.sleep(delay)
                continue
            raise StepError(f"{endpoint.label}: {exc}") from None
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise StepError(f"{endpoint.label}: reply wasn't in the chat-completions format.") from None
    try:
        return parse_json_object(content)
    except StepError as exc:
        raise StepError(f"{endpoint.label}: {exc}") from None


def chat_json_with_fallback(endpoints: list[ModelEndpoint], system: str, user: str,
                            probe_timeout: float = 3) -> tuple[dict[str, Any], ModelEndpoint]:
    """Try each configured endpoint in order; return the first good answer and who gave it.

    Before the (slow) request, each endpoint gets a quick reachability probe so an
    offline workstation costs a few seconds, not the full model timeout.
    """
    candidates = [e for e in endpoints if e.configured]
    if not candidates:
        raise StepError("No AI model is set up. Add one in this job's settings (the tune icon at the top).")
    errors = []
    for endpoint in candidates:
        if not reachable(endpoint.url, probe_timeout):
            errors.append(f"{endpoint.label}: not reachable")
            continue
        try:
            return chat_json(endpoint, system, user), endpoint
        except StepError as exc:
            errors.append(str(exc))
    raise StepError("Couldn't parse the log. " + " · ".join(errors))

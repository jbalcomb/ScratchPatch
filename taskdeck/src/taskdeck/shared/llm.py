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

from . import tailscale
from .http import HttpError, post_json, reachable

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
RETRY_DELAYS = (2.0, 5.0)  # seconds before the 2nd and 3rd attempt on a busy/transient error
MAX_TIMEOUT_RETRIES = 1  # a stalled connection is retried once on a fresh connection
MAX_RATE_LIMIT_WAIT = 20.0  # wait out a 429 only if the server asks for at most this many seconds


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


def chat_json(endpoint: ModelEndpoint, system: str, user: str,
              timeout_retries: int = MAX_TIMEOUT_RETRIES) -> dict[str, Any]:
    """One request to one endpoint. Raises StepError with a user-facing message."""
    payload = {
        "model": endpoint.model or "local",
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    key = extract_api_key(endpoint.api_key)
    headers = {"Authorization": f"Bearer {key}"} if key else None
    timeouts = 0
    for attempt, delay in enumerate(RETRY_DELAYS + (None,)):
        try:
            resp = post_json(endpoint.url.strip(), payload, headers=headers, timeout=endpoint.timeout)
            break
        except HttpError as exc:
            # Busy / rate-limited / transient server errors are worth a short retry; a stalled
            # connection gets one fresh try (each costs a full timeout, so only one).
            if exc.timed_out and timeouts < timeout_retries and delay is not None:
                timeouts += 1
                continue
            if exc.status == 429:
                wait = retry_after(exc.body)
                # A per-day quota says "retry in hours": fail now so the next model can answer.
                if delay is not None and (wait is None or wait <= MAX_RATE_LIMIT_WAIT):
                    time.sleep(wait if wait is not None else delay)
                    continue
                raise StepError(f"{endpoint.label}: {_rate_limit_message(wait)}") from None
            if delay is not None and exc.status in (500, 502, 503, 504):
                time.sleep(delay)
                continue
            raise StepError(f"{endpoint.label}: {_friendly(exc)}") from None
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise StepError(f"{endpoint.label}: reply wasn't in the chat-completions format.") from None
    try:
        return parse_json_object(content)
    except StepError as exc:
        raise StepError(f"{endpoint.label}: {exc}") from None


def retry_after(body: str) -> float | None:
    """Seconds the server asks us to wait, from a Google RetryInfo detail ("retryDelay": "77833s")."""
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body or "")
    return float(match.group(1)) if match else None


def _rate_limit_message(wait: float | None) -> str:
    if wait is not None and wait > 3600:
        return f"daily free-tier limit reached (resets in about {wait / 3600:.0f} h)."
    if wait is not None and wait > MAX_RATE_LIMIT_WAIT:
        return f"rate limit reached; try again in about {wait / 60:.0f} min."
    return "rate limit reached (too many requests in the last minute). Wait a minute and try again."


def _friendly(exc: HttpError) -> str:
    if exc.status == 429:
        return _rate_limit_message(retry_after(exc.body))
    if exc.timed_out:
        return f"{exc}. The connection or the model is slow right now; try again."
    if exc.status in (401, 403):
        return f"{exc}. The API key is missing or wrong; check it in this job's settings."
    return str(exc)


def ping(endpoint: ModelEndpoint, probe_timeout: float = 3) -> str:
    """One-line health check for the settings screen: reachable? answers? how fast?"""
    if not endpoint.configured:
        return f"{endpoint.label}: not set up"
    started = time.time()
    if not reachable(endpoint.url, probe_timeout):
        why = tailscale.why_unreachable(endpoint.url)
        return f"{endpoint.label}: not reachable ({endpoint.url.strip()})" + (f": {why}" if why else "")
    try:
        chat_json(endpoint, 'Reply with the JSON object {"ok": true}.', "ping")
    except StepError as exc:
        return f"✗ {exc} ({time.time() - started:.1f} s)"
    return f"✓ {endpoint.label}: answered in {time.time() - started:.1f} s"


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
    for i, endpoint in enumerate(candidates):
        if not reachable(endpoint.url, probe_timeout):
            why = tailscale.why_unreachable(endpoint.url)
            errors.append(f"{endpoint.label}: not reachable" + (f" ({why})" if why else ""))
            continue
        # A stalled model (Gemini sometimes hangs instead of answering, e.g. when its quota
        # is used up) is only retried if nothing else is left to try.
        retries = MAX_TIMEOUT_RETRIES if i == len(candidates) - 1 else 0
        try:
            return chat_json(endpoint, system, user, timeout_retries=retries), endpoint
        except StepError as exc:
            errors.append(str(exc))
    raise StepError("Couldn't parse the log. " + " · ".join(errors))

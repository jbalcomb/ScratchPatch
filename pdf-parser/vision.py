"""The vision model on the workstation's llama.cpp server: is the crop complete and meaningful, and what is it?

Configuration comes from the environment, never from code:

    PDF_PARSER_LLM_URL       the server (default LLM_URL). A base URL or a full .../v1/chat/completions URL.
    PDF_PARSER_VISION_MODEL  the model, a section name in the router's preset (default qwen3-vl-8b)
    LLM_API_KEY              sent as a bearer token when set

The model must be multimodal (loaded with its mmproj). :meth:`Vision.check` looks at what the server says about
the model without loading it; the first image request settles it for sure. When the server isn't there or the
model can't see images, parsing carries on without it (images it would have checked go to needs_review): see
:class:`Budget` for when a run stops asking.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "qwen3-vl-8b"
REQUEST_TIMEOUT = 180       # the router may first unload another model and load this one (about a minute)
MAX_CONSECUTIVE_FAILURES = 3
MAX_FAILED_SECONDS = 300    # time spent on requests that failed, per PDF
MAX_SIDE = 1536             # images are shrunk to this before sending

NOT_MEANINGFUL = {"blank", "fragment", "decoration", "background"}
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "content_type": {"type": "string"},
        "issue": {"type": ["string", "null"]},
    },
    "required": ["complete", "content_type", "issue"],
}
PROMPT = (
    "This image was cut out of page {page} of a PDF. Is the crop complete (nothing important cut off at an edge) "
    "and meaningful (a real photo, chart, diagram, logo, table or figure, not blank, background or a fragment of "
    "something larger)? Answer only with JSON: "
    '{{"complete": true or false, "content_type": "photo|chart|diagram|logo|icon|table|text|screenshot|map|'
    'decoration|background|fragment|blank|other", "issue": null or a short description of what is wrong}}'
)


class VisionError(Exception):
    pass


class NotMultimodal(VisionError):
    """The server answered, but the model can't take images: it was loaded without its mmproj."""


def config(env=None):
    env = os.environ if env is None else env
    url = (env.get("PDF_PARSER_LLM_URL") or env.get("LLM_URL") or "").strip()
    return {"url": url, "model": (env.get("PDF_PARSER_VISION_MODEL") or DEFAULT_MODEL).strip(),
            "api_key": env.get("LLM_API_KEY") or ""}


def base_url(url):
    """http://host:port from either a base URL or a .../v1/chat/completions URL."""
    return re.sub(r"/(v1/)?(chat/completions)?/*$", "", url.rstrip("/")).rstrip("/")


def image_part(path, max_side=MAX_SIDE):
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "PNG")
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}}


def parse_verdict(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise VisionError(f"no JSON in the reply: {(text or '')[:120]!r}")
    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise VisionError(f"bad JSON in the reply: {exc}") from None
    if not isinstance(data.get("complete"), bool) or not isinstance(data.get("content_type"), str):
        raise VisionError(f"reply isn't {{complete: bool, content_type: str, issue}}: {data}")
    issue = data.get("issue")
    return {"complete": data["complete"], "content_type": data["content_type"].strip().lower() or "other",
            "issue": str(issue) if issue not in (None, "", "null") else None}


def meaningful(verdict):
    return verdict["content_type"] not in NOT_MEANINGFUL


_IMAGE_REFUSALS = re.compile(r"image input is not supported|mmproj|multimodal|does not support (image|vision)", re.I)


class Vision:
    """A client for one model. Thread-safe enough for one worker; keeps counts for /api/health."""

    def __init__(self, url, model=DEFAULT_MODEL, api_key="", timeout=REQUEST_TIMEOUT):
        self.url, self.model, self.api_key, self.timeout = url, model, api_key, timeout
        self.multimodal = None          # True / False / None (not known yet)
        self.message = "" if url else "no vision model configured (set PDF_PARSER_LLM_URL or LLM_URL)"
        self.requests = self.failures = self.in_a_row = 0
        self.last_ok = self.last_error = None
        self.checked_at = None

    @classmethod
    def from_env(cls, env=None):
        c = config(env)
        return cls(c["url"], c["model"], c["api_key"])

    @property
    def configured(self):
        return bool(self.url)

    def _http(self, method, path, body=None, timeout=10):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base_url(self.url) + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode(errors="replace")
            if _IMAGE_REFUSALS.search(detail):
                raise NotMultimodal(f"{self.model} can't take images (start it with its mmproj): {detail}") from None
            raise VisionError(f"HTTP {exc.code} from {path}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VisionError(f"{base_url(self.url)} didn't answer: {getattr(exc, 'reason', exc)}") from None

    def check(self):
        """At startup: is the server there, does it have the model, and was the model given an mmproj?

        Looks only at what the server reports (GET /v1/models, then /props), so it doesn't make the router load
        the model and push another one off the GPU. Returns (ok, message); ok is None when it can't tell yet.
        """
        self.checked_at = time.time()
        if not self.url:
            return False, self.message
        try:
            models = self._http("GET", "/v1/models").get("data") or []
        except VisionError as exc:
            self.message = f"vision model server: {exc}"
            return False, self.message
        entry = next((m for m in models if m.get("id") == self.model), None)
        if models and entry is None and len(models) > 1:
            names = ", ".join(sorted(str(m.get("id")) for m in models))
            self.multimodal, self.message = False, f"the server has no model {self.model!r} (it has: {names})"
            return False, self.message
        described = json.dumps(entry or {}).lower()
        if "mmproj" in described or '"vision"' in described or "multimodal" in described:
            self.multimodal, self.message = True, f"{self.model}: multimodal (mmproj)"
            return True, self.message
        try:  # a single-model server reports its modalities here
            props = self._http("GET", f"/props?model={self.model}")
            vision = (props.get("modalities") or {}).get("vision")
        except VisionError:
            vision = None
        if vision is True:
            self.multimodal, self.message = True, f"{self.model}: multimodal"
            return True, self.message
        if vision is False:
            self.multimodal = False
            self.message = (f"{self.model} is loaded without an mmproj, so it can't see images. Add the model's "
                            f"mmproj to its preset section (or --mmproj) and restart it.")
            return False, self.message
        self.message = f"{self.model}: listed; whether it takes images is checked on the first image"
        return None, self.message

    def verdict(self, path, page):
        """Ask about one crop. -> {complete, content_type, issue}. Raises VisionError / NotMultimodal."""
        if self.multimodal is False:
            raise NotMultimodal(self.message)
        body = {"model": self.model, "temperature": 0, "max_tokens": 200,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "schema": VERDICT_SCHEMA}},
                "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT.format(page=page)},
                                                           image_part(path)]}]}
        self.requests += 1
        try:
            reply = self._http("POST", "/v1/chat/completions", body, timeout=self.timeout)
            result = parse_verdict(reply["choices"][0]["message"].get("content"))
        except NotMultimodal as exc:
            self.multimodal, self.message = False, str(exc)
            self._failed(exc)
            raise
        except (VisionError, KeyError, IndexError, TypeError) as exc:
            self._failed(exc)
            raise VisionError(str(exc)) from None
        self.multimodal, self.in_a_row = True, 0
        self.last_ok = time.time()
        return result

    def _failed(self, exc):
        self.failures += 1
        self.in_a_row += 1
        self.last_error = {"at": time.time(), "error": str(exc)[:300]}

    @property
    def state(self):
        """'ok', 'unknown' (not asked yet / can't tell) or 'unavailable' (not configured, can't take images, or
        the last MAX_CONSECUTIVE_FAILURES requests failed)."""
        if not self.configured or self.multimodal is False or self.in_a_row >= MAX_CONSECUTIVE_FAILURES:
            return "unavailable"
        return "ok" if self.last_ok or self.multimodal else "unknown"

    def health(self):
        return {"state": self.state, "configured": self.configured, "model": self.model,
                "multimodal": self.multimodal, "message": self.message, "requests": self.requests,
                "failures": self.failures, "failures_in_a_row": self.in_a_row, "last_ok": self.last_ok,
                "last_error": self.last_error}


class Budget:
    """How long one PDF keeps asking the vision model. Every request is logged (``log``, one JSON line each).

    It stops after MAX_CONSECUTIVE_FAILURES failures in a row, after MAX_FAILED_SECONDS spent on failed requests,
    or at once when the model can't take images. After that, images that would have been checked go to
    needs_review instead, and the reason is in the manifest. Parsing never fails because of the vision model.
    """

    def __init__(self, vision, log_path=None, enabled=True):
        self.vision = vision if (vision and vision.configured and enabled) else None
        self.log_path = Path(log_path) if log_path else None
        self.requests = self.failures = self.in_a_row = 0
        self.failed_seconds = 0.0
        self.stopped = None if self.vision else ("vision disabled" if vision and vision.configured else
                                                 "no vision model configured")

    @property
    def available(self):
        return self.stopped is None

    def log(self, **event):
        if self.log_path:
            with self.log_path.open("a") as f:
                f.write(json.dumps({"ts": round(time.time(), 3), **event}) + "\n")

    def ask(self, path, page, **context):
        """-> verdict, or None when the model didn't answer (or this PDF stopped asking)."""
        if not self.available:
            return None
        started = time.monotonic()
        self.requests += 1
        try:
            verdict = self.vision.verdict(path, page)
        except VisionError as exc:
            seconds = time.monotonic() - started
            self.failures += 1
            self.in_a_row += 1
            self.failed_seconds += seconds
            self.log(event="vision", ok=False, ms=round(seconds * 1000), error=str(exc)[:300], **context)
            if isinstance(exc, NotMultimodal):
                self.stopped = str(exc)
            elif self.in_a_row >= MAX_CONSECUTIVE_FAILURES:
                self.stopped = f"{self.in_a_row} vision requests in a row failed (last: {exc})"
            elif self.failed_seconds >= MAX_FAILED_SECONDS:
                self.stopped = f"{round(self.failed_seconds)} s spent on failed vision requests (last: {exc})"
            if self.stopped:
                self.log(event="vision_stopped", reason=self.stopped)
            return None
        self.in_a_row = 0
        self.log(event="vision", ok=True, ms=round((time.monotonic() - started) * 1000), verdict=verdict, **context)
        return verdict

    def summary(self):
        return {"requests": self.requests, "failed": self.failures, "available": self.available,
                "stopped": self.stopped, "model": self.vision.model if self.vision else None}

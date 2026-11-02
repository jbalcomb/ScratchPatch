"""After transcription: give the note a title, a category, tags, a summary, action items and the people,
projects and places it mentions (for the knowledge graph), using the local llama.cpp server.

Categories come from ``categories.txt`` (one per line, edit freely; the model may only pick from it).
Without a model, or if it fails, the note still gets archived: title from its first words, category Inbox.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FALLBACK_CATEGORY = "Inbox"
ENTITY_TYPES = ("person", "project", "place", "organization", "topic")

SYSTEM = """You file voice notes that were transcribed by speech recognition (expect small mistakes).
Reply with one JSON object only:
{"title": "3-8 word title",
 "category": "one of: {categories}",
 "tags": ["2-5 short lowercase tags"],
 "summary": "one or two sentences",
 "actions": ["things the speaker said they need to do, as short imperative sentences; [] if none"],
 "entities": [{"name": "Proper Name", "type": "person|project|place|organization|topic"}]}
Use the speaker's own words where you can. Don't invent facts. Pick the closest category; use Inbox if none fits."""


def categories(path: Path | None = None) -> list[str]:
    path = path or Path(os.environ.get("VOICE_REC_CATEGORIES") or HERE / "categories.txt")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    cats = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if FALLBACK_CATEGORY not in cats:
        cats.append(FALLBACK_CATEGORY)
    return cats


def chat_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")


def fallback(transcript: str) -> dict[str, Any]:
    words = transcript.split()
    title = " ".join(words[:7]).strip(" ,.;:") or "Empty voice note"
    return {"title": title[:80], "category": FALLBACK_CATEGORY, "tags": [], "summary": "", "actions": [], "entities": []}


def slug_tag(tag: str) -> str:
    tag = re.sub(r"[^\w\-/]+", "-", str(tag).strip().lower().lstrip("#")).strip("-")
    return tag[:40]


def clean(data: dict[str, Any], cats: list[str], transcript: str) -> dict[str, Any]:
    out = fallback(transcript)
    if isinstance(data.get("title"), str) and data["title"].strip():
        out["title"] = re.sub(r"\s+", " ", data["title"]).strip().strip('"')[:80]
    by_lower = {c.lower(): c for c in cats}
    out["category"] = by_lower.get(str(data.get("category") or "").strip().lower(), FALLBACK_CATEGORY)
    out["tags"] = [t for t in dict.fromkeys(slug_tag(t) for t in (data.get("tags") or []) if str(t).strip()) if t][:8]
    if isinstance(data.get("summary"), str):
        out["summary"] = data["summary"].strip()
    out["actions"] = [str(a).strip() for a in (data.get("actions") or []) if str(a).strip()][:20]
    ents = []
    for e in data.get("entities") or []:
        if isinstance(e, dict) and str(e.get("name") or "").strip():
            kind = str(e.get("type") or "topic").lower()
            ents.append({"name": str(e["name"]).strip()[:80], "type": kind if kind in ENTITY_TYPES else "topic"})
    out["entities"] = ents[:30]
    return out


def organize(transcript: str, env=os.environ, cats: list[str] | None = None, timeout: float = 180) -> tuple[dict, str]:
    """(info, problem): problem is "" or why the fallback was used."""
    cats = cats or categories()
    if not transcript.strip():
        return fallback(transcript), ""
    url = env.get("VOICE_REC_LLM_URL") or env.get("LLM_URL")
    if not url:
        return fallback(transcript), "no LLM_URL set, so it wasn't categorized"
    key = env.get("VOICE_REC_LLM_KEY") or env.get("LLM_API_KEY") or ""
    payload = {
        "model": env.get("VOICE_REC_LLM_MODEL") or env.get("LLM_MODEL") or "local",
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},  # llama.cpp: no reasoning needed
        "messages": [{"role": "system", "content": SYSTEM.replace("{categories}", ", ".join(cats))},
                     {"role": "user", "content": transcript[:24000]}],
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(chat_url(url), data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.loads(resp.read())
        content = reply["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content or "", re.S)
        data = json.loads(match.group(0)) if match else {}
    except Exception as exc:  # the note is archived anyway; the error is kept on the row
        return fallback(transcript), f"categorizing failed ({type(exc).__name__}: {exc})"
    return clean(data if isinstance(data, dict) else {}, cats, transcript), ""

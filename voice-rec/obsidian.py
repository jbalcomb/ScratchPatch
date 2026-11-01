"""Write each archived voice note into an Obsidian vault as Markdown.

    <vault>/Voice Notes/<Category>/2026-11-01 1402 Call the dentist.md
    <vault>/Voice Notes/_audio/2026-11-01 1402 3f2a9c1b.m4a      (OBSIDIAN_AUDIO=1, the default)

Front matter holds the ids and timestamps (Dataview can query them); people, projects and places become
[[wikilinks]], so they show up in Obsidian's graph view too. Archiving again rewrites the same note (it moves
if its title or category changed). Notes are rewritten by the server: edit the transcript in Postgres, or
add your own notes below the "Your notes" line, which is kept.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from transcribe import extension

KEEP_MARKER = "## Your notes"
_BAD = re.compile(r'[\\/:*?"<>|#^\[\]\n\r\t]+')


def safe_name(text: str, limit: int = 80) -> str:
    name = _BAD.sub(" ", text or "").strip().strip(".")
    return re.sub(r"\s+", " ", name)[:limit].strip() or "Untitled"


def _local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone()


def _yaml(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_yaml(v) for v in value) + "]"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def note_paths(rec: dict[str, Any], vault: Path, folder: str) -> tuple[Path, Path]:
    when = _local(rec["recorded_at"])
    stamp = when.strftime("%Y-%m-%d %H%M")
    base = vault / folder
    note = base / safe_name(rec.get("category") or "Inbox", 40) / f"{stamp} {safe_name(rec.get('title') or 'Voice note')}.md"
    audio = base / "_audio" / f"{stamp} {rec['id'][:8]}{extension(rec.get('mime', ''))}"
    return note, audio


def render(rec: dict[str, Any], audio_link: str | None, kept: str = "") -> str:
    recorded, transcribed = _local(rec["recorded_at"]), _local(rec.get("transcribed_at"))
    tags = ["voice-note", *[t for t in rec.get("tags") or [] if t != "voice-note"]]
    entities = rec.get("entities") or []
    front = {
        "id": rec["id"],
        "type": "voice-note",
        "recorded": recorded.isoformat(timespec="seconds"),
        "transcribed": transcribed.isoformat(timespec="seconds") if transcribed else None,
        "duration": round(rec.get("duration_s") or 0),
        "category": rec.get("category") or "Inbox",
        "tags": tags,
        "people": [f"[[{e['name']}]]" for e in entities if e.get("type") == "person"],
        "projects": [f"[[{e['name']}]]" for e in entities if e.get("type") == "project"],
        "language": rec.get("language"),
        "model": rec.get("model"),
    }
    lines = ["---", *[f"{k}: {_yaml(v)}" for k, v in front.items()], "---", "",
             f"# {rec.get('title') or 'Voice note'}", "",
             f"Recorded {recorded.strftime('%a %b %-d %Y, %H:%M')}"
             + (f" · transcribed {transcribed.strftime('%b %-d, %H:%M')}" if transcribed else ""), ""]
    if audio_link:
        lines += [f"![[{audio_link}]]", ""]
    if rec.get("summary"):
        lines += [f"> {rec['summary']}", ""]
    if rec.get("actions"):
        lines += ["## Actions", *[f"- [ ] {a}" for a in rec["actions"]], ""]
    if entities:
        lines += ["## Mentions", " · ".join(f"[[{e['name']}]] ({e['type']})" for e in entities), ""]
    lines += ["## Transcript", "", rec.get("transcript") or "_(no speech found)_", "", KEEP_MARKER, ""]
    out = "\n".join(lines)
    return out + (kept.strip() + "\n" if kept.strip() else "")


def write(rec: dict[str, Any], audio: bytes | None, vault: Path, folder: str = "Voice Notes",
          with_audio: bool = True) -> Path:
    vault = Path(vault)
    if not vault.is_dir():
        raise FileNotFoundError(f"Obsidian vault folder {vault} doesn't exist")
    note, audio_path = note_paths(rec, vault, folder)
    old = vault / rec["note_path"] if rec.get("note_path") else None
    kept = ""
    for candidate in (old, note):
        if candidate and candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            if KEEP_MARKER in text:
                kept = text.split(KEEP_MARKER, 1)[1]
            break
    link = None
    if with_audio and audio:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        if not audio_path.exists() or audio_path.stat().st_size != len(audio):
            audio_path.write_bytes(audio)
        link = audio_path.relative_to(vault).as_posix()
    note.parent.mkdir(parents=True, exist_ok=True)
    tmp = note.with_suffix(".md.tmp")
    tmp.write_text(render(rec, link, kept), encoding="utf-8")
    os.replace(tmp, note)
    if old and old != note and old.exists():
        old.unlink()
    return note.relative_to(vault)

"""The voice notes kept on the phone: audio files plus an index of where each one is in the process.

Stages, each with its own network rule (Wi-Fi only unless the recording says otherwise):

    record (on the phone, no network)
      → transcribe: the audio goes to the AI Workstation, which queues and transcribes it;
                    the phone fetches the transcript back
      → archive:    the phone tells the workstation to file it (Postgres, Obsidian, knowledge graph)

A recording's ``status`` says which step is next:

    recorded     saved on the phone, not sent yet             (transcribe stage)
    queued       on the workstation, waiting to be transcribed  (transcribe stage)
    transcribed  transcript is back on the phone              (archive stage)
    archiving    the workstation is filing it                 (archive stage)
    archived     done
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from taskdeck.core import JsonStore

RECORDED, QUEUED, TRANSCRIBED, ARCHIVING, ARCHIVED = "recorded", "queued", "transcribed", "archiving", "archived"
TRANSCRIBE, ARCHIVE = "transcribe", "archive"
STAGES = (TRANSCRIBE, ARCHIVE)
STAGE_OF = {RECORDED: TRANSCRIBE, QUEUED: TRANSCRIBE, TRANSCRIBED: ARCHIVE, ARCHIVING: ARCHIVE}
STAGE_LABEL = {TRANSCRIBE: "Transcribe", ARCHIVE: "Archive"}
EXTENSION = ".m4a"
MIME = "audio/mp4"
MIMES = {".m4a": "audio/mp4", ".wav": "audio/wav"}  # the formats the recorder writes (see screen.PROFILES)


def mime_of(file_name: str) -> str:
    return MIMES.get(Path(file_name).suffix.lower(), MIME)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_time(value: str | None) -> str:
    """'Nov 1, 14:02' in the phone's time zone (with the year when it isn't this year)."""
    when = parse_iso(value)
    if when is None:
        return ""
    when = when.astimezone()
    fmt = "%b %-d, %H:%M" if when.year == datetime.now().year else "%b %-d %Y, %H:%M"
    try:
        return when.strftime(fmt)
    except ValueError:  # %-d isn't supported everywhere
        return when.strftime(fmt.replace("%-d", "%d"))


def clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class Recording:
    id: str
    file: str  # file name inside the library's audio folder; "" once the audio was removed
    recorded_at: str  # ISO 8601, UTC
    duration: float = 0.0  # seconds
    size: int = 0
    status: str = RECORDED
    # Per-recording override: stage -> True when that stage may use mobile data.
    mobile_data: dict[str, bool] = field(default_factory=dict)
    uploaded_at: str = ""
    transcribed_at: str = ""
    archived_at: str = ""
    transcript: str = ""
    title: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""  # last problem, shown on the recording; cleared when a step works

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Recording":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    @property
    def stage(self) -> str | None:
        """The stage whose network rule applies to the next step (None when archived)."""
        return STAGE_OF.get(self.status)

    def may_use_mobile_data(self, stage: str) -> bool:
        return bool(self.mobile_data.get(stage))

    def allowed(self, on_wifi: bool) -> bool:
        stage = self.stage
        return stage is not None and (on_wifi or self.may_use_mobile_data(stage))

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        words = self.transcript.split()
        if words:
            return " ".join(words[:8]) + ("…" if len(words) > 8 else "")
        return "Voice note"


class Library:
    """Recordings in ``root/audio`` and their index in ``root/index.json``. Thread-safe."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.audio_dir = self.root / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._store = JsonStore(self.root / "index.json")
        self._lock = threading.RLock()

    # ---- reading -----------------------------------------------------------------------------

    def all(self) -> list[Recording]:
        """Oldest first (ascending by the time it was recorded)."""
        with self._lock:
            recs = [Recording.from_dict(d) for d in self._store.get("recordings", [])]
        return sorted(recs, key=lambda r: (r.recorded_at, r.id))

    def get(self, rec_id: str) -> Recording | None:
        return next((r for r in self.all() if r.id == rec_id), None)

    def path(self, rec: Recording) -> Path | None:
        return self.audio_dir / rec.file if rec.file else None

    def pending(self) -> list[Recording]:
        return [r for r in self.all() if r.stage is not None]

    # ---- writing -----------------------------------------------------------------------------

    def new_path(self, extension: str = EXTENSION) -> tuple[str, Path]:
        """An id and the file to record into."""
        rec_id = str(uuid.uuid4())
        return rec_id, self.audio_dir / f"{rec_id}{extension}"

    def add(self, rec_id: str, path: Path, recorded_at: str, duration: float,
            mobile_data: dict[str, bool] | None = None) -> Recording:
        path = Path(path)
        rec = Recording(id=rec_id, file=path.name, recorded_at=recorded_at, duration=round(duration, 1),
                        size=path.stat().st_size if path.exists() else 0,
                        mobile_data={s: bool((mobile_data or {}).get(s)) for s in STAGES})
        self.save(rec)
        return rec

    def save(self, rec: Recording) -> None:
        with self._lock:
            items = [d for d in self._store.get("recordings", []) if d.get("id") != rec.id]
            items.append(asdict(rec))
            self._store.set("recordings", items)

    def update(self, rec_id: str, **changes: Any) -> Recording | None:
        with self._lock:
            rec = self.get(rec_id)
            if rec is None:
                return None
            for k, v in changes.items():
                setattr(rec, k, v)
            self.save(rec)
            return rec

    def set_mobile_data(self, rec_id: str, stage: str, allowed: bool) -> Recording | None:
        with self._lock:
            rec = self.get(rec_id)
            if rec is None:
                return None
            rec.mobile_data = {**rec.mobile_data, stage: bool(allowed)}
            self.save(rec)
            return rec

    def remove_audio(self, rec_id: str) -> None:
        with self._lock:
            rec = self.get(rec_id)
            if rec is None or not rec.file:
                return
            path = self.path(rec)
            if path:
                path.unlink(missing_ok=True)
            rec.file = ""
            self.save(rec)

    def delete(self, rec_id: str) -> None:
        with self._lock:
            self.remove_audio(rec_id)
            items = [d for d in self._store.get("recordings", []) if d.get("id") != rec_id]
            self._store.set("recordings", items)

    def orphans(self) -> list[Path]:
        """Audio files with no index entry (e.g. the app was killed while recording)."""
        known = {r.file for r in self.all()}
        return [p for p in self.audio_dir.iterdir() if p.suffix.lower() in MIMES and p.name not in known]

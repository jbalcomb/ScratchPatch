"""Move each recording one step along: send it, fetch its transcript, have it archived.

Pure logic (no Flet), so it is tested directly. The app's :class:`VoiceSync` (screen.py) runs
:func:`sync_once` on a background thread whenever something could have changed: a new recording,
Wi-Fi connecting, the screen opening, and every few seconds while the workstation is busy.

Network rule: a recording's next step runs on Wi-Fi, or on any network if the recording allows mobile
data for that stage. When the network type isn't known (desktop), everything counts as Wi-Fi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from .client import Client, ServerError
from .library import ARCHIVE, ARCHIVED, ARCHIVING, QUEUED, RECORDED, TRANSCRIBED, Library, Recording, now_iso

SERVER_DONE_TRANSCRIBING = {"transcribed", "archiving", "archived"}


@dataclass
class SyncResult:
    uploaded: int = 0
    transcribed: int = 0
    archived: int = 0
    waiting_wifi: int = 0  # next step needs Wi-Fi and the phone is on mobile data
    busy: int = 0  # waiting for the workstation (transcribing or archiving)
    errors: list[str] = field(default_factory=list)
    unreachable: str = ""  # set when the workstation didn't answer: nothing else was tried

    @property
    def changed(self) -> bool:
        return bool(self.uploaded or self.transcribed or self.archived)

    def summary(self) -> str:
        parts = []
        if self.uploaded:
            parts.append(f"{self.uploaded} sent")
        if self.transcribed:
            parts.append(f"{self.transcribed} transcribed")
        if self.archived:
            parts.append(f"{self.archived} archived")
        return "Voice notes: " + ", ".join(parts) if parts else ""


def recover_orphans(lib: Library) -> int:
    """Audio left without an index entry (app killed mid-recording) becomes a recording again."""
    count = 0
    for path in lib.orphans():
        if path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            continue
        when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        lib.add(path.stem, path, when, 0.0)
        count += 1
    return count


def _apply_server(rec: Recording, info: dict) -> None:
    rec.transcript = info.get("transcript") or rec.transcript
    rec.transcribed_at = info.get("transcribed_at") or rec.transcribed_at or now_iso()
    for key in ("title", "category", "summary"):
        if info.get(key):
            setattr(rec, key, info[key])
    if isinstance(info.get("tags"), list):
        rec.tags = [str(t) for t in info["tags"]]


def step(lib: Library, client: Client, rec: Recording, on_wifi: bool, auto_archive: bool, keep_audio: bool,
         result: SyncResult) -> None:
    """One recording, as far as its network rules let it go now. Raises ServerError when the
    workstation is unreachable. The caller checked the rule for the recording's current stage; the
    archive stage's rule is checked here, since transcribing can finish in the same pass."""
    if rec.status == RECORDED:
        path = lib.path(rec)
        if not path or not path.exists():
            rec.error = "The audio file is missing on the phone."
            lib.save(rec)
            return
        client.upload(rec, path.read_bytes())
        rec.status, rec.uploaded_at, rec.error = QUEUED, now_iso(), ""
        lib.save(rec)
        result.uploaded += 1

    if rec.status == QUEUED:
        info = client.get(rec.id)
        if info is None:  # the workstation lost it: send it again next time
            rec.status, rec.error = RECORDED, "The workstation didn't have it; it will be sent again."
            lib.save(rec)
            return
        if info.get("status") == "failed":
            rec.error = f"Transcription failed: {info.get('error') or 'unknown error'}"
            lib.save(rec)
            return
        if info.get("status") not in SERVER_DONE_TRANSCRIBING:
            result.busy += 1
            return
        _apply_server(rec, info)
        rec.status, rec.error = TRANSCRIBED, ""
        lib.save(rec)
        result.transcribed += 1
        if info.get("status") == "archived":  # the workstation archived it by itself
            rec.status = ARCHIVING

    if rec.status == TRANSCRIBED:
        if not auto_archive:
            return
        if not (on_wifi or rec.may_use_mobile_data(ARCHIVE)):
            result.waiting_wifi += 1
            return
        client.archive(rec.id)
        rec.status, rec.error = ARCHIVING, ""
        lib.save(rec)

    if rec.status == ARCHIVING:
        info = client.get(rec.id)
        if info is None:
            rec.status, rec.error = RECORDED, "The workstation didn't have it; it will be sent again."
            lib.save(rec)
            return
        if info.get("status") != "archived":
            if info.get("error"):
                rec.error = f"Archiving: {info['error']}"
                lib.save(rec)
            result.busy += 1
            return
        _apply_server(rec, info)
        rec.status, rec.archived_at, rec.error = ARCHIVED, info.get("archived_at") or now_iso(), ""
        lib.save(rec)
        result.archived += 1
        if not keep_audio:
            lib.remove_audio(rec.id)


def sync_once(lib: Library, client: Client, on_wifi: bool, auto_archive: bool = True, keep_audio: bool = True,
              cancelled: Callable[[], bool] = lambda: False, archive_now: Iterable[str] = ()) -> SyncResult:
    """Every pending recording (oldest first), each as far as its network rules allow.

    ``archive_now``: ids to archive even when automatic archiving is off ("Archive now")."""
    result = SyncResult()
    if not client.configured:
        result.unreachable = "Set the workstation address in this job's settings (the tune icon)."
        return result
    archive_now = set(archive_now)
    for rec in lib.pending():
        if cancelled():
            break
        if not rec.allowed(on_wifi):
            result.waiting_wifi += 1
            continue
        try:
            step(lib, client, rec, on_wifi, auto_archive or rec.id in archive_now, keep_audio, result)
        except ServerError as exc:
            if exc.unreachable:
                result.unreachable = str(exc)
                break
            rec.error = str(exc)
            lib.save(rec)
            result.errors.append(f"{rec.display_title}: {exc}")
    return result

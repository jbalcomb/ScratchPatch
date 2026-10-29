"""Extract Archive, without the UI: scan an archive, check a destination, extract as a task."""

from __future__ import annotations

import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from taskdeck.core import Cancelled, JobNature, Task, copying
from taskdeck.shared import archive, files, gdrive, google_oauth, http
from taskdeck.shared import drive_uploads as uploads
from taskdeck.shared.gdrive import DriveLocation


@dataclass
class Scan:
    path: Path
    format: str = ""
    files: int = 0
    folders: int = 0
    unpacked: int = 0
    encrypted: bool = False  # some files need a password
    names_hidden: bool = False  # encrypted file names: nothing listed until the password is right
    error: str | None = None
    unsafe: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.names_hidden:
            return f"{files.human_size(self.path.stat().st_size)} · encrypted archive"
        parts = [files.human_size(self.path.stat().st_size), self.format, f"{self.files:,} file{'s' * (self.files != 1)}"]
        return " · ".join(p for p in parts if p)


def scan(path: Path, password: str | None = None) -> Scan:
    """Read the archive's index (fast): format, counts, unpacked size, encryption."""
    result = Scan(path)
    try:
        with archive.Archive(path, password or None) as a:
            entries = a.entries()
            result.format = a.format
            result.unsafe = a.unsafe_entries()
    except archive.ArchiveError as exc:
        if exc.code == archive.E_NEED_PASSWORD:
            result.names_hidden = result.encrypted = True
        elif exc.code == archive.E_WRONG_PASSWORD:
            result.names_hidden = result.encrypted = True
            result.error = str(exc)
        else:
            result.error = str(exc)
        return result
    result.files = sum(1 for e in entries if not e.is_dir)
    result.folders = len(entries) - result.files
    result.unpacked = sum(e.size for e in entries if not e.is_dir)
    result.encrypted = any(e.encrypted for e in entries)
    if result.unsafe:
        result.error = (f"This archive contains a path that points outside the folder it's extracted to "
                        f"(“{result.unsafe[0]}”). TaskDeck won't extract it.")
    return result


def default_destination(path: Path) -> tuple[Path, str]:
    """Next to the archive, in a new folder named after it."""
    return path.parent, files.archive_stem(path.name)


def problems(s: Scan, parent: "Path | DriveLocation", name: str | None, password: str,
             drive_connected: bool = False, temp_root: Path | None = None) -> list[str]:
    """What would stop the extraction, checked before starting."""
    if isinstance(parent, DriveLocation):
        return drive_problems(s, name, password, drive_connected, temp_root)
    out = []
    if s.error:
        out.append(s.error)
    if s.encrypted and not password:
        out.append("Enter the archive's password.")
    if name is not None and (err := files.valid_folder_name(name)):
        out.append(err)
    target_parent = parent if parent.exists() else None
    if target_parent is None:
        out.append(f"The folder {files.display_path(parent)} doesn't exist any more. Choose another.")
    elif not os.access(target_parent, os.W_OK):
        out.append(f"TaskDeck can't write to {files.display_path(parent)}. Choose another folder.")
    if s.unpacked and target_parent is not None:
        free = files.free_space(target_parent)
        if s.unpacked > free:
            out.append(f"Not enough space: needs {files.human_size(s.unpacked)}, {files.human_size(free)} free.")
    return out


def extract(task: Task, path: Path, dest: Path, password: str | None) -> str:
    """The background work: extract everything into dest, replacing existing files."""
    count = 0
    try:
        with archive.Archive(path, password or None) as a:
            entries = a.entries()
            count = sum(1 for e in entries if not e.is_dir)
            total = sum(e.size for e in entries if not e.is_dir)
            task.log("archive", name=path.name, format=a.format, size=path.stat().st_size, files=count,
                     bytes=total, encrypted=bool(password))
            task.report(0, total)

            def progress(done: int, total_bytes: int) -> bool:
                task.report(done, total_bytes or total)
                return task.cancel_requested

            a.extract(dest, progress=progress, password=password or None)
            task.report(total, total)
    except archive.ArchiveError as exc:
        if exc.code == archive.E_CANCELLED:
            raise Cancelled(f"Stopped. Files already extracted stay in {files.display_path(dest)}.") from None
        raise RuntimeError(str(exc)) from None
    return f"Extracted {count:,} file{'s' * (count != 1)} ({files.human_size(task.total)}) to {files.display_path(dest)}"


# ---- Google Drive ------------------------------------------------------------------------------

def drive_problems(s: Scan, name: str | None, password: str, connected: bool, temp_root: Path | None) -> list[str]:
    out = []
    if s.error:
        out.append(s.error)
    if s.encrypted and not password:
        out.append("Enter the archive's password.")
    if name is not None and (err := files.valid_folder_name(name)):
        out.append(err)
    if not connected:
        out.append("Connect Google Drive first (Settings → Accounts).")
    if s.unpacked and temp_root is not None:
        free = files.free_space(temp_root)
        if s.unpacked > free:
            out.append(f"Not enough space on the phone to unpack before uploading: needs "
                       f"{files.human_size(s.unpacked)}, {files.human_size(free)} free.")
    return out


# An archive is a fixed snapshot: extracting it to Drive is a one-time copy (see taskdeck.core.copying).
NATURE = JobNature.ONE_TIME_COPY

PAUSED = uploads.PAUSED


def extract_to_drive(task: Task, path: Path, password: str | None, drive: gdrive.DriveClient,
                     location: DriveLocation, name: str | None, temp_root: Path) -> str:
    """Unpack into a temporary folder on the phone, upload everything, delete the temporary copy.

    A one-time copy (NATURE): same-named folders in Drive are reused; a same-named file with the same
    content (size and MD5 checksum) is already done and skipped, so running it again after a cancel or
    crash only sends what's missing. Any other same-named file is a broken leftover of an earlier run:
    the file is uploaded fresh and the leftover (and any duplicates) then goes to Drive's trash."""
    # Unique per run: after a reload, a task of the previous app instance (same process, task ids
    # starting again at 1) may still be using its own folder.
    temp = temp_root / f"task-{task.id}-{secrets.token_hex(4)}-{os.getpid()}"
    count = sent = skipped = skipped_bytes = 0
    # Android cuts the network of apps in the background: wait for it instead of failing.
    drive.keep_waiting = lambda: not task.cancel_requested
    drive.on_offline = lambda offline: task.set_note(PAUSED if offline else "")
    drive.on_throttle = lambda wait, status: task.log("throttle", status=status, pause=round(wait, 1))
    target = location.display(name)
    try:
        with archive.Archive(path, password or None) as a:
            entries = a.entries()
            total = sum(e.size for e in entries if not e.is_dir)
            task.log("archive", name=path.name, format=a.format, size=path.stat().st_size,
                     files=sum(1 for e in entries if not e.is_dir), bytes=total, encrypted=bool(password))
            # Uploading counts files, not bytes: Google takes seconds per file however small, so by bytes a
            # few big files make it look 99% done with thousands of small files (hours) still to go.
            task.set_phases(("Unpacking", total), ("Checking what's already in Google Drive", 0, "files"))
            unpack_started = time.monotonic()
            a.extract(temp, progress=lambda done, _t: (task.advance(0, done), task.cancel_requested)[1],
                      password=password or None)
            task.log("unpacked", secs=round(time.monotonic() - unpack_started, 2))
        task.advance(1, 0)
        root_id = location.folder_id
        if name:
            existing, _ = drive.children(root_id)
            root_id = existing.get(name) or drive.create_folder(name, root_id).id
            task.result = location.child(gdrive.Folder(root_id, name))  # "Open in Drive" goes straight there
        # 1. Folders, listings and skips, one by one (cheap). 2. The uploads, several at once: Google
        # takes ~5 s to confirm each new file, so one at a time thousands of small files take hours.
        folder_ids = {Path("."): root_id}
        todo: list[tuple[Path, str, str, Path, int, str | None]] = []  # local, parent, name, rel, size, replace
        for dirpath, dirnames, filenames in os.walk(temp):
            dirnames.sort()
            here = Path(dirpath).relative_to(temp)
            parent_id = folder_ids[here]
            sub_folders, sub_files = drive.children_files(parent_id)
            for d in dirnames:
                folder_ids[here / d] = sub_folders.get(d) or drive.create_folder(d, parent_id).id
                if task.cancel_requested:
                    raise gdrive.UploadCancelled()
            for f in sorted(filenames):
                local = Path(dirpath) / f
                size = local.stat().st_size
                plan = copying.plan(NATURE, size, lambda: copying.md5_of(local), sub_files.get(f, []))
                if not plan.upload:  # already there, the same content (an earlier, interrupted run)
                    for leftover in plan.trash:  # duplicates of the name
                        drive.trash(leftover)
                    task.log("file", path=str(here / f), bytes=size, skipped=True, trashed=len(plan.trash))
                    local.unlink()
                    skipped += 1
                    skipped_bytes += size
                    continue
                todo.append((local, parent_id, f, here / f, size, plan.replace, plan.trash))
        label = "Uploading to Google Drive" + (f" ({skipped:,} already there)" if skipped else "")
        task.set_phase(1, label=label, total=len(todo))
        count, sent = uploads.upload_all(task, drive, todo)
    except uploads.Interrupted as stop:
        count, sent = stop.count, stop.sent
        if isinstance(stop.error, (gdrive.UploadCancelled, Cancelled)):
            raise Cancelled(f"Stopped. {uploads.were_uploaded(count)} to {target}.") from None
        done = f" {uploads.were_uploaded(count)} before that." if count else ""
        raise RuntimeError(f"{stop.error}{done}") from None
    except archive.ArchiveError as exc:
        if exc.code == archive.E_CANCELLED:
            raise Cancelled("Stopped before uploading anything.") from None
        raise RuntimeError(str(exc)) from None
    except (gdrive.UploadCancelled, Cancelled):  # Cancel, also while paused or waiting for Wi-Fi
        raise Cancelled(f"Stopped. {uploads.were_uploaded(count)} to {target}.") from None
    except (gdrive.DriveError, http.HttpError, google_oauth.OAuthError, OSError) as exc:
        done = f" {uploads.were_uploaded(count)} before that." if count else ""
        raise RuntimeError(f"{exc}{done}") from None
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    message = f"Uploaded {count:,} file{'s' * (count != 1)} ({files.human_size(sent)}) to {target}"
    if skipped:
        message += f". Skipped {skipped:,} already there ({files.human_size(skipped_bytes)})."
    return message

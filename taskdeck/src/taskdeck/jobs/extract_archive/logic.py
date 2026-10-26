"""Extract Archive, without the UI: scan an archive, check a destination, extract as a task."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from taskdeck.core import Cancelled, Task
from taskdeck.shared import archive, files


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


def problems(s: Scan, parent: Path, name: str | None, password: str) -> list[str]:
    """What would stop the extraction, checked before starting."""
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

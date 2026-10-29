"""Pushing a parsed-executable folder to Google Drive, as a mirror of the folder on the phone.

A one-time copy (taskdeck.core.copying): the folder is a snapshot of one executable, so a same-named
file with the same content is already done and skipped (parsing again and pushing again only sends what
changed), and a different one is a leftover, uploaded fresh. Unlike Extract Archive, files and folders in
Drive that the local folder no longer has (e.g. chunks of an earlier parse with another chunk size) go to
Drive's trash, so the agent never reads a stale chunk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from taskdeck.core import Cancelled, JobNature, Task, copying
from taskdeck.shared import drive_uploads, gdrive
from taskdeck.shared.gdrive import DriveLocation

NATURE = JobNature.ONE_TIME_COPY


class _JobTask(Task):
    """A Task for the uploader, cancelled with the job's own Cancel button (the pipeline's context)."""

    def __init__(self, cancelled: Callable[[], bool]):
        self._is_cancelled = cancelled
        super().__init__(0, "push to Google Drive", "exe_parse")

    @property
    def cancel_requested(self) -> bool:
        return self._is_cancelled()

    @cancel_requested.setter
    def cancel_requested(self, _value: bool) -> None:
        pass  # Task's __init__ sets it; the context decides


def folder(drive: gdrive.DriveClient, path: str) -> DriveLocation:
    """The Drive folder at ``path`` ("TaskDeck/Executables"), under My Drive, created where missing."""
    location = DriveLocation()
    for name in (p.strip() for p in path.replace("\\", "/").split("/")):
        if name:
            folders, _ = drive.children(location.folder_id)
            found = folders.get(name)
            location = location.child(gdrive.Folder(found, name) if found else drive.create_folder(name, location.folder_id))
    return location


def push(drive: gdrive.DriveClient, local: Path, parent_path: str,
         cancelled: Callable[[], bool] = lambda: False) -> tuple[DriveLocation, int, int, int]:
    """Mirror ``local`` into ``parent_path``/<local's name> in Drive.
    Returns (the folder, files uploaded, files already there, stale items trashed)."""
    task = _JobTask(cancelled)
    drive.keep_waiting = lambda: not cancelled()
    parent = folder(drive, parent_path)
    folders, _ = drive.children(parent.folder_id)
    root = parent.child(gdrive.Folder(folders[local.name], local.name) if local.name in folders
                        else drive.create_folder(local.name, parent.folder_id))
    ids = {Path("."): root.folder_id}
    todo, skipped, trashed = [], 0, 0
    for dirpath, dirnames, filenames in os.walk(local):
        if cancelled():
            raise Cancelled()
        dirnames.sort()
        here = Path(dirpath).relative_to(local)
        sub_folders, sub_files = drive.children_files(ids[here])
        for d in dirnames:
            ids[here / d] = sub_folders.get(d) or drive.create_folder(d, ids[here]).id
        for gone in set(sub_folders) - set(dirnames):
            drive.trash(sub_folders[gone])
            trashed += 1
        for gone in set(sub_files) - set(filenames):
            for copy in sub_files[gone]:
                drive.trash(copy.id)
                trashed += 1
        for f in sorted(filenames):
            path = Path(dirpath) / f
            size = path.stat().st_size
            plan = copying.plan(NATURE, size, lambda p=path: copying.md5_of(p), sub_files.get(f, []))
            if not plan.upload:
                for leftover in plan.trash:
                    drive.trash(leftover)
                skipped += 1
                continue
            todo.append((path, ids[here], f, here / f, size, plan.replace, plan.trash))
    try:
        count, _ = drive_uploads.upload_all(task, drive, todo, keep=True)
    except drive_uploads.Interrupted as stop:
        if isinstance(stop.error, (gdrive.UploadCancelled, Cancelled)):
            raise Cancelled() from None
        raise stop.error from None
    return root, count, skipped, trashed

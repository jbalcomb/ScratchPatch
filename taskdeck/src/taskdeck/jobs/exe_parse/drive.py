"""Pushing a parsed-executable folder to Google Drive, as a mirror of the folder on the phone.

A one-time copy (taskdeck.core.copying): the folder is a snapshot of one executable, so a same-named
file with the same content is already done and skipped (parsing again and pushing again only sends what
changed), and a different one is a leftover, uploaded fresh. Unlike Extract Archive, files and folders in
Drive that the local folder no longer has (e.g. chunks of an earlier parse with another chunk size) go to
Drive's trash, so the agent never reads a stale chunk.

Runs as a background task (taskdeck.core.tasks) with two phases, like Extract Archive's upload:
checking what's already in Drive, then uploading (several files at once, see shared.drive_uploads).
"""

from __future__ import annotations

import os
from pathlib import Path

from taskdeck.core import Cancelled, JobNature, Task, copying
from taskdeck.shared import drive_uploads, files, gdrive, google_oauth, http
from taskdeck.shared.gdrive import DriveLocation

NATURE = JobNature.ONE_TIME_COPY


def _parts(path: str) -> list[str]:
    return [p.strip() for p in path.replace("\\", "/").split("/") if p.strip()]


def target(parent_path: str, name: str) -> str:
    """Where the folder goes, for the screen: "Google Drive › My Drive › TaskDeck › Executables › WIZARDS.EXE"."""
    return DriveLocation(names=("My Drive", *_parts(parent_path))).display(name)


def folder(client: gdrive.DriveClient, path: str) -> DriveLocation:
    """The Drive folder at ``path`` ("TaskDeck/Executables"), under My Drive, created where missing."""
    location = DriveLocation()
    for name in _parts(path):
        folders, _ = client.children(location.folder_id)
        found = folders.get(name)
        location = location.child(gdrive.Folder(found, name) if found else client.create_folder(name, location.folder_id))
    return location


def push(task: Task, client: gdrive.DriveClient, local: Path, parent_path: str) -> str:
    """Mirror ``local`` into ``parent_path``/<local's name> in Drive. Sets ``task.result`` to that folder
    (for "Open in Drive") and returns the message for the finished task."""
    local_files = sum(len(names) for _, _, names in os.walk(local))
    task.set_phases(("Checking what's already in Google Drive", local_files, "files"),
                    ("Uploading to Google Drive", 0, "files"))
    # Android cuts the network of apps in the background: wait for it instead of failing.
    client.keep_waiting = lambda: not task.cancel_requested
    client.on_offline = lambda offline: task.set_note(drive_uploads.PAUSED if offline else "")
    client.on_throttle = lambda wait, status: task.log("throttle", status=status, pause=round(wait, 1))
    count = skipped = trashed = 0
    target = "Google Drive"
    try:
        parent = folder(client, parent_path)
        folders, _ = client.children(parent.folder_id)
        found = folders.get(local.name)
        root = parent.child(gdrive.Folder(found, local.name) if found else client.create_folder(local.name, parent.folder_id))
        task.result, target = root, root.display()
        ids = {Path("."): root.folder_id}
        todo, checked = [], 0
        for dirpath, dirnames, filenames in os.walk(local):
            if task.cancel_requested:
                raise gdrive.UploadCancelled()
            dirnames.sort()
            here = Path(dirpath).relative_to(local)
            sub_folders, sub_files = client.children_files(ids[here])
            for d in dirnames:
                ids[here / d] = sub_folders.get(d) or client.create_folder(d, ids[here]).id
            for gone in set(sub_folders) - set(dirnames):  # left from an earlier parse
                client.trash(sub_folders[gone])
                trashed += 1
            for gone in set(sub_files) - set(filenames):
                for copy in sub_files[gone]:
                    client.trash(copy.id)
                    trashed += 1
            for f in sorted(filenames):
                path = Path(dirpath) / f
                size = path.stat().st_size
                plan = copying.plan(NATURE, size, lambda p=path: copying.md5_of(p), sub_files.get(f, []))
                checked += 1
                task.advance(0, checked)
                if not plan.upload:
                    for leftover in plan.trash:
                        client.trash(leftover)
                    skipped += 1
                    continue
                todo.append((path, ids[here], f, here / f, size, plan.replace, plan.trash))
        label = "Uploading to Google Drive" + (f" ({skipped:,} already there)" if skipped else "")
        task.set_phase(1, label=label, total=len(todo))
        task.log("plan", files=local_files, upload=len(todo), skipped=skipped, trashed=trashed)
        count, sent = drive_uploads.upload_all(task, client, todo, keep=True)
    except drive_uploads.Interrupted as stop:
        count = stop.count
        if isinstance(stop.error, (gdrive.UploadCancelled, Cancelled)):
            raise Cancelled(f"Stopped. {drive_uploads.were_uploaded(count)} to {target}.") from None
        raise RuntimeError(f"{stop.error}{_before(count)}") from None
    except (gdrive.UploadCancelled, Cancelled):  # Cancel, also while paused or waiting for Wi-Fi
        raise Cancelled(f"Stopped. {drive_uploads.were_uploaded(count)} to {target}.") from None
    except (gdrive.DriveError, http.HttpError, google_oauth.OAuthError, OSError) as exc:
        raise RuntimeError(f"{exc}{_before(count)}") from None
    message = f"Uploaded {count:,} file{'s' * (count != 1)} ({files.human_size(sent)}) to {target}"
    if skipped:
        message += f". Skipped {skipped:,} already there"
    if trashed:
        message += f". Moved {trashed:,} left from an earlier parse to the trash"
    return message + "."


def _before(count: int) -> str:
    return f" {drive_uploads.were_uploaded(count)} before that." if count else ""

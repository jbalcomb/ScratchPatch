"""Google Drive (REST v3): folders and resumable uploads, signed in as the user.

With the ``drive.file`` scope TaskDeck only sees folders and files it created itself, so
``list_folders`` lists TaskDeck's folders, not the whole Drive.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import http

API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT = "root"  # My Drive
CHUNK = 8 * 1024 * 1024  # upload piece size; Google requires a multiple of 256 KiB
RETRIES = 5


class DriveError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class UploadCancelled(Exception):
    pass


@dataclass(frozen=True)
class Folder:
    id: str
    name: str


@dataclass(frozen=True)
class DriveLocation:
    """A folder in Google Drive: its id and the names from My Drive down, for display."""

    folder_id: str = ROOT
    names: tuple[str, ...] = ("My Drive",)

    def display(self, name: str | None = None) -> str:
        return " › ".join(("Google Drive", *self.names, *((name,) if name else ())))

    def child(self, folder: Folder) -> "DriveLocation":
        return DriveLocation(folder.id, (*self.names, folder.name))


def _explain(resp: http.Response, what: str) -> DriveError:
    data = resp.json() if resp.body else None
    err = (data or {}).get("error", {}) if isinstance(data, dict) else {}
    reason = ""
    if isinstance(err, dict):
        errors = err.get("errors") or [{}]
        reason = str(errors[0].get("reason", "")) if isinstance(errors, list) and errors else ""
        detail = str(err.get("message", ""))
    else:
        detail = str(err)
    if resp.status == 401:
        return DriveError("Google sign-in expired. Reconnect Google Drive in Settings → Accounts.", 401)
    if reason in ("storageQuotaExceeded", "quotaExceeded") or "storage quota" in detail.lower():
        return DriveError("Your Google Drive is full.", resp.status)
    if resp.status == 404:
        return DriveError(f"Google Drive: {what} not found (it may have been deleted).", 404)
    if resp.status == 403 and "has not been used" in detail:
        return DriveError("The Google Drive API isn't turned on for this project "
                          "(docs/GOOGLE_DRIVE_SETUP.md, step 1).", 403)
    return DriveError(f"Google Drive couldn't {what}: HTTP {resp.status}" + (f" ({detail})" if detail else ""),
                      resp.status)


class DriveClient:
    def __init__(self, token: Callable[[], str], send: Callable[..., http.Response] = http.send,
                 sleep: Callable[[float], None] = time.sleep, chunk: int = CHUNK):
        self.token = token
        self.send = send
        self.sleep = sleep
        self.chunk = chunk

    def _call(self, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None,
              timeout: float = 60) -> http.Response:
        """One API call with the access token; retries rate limits, server errors and network hiccups."""
        for attempt in range(RETRIES):
            hdrs = {"Authorization": f"Bearer {self.token()}", **(headers or {})}
            try:
                resp = self.send(method, url, body, hdrs, timeout)
            except http.HttpError:
                if attempt == RETRIES - 1:
                    raise
            else:
                if resp.status not in (429, 500, 502, 503, 504):
                    return resp
                if attempt == RETRIES - 1:
                    return resp
            self.sleep(min(2 ** attempt, 30))
        raise AssertionError("unreachable")

    # ---- folders ------------------------------------------------------------------------

    def list_folders(self, parent: str = ROOT) -> list[Folder]:
        q = f"mimeType='{FOLDER_MIME}' and trashed=false and '{parent}' in parents"
        folders, page = [], ""
        while True:
            params = {"q": q, "fields": "nextPageToken,files(id,name)", "orderBy": "name", "pageSize": "1000"}
            if page:
                params["pageToken"] = page
            resp = self._call("GET", f"{API}?{urllib.parse.urlencode(params)}")
            if not resp.ok:
                raise _explain(resp, "list folders")
            data = resp.json() or {}
            folders += [Folder(f["id"], f["name"]) for f in data.get("files", [])]
            page = data.get("nextPageToken", "")
            if not page:
                return folders

    def children(self, parent: str) -> tuple[dict[str, str], dict[str, str]]:
        """(folders, files) directly in parent, as name -> id (TaskDeck's own only, with drive.file)."""
        q = f"trashed=false and '{parent}' in parents"
        folders: dict[str, str] = {}
        files: dict[str, str] = {}
        page = ""
        while True:
            params = {"q": q, "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": "1000"}
            if page:
                params["pageToken"] = page
            resp = self._call("GET", f"{API}?{urllib.parse.urlencode(params)}")
            if not resp.ok:
                raise _explain(resp, "read a folder")
            data = resp.json() or {}
            for f in data.get("files", []):
                (folders if f.get("mimeType") == FOLDER_MIME else files)[f["name"]] = f["id"]
            page = data.get("nextPageToken", "")
            if not page:
                return folders, files

    def create_folder(self, name: str, parent: str = ROOT) -> Folder:
        body = json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}).encode()
        resp = self._call("POST", f"{API}?fields=id,name", body, {"Content-Type": "application/json; charset=UTF-8"})
        if not resp.ok:
            raise _explain(resp, f"create the folder “{name}”")
        data = resp.json()
        return Folder(data["id"], data.get("name", name))

    # ---- uploads -------------------------------------------------------------------------

    def upload(self, path: Path, parent: str, name: str | None = None,
               progress: Callable[[int], bool] | None = None, replace: str | None = None) -> str:
        """Upload a file (resumable, in pieces); with ``replace`` (a file id) its content is replaced instead.
        progress(bytes sent) returns True to cancel. Returns the file id."""
        size = os.path.getsize(path)
        name = name or path.name
        session = self._start_session(name, parent, size, replace)
        offset, restarts, failures = 0, 0, 0
        with open(path, "rb") as fh:
            while True:
                if failures > RETRIES:
                    raise DriveError(f"Google Drive kept failing while uploading “{name}”. Try again later.")
                fh.seek(offset)
                piece = fh.read(self.chunk)
                end = offset + len(piece) - 1
                content_range = f"bytes {offset}-{end}/{size}" if size else "bytes */0"
                try:
                    resp = self.send("PUT", session, piece, {"Content-Length": str(len(piece)),
                                                             "Content-Range": content_range}, 120)
                except http.HttpError:
                    failures += 1
                    offset = self._resume_offset(session, size)  # connection dropped: ask Google what arrived
                    continue
                if resp.status in (200, 201):
                    if progress:
                        progress(size)
                    return str((resp.json() or {}).get("id", ""))
                if resp.status == 308:
                    offset, failures = _range_end(resp.headers.get("range")) + 1, 0
                    if progress and progress(offset):
                        raise UploadCancelled()
                    continue
                if resp.status in (404, 410) and restarts < 1:  # upload session expired: start over once
                    restarts += 1
                    session, offset = self._start_session(name, parent, size, replace), 0
                    continue
                if resp.status in (429, 500, 502, 503, 504):
                    failures += 1
                    self.sleep(min(2 ** failures, 30))
                    offset = self._resume_offset(session, size)
                    continue
                raise _explain(resp, f"upload “{name}”")

    def _start_session(self, name: str, parent: str, size: int, replace: str | None = None) -> str:
        if replace:  # new content for an existing file (same id, same place)
            method, url, meta = "PATCH", f"{UPLOAD}/{replace}?uploadType=resumable&fields=id", {}
        else:
            method, url, meta = "POST", f"{UPLOAD}?uploadType=resumable&fields=id", {"name": name, "parents": [parent]}
        resp = self._call(method, url, json.dumps(meta).encode(),
                          {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(size)})
        if not resp.ok or "location" not in resp.headers:
            raise _explain(resp, f"start uploading “{name}”")
        return resp.headers["location"]

    def _resume_offset(self, session: str, size: int) -> int:
        """Ask how much of an interrupted upload Google has (retrying while the network is down)."""
        for attempt in range(RETRIES):
            try:
                resp = self.send("PUT", session, b"", {"Content-Length": "0", "Content-Range": f"bytes */{size}"}, 60)
            except http.HttpError:
                self.sleep(min(2 ** attempt, 30))
                continue
            if resp.status == 308:
                return _range_end(resp.headers.get("range")) + 1
            if resp.status in (200, 201):
                return size
            self.sleep(min(2 ** attempt, 30))
        raise DriveError("Lost the connection to Google Drive while uploading.")


def _range_end(header: str | None) -> int:
    """'bytes=0-1048575' -> 1048575; no header (nothing received yet) -> -1."""
    if not header or "-" not in header:
        return -1
    return int(header.rsplit("-", 1)[1])

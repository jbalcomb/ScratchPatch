"""Google Drive (REST v3): folders and resumable uploads, signed in as the user.

With the ``drive.file`` scope TaskDeck only sees folders and files it created itself, so
``list_folders`` lists TaskDeck's folders, not the whole Drive.
"""

from __future__ import annotations

import io
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


OFFLINE_RETRY_MAX = 15  # seconds between tries while waiting for the network to come back


class DriveClient:
    """``keep_waiting`` turns network loss into a pause: instead of giving up after a few tries,
    requests are retried (every few seconds) for as long as keep_waiting() is True, and
    ``on_offline(True/False)`` reports when that starts and ends. Android cuts the network of apps
    in the background, so without it switching apps mid-upload ended the upload."""

    def __init__(self, token: Callable[[], str], send: Callable[..., http.Response] = http.send,
                 sleep: Callable[[float], None] = time.sleep, chunk: int = CHUNK,
                 keep_waiting: Callable[[], bool] | None = None,
                 on_offline: Callable[[bool], None] | None = None):
        self.token = token
        self.send = send
        self.sleep = sleep
        self.chunk = chunk
        self.keep_waiting = keep_waiting
        self.on_offline = on_offline or (lambda offline: None)
        self._offline = False

    def _wait_offline(self, attempt: int) -> bool:
        """The network is down: pause and say so. False = stop trying (raise the error)."""
        if self.keep_waiting is None:
            if attempt >= RETRIES - 1:
                return False
            self.sleep(min(2 ** attempt, 30))
            return True
        if not self.keep_waiting():
            raise UploadCancelled()
        if not self._offline:
            self._offline = True
            self.on_offline(True)
        self.sleep(min(2 ** min(attempt, 4), OFFLINE_RETRY_MAX))
        return True

    def _online(self) -> None:
        if self._offline:
            self._offline = False
            self.on_offline(False)

    def _call(self, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None,
              timeout: float = 60) -> http.Response:
        """One API call with the access token; retries rate limits, server errors and network hiccups."""
        attempt = 0
        while True:
            try:
                hdrs = {"Authorization": f"Bearer {self.token()}", **(headers or {})}  # refreshing needs the network too
                resp = self.send(method, url, body, hdrs, timeout)
            except http.HttpError:
                if not self._wait_offline(attempt):
                    raise
                attempt += 1
                continue
            self._online()
            if resp.status in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                self.sleep(min(2 ** attempt, 30))
                attempt += 1
                continue
            return resp

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
        folders, files = self.children_sized(parent)
        return folders, {name: fid for name, (fid, _size) in files.items()}

    def children_sized(self, parent: str) -> tuple[dict[str, str], dict[str, tuple[str, int]]]:
        """(folders as name -> id, files as name -> (id, size in bytes))."""
        q = f"trashed=false and '{parent}' in parents"
        folders: dict[str, str] = {}
        files: dict[str, tuple[str, int]] = {}
        page = ""
        while True:
            params = {"q": q, "fields": "nextPageToken,files(id,name,mimeType,size)", "pageSize": "1000"}
            if page:
                params["pageToken"] = page
            resp = self._call("GET", f"{API}?{urllib.parse.urlencode(params)}")
            if not resp.ok:
                raise _explain(resp, "read a folder")
            data = resp.json() or {}
            for f in data.get("files", []):
                if f.get("mimeType") == FOLDER_MIME:
                    folders[f["name"]] = f["id"]
                else:
                    files[f["name"]] = (f["id"], int(f.get("size") or -1))  # Google Docs have no size
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
                body = Piece(piece, (lambda n, at=offset: progress(at + n)) if progress else None)
                try:
                    resp = self.send("PUT", session, body, {"Content-Length": str(len(piece)),
                                                            "Content-Range": content_range}, 60)
                except http.HttpError:
                    if self.keep_waiting is None:  # when waiting, a dropped network is a pause, not a failure
                        failures += 1
                    offset = self._resume_offset(session, size)  # connection dropped: ask Google what arrived
                    continue
                self._online()
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
        attempt, server_errors = 0, 0
        while server_errors < RETRIES:
            try:
                resp = self.send("PUT", session, b"", {"Content-Length": "0", "Content-Range": f"bytes */{size}"}, 60)
            except http.HttpError:
                if not self._wait_offline(attempt):
                    break
                attempt += 1
                continue
            self._online()
            if resp.status == 308:
                return _range_end(resp.headers.get("range")) + 1
            if resp.status in (200, 201):
                return size
            self.sleep(min(2 ** server_errors, 30))
            server_errors += 1
        raise DriveError("Lost the connection to Google Drive while uploading.")


class Piece(io.BytesIO):
    """An upload piece that reports its progress as it is sent (every REPORT_EVERY bytes), instead of
    only when the whole piece has gone through: smooth progress and speed on slow connections."""

    REPORT_EVERY = 256 * 1024

    def __init__(self, data: bytes, on_sent: Callable[[int], object] | None = None):
        super().__init__(data)
        self.size = len(data)
        self.on_sent = on_sent
        self._reported = 0

    def __len__(self) -> int:
        return self.size

    def read(self, n: int = -1) -> bytes:
        out = super().read(n)
        sent = self.tell()
        if self.on_sent and (sent - self._reported >= self.REPORT_EVERY or (sent == self.size and out)):
            self._reported = sent
            self.on_sent(sent)  # cancelling waits for the end of the piece
        return out


def _range_end(header: str | None) -> int:
    """'bytes=0-1048575' -> 1048575; no header (nothing received yet) -> -1."""
    if not header or "-" not in header:
        return -1
    return int(header.rsplit("-", 1)[1])

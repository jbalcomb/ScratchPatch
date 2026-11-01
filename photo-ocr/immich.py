"""Immich, the single landing spot for photos (PRD.md, Photo ingest), over its REST API with an API key.

Written against the Immich API as documented for v1.1xx/v2 (api.immich.app); not yet run against the
workstation's Immich. Without IMMICH_URL the service keeps photos in a local folder instead
(LocalPhotos), so it can run before Immich is set up.
"""
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ImmichError(Exception):
    pass


def multipart(fields, files):
    """fields {name: str}, files [(name, filename, bytes, mime)] -> (body, content type)."""
    boundary = uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, data, mime in files:
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                   f'Content-Type: {mime}\r\n\r\n'.encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class Immich:
    def __init__(self, url, api_key, timeout=60):
        self.base = url.rstrip("/")
        if not self.base.endswith("/api"):
            self.base += "/api"
        self.key, self.timeout = api_key, timeout
        self._albums = {}

    def _req(self, method, path, body=None, content_type="application/json", raw=False):
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data, method=method,
                                     headers={"x-api-key": self.key, "Accept": "application/json",
                                              **({"Content-Type": content_type} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            raise ImmichError(f"Immich {method} {path}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ImmichError(f"Immich isn't reachable at {self.base}: {getattr(e, 'reason', e)}") from None
        return payload if raw else (json.loads(payload) if payload else None)

    def ping(self):
        try:
            return (self._req("GET", "/server/ping") or {}).get("res") == "pong"
        except ImmichError:
            return False

    def album_id(self, name):
        """The id of the album with this name, created if missing."""
        if name not in self._albums:
            found = [a for a in self._req("GET", "/albums") or [] if a.get("albumName") == name]
            self._albums[name] = found[0]["id"] if found else self._req("POST", "/albums", {"albumName": name})["id"]
        return self._albums[name]

    def album_assets(self, name):
        """[{id, originalFileName, originalPath, ...}] in the album."""
        return self._req("GET", f"/albums/{self.album_id(name)}")["assets"]

    def add_to_album(self, name, asset_ids):
        if asset_ids:
            self._req("PUT", f"/albums/{self.album_id(name)}/assets", {"ids": list(asset_ids)})

    def upload(self, data, filename, album=None, taken=None):
        """Upload one photo; returns its asset id (an existing one if Immich already has the same file)."""
        stamp = (taken or datetime.now(timezone.utc)).isoformat()
        fields = {"deviceAssetId": f"receipts-{uuid.uuid4().hex}", "deviceId": "receipts-service",
                  "fileCreatedAt": stamp, "fileModifiedAt": stamp}
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
        try:
            body, ctype = multipart(fields, [("assetData", filename, data, mime)])
            res = self._req("POST", "/assets", body, ctype)
        except ImmichError as e:
            if "should not exist" not in str(e):
                raise
            # Newer servers dropped the device fields: send without them.
            body, ctype = multipart({k: v for k, v in fields.items() if not k.startswith("device")},
                                    [("assetData", filename, data, mime)])
            res = self._req("POST", "/assets", body, ctype)
        if album:
            self.add_to_album(album, [res["id"]])
        return res["id"]

    def download(self, asset_id):
        return self._req("GET", f"/assets/{urllib.parse.quote(asset_id)}/original", raw=True)

    def library_assets(self, library_id, page_size=250, max_pages=40):
        """Assets of an external library (the Syncthing folder), newest first, with their originalPath."""
        out, page = [], 1
        while page and page <= max_pages:
            res = self._req("POST", "/search/metadata", {"libraryId": library_id, "page": page, "size": page_size,
                                                         "order": "desc"})
            assets = res.get("assets", {})
            out += assets.get("items", [])
            page = int(assets["nextPage"]) if assets.get("nextPage") else None
        return out


class LocalPhotos:
    """Stand-in for Immich: photos in a folder, asset ids 'local-…'. Albums are subfolders."""

    def __init__(self, folder):
        self.folder = Path(folder)

    def ping(self):
        return False

    def upload(self, data, filename, album=None, taken=None):
        asset_id = f"local-{uuid.uuid4().hex}"
        dest = self.folder / (album or "other")
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"{asset_id}{Path(filename).suffix.lower() or '.jpg'}").write_bytes(data)
        return asset_id

    def download(self, asset_id):
        for path in self.folder.glob(f"*/{asset_id}.*"):
            return path.read_bytes()
        raise ImmichError(f"no local photo {asset_id}")

    def album_assets(self, name):
        return []

    def add_to_album(self, name, asset_ids):
        pass

    def library_assets(self, library_id, **_):
        return []


def from_env(home):
    url, key = os.environ.get("IMMICH_URL"), os.environ.get("IMMICH_API_KEY")
    if url and key:
        return Immich(url, key)
    return LocalPhotos(Path(home) / "photos")

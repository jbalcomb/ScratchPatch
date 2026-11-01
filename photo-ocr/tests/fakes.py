"""Stand-ins for llama-server, Immich and Google, as small HTTP servers, so the tests need no GPU or accounts."""
import json
import re
import struct
import sys
import threading
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def png(shade=255):
    """A valid 1x1 PNG; a different shade gives different bytes."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, shade, shade, shade]))) + chunk(b"IEND", b""))


class Server:
    """Runs handle(method, path, query, headers, body) -> (status, bytes|dict, content type) in a thread."""

    def __init__(self, handle):
        outer = self
        self.requests = []

        class H(BaseHTTPRequestHandler):
            def _do(self, method):
                u = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append((method, u.path, body))
                status, payload, ctype = handle(method, u.path, parse_qs(u.query), self.headers, body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype or "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PUT(self):
                self._do("PUT")

            def do_DELETE(self):
                self._do("DELETE")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()  # so later connections are refused, not left waiting


RECEIPT = {"store": "Giant Eagle", "location": "Mentor, OH", "date": "2026-10-12", "tax": 0.42, "total": 7.19,
           "items": [{"name": "BANANAS", "quantity": 1, "price": 1.29}, {"name": "MILK 2% GAL", "quantity": 1, "price": 3.99},
                     {"name": "BREAD WHT", "quantity": 1, "price": 2.49}, {"name": "BONUS SAVINGS", "price": -1.00}]}


class FakeModel:
    """An OpenAI-compatible chat endpoint. Answers by what is asked: receipts get self.receipt (per model
    name, see by_model), products self.product, catalog picks self.pick, online orders self.order."""

    def __init__(self):
        self.receipt = json.loads(json.dumps(RECEIPT))
        self.by_model = {}
        self.product = {"name": "Whole milk", "brand": "Great Value", "size": "1 gal"}
        self.pick = None
        self.order = None
        self.fail = False
        self.server = Server(self.handle)
        self.url = self.server.url
        self.requests = self.server.requests

    def handle(self, method, path, query, headers, body):
        if self.fail:
            return 500, {"error": "model crashed"}, None
        req = json.loads(body)
        content = req["messages"][0]["content"]
        text = content if isinstance(content, str) else " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        if "one grocery or household product" in text:
            reply = self.product
        elif "Which of these catalog products" in text:
            reply = {"product_id": self.pick}
        elif "online grocery order" in text:
            reply = self.order
        else:
            reply = self.by_model.get(req.get("model"), self.receipt)
        return 200, {"choices": [{"message": {"content": json.dumps(reply)}}]}, None

    def close(self):
        self.server.close()


class FakeImmich:
    """Albums and assets in memory, behind the endpoints immich.py uses."""

    def __init__(self):
        self.albums = {}    # id -> {albumName, assets: [ids]}
        self.assets = {}    # id -> {id, originalFileName, originalPath, data, libraryId}
        self.server = Server(self.handle)
        self.url = self.server.url
        self.key_seen = set()

    def add_asset(self, data, name, album=None, path=None, library=None):
        aid = str(uuid.uuid4())
        self.assets[aid] = {"id": aid, "originalFileName": name, "originalPath": path or f"/upload/{name}",
                            "data": data, "libraryId": library, "fileCreatedAt": "2026-10-31T10:00:00Z"}
        if album:
            self._album(album)["assets"].append(aid)
        return aid

    def _album(self, name):
        for a in self.albums.values():
            if a["albumName"] == name:
                return a
        aid = str(uuid.uuid4())
        self.albums[aid] = {"id": aid, "albumName": name, "assets": []}
        return self.albums[aid]

    def _public(self, aid):
        return {k: v for k, v in self.assets[aid].items() if k != "data"}

    def handle(self, method, path, query, headers, body):
        self.key_seen.add(headers.get("x-api-key"))
        if path == "/api/server/ping":
            return 200, {"res": "pong"}, None
        if path == "/api/albums" and method == "GET":
            return 200, [{"id": a["id"], "albumName": a["albumName"]} for a in self.albums.values()], None
        if path == "/api/albums" and method == "POST":
            return 201, {"id": self._album(json.loads(body)["albumName"])["id"]}, None
        m = re.fullmatch(r"/api/albums/([\w-]+)(/assets)?", path)
        if m and m[1] in self.albums:
            album = self.albums[m[1]]
            if m[2] and method == "PUT":
                for aid in json.loads(body)["ids"]:
                    if aid not in album["assets"]:
                        album["assets"].append(aid)
                return 200, [], None
            return 200, {**album, "assets": [self._public(a) for a in album["assets"]]}, None
        if path == "/api/assets" and method == "POST":
            import web
            fields, files = web.parse_multipart(headers["Content-Type"], body)
            assert {"fileCreatedAt", "fileModifiedAt"} <= set(fields)
            name, fn, data = files[0]
            assert name == "assetData"
            for aid, a in self.assets.items():
                if a["data"] == data:
                    return 200, {"id": aid, "status": "duplicate"}, None
            return 201, {"id": self.add_asset(data, fn), "status": "created"}, None
        m = re.fullmatch(r"/api/assets/([\w-]+)/original", path)
        if m:
            return 200, self.assets[m[1]]["data"], "image/png"
        if path == "/api/search/metadata":
            q = json.loads(body)
            items = [self._public(a) for a, v in self.assets.items() if v["libraryId"] == q.get("libraryId")]
            return 200, {"assets": {"items": items, "nextPage": None}}, None
        return 404, {"message": "not found"}, None

    def close(self):
        self.server.close()


class FakeGoogle:
    """OAuth token endpoint plus the Photos Picker API."""

    def __init__(self, photos):
        self.photos = photos              # [(filename, bytes)]
        self.polls_until_set = 1
        self.server = Server(self.handle)
        self.url = self.server.url
        self.deleted = []

    def handle(self, method, path, query, headers, body):
        if path == "/token":
            form = parse_qs(body.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["the-code"]
                return 200, {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600}, None
            return 200, {"access_token": "at2", "expires_in": 3600}, None
        assert headers.get("Authorization", "").startswith("Bearer at"), "no access token"
        if path == "/v1/sessions" and method == "POST":
            return 200, {"id": "sess1", "pickerUri": "https://photos.google.com/picker/sess1",
                         "pollingConfig": {"pollInterval": "0.01s"}, "mediaItemsSet": False}, None
        if path == "/v1/sessions/sess1" and method == "GET":
            self.polls_until_set -= 1
            return 200, {"id": "sess1", "mediaItemsSet": self.polls_until_set < 0, "pollingConfig": {"pollInterval": "0.01s"}}, None
        if path == "/v1/sessions/sess1" and method == "DELETE":
            self.deleted.append("sess1")
            return 200, {}, None
        if path == "/v1/mediaItems":
            return 200, {"mediaItems": [{"id": f"m{i}", "type": "PHOTO", "mediaFile": {
                "baseUrl": f"{self.url}/media/{i}", "mimeType": "image/png", "filename": fn}}
                for i, (fn, _) in enumerate(self.photos)]}, None
        m = re.fullmatch(r"/media/(\d+)=d", path)
        if m:
            return 200, self.photos[int(m[1])][1], "image/png"
        return 404, {"error": path}, None

    def close(self):
        self.server.close()

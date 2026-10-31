#!/usr/bin/env python3
"""Receipts service: the HTTP API (API.md), the phone pages, and the background worker, in one process.

    python web.py                       # http://127.0.0.1:8093, data in ~/receipts (RECEIPTS_HOME)
    python web.py --host 0.0.0.0 --port 8093
    python web.py export-labels         # approved receipts -> the bake-off test set

No login: keep it behind Tailscale Serve (README.md); don't forward the port to the internet.
"""
import argparse
import email.parser
import email.policy
import json
import logging
import os
import re
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog  # noqa: E402
import pages  # noqa: E402
import reconcile  # noqa: E402
from immich import LocalPhotos  # noqa: E402
from picker import Picker, PickerError  # noqa: E402
from service import ALBUMS, NotFound, Service  # noqa: E402

log = logging.getLogger("receipts")
MAX_BYTES = 80 * 1024 * 1024   # one request: several full-size phone photos
PHOTO_TYPES = (".jpg", ".jpeg", ".png", ".webp", ".heic")


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_multipart(content_type, body):
    """-> (fields {name: str}, files [(field name, filename, bytes)]), in the order sent."""
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
    if not msg.is_multipart():
        raise HttpError(400, "expected multipart/form-data")
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename is not None:
            files.append((name, filename, data))
        elif name:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def photos_from(files, field="photo"):
    out = [(fn or "photo.jpg", data) for name, fn, data in files if name == field and data]
    if not out:
        raise HttpError(400, f"no photos (send them as '{field}' fields)")
    for fn, _ in out:
        if not fn.lower().endswith(PHOTO_TYPES):
            raise HttpError(400, f"{fn}: not a photo (JPEG, PNG, WebP or HEIC)")
    return out


class App:
    """Routes: (method, regex) -> handler(match, query, body, headers) -> (status, payload | (bytes, type))."""

    def __init__(self, service, picker=None):
        self.s = service
        self.picker = picker
        self.picker_threads = {}
        self.routes = [
            ("GET", r"/api/health", self.health),
            ("GET", r"/api/summary", lambda m, q, b, h: (200, self.s.counts())),
            ("POST", r"/api/receipts", self.post_receipt),
            ("GET", r"/api/receipts", self.get_receipts),
            ("GET", r"/api/receipts/(\d+)", lambda m, q, b, h: (200, self.s.receipt(int(m[1])))),
            ("POST", r"/api/receipts/(\d+)", lambda m, q, b, h: (200, self.s.edit(int(m[1]), self.json(b)))),
            ("DELETE", r"/api/receipts/(\d+)", self.delete_receipt),
            ("POST", r"/api/receipts/(\d+)/photos", self.post_receipt_photos),
            ("POST", r"/api/receipts/(\d+)/reparse", self.reparse),
            ("POST", r"/api/receipts/(\d+)/merge", self.merge),
            ("GET", r"/api/images/(\d+)", self.image),
            ("GET", r"/api/items/unmatched", lambda m, q, b, h: (200, {"items": self.s.unmatched_items(q.get("limit", 50))})),
            ("POST", r"/api/items/(\d+)/match", self.match_item),
            ("GET", r"/api/products", lambda m, q, b, h: (200, {"products": self.s.products(q.get("q"))})),
            ("POST", r"/api/products", self.post_product),
            ("GET", r"/api/products/(\d+)", lambda m, q, b, h: (200, self.s.product(int(m[1])))),
            ("POST", r"/api/products/(\d+)", lambda m, q, b, h: (200, self.s.edit_product(int(m[1]), self.json(b)))),
            ("POST", r"/api/products/(\d+)/merge", self.merge_product),
            ("GET", r"/api/shopping-list", lambda m, q, b, h: (200, catalog.shopping_list(self.s.store))),
            ("GET", r"/api/picker/status", self.picker_status),
            ("POST", r"/api/picker/sessions", self.picker_start),
            ("GET", r"/api/picker/sessions/([\w.-]+)", self.picker_session),
            ("POST", r"/api/bank/import", self.bank_import),
            ("POST", r"/api/bank/sync-budget", self.bank_sync),
            ("POST", r"/api/orders", self.post_order),
            ("POST", r"/api/reconcile/run", lambda m, q, b, h: (200, {"matched": reconcile.run(self.s.store)})),
            ("GET", r"/api/reconcile", self.get_reconcile),
            ("POST", r"/api/reconcile/match", self.reconcile_match),
            ("GET", r"/picker/auth", self.picker_auth),
            ("GET", r"/picker/callback", self.picker_callback),
            ("GET", r"/manifest.webmanifest", lambda m, q, b, h: (200, (pages.MANIFEST.encode(), "application/manifest+json"))),
            ("GET", r"/sw.js", lambda m, q, b, h: (200, (pages.SERVICE_WORKER.encode(), "text/javascript"))),
            ("GET", r"/icon.svg", lambda m, q, b, h: (200, (pages.ICON.encode(), "image/svg+xml"))),
            ("GET", r"/(|capture|review|review/\d+|items|products|products/\d+|shopping|reconcile|picker)", self.page),
        ]

    # ---- helpers ----

    @staticmethod
    def json(body):
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            raise HttpError(400, "the body isn't JSON") from None
        if not isinstance(data, dict):
            raise HttpError(400, "expected a JSON object")
        return data

    @staticmethod
    def form(headers, body):
        ctype = headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            raise HttpError(400, "send multipart/form-data")
        return parse_multipart(ctype, body)

    def handle(self, method, path, query, body, headers):
        for m, pattern, fn in self.routes:
            match = re.fullmatch(pattern, path)
            if match and m == method:
                return fn(match, {k: v[0] for k, v in query.items()}, body, headers)
        if any(re.fullmatch(p, path) for _, p, _ in self.routes):
            raise HttpError(405, f"{method} isn't allowed here")
        raise HttpError(404, f"nothing at {path}")

    # ---- receipts ----

    def health(self, m, q, b, h):
        # immich: null when not set up (photos are kept locally), false when set up but not answering.
        immich_state = None if isinstance(self.s.photos, LocalPhotos) else self.s.photos.ping()
        return 200, {"ok": True, "immich": immich_state, "engines": [e["name"] for e in self.s.parse_engines],
                     "version": 1}

    def post_receipt(self, m, q, b, h):
        fields, files = self.form(h, b)
        source = fields.get("source") or "in-store"
        rid = self.s.create_receipt(photos_from(files), source=source, note=fields.get("note") or None)
        return 201, {"id": rid, "photos": len(photos_from(files)), "status": "pending"}

    def post_receipt_photos(self, m, q, b, h):
        _, files = self.form(h, b)
        n = self.s.add_photos(int(m[1]), photos_from(files))
        return 200, {"id": int(m[1]), "photos": n, "status": "pending"}

    def get_receipts(self, m, q, b, h):
        return 200, {"receipts": self.s.list_receipts(q.get("bucket"), q.get("status"), q.get("limit", 50))}

    def delete_receipt(self, m, q, b, h):
        self.s.delete(int(m[1]))
        return 200, {"ok": True}

    def reparse(self, m, q, b, h):
        self.s.reparse(int(m[1]))
        return 200, {"status": "pending"}

    def merge(self, m, q, b, h):
        into = self.json(b).get("into")
        if not isinstance(into, int):
            raise HttpError(400, "send {\"into\": <receipt id>}")
        self.s.merge(int(m[1]), into)
        return 200, self.s.receipt(into)

    def image(self, m, q, b, h):
        path = self.s.image_path(int(m[1]))
        ctype = {".png": "image/png", ".webp": "image/webp", ".heic": "image/heic"}.get(path.suffix.lower(), "image/jpeg")
        return 200, (path.read_bytes(), ctype)

    def post_order(self, m, q, b, h):
        ctype = h.get("Content-Type", "")
        if ctype.startswith("application/json"):
            data = self.json(b)
            text, source = str(data.get("text") or ""), data.get("source") or "instacart"
        else:
            text, source = email_text(b), q.get("source", "instacart")
        return 201, {"id": self.s.create_order(text, source), "status": "pending"}

    # ---- items and products ----

    def match_item(self, m, q, b, h):
        data = self.json(b)
        if not any(k in data for k in ("product_id", "new_product", "skip")):
            raise HttpError(400, "send product_id, new_product or skip")
        try:
            return 200, catalog.confirm_match(self.s.store, int(m[1]), data.get("product_id"), data.get("new_product"),
                                              bool(data.get("skip")))
        except KeyError as e:
            raise NotFound(str(e).strip("'")) from None

    def post_product(self, m, q, b, h):
        fields, files = self.form(h, b)
        pid = self.s.create_product(photos_from(files), name=fields.get("name") or None)
        return 201, {"id": pid, "status": "pending" if not fields.get("name") else "ready"}

    def merge_product(self, m, q, b, h):
        into = self.json(b).get("into")
        if not isinstance(into, int):
            raise HttpError(400, "send {\"into\": <product id>}")
        return 200, self.s.merge_products(int(m[1]), into)

    # ---- Google Photos Picker ----

    def _picker(self):
        if not self.picker or not self.picker.configured:
            raise HttpError(409, "Google Photos isn't set up: set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
                                 "RECEIPTS_PUBLIC_URL (README.md)")
        return self.picker

    def picker_status(self, m, q, b, h):
        p = self.picker
        return 200, {"configured": bool(p and p.configured), "signed_in": bool(p and p.configured and p.signed_in),
                     "auth_url": "/picker/auth"}

    def picker_auth(self, m, q, b, h):
        return 302, self._picker().auth_url()

    def picker_callback(self, m, q, b, h):
        if q.get("error"):
            raise HttpError(400, f"Google sign-in was cancelled: {q['error']}")
        self._picker().finish_auth(q.get("code"), q.get("state"))
        return 302, "/picker"

    def picker_start(self, m, q, b, h):
        album = self.json(b).get("album", "receipts")
        if album not in ALBUMS:
            raise HttpError(400, "album must be receipts or products")
        p = self._picker()
        session = p.create_session()
        sid = session["id"]
        store = self.s.store
        store.x("INSERT INTO picker_sessions (id, album, picker_uri, state, created_at) VALUES (?, ?, ?, 'waiting', datetime('now'))",
                sid, album, session.get("pickerUri"))

        def on_photo(data, filename):
            store.x("UPDATE picker_sessions SET state = 'importing' WHERE id = ?", sid)
            if album == "receipts":
                self.s.create_receipt([(filename, data)], via="picker")
            else:
                self.s.create_product([(filename, data)], via="picker")
            store.x("UPDATE picker_sessions SET imported = imported + 1 WHERE id = ?", sid)

        def work():
            try:
                p.wait_and_import(sid, on_photo)
                store.x("UPDATE picker_sessions SET state = 'done' WHERE id = ?", sid)
            except Exception as e:  # noqa: BLE001
                store.x("UPDATE picker_sessions SET state = 'error', error = ? WHERE id = ?", str(e), sid)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        self.picker_threads[sid] = t
        return 200, {"id": sid, "picker_uri": session.get("pickerUri")}

    def picker_session(self, m, q, b, h):
        row = self.s.store.one("SELECT state, imported, error FROM picker_sessions WHERE id = ?", m[1])
        if not row:
            raise NotFound(f"no picker session {m[1]}")
        return 200, row

    # ---- reconciliation ----

    def bank_import(self, m, q, b, h):
        ctype = h.get("Content-Type", "")
        name, account = "upload.csv", q.get("account", "checking")
        if ctype.startswith("multipart/form-data"):
            fields, files = parse_multipart(ctype, b)
            account = fields.get("account") or account
            if not files:
                raise HttpError(400, "no file")
            _, name, b = files[0]
        try:
            rows = reconcile.parse_csv(b.decode("utf-8-sig", "replace"))
        except ValueError as e:
            raise HttpError(400, f"{name}: {e}") from None
        return 200, reconcile.add_bank_rows(self.s.store, rows, account, f"csv:{name}")

    def bank_sync(self, m, q, b, h):
        if not Path(self.s.budget_db).exists():
            raise HttpError(409, f"no budget store at {self.s.budget_db} (set BUDGET_DB)")
        return 200, reconcile.sync_budget(self.s.store, self.s.budget_db)

    def get_reconcile(self, m, q, b, h):
        receipts, bank, matched = reconcile.report(self.s.store, q.get("since"))
        return 200, {"receipts_unmatched": [self.s.summary(r) for r in receipts], "bank_unmatched": bank, "matched": matched}

    def reconcile_match(self, m, q, b, h):
        data = self.json(b)
        rid = data.get("receipt_id")
        if not isinstance(rid, int):
            raise HttpError(400, "send receipt_id (and bank_id, or unmatch)")
        self.s._receipt_row(rid)
        if not data.get("unmatch"):
            bid = data.get("bank_id")
            if not isinstance(bid, int) or not self.s.store.one("SELECT id FROM bank_txns WHERE id = ?", bid):
                raise HttpError(400, "unknown bank_id")
        reconcile.match(self.s.store, rid, data.get("bank_id"), bool(data.get("unmatch")))
        return 200, {"ok": True}

    # ---- pages ----

    def page(self, m, q, b, h):
        return 200, (pages.render(m[1]).encode(), "text/html; charset=utf-8")


def email_text(raw):
    """The readable text of an .eml (plain part, else the HTML stripped), or the body as-is."""
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    if not msg.get("From") and not msg.get("Subject"):
        return raw.decode("utf-8", "replace")
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return raw.decode("utf-8", "replace")
    text = part.get_content()
    if part.get_content_subtype() == "html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
    subject = msg.get("Subject")
    return (f"Subject: {subject}\n" if subject else "") + text.strip()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "receipts/1"

        def _do(self, method):
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BYTES:
                    raise HttpError(413, f"too large (over {MAX_BYTES // (1024 * 1024)} MB)")
                body = self.rfile.read(length) if length else b""
                status, payload = app.handle(method, url.path.rstrip("/") or "/", parse_qs(url.query), body, self.headers)
            except HttpError as e:
                status, payload = e.status, {"error": str(e)}
            except NotFound as e:
                status, payload = 404, {"error": str(e)}
            except (ValueError, PickerError) as e:
                status, payload = 400, {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                status, payload = 500, {"error": f"{type(e).__name__}: {e}"}
            if status == 302:
                self.send_response(302)
                self.send_header("Location", payload)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if isinstance(payload, tuple):
                data, ctype = payload
            else:
                data, ctype = json.dumps(payload).encode(), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if ctype.startswith(("application/json", "text/html")) else "max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def do_DELETE(self):
            self._do("DELETE")

        def log_message(self, fmt, *args):
            if os.environ.get("RECEIPTS_ACCESS_LOG"):
                super().log_message(fmt, *args)

    return Handler


def home():
    return Path(os.environ.get("RECEIPTS_HOME") or Path.home() / "receipts")


def make_picker(service):
    env = os.environ
    return Picker(service.store, env.get("GOOGLE_CLIENT_ID"), env.get("GOOGLE_CLIENT_SECRET"), env.get("RECEIPTS_PUBLIC_URL"))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", nargs="?", default="serve", choices=["serve", "work", "export-labels"])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8093)
    p.add_argument("--home", type=Path, default=home(), help="store and photo cache (default ~/receipts, or RECEIPTS_HOME)")
    p.add_argument("--set", type=Path, help="export-labels: the bake-off set folder (default <home>/bakeoff/set)")
    p.add_argument("--interval", type=int, default=60, help="seconds between checks of Immich for new photos")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    service = Service(args.home)
    if args.command == "export-labels":
        print(f"{service.export_labels(args.set or args.home / 'bakeoff' / 'set')} receipts exported")
        return 0
    if args.command == "work":
        print(json.dumps(service.work_once(), indent=1))
        return 0
    threading.Thread(target=service.run_worker, kwargs={"interval": args.interval}, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(App(service, make_picker(service))))
    print(f"receipts on http://{args.host}:{args.port}  (data in {args.home}; engines: "
          f"{', '.join(e['name'] for e in service.parse_engines)}; photos: {type(service.photos).__name__})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

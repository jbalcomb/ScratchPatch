"""Receipts jobs against a fake receipts service (receipts/API.md) on a local socket."""

import email.parser
import email.policy
import http.server as http_server
import json
import threading
import urllib.parse

import pytest

from taskdeck.jobs import receipts
from taskdeck.jobs.receipts import api, items, lists, photos, reconcile
from taskdeck.shared import http

RECEIPT = {"id": 17, "status": "parsed", "bucket": "review", "merchant": "Giant Eagle", "purchase_date": "2026-10-12",
           "total": 4519, "tax": 112, "source": "in-store", "photos": 2, "items": 14, "suspect": ["total", "merchant"],
           "created_at": "2026-10-31T18:20:00", "url": "/review/17"}
RETAKE = {**RECEIPT, "id": 18, "bucket": "retake", "merchant": None, "purchase_date": None, "total": None,
          "suspect": ["merchant", "purchase_date", "total", "tax", "items"], "url": "/review/18"}
SUMMARY = {"pending": 2, "review": 3, "retake": 1, "approved": 40, "unmatched_items": 12, "products": 25, "due": 4,
           "unreconciled_receipts": 5, "unreconciled_bank": 7}
UNMATCHED = [
    {"id": 201, "receipt_id": 17, "raw_text": "GV WHL MLK GAL", "line_total": 329, "merchant": "Walmart",
     "purchase_date": "2026-10-12", "suggestion": {"product_id": 5, "name": "Whole milk", "by": "text", "score": 0.95}},
    {"id": 202, "receipt_id": 17, "raw_text": "BNNA", "line_total": 129, "merchant": "Walmart",
     "purchase_date": "2026-10-12", "suggestion": {"product_id": 7, "name": "Bananas", "by": "model", "score": 0.82}},
    {"id": 203, "receipt_id": 17, "raw_text": "BAG FEE", "line_total": 10, "merchant": "Walmart",
     "purchase_date": "2026-10-12", "suggestion": None},
    {"id": 204, "receipt_id": 19, "raw_text": "GV WHL MLK GAL", "line_total": 309, "merchant": "Walmart",
     "purchase_date": "2026-10-19", "suggestion": {"product_id": 5, "name": "Whole milk", "by": "text", "score": 0.95}},
    {"id": 205, "receipt_id": 19, "raw_text": "EGGS 12CT", "line_total": 289, "merchant": "Aldi",
     "purchase_date": "2026-10-19", "suggestion": {"product_id": 9, "name": "Eggs", "by": "text", "score": 0.9}},
]
SHOPPING = {"rule": "due when the days since the last purchase reach 90% of the usual days between purchases",
            "items": [{"product_id": 5, "name": "Whole milk", "brand": "Great Value", "size": "1 gal",
                       "last_bought": "2026-10-20", "every_days": 7, "overdue_days": 4, "usual_store": "Walmart",
                       "last_price": 329, "best_price": 309, "best_store": "Aldi"},
                      {"product_id": 9, "name": "Eggs", "brand": None, "size": "12 ct", "last_bought": "2026-10-25",
                       "every_days": 7, "overdue_days": 0, "usual_store": "Aldi", "last_price": 289,
                       "best_price": 289, "best_store": "Aldi"}]}
BANK = {"id": 3, "date": "2026-10-13", "description": "GIANT EAGLE #4", "amount": -4519, "account": "checking"}


def parse_multipart(content_type: str, body: bytes) -> list[tuple[str, str | None, bytes]]:
    """(field name, file name, content) for each part, in order."""
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body)
    return [(p.get_param("name", header="content-disposition"), p.get_filename(), p.get_payload(decode=True))
            for p in msg.iter_parts()]


class FakeService:
    """The endpoints the jobs use. ``errors`` maps "METHOD /path" to (status, body) to fail a request."""

    def __init__(self):
        self.requests, self.errors, self.matched_ids = [], {}, []
        self.health = {"ok": True, "immich": True, "engines": ["qwen3-vl-8b"], "version": 1}
        fake = self

        class Handler(http_server.BaseHTTPRequestHandler):
            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def _handle(self, method):
                parts = urllib.parse.urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                req = {"method": method, "path": parts.path, "query": dict(urllib.parse.parse_qsl(parts.query)),
                       "content_type": self.headers.get("Content-Type", ""), "body": body}
                fake.requests.append(req)
                status, answer = fake.errors.get(f"{method} {parts.path}") or fake.answer(req)
                data = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, req):
        path, method = req["path"], req["method"]
        if path == "/api/health":
            return 200, self.health
        if path == "/api/summary":
            return 200, SUMMARY
        if path == "/api/receipts" and method == "GET":
            return 200, {"receipts": [RECEIPT, RETAKE]}
        if path == "/api/receipts" and method == "POST":
            n = sum(1 for p in parse_multipart(req["content_type"], req["body"]) if p[0] == "photo")
            return 201, {"id": 17, "photos": n, "status": "pending"}
        if path == "/api/products" and method == "POST":
            return 201, {"id": 26, "status": "pending"}
        if path == "/api/items/unmatched":
            return 200, {"items": UNMATCHED}
        if path.startswith("/api/items/") and path.endswith("/match"):
            item_id = int(path.split("/")[3])
            self.matched_ids.append(item_id)
            same = sum(1 for i in UNMATCHED if i["raw_text"] == next(u["raw_text"] for u in UNMATCHED if u["id"] == item_id))
            return 200, {"matched": same, "product_id": json.loads(req["body"])["product_id"]}
        if path == "/api/shopping-list":
            return 200, SHOPPING
        if path == "/api/bank/import":
            return 200, {"rows": 120, "new": 31}
        if path == "/api/bank/sync-budget":
            return 200, {"rows": 640, "new": 12}
        if path == "/api/reconcile/run":
            return 200, {"matched": 9}
        if path == "/api/reconcile":
            return 200, {"receipts_unmatched": [RECEIPT], "bank_unmatched": [BANK],
                         "matched": [{"receipt_id": 16, "bank_id": 2, "method": "auto"}]}
        return 404, {"error": f"no route {method} {path}"}

    def calls(self):
        return [(r["method"], r["path"]) for r in self.requests]


@pytest.fixture
def service():
    fake = FakeService()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def settings(service):
    return {"service_url": service.url + "/"}  # a trailing slash is fine


def photo_files(tmp_path, *names):
    paths = []
    for i, name in enumerate(names):
        p = tmp_path / name
        p.write_bytes(f"image {i} {name}".encode() + bytes(range(256)))  # binary content survives
        paths.append(str(p))
    return "\n".join(paths)


def run(plugin, settings, **inputs):
    return plugin.run(inputs, settings=settings)


# ---- package and settings ---------------------------------------------------------------------------

def test_plugins_share_one_settings_set_and_category():
    ids = [p.id for p in receipts.PLUGINS]
    assert ids == ["receipts_capture", "receipts_review", "receipts_unmatched", "receipts_product_photo",
                   "receipts_shopping", "receipts_reconcile"]
    assert {p.settings_id for p in receipts.PLUGINS} == {"receipts_capture"}
    assert {p.category for p in receipts.PLUGINS} == {"Receipts"}
    assert all(p.settings == api.SETTINGS for p in receipts.PLUGINS)
    assert next(f for f in api.SETTINGS if f.name == "service_url").default is None  # no default host


@pytest.mark.parametrize("url,expected", [
    ("", "Set “Receipts service URL”"),
    ("ai-workstation.tail1234.ts.net:8443", "must start with https://"),
    ("http://localhost:8093", "localhost"),
    ("https://ai-workstation.tail1234.ts.net:8443", None),
])
def test_check_ready(url, expected):
    problems = photos.CAPTURE.check_ready({"service_url": url})
    if expected is None:
        assert problems == []
    else:
        assert len(problems) == 1 and expected in problems[0].message and problems[0].blocking


def test_check_ready_online(service, settings):
    assert photos.CAPTURE.check_ready_online(settings) == []
    service.health = {"ok": True, "immich": False}
    [problem] = photos.CAPTURE.check_ready_online(settings)
    assert "Immich" in problem.message and not problem.blocking
    assert lists.REVIEW.check_ready_online(settings) == []  # no photos, so Immich doesn't matter
    assert service.calls()[0] == ("GET", "/api/health")


def test_check_ready_online_unreachable_explains_tailscale(monkeypatch):
    monkeypatch.setattr("taskdeck.shared.tailscale.phone_state", lambda host, timeout=1.5: "off")

    def unreachable(method, url, **kw):
        raise http.HttpError("Can't reach ai-box:9: [Errno -2] Name or service not known")
    monkeypatch.setattr(http, "request", unreachable)
    [problem] = photos.CAPTURE.check_ready_online({"service_url": "http://ai-box:9"})
    assert problem.blocking and problem.fix == "recheck"
    assert "isn't connected to Tailscale" in problem.message


def test_check_ready_online_not_the_service(service, settings):
    service.errors["GET /api/health"] = (200, b"<html>hello</html>")
    [problem] = photos.CAPTURE.check_ready_online(settings)
    assert "didn't answer like the receipts service" in problem.message


# ---- capture and product photos ----------------------------------------------------------------------

def test_capture_sends_one_multipart_request_in_order(service, settings, tmp_path):
    files = photo_files(tmp_path, "top.jpg", "middle.PNG", "bottom.webp")
    report = run(photos.CAPTURE, settings, photos=files, note="Groceries for the party")
    assert report.ok, report.error
    [req] = service.requests
    assert (req["method"], req["path"]) == ("POST", "/api/receipts")
    assert req["content_type"].startswith("multipart/form-data; boundary=")
    parts = parse_multipart(req["content_type"], req["body"])
    assert [(name, filename) for name, filename, _ in parts] == [
        ("note", None), ("photo", "top.jpg"), ("photo", "middle.PNG"), ("photo", "bottom.webp")]
    assert parts[0][2] == b"Groceries for the party"
    assert parts[1][2] == b"image 0 top.jpg" + bytes(range(256))
    assert parts[3][2].startswith(b"image 2 bottom.webp")
    rows = dict(report.result.rows)
    assert rows == {"Receipt": "#17", "Photos": "3", "Status": "pending",
                    "Review page": f"{service.url}/review/17"}


def test_capture_without_note_sends_only_photos(service, settings, tmp_path):
    report = run(photos.CAPTURE, settings, photos=photo_files(tmp_path, "r.jpeg"), note="  ")
    assert report.ok
    parts = parse_multipart(service.requests[0]["content_type"], service.requests[0]["body"])
    assert [p[0] for p in parts] == ["photo"]


def test_capture_rejects_bad_files_before_sending(service, settings, tmp_path):
    report = run(photos.CAPTURE, settings, photos=photo_files(tmp_path, "r.heic"))
    assert not report.ok and "r.heic isn't a .jpg, .jpeg, .png, .webp file" in report.error
    report = run(photos.CAPTURE, settings, photos=str(tmp_path / "gone.jpg"))
    assert not report.ok and "Can't read" in report.error
    report = run(photos.CAPTURE, settings, photos="")
    assert not report.ok and "required" in report.error
    assert service.requests == []


def test_server_error_is_the_job_error(service, settings, tmp_path):
    service.errors["POST /api/receipts"] = (400, {"error": "photo 2 is not an image"})
    report = run(photos.CAPTURE, settings, photos=photo_files(tmp_path, "a.jpg", "b.jpg"))
    assert not report.ok and report.error == "photo 2 is not an image"


def test_server_error_without_json(service, settings):
    service.errors["GET /api/shopping-list"] = (502, b"Bad Gateway")
    report = run(lists.SHOPPING, settings)
    assert not report.ok and report.error.startswith("The receipts service answered: HTTP 502")


def test_product_photo(service, settings, tmp_path):
    report = run(photos.PRODUCT, settings, photos=photo_files(tmp_path, "front.jpg", "label.jpg"))
    assert report.ok, report.error
    [req] = service.requests
    assert (req["method"], req["path"]) == ("POST", "/api/products")
    assert [(n, f) for n, f, _ in parse_multipart(req["content_type"], req["body"])] == [
        ("photo", "front.jpg"), ("photo", "label.jpg")]
    rows = dict(report.result.rows)
    assert rows["Product"] == "#26" and rows["Photos"] == "2" and rows["Status"] == "pending"
    assert rows["Product page"] == f"{service.url}/products/26"


# ---- read-only lists -------------------------------------------------------------------------------------

def test_review_queue(service, settings):
    report = run(lists.REVIEW, settings)
    assert report.ok, report.error
    assert service.calls() == [("GET", "/api/summary"), ("GET", "/api/receipts")]
    assert service.requests[1]["query"] == {"status": "parsed"}
    assert report.result.rows == [
        ("To review", "3"), ("Retake", "1"), ("Waiting to be parsed", "2"), ("Approved", "40"),
        ("#17 Giant Eagle", "2026-10-12 · $45.19 · review · check: total, store"),
        ("#18 Unknown store", "no date · — · retake · check: store, date, total, tax, items · new photo needed"),
    ]
    assert report.result.text == f"Review them on {service.url}/review"
    assert report.result.warning == "1 receipt needs a new photo."
    assert report.result.title == "2 receipts to check"


def test_shopping_list(service, settings):
    report = run(lists.SHOPPING, settings)
    assert report.ok, report.error
    assert report.result.rows == [
        ("Whole milk · Great Value · 1 gal", "4 days overdue · usually Walmart · last $3.29 · best $3.09 at Aldi"),
        ("Eggs · 12 ct", "due today · usually Aldi · last $2.89 · best $2.89 at Aldi"),
    ]
    assert report.result.title == "2 items due"
    assert report.result.text.startswith("Rule: due when")


def test_dollars():
    assert api.dollars(4519) == "$45.19"
    assert api.dollars(-4519) == "-$45.19"
    assert api.dollars(123456) == "$1,234.56"
    assert api.dollars(None) == "—"


# ---- unmatched items ------------------------------------------------------------------------------------

def test_unmatched_items_preview_has_no_side_effects(service, settings):
    report = run(items.PLUGIN, settings)
    assert report.ok, report.error
    assert service.calls() == [("GET", "/api/items/unmatched")]
    assert report.result.rows[:3] == [
        ("GV WHL MLK GAL (Walmart, 2026-10-12, $3.29)", "→ Whole milk (95%)"),
        ("BNNA (Walmart, 2026-10-12, $1.29)", "→ Bananas (82%)"),
        ("BAG FEE (Walmart, 2026-10-12, $0.10)", "no suggestion"),
    ]
    assert [p["id"] for p in report.result.payload] == [201, 204, 205]  # score >= 0.9 only
    lines = report.result.text.splitlines()
    assert lines[:4] == ["Accept suggestions will match these 3 items:", "• GV WHL MLK GAL → Whole milk",
                         "• GV WHL MLK GAL → Whole milk", "• EGGS 12CT → Eggs"]


def test_accept_suggestions(service, settings):
    payload = run(items.PLUGIN, settings).result.payload
    ok, message = items.PLUGIN.try_commit(payload, settings)
    assert ok, message
    # 204 has the same text as 201: matching 201 already applied it there.
    assert service.matched_ids == [201, 205]
    bodies = [json.loads(r["body"]) for r in service.requests if r["path"].endswith("/match")]
    assert bodies == [{"product_id": 5}, {"product_id": 9}]
    assert message == "Accepted 3 suggestions: 3 receipt lines matched."


def test_accept_suggestions_reports_failures(service, settings):
    payload = run(items.PLUGIN, settings).result.payload
    service.errors["POST /api/items/205/match"] = (404, {"error": "item 205 not found"})
    ok, message = items.PLUGIN.try_commit(payload, settings)
    assert ok and message.endswith("Not accepted: EGGS 12CT: item 205 not found")
    for item_id in (201, 204, 205):  # 204 is tried when 201, with the same text, fails
        service.errors[f"POST /api/items/{item_id}/match"] = (500, {"error": "database is locked"})
    ok, message = items.PLUGIN.try_commit(payload, settings)
    assert not ok and "Nothing was accepted" in message and "database is locked" in message


def test_nothing_to_accept(service, settings, monkeypatch):
    monkeypatch.setattr(service, "answer", lambda req: (200, {"items": UNMATCHED[1:3]}))
    report = run(items.PLUGIN, settings)
    assert report.ok and report.result.payload is None
    assert report.result.text.startswith("No suggestion scores 90% or more")


# ---- reconciliation --------------------------------------------------------------------------------------

def test_reconcile_imports_then_matches(service, settings, tmp_path):
    csv = tmp_path / "stmt.csv"
    csv.write_text("Date,Description,Amount\n10/13/2026,GIANT EAGLE #4,-45.19\n")
    report = run(reconcile.PLUGIN, settings, bank_csv=str(csv), sync_budget=True)
    assert report.ok, report.error
    assert service.calls() == [("POST", "/api/bank/import"), ("POST", "/api/bank/sync-budget"),
                               ("POST", "/api/reconcile/run"), ("GET", "/api/reconcile")]
    [part] = parse_multipart(service.requests[0]["content_type"], service.requests[0]["body"])
    assert part == ("file", "stmt.csv", csv.read_bytes())
    assert report.result.rows == [
        ("Bank CSV", "120 rows, 31 new"), ("Budget store", "640 rows, 12 new"), ("Matched now", "9"),
        ("Receipts with no bank charge", "1"), ("Bank charges with no receipt", "1"),
        ("#17 Giant Eagle", "2026-10-12 · $45.19 · review · check: total, store · no bank charge"),
        ("Bank 2026-10-13 GIANT EAGLE #4", "-$45.19 · checking · no receipt"),
    ]
    assert report.result.title == "1 receipt and 1 bank charge unmatched"


def test_reconcile_without_imports(service, settings):
    report = run(reconcile.PLUGIN, settings)
    assert report.ok, report.error
    assert service.calls() == [("POST", "/api/reconcile/run"), ("GET", "/api/reconcile")]


def test_jobs_refuse_to_run_without_url(service):
    report = run(lists.SHOPPING, {"service_url": ""})
    assert not report.ok and "Set the receipts service URL" in report.error
    assert service.requests == []


def test_multipart_helper_escapes_names():
    body, ctype = http.multipart([("note", "a\r\nb")], [("photo", 'we"ird.jpg', b"x")])
    parts = parse_multipart(ctype, body)
    assert parts[0] == ("note", None, b"a\r\nb")
    assert parts[1][1] == "we%22ird.jpg" and parts[1][2] == b"x"
    assert b"Content-Type: image/jpeg" in body

"""PDF Parser job against a fake PDF parser service (pdf-parser/README.md) on a local socket."""

import email.parser
import email.policy
import http.server as http_server
import json
import threading
import urllib.parse

import pytest

from taskdeck.core import PluginRegistry
from taskdeck.jobs import pdf_parser

SUMMARY = {"pages": 12, "text_pages": 11, "scanned_pages": 1, "text_blocks": 140, "tables": 3, "images_kept": 8,
           "images_needs_review": 1, "images_duplicate": 2, "images_merged": 0, "images_small": 5,
           "vision": {"requests": 4, "failed": 1, "available": True, "stopped": None, "model": "qwen3-vl-8b"}}
JOB = {"id": "20261108-101500-abc123", "file": "report.pdf", "status": "queued", "step": None, "error": None,
       "summary": None, "url": "/jobs/20261108-101500-abc123/", "zip": "/jobs/20261108-101500-abc123.zip"}


class FakeService:
    def __init__(self):
        self.requests, self.uploads = [], []
        self.health = {"ok": True, "vision": {"state": "ok", "model": "qwen3-vl-8b"}, "tesseract": True, "queued": 0}
        self.statuses = [{**JOB, "status": "running", "step": "page 3 of 12"},
                         {**JOB, "status": "done", "summary": SUMMARY}]
        self.upload_answer = (202, JOB)
        fake = self

        class Handler(http_server.BaseHTTPRequestHandler):
            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def _handle(self, method):
                path = urllib.parse.urlsplit(self.path).path
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append((method, path))
                if method == "POST" and path == "/api/jobs":
                    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                        f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body)
                    fake.uploads += [(p.get_param("name", header="content-disposition"), p.get_filename(),
                                      p.get_payload(decode=True)) for p in msg.iter_parts()]
                    status, answer = fake.upload_answer
                elif path == "/api/health":
                    status, answer = 200, fake.health
                elif path == f"/api/jobs/{JOB['id']}":
                    status, answer = 200, fake.statuses.pop(0) if len(fake.statuses) > 1 else fake.statuses[0]
                else:
                    status, answer = 404, {"error": "not found"}
                data = json.dumps(answer).encode()
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


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(pdf_parser, "POLL_SECONDS", 0.01)
    fake = FakeService()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7\n" + bytes(range(256)))
    return path


def run(service, **inputs):
    return pdf_parser.PLUGIN.run(inputs, settings={"service_url": service.url})


def test_discovered():
    registry = PluginRegistry().discover()
    assert registry.get("pdf_parser") is pdf_parser.PLUGIN and "pdf_parser" not in registry.errors


def test_sends_the_pdf_waits_and_summarizes(service, pdf):
    report = run(service, pdf=str(pdf))
    assert report.ok, report.error
    assert service.requests == [("POST", "/api/jobs")] + [("GET", f"/api/jobs/{JOB['id']}")] * 2
    assert service.uploads == [("pdf", "report.pdf", pdf.read_bytes())]
    r = report.result
    assert r.title == "report.pdf: 12 pages, 3 tables, 8 images kept, 1 to review"
    assert r.rows == [("Pages", "12 (1 scanned, OCR'd)"), ("Text blocks", "140"), ("Tables", "3"),
                      ("Images kept", "8"), ("Needs review", "1"), ("Also", "2 duplicate, 5 icons"),
                      ("Vision checks", "4 (1 failed)"),
                      ("Output folder", f"{service.url}/jobs/{JOB['id']}/"),
                      ("Download (.zip)", f"{service.url}/jobs/{JOB['id']}.zip")]
    assert r.warning.startswith("1 image needs a look")


def test_vision_stopped_is_a_warning(service, pdf):
    service.statuses = [{**JOB, "status": "done", "summary": {
        **SUMMARY, "images_needs_review": 0,
        "vision": {"requests": 3, "failed": 3, "available": False, "stopped": "3 vision requests in a row failed"}}}]
    report = run(service, pdf=str(pdf))
    assert report.ok and report.result.warning == ("Images weren't checked by the vision model: "
                                                   "3 vision requests in a row failed.")


def test_failed_job_is_the_error(service, pdf):
    service.statuses = [{**JOB, "status": "failed", "error": "ValueError: the PDF is password-protected"}]
    report = run(service, pdf=str(pdf))
    assert not report.ok and report.error == "report.pdf couldn't be parsed: ValueError: the PDF is password-protected"


def test_still_parsing_after_the_wait(service, pdf, monkeypatch):
    monkeypatch.setattr(pdf_parser, "WAIT_SECONDS", 0)
    service.upload_answer = (202, {**JOB, "status": "running", "step": "page 2 of 12"})
    report = run(service, pdf=str(pdf))
    assert report.ok and report.result.title == "report.pdf: still parsing (page 2 of 12)"
    assert ("Output folder", f"{service.url}/jobs/{JOB['id']}/") in report.result.rows


def test_bad_files_are_refused_before_sending(service, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("hi")
    assert "isn't a .pdf file" in run(service, pdf=str(notes)).error
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"hello")
    assert "doesn't look like a PDF" in run(service, pdf=str(fake)).error
    assert "Can't read" in run(service, pdf=str(tmp_path / "gone.pdf")).error
    assert "required" in run(service, pdf="").error
    assert service.requests == []


def test_service_error_is_the_job_error(service, pdf):
    service.upload_answer = (400, {"error": "report.pdf isn't a PDF"})
    report = run(service, pdf=str(pdf))
    assert not report.ok and report.error == "report.pdf isn't a PDF"


def test_readiness(service):
    plugin = pdf_parser.PLUGIN
    settings = {"service_url": service.url}
    assert plugin.check_ready_online(settings) == []
    service.health = {**service.health, "vision": {"state": "unavailable", "message": "qwen3-vl-8b can't take images"},
                      "tesseract": False}
    problems = plugin.check_ready_online(settings)
    assert [p.blocking for p in problems] == [False, False]
    assert "qwen3-vl-8b can't take images" in problems[0].message and "Tesseract" in problems[1].message
    assert plugin.check_ready({"service_url": "http://localhost:8096"})[0].message.startswith(
        "the PDF parser service URL points at the phone itself")
    assert plugin.resolve_settings({})["service_url"] == pdf_parser.DEFAULT_URL
    assert plugin.clean_settings({"service_url": "pdf-parser.example.ts.net/"})["service_url"] == \
        "https://pdf-parser.example.ts.net"


def test_unreachable_service_blocks(monkeypatch):
    problems = pdf_parser.PLUGIN.check_ready_online({"service_url": "http://127.0.0.1:9"})
    assert problems and problems[0].blocking and problems[0].fix == "recheck"

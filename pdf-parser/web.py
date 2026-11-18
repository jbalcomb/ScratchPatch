#!/usr/bin/env python3
"""PDF parser service: upload a PDF, a worker parses it (parse_pdf.py), fetch the status and the bundle.

    python web.py                         # http://127.0.0.1:8096, jobs in ~/pdf-parser (PDF_PARSER_HOME)
    python web.py --host 127.0.0.1 --port 8096

    GET  /                    the latest jobs, with links to their files, review and zip
    GET  /api/health          {ok, vision: {state, model, message, ...}, tesseract, queued}
    POST /api/jobs            multipart, the PDF as field "pdf"  -> {id, status, url, zip}
    GET  /api/jobs            the latest 50 jobs
    GET  /api/jobs/<id>       {id, file, status: queued|running|done|failed, step, error, summary, url, zip}
    GET  /jobs/<id>/          the bundle's files, as a page
    GET  /jobs/<id>/<path>    one file of the bundle
    GET  /jobs/<id>.zip       the whole bundle

No login: keep it behind Tailscale Serve (README.md); don't forward the port to the internet.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.parser
import email.policy
import html
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocr  # noqa: E402
import parse_pdf  # noqa: E402
import pymupdf  # noqa: E402
import reviews  # noqa: E402
import vision as vision_mod  # noqa: E402

log = logging.getLogger("pdf-parser")
PORT = 8096
MAX_BYTES = 200 * 1024 * 1024
RECHECK_SECONDS = 300   # /api/health asks the model server again when vision isn't known to work
REVIEW_DPI = 144        # page images for the review page: 2 pixels per PDF point
REVIEW_PAGE = Path(__file__).resolve().parent / "review.html"


def home():
    return Path(os.environ.get("PDF_PARSER_HOME") or Path.home() / "pdf-parser")


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


class Jobs:
    """One folder per job under ``root``: the PDF, status.json, out/<name>/ (the bundle) and bundle.zip."""

    def __init__(self, root, vision):
        self.root = Path(root) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.vision = vision
        self.queue = queue.Queue()
        self.lock = threading.Lock()

    def folder(self, job_id):
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", job_id or ""):
            return None
        path = self.root / job_id
        return path if (path / "status.json").exists() else None

    def status(self, job_id):
        folder = self.folder(job_id)
        return json.loads((folder / "status.json").read_text()) if folder else None

    def _write(self, folder, **changes):
        with self.lock:
            path = folder / "status.json"
            data = json.loads(path.read_text()) if path.exists() else {}
            data.update(changes)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, path)
            return data

    def create(self, filename, data):
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename or "document.pdf").name).strip() or "document.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        job_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        folder = self.root / job_id
        folder.mkdir()
        (folder / name).write_bytes(data)
        status = self._write(folder, id=job_id, file=name, status="queued", step=None, error=None, summary=None,
                             created=now(), started=None, finished=None, url=f"/jobs/{job_id}/",
                             zip=f"/jobs/{job_id}.zip")
        self.queue.put(job_id)
        return status

    def recent(self, limit=50):
        return [json.loads((p / "status.json").read_text())
                for p in sorted(self.root.iterdir(), reverse=True)[:limit] if (p / "status.json").exists()]

    def requeue(self):
        """Jobs left queued or half-done by a restart start again."""
        for p in sorted(self.root.iterdir()):
            st = p / "status.json"
            if st.exists() and json.loads(st.read_text()).get("status") in ("queued", "running"):
                self._write(p, status="queued", step=None)
                self.queue.put(p.name)

    def run_one(self, job_id):
        folder = self.root / job_id
        st = self._write(folder, status="running", started=now(), step="starting")
        out = folder / "out"
        try:
            manifest = parse_pdf.parse(folder / st["file"], out, self.vision,
                                       progress=lambda msg: self._write(folder, step=msg))
            bundle = out / Path(st["file"]).stem
            shutil.make_archive(str(folder / "bundle"), "zip", bundle.parent, bundle.name)
            self._write(folder, status="done", step=None, finished=now(), summary=manifest["summary"])
        except Exception as exc:  # a broken PDF fails its job, not the service
            log.exception("job %s failed", job_id)
            self._write(folder, status="failed", step=None, finished=now(), error=f"{type(exc).__name__}: {exc}")

    def work(self, stop=None):
        while not (stop and stop.is_set()):
            try:
                job_id = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            self.run_one(job_id)

    def manifest(self, job_id):
        bundle = self.bundle_dir(job_id)
        return json.loads((bundle / "manifest.json").read_text()) if bundle else None

    def review_store(self, manifest):
        return reviews.Store(self.root.parent, manifest["source"]["sha256"], manifest["source"]["file"])

    def review_data(self, job_id):
        """What the review page shows: pages, the reviewable elements with their reviews, and coverage."""
        manifest = self.manifest(job_id)
        if not manifest:
            return None
        elements = reviews.annotate(manifest, self.review_store(manifest).load())
        st = self.status(job_id)
        return {"job": job_id, "file": st["file"], "pages": manifest["pages"], "elements": elements,
                "coverage": reviews.coverage(manifest, elements)}

    def page_image(self, job_id, page_no):
        """The page as displayed, rendered at REVIEW_DPI (made once, then kept in the job folder)."""
        folder, st = self.folder(job_id), self.status(job_id)
        if not folder or st["status"] != "done":
            return None
        path = folder / "review-pages" / f"p{page_no:03d}.png"
        if not path.exists():
            with pymupdf.open(folder / st["file"]) as doc:
                if not 1 <= page_no <= doc.page_count:
                    return None
                path.parent.mkdir(exist_ok=True)
                doc[page_no - 1].get_pixmap(dpi=REVIEW_DPI).save(path)
        return path

    def bundle_dir(self, job_id):
        st = self.status(job_id)
        return (self.root / job_id / "out" / Path(st["file"]).stem) if st and st["status"] == "done" else None


def parse_multipart(content_type, body):
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
    if not msg.is_multipart():
        raise ValueError("expected multipart/form-data")
    return [(p.get_param("name", header="content-disposition"), p.get_filename(), p.get_payload(decode=True) or b"")
            for p in msg.iter_parts()]


def listing_page(job_id, bundle):
    rows = "".join(f'<li><a href="{html.escape(str(p.relative_to(bundle)))}">{html.escape(str(p.relative_to(bundle)))}'
                   f"</a> <small>{p.stat().st_size:,} bytes</small></li>"
                   for p in sorted(bundle.rglob("*")) if p.is_file())
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(bundle.name)}</title><body style='font-family:sans-serif;margin:16px'>"
            f"<h2>{html.escape(bundle.name)}</h2><p><a href='/jobs/{job_id}/review/'>Review</a> · "
            f"<a href='/jobs/{job_id}.zip'>Download all (.zip)</a> · "
            f"<a href='manifest.json'>manifest.json</a></p><ul>{rows}</ul>")


def _when(iso):
    try:
        return dt.datetime.fromisoformat(iso).strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return ""


def summary_line(st):
    """One line for a job: what was found, or where it is."""
    s = st.get("summary") or {}
    if st.get("status") == "done":
        review = s.get("images_needs_review") or 0
        return (f"{s.get('pages', 0)} pages · {s.get('tables', 0)} tables · {s.get('images_kept', 0)} images kept"
                + (f" · {review} to review" if review else ""))
    if st.get("status") == "failed":
        return f"failed: {st.get('error') or 'no reason given'}"
    return st.get("status", "") + (f": {st['step']}" if st.get("step") else "")


def home_page(jobs, limit=30):
    """The service's front page: the latest jobs, each with links to its files, its review and its zip."""
    rows = []
    for st in jobs.recent(limit):
        jid = html.escape(st["id"])
        links = (f"<a href='/jobs/{jid}/'>Files</a> · <a href='/jobs/{jid}/review/'>Review</a> · "
                 f"<a href='/jobs/{jid}.zip'>Download .zip</a>") if st.get("status") == "done" else ""
        rows.append(f"<li><b>{html.escape(st.get('file') or '')}</b> <small>{_when(st.get('created'))}</small><br>"
                    f"<span class='{html.escape(st.get('status') or '')}'>{html.escape(summary_line(st))}</span>"
                    f"{'<br>' + links if links else ''}</li>")
    body = "".join(rows) or "<li>No PDFs parsed yet: send one from TaskDeck's PDF Parser job.</li>"
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>PDF Parser</title><style>body{font-family:system-ui,sans-serif;margin:16px;max-width:760px;"
            "line-height:1.5}li{margin:0 0 14px}ul{padding-left:18px}.failed{color:#c62828}"
            "small{color:#777}</style><body><h2>PDF Parser</h2><p>Latest jobs, newest first.</p>"
            f"<ul>{body}</ul>")


def make_handler(jobs, vision):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status, body, content_type="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status, message):
            self._send(status, {"error": message})

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == "/api/health":
                if vision.state != "ok" and (not vision.checked_at or time.time() - vision.checked_at > RECHECK_SECONDS):
                    vision.check()
                return self._send(200, {"ok": True, "vision": vision.health(), "tesseract": ocr.available(),
                                        "queued": jobs.queue.qsize()})
            if path == "/api/jobs":
                return self._send(200, {"jobs": jobs.recent()})
            if path == "/":
                return self._send(200, home_page(jobs).encode(), "text/html; charset=utf-8")
            m = re.fullmatch(r"/api/jobs/([^/]+)", path)
            if m:
                st = jobs.status(m.group(1))
                return self._send(200, st) if st else self._error(404, "no such job")
            m = re.fullmatch(r"/jobs/([^/]+)\.zip", path)
            if m:
                folder = jobs.folder(m.group(1))
                if not folder or not (folder / "bundle.zip").exists():
                    return self._error(404, "no bundle (yet) for this job")
                st = jobs.status(m.group(1))
                return self._send(200, (folder / "bundle.zip").read_bytes(), "application/zip",
                                  {"Content-Disposition": f'attachment; filename="{Path(st["file"]).stem}.zip"'})
            m = re.fullmatch(r"/jobs/([^/]+)/review/?", path)
            if m:
                if not jobs.bundle_dir(m.group(1)):
                    return self._error(404, "no such job, or it isn't done")
                return self._send(200, REVIEW_PAGE.read_bytes(), "text/html; charset=utf-8")
            m = re.fullmatch(r"/api/jobs/([^/]+)/review", path)
            if m:
                data = jobs.review_data(m.group(1))
                return self._send(200, data) if data else self._error(404, "no such job, or it isn't done")
            m = re.fullmatch(r"/jobs/([^/]+)/page/([0-9]+)\.png", path)
            if m:
                image = jobs.page_image(m.group(1), int(m.group(2)))
                if not image:
                    return self._error(404, "no such page")
                return self._send(200, image.read_bytes(), "image/png", {"Cache-Control": "max-age=86400"})
            m = re.fullmatch(r"/jobs/([^/]+)/(.*)", path)
            if m:
                bundle = jobs.bundle_dir(m.group(1))
                if not bundle:
                    return self._error(404, "no such job, or it isn't done")
                if not m.group(2):
                    return self._send(200, listing_page(m.group(1), bundle).encode(), "text/html; charset=utf-8")
                target = (bundle / m.group(2)).resolve()
                if not target.is_file() or bundle.resolve() not in target.parents:
                    return self._error(404, "no such file")
                kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return self._send(200, target.read_bytes(), kind)
            return self._error(404, "not found")

        def do_POST(self):
            m = re.fullmatch(r"/api/jobs/([^/]+)/reviews", urlparse(self.path).path)
            if m:
                return self._post_review(m.group(1))
            if urlparse(self.path).path != "/api/jobs":
                return self._error(404, "not found")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BYTES:
                return self._error(413 if length else 400, f"send one PDF of at most {MAX_BYTES // 2**20} MB")
            try:
                parts = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
            except ValueError as exc:
                return self._error(400, str(exc))
            pdf = next(((fn, data) for name, fn, data in parts if name == "pdf" and data), None)
            if not pdf:
                return self._error(400, "no PDF (send it as the 'pdf' field)")
            if not pdf[1].startswith(b"%PDF"):
                return self._error(400, f"{pdf[0] or 'the file'} isn't a PDF")
            self._send(202, jobs.create(pdf[0], pdf[1]))

        def _post_review(self, job_id):
            manifest = jobs.manifest(job_id)
            if not manifest:
                return self._error(404, "no such job, or it isn't done")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("send one review as a JSON object")
                ids = {e["id"] for e in manifest["elements"] if reviews.reviewable(e)}
                stored = jobs.review_store(manifest).add(body, ids)
            except (ValueError, reviews.ReviewError) as exc:
                return self._error(400, str(exc))
            data = jobs.review_data(job_id)
            element = next(e for e in data["elements"] if e["id"] == stored["element"])
            self._send(200, {"review": stored, "element": element, "coverage": data["coverage"]})

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="PDF parser service")
    p.add_argument("--host", default=os.environ.get("PDF_PARSER_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PDF_PARSER_PORT", PORT)))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    vision = vision_mod.Vision.from_env()
    ok, message = vision.check()
    (log.info if ok else log.error)("vision: %s", message)
    if ok is False:
        log.error("vision: parsing carries on without it; images that look wrong go to needs_review")
    if not ocr.available():
        log.error("tesseract not found: scanned pages and image OCR are skipped")
    jobs = Jobs(home(), vision)
    jobs.requeue()
    threading.Thread(target=jobs.work, daemon=True, name="worker").start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(jobs, vision))
    log.info("listening on http://%s:%s, jobs in %s", args.host, args.port, jobs.root)
    server.serve_forever()


if __name__ == "__main__":
    main()

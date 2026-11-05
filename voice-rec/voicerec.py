"""voice-rec server: receives voice notes from TaskDeck, transcribes them, files them.

    python voicerec.py serve                 # API + worker (what the container runs)
    python voicerec.py status                # counts per status
    python voicerec.py list [-q words]       # oldest first; -q = full-text search
    python voicerec.py show <id>             # one note, with its transcript
    python voicerec.py retry <id>            # transcribe (or archive) again
    python voicerec.py rearchive <id>|--all  # re-categorize and rewrite the Obsidian note / graph
    python voicerec.py audio <id> out.m4a    # get the audio back out of Postgres

API (Bearer VOICE_REC_TOKEN when it is set; Tailscale Serve is the only way in):

    PUT  /api/recordings/<uuid>          body = audio; X-Recorded-At, X-Duration, X-Mobile-Data
    GET  /api/recordings/<uuid>          JSON without the audio
    GET  /api/recordings/<uuid>/audio    the audio
    POST /api/recordings/<uuid>/archive  file it (categorize, Obsidian, knowledge graph)
    GET  /api/recordings[?q=words]       list, oldest first
    GET  /health
    GET  /                               a plain page listing the notes (?token=... when a token is set)
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph  # noqa: E402
import obsidian  # noqa: E402
import organize  # noqa: E402
import transcribe  # noqa: E402
from store import Conflict, Store  # noqa: E402

log = logging.getLogger("voicerec")
MAX_UPLOAD = int(float(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024)
ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def env_flag(name: str, default: bool = False, env=os.environ) -> bool:
    value = env.get(name)
    return default if value is None or value == "" else value.strip().lower() in ("1", "true", "yes", "on")


def default_dsn(env=os.environ) -> str:
    return env.get("DATABASE_URL") or (
        f"postgresql://voicerec:{env.get('VOICE_REC_DB_PASSWORD', '')}@127.0.0.1:{env.get('VOICE_REC_DB_PORT', '5433')}"
        "/voicerec")


# ---- worker -------------------------------------------------------------------------------------


class Worker:
    """Transcribes queued notes (oldest first), then archives the ones the phone asked for."""

    def __init__(self, store: Store, transcriber, env=os.environ):
        self.store, self.transcriber, self.env = store, transcriber, env
        self.auto_archive = env_flag("AUTO_ARCHIVE", False, env)
        vault = env.get("OBSIDIAN_VAULT") or ""
        self.vault = Path(vault) if vault else None
        self.folder = env.get("OBSIDIAN_FOLDER") or "Voice Notes"
        self.with_audio = env_flag("OBSIDIAN_AUDIO", True, env)
        self.wake = threading.Event()
        self.current = ""

    def run_once(self) -> bool:
        claimed = self.store.claim(self.auto_archive)
        if not claimed:
            return False
        stage, job = claimed
        self.current = f"{stage} {job.id}"
        started = time.time()
        try:
            if stage == "transcribe":
                t = self.transcriber(job.audio, job.mime)
                self.store.transcribed(job.id, t.text, t.language, t.segments, t.model)
            else:
                self.archive(job)
            log.info("%s %s: done in %.1f s", stage, job.id, time.time() - started)
        except Exception as exc:
            log.exception("%s %s failed", stage, job.id)
            self.store.failed(job.id, stage, f"{type(exc).__name__}: {exc}")
        finally:
            self.current = ""
        return True

    def archive(self, job) -> None:
        rec = self.store.full(job.id)
        info, problem = organize.organize(rec.get("transcript") or "", self.env)
        self.store.organized(job.id, info)
        rec = self.store.full(job.id)
        note_path = rec.get("note_path")
        if self.vault:
            note_path = obsidian.write(rec, job.audio, self.vault, self.folder, self.with_audio).as_posix()
            rec["note_path"] = note_path
        graph.send(rec, self.env)
        self.store.archived(job.id, note_path, problem or None)

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # e.g. the database restarting
                log.exception("worker: can't reach the database; retrying")
                busy = False
                stop.wait(10)
            if not busy:
                self.wake.wait(30)
                self.wake.clear()


# ---- HTTP ---------------------------------------------------------------------------------------


def make_handler(store: Store, worker: Worker | None, token: str, transcriber_label: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "voice-rec/1"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        # ---- helpers ----
        def _json(self, status: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, query: dict) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            given = header[7:].strip() if header.lower().startswith("bearer ") else ""
            given = given or (query.get("token") or [""])[0].strip()  # the page, opened in a browser
            return hmac.compare_digest(given.encode(), token.encode())

        def _route(self) -> tuple[list[str], dict]:
            url = urlparse(self.path)
            return [p for p in url.path.split("/") if p], parse_qs(url.query)

        def _guard(self) -> tuple[list[str], dict] | None:
            parts, query = self._route()
            if not self._authorized(query):
                self._json(401, {"error": "unauthorized"})
                return None
            if len(parts) >= 3 and parts[:2] == ["api", "recordings"] and not ID.match(parts[2]):
                self._json(400, {"error": "bad recording id"})
                return None
            return parts, query

        # ---- methods ----
        def do_GET(self):
            routed = self._guard()
            if not routed:
                return
            parts, query = routed
            if parts == ["health"]:
                counts = store.counts()
                return self._json(200, {"ok": True, "transcriber": transcriber_label, "queued": counts.get("queued", 0),
                                        "counts": counts, "working_on": worker.current if worker else ""})
            if parts == ["api", "recordings"]:
                return self._json(200, {"recordings": store.list(query=(query.get("q") or [""])[0])})
            if len(parts) == 3 and parts[:2] == ["api", "recordings"]:
                rec = store.get(parts[2])
                return self._json(200, rec) if rec else self._json(404, {"error": "not found"})
            if len(parts) == 4 and parts[:2] == ["api", "recordings"] and parts[3] == "audio":
                got = store.audio(parts[2])
                if not got:
                    return self._json(404, {"error": "not found"})
                audio, mime = got
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
                return
            if parts == []:
                return self._page(query)
            self._json(404, {"error": "not found"})

        def do_PUT(self):
            routed = self._guard()
            if not routed:
                return
            parts, _ = routed
            if not (len(parts) == 3 and parts[:2] == ["api", "recordings"]):
                return self._json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._json(400, {"error": "empty body"})
            if length > MAX_UPLOAD:
                return self._json(413, {"error": f"larger than {MAX_UPLOAD // 1024 // 1024} MB"})
            audio = self.rfile.read(length)
            recorded = self.headers.get("X-Recorded-At") or datetime.now(timezone.utc).isoformat()
            try:
                datetime.fromisoformat(recorded.replace("Z", "+00:00"))
                duration = float(self.headers.get("X-Duration") or 0) or None
            except ValueError:
                return self._json(400, {"error": "bad X-Recorded-At or X-Duration"})
            mime = (self.headers.get("Content-Type") or "audio/mp4").split(";")[0]
            try:
                rec, created = store.add(str(uuid.UUID(parts[2])), audio, mime, recorded, duration,
                                         self.headers.get("X-Mobile-Data") or "")
            except Conflict:
                return self._json(409, {"error": "a different recording already has this id"})
            if worker:
                worker.wake.set()
            self._json(201 if created else 200, rec)

        def do_POST(self):
            routed = self._guard()
            if not routed:
                return
            parts, _ = routed
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 1 << 20))
            if len(parts) == 4 and parts[:2] == ["api", "recordings"] and parts[3] == "archive":
                rec = store.request_archive(parts[2])
                if not rec:
                    return self._json(404, {"error": "not found"})
                if worker:
                    worker.wake.set()
                return self._json(202, rec)
            self._json(404, {"error": "not found"})

        def _page(self, query: dict) -> None:
            q = (query.get("q") or [""])[0]
            tok = (query.get("token") or [""])[0]
            suffix = f"?token={html.escape(tok)}" if tok else ""
            rows = []
            for r in store.list(query=q):
                rows.append(
                    f"<article><h3>{html.escape(r.get('title') or 'Voice note')}</h3>"
                    f"<p class=m>Recorded {html.escape(r['recorded_at'])} · transcribed "
                    f"{html.escape(r.get('transcribed_at') or '—')} · {html.escape(r['status'])}"
                    f"{' · ' + html.escape(r['category']) if r.get('category') else ''}</p>"
                    f"<audio controls preload=none src='/api/recordings/{r['id']}/audio{suffix}'></audio>"
                    f"<p>{html.escape(r.get('transcript') or '')}</p></article>")
            body = (
                "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Voice notes</title><style>body{font:16px system-ui;max-width:760px;margin:auto;padding:16px}"
                "article{border-bottom:1px solid #ccc;padding:8px 0}.m{color:#666;font-size:14px}audio{width:100%}"
                "</style><h1>Voice notes</h1>"
                f"<form><input name=q value='{html.escape(q)}' placeholder=Search>"
                f"{f'<input type=hidden name=token value={html.escape(tok)!r}>' if tok else ''}</form>"
                + ("".join(rows) or "<p>No notes yet.</p>")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(args) -> None:
    store = Store(args.dsn)
    for attempt in range(30):  # the database container may still be starting
        try:
            store.migrate()
            break
        except Exception as exc:
            log.warning("database not ready (%s); waiting", exc)
            time.sleep(2)
    else:
        raise SystemExit("Can't reach Postgres at DATABASE_URL")
    if n := store.recover():
        log.info("picked up %d notes a previous run didn't finish", n)
    transcriber = transcribe.from_env()
    worker = Worker(store, transcriber)
    stop = threading.Event()
    threading.Thread(target=worker.loop, args=(stop,), daemon=True, name="worker").start()
    token = os.environ.get("VOICE_REC_TOKEN", "").strip()
    if not token:
        log.warning("VOICE_REC_TOKEN isn't set: anyone who can reach this port can send and read notes")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, worker, token, transcriber.label))
    log.info("voice-rec on http://%s:%d · %s · vault %s · graph %s", args.host, args.port, transcriber.label,
             worker.vault or "off", os.environ.get("GRAPH_KIND") or "off")
    try:
        server.serve_forever()
    finally:
        stop.set()


# ---- CLI ----------------------------------------------------------------------------------------


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=default_dsn())
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--host", default=os.environ.get("VOICE_REC_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("VOICE_REC_PORT", "8094")))
    sub.add_parser("status")
    s = sub.add_parser("list")
    s.add_argument("-q", "--query", default="")
    s = sub.add_parser("show")
    s.add_argument("id")
    s = sub.add_parser("retry")
    s.add_argument("id")
    s = sub.add_parser("rearchive")
    s.add_argument("id", nargs="?")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("audio")
    s.add_argument("id")
    s.add_argument("out")
    args = p.parse_args(argv)
    if args.cmd in (None, "serve"):
        if args.cmd is None:
            args.host, args.port = os.environ.get("VOICE_REC_HOST", "127.0.0.1"), int(os.environ.get("VOICE_REC_PORT", "8094"))
        return serve(args)
    store = Store(args.dsn)
    store.migrate()
    if args.cmd == "status":
        print(json.dumps(store.counts(), indent=2))
    elif args.cmd == "list":
        for r in store.list(query=args.query, limit=10000):
            print(f"{r['recorded_at']}  {r['status']:<12} {r['id']}  {r.get('category') or '':<10} "
                  f"{r.get('title') or (r.get('transcript') or '')[:60]}")
    elif args.cmd == "show":
        print(json.dumps(store.get(args.id), indent=2, default=str))
    elif args.cmd == "retry":
        rec = store.get(args.id)
        if rec and rec.get("transcript") and rec["status"] != "failed":
            store.request_archive(args.id)
            print("archive requested again")
        else:
            store.retry(args.id)
            print("queued for transcription again")
    elif args.cmd == "rearchive":
        if not (args.id or args.all):
            raise SystemExit("give an id or --all")
        print(f"{store.rearchive(None if args.all else args.id)} notes will be archived again "
              "(by the running server)")
    elif args.cmd == "audio":
        got = store.audio(args.id)
        if not got:
            raise SystemExit("not found")
        Path(args.out).write_bytes(got[0])
        print(f"wrote {len(got[0])} bytes ({got[1]})")


if __name__ == "__main__":
    main()

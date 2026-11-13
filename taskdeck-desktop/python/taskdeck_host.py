"""TaskDeck Desktop's job host: runs the phone app's Python jobs for the C++ app.

The C++ app starts this as a child process and talks to it in JSON lines (one JSON object per line, UTF-8)
over stdin and stdout. The jobs are loaded unchanged from the phone app's source (``taskdeck.jobs``, the
``JobPlugin`` contract in ``taskdeck/core/plugin.py``).

    python -u taskdeck_host.py --src <folder holding the taskdeck package> [--log <file>]

Host -> app, unasked:
    {"event": "ready", "python": "3.14.3", "jobs": [<job>...], "errors": {"module": "why it didn't load"}}
    {"event": "fatal", "error": "..."}                       the jobs couldn't be loaded; the host exits
    {"event": "progress", "run": 7, "step": 1, "total": 4, "name": "check archive"}
    {"event": "done", "run": 7, "ok": true, "cancelled": false, "error": null, "seconds": 0.12,
     "result": <result or null>, "steps": [{"name", "stage", "seconds", "ok", "error"}...]}
    {"event": "committed", "run": 7, "ok": true, "message": "...", "confirm_label": null}

App -> host:
    {"op": "check", "req": 3, "job": "exe_parse", "settings": {}}  -> {"reply": 3, "problems": [<problem>...]}
    {"op": "run", "run": 7, "job": "exe_parse", "inputs": {...}, "settings": {}}
    {"op": "cancel", "run": 7}
    {"op": "commit", "run": 7, "settings": {}, "confirm": false}   confirm: commit the payload a
                                                                    "needs confirmation" answer offered
    {"op": "forget", "run": 7}                                      drop the run's commit payload

Run ids and request ids are chosen by the app. Jobs run on their own threads, so several can run at once
and a cancel arrives while one runs. Anything a job prints goes to the log, never to stdout, which carries
only the protocol. A job's commit payload stays here (it needn't be JSON) until committed or forgotten.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from typing import Any

_out = sys.stdout  # the protocol channel; sys.stdout itself is pointed at the log below
_out_lock = threading.Lock()


def send(msg: dict[str, Any]) -> None:
    line = json.dumps(msg, ensure_ascii=False, default=str)
    with _out_lock:
        _out.write(line + "\n")
        _out.flush()


def field_json(f: Any) -> dict[str, Any]:
    return {
        "name": f.name, "label": f.label, "kind": f.kind.value, "required": f.required,
        "default": f.default, "help": f.help, "choices": list(f.choices), "secret": f.secret,
        "lines": f.lines, "extensions": list(f.extensions), "grouped": f.grouped,
    }


def job_json(p: Any) -> dict[str, Any]:
    return {
        "id": p.id, "title": p.title, "description": p.description, "category": p.category,
        "tags": list(p.tags), "fields": [field_json(f) for f in p.fields],
        "settings": [field_json(f) for f in p.settings], "run_label": p.run_label,
        "commit_label": p.commit_label, "committed_label": p.committed_label,
        "quick_actions": [{"title": q.title, "inputs": q.inputs} for q in p.quick_actions],
    }


def problem_json(p: Any) -> dict[str, Any]:
    return {"message": p.message, "blocking": p.blocking, "fix": p.fix, "ok": p.ok}


def result_json(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "title": r.title, "text": r.text, "rows": [list(map(str, row)) for row in r.rows], "warning": r.warning,
        "table_title": r.table_title, "table": [list(map(str, row)) for row in r.table],
        "images": [i for i in r.images if isinstance(i, str)], "has_payload": r.payload is not None,
    }


class Host:
    def __init__(self, registry: Any) -> None:
        from taskdeck.core import Context

        self.registry = registry
        self.Context = Context
        self.lock = threading.Lock()
        self.contexts: dict[int, Any] = {}   # running: run id -> Context (for cancel)
        self.payloads: dict[int, tuple[Any, Any]] = {}  # run id -> (plugin, payload) awaiting commit
        self.confirms: dict[int, Any] = {}   # run id -> payload a NeedsConfirmation offered

    def handle(self, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        if op == "check":
            plugin = self.registry.get(msg.get("job", ""))
            problems = []
            if plugin is None:
                problems = [{"message": f"Unknown job: {msg.get('job')}", "blocking": True, "fix": "", "ok": False}]
            else:
                try:
                    problems = [problem_json(p) for p in plugin.check_ready(plugin.resolve_settings(msg.get("settings")))]
                except Exception as exc:
                    problems = [{"message": f"Couldn't check: {type(exc).__name__}: {exc}", "blocking": False,
                                 "fix": "recheck", "ok": False}]
            send({"reply": msg.get("req"), "problems": problems})
        elif op == "run":
            threading.Thread(target=self.run, args=(msg,), daemon=True).start()
        elif op == "cancel":
            with self.lock:
                ctx = self.contexts.get(msg.get("run"))
            if ctx is not None:
                ctx.cancel()
        elif op == "commit":
            threading.Thread(target=self.commit, args=(msg,), daemon=True).start()
        elif op == "forget":
            with self.lock:
                self.payloads.pop(msg.get("run"), None)
                self.confirms.pop(msg.get("run"), None)

    def run(self, msg: dict[str, Any]) -> None:
        rid = msg.get("run")
        plugin = self.registry.get(msg.get("job", ""))
        if plugin is None:
            send({"event": "done", "run": rid, "ok": False, "cancelled": False, "result": None, "steps": [],
                  "seconds": 0, "error": f"Unknown job: {msg.get('job')}"})
            return
        ctx = self.Context({})
        with self.lock:
            self.contexts[rid] = ctx

        def progress(i: int, total: int, step: Any) -> None:
            send({"event": "progress", "run": rid, "step": i, "total": total, "name": step.name})

        try:
            report = plugin.run(msg.get("inputs") or {}, progress, ctx, msg.get("settings"))
        except Exception as exc:  # run() turns job errors into reports; this is a bug in the job or the host
            traceback.print_exc()
            report = None
            error = f"{type(exc).__name__}: {exc}"
        finally:
            with self.lock:
                self.contexts.pop(rid, None)
        if report is None:
            send({"event": "done", "run": rid, "ok": False, "cancelled": False, "result": None, "steps": [],
                  "seconds": 0, "error": error})
            return
        if report.ok and report.result is not None and report.result.payload is not None and plugin.has_commit:
            with self.lock:
                self.payloads[rid] = (plugin, report.result.payload)
        send({"event": "done", "run": rid, "ok": report.ok, "cancelled": report.cancelled, "error": report.error,
              "seconds": report.seconds, "result": result_json(report.result),
              "steps": [{"name": s.name, "stage": s.stage.value, "seconds": s.seconds, "ok": s.ok, "error": s.error}
                        for s in report.steps]})

    def commit(self, msg: dict[str, Any]) -> None:
        rid = msg.get("run")
        with self.lock:
            plugin, payload = self.payloads.get(rid, (None, None))
            if msg.get("confirm") and rid in self.confirms:
                payload = self.confirms.pop(rid)
        if plugin is None:
            send({"event": "committed", "run": rid, "ok": False, "message": "Nothing to commit for this run.",
                  "confirm_label": None})
            return
        outcome = plugin.attempt_commit(payload, msg.get("settings"))
        with self.lock:
            if outcome.ok:
                self.payloads.pop(rid, None)
            elif outcome.confirm is not None:
                self.confirms[rid] = outcome.confirm.payload
        send({"event": "committed", "run": rid, "ok": outcome.ok, "message": outcome.message,
              "confirm_label": outcome.confirm.confirm_label if outcome.confirm else None})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="folder holding the taskdeck package")
    ap.add_argument("--log", help="file for anything jobs print, and the host's own errors")
    args = ap.parse_args(argv)
    sys.stdin.reconfigure(encoding="utf-8")
    _out.reconfigure(encoding="utf-8", newline="\n")  # Windows would otherwise write cp1252 and \r\n
    if args.log:
        sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr  # jobs that print must not corrupt the protocol
    sys.path.insert(0, args.src)
    try:
        from taskdeck.core import PluginRegistry

        registry = PluginRegistry().discover()
    except Exception as exc:
        traceback.print_exc()
        send({"event": "fatal", "error": f"Couldn't load the jobs from {args.src}: {type(exc).__name__}: {exc}"})
        return 1
    host = Host(registry)
    send({"event": "ready", "python": sys.version.split()[0], "jobs": [job_json(p) for p in registry],
          "errors": registry.errors})
    for line in sys.stdin:  # ends when the app closes our stdin (or exits)
        if not line.strip():
            continue
        try:
            host.handle(json.loads(line))
        except Exception:
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())

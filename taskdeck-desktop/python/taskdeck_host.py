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
    {"event": "task", "id": 2, "run": 7, "job": "exe_parse", "title": "Uploading ...", "status": "running",
     "done": 10, "total": 40, "fraction": 0.25, "eta": 12.5, "speed": 1048576.0, "note": "", "message": "",
     "fix": "", "phases": [{"label", "done", "total", "unit", "fraction"}...], "retry_label": "Retry upload",
     "drive_url": null, "inputs": {...}}
        a background task a job started (core/tasks.py: an upload that goes on after the run is done); sent
        when it starts, a few times a second while it moves, and when it ends (status done, failed, cancelled)
    {"event": "google_code", "req": 8, "user_code": "ABCD-EFGH", "url": "https://www.google.com/device"}
    {"event": "google_done", "req": 8, "ok": true, "refresh_token": "...", "email": "...", "error": ""}

App -> host:
    {"op": "check", "req": 3, "job": "exe_parse", "settings": {...}, "online": false}
        -> {"reply": 3, "problems": [<problem>...], "fixes": {...}}
        online: also the job's online checks (network), when the offline ones found nothing blocking. fixes:
        settings the checks found that work (a corrected address, say); the app saves them.
    {"op": "test_settings", "req": 4, "job": "daily_log", "values": {...}}   settings as entered, unsaved
        -> {"reply": 4, "settings": {...}, "passed": ["..."], "problems": [<problem>...], "fixes": {...}}
        tidies them (clean_settings), then runs the offline and online checks; settings: as tested, fixed
    {"op": "settings_action", "req": 5, "job": "voice_rec", "index": 0, "values": {...}}
        -> {"reply": 5, "text": "...", "values": {...}}   a settings screen button ("Test connection")
    {"op": "run", "run": 7, "job": "exe_parse", "inputs": {...}, "settings": {}}
    {"op": "cancel", "run": 7}
    {"op": "commit", "run": 7, "settings": {}, "confirm": false}   confirm: commit the payload a
                                                                    "needs confirmation" answer offered
    {"op": "forget", "run": 7}                                      drop the run's commit payload
    {"op": "task_cancel", "task": 2}     {"op": "task_forget", "task": 2}
    {"op": "task_retry", "task": 2}      run a failed or stopped task's work again (a new task, same run)
    {"op": "accounts", "google": {"client_id", "client_secret", "refresh_token"}}
        the Google account jobs sign in with (Context.services["google_drive"]); {} or blanks = not connected.
        The app sends it after "ready" and whenever it changes. The host keeps it in memory only.
    {"op": "google_connect", "req": 8, "client_id": "...", "client_secret": "..."}   Google's device sign-in:
        google_code, then google_done once approved on any device (or refused, expired, cancelled)
    {"op": "google_cancel", "req": 8}    {"op": "google_revoke", "token": "..."}   (best effort)

Settings sent by the app are the job's stored settings (secrets included); the host keeps none. Run ids and
request ids are chosen by the app. Jobs run on their own threads, so several can run at once
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
        "lines": f.lines, "extensions": list(f.extensions), "grouped": f.grouped, "importable": f.importable,
    }


def job_json(p: Any) -> dict[str, Any]:
    return {
        "id": p.id, "title": p.title, "description": p.description, "category": p.category,
        "tags": list(p.tags), "fields": [field_json(f) for f in p.fields],
        "settings": [field_json(f) for f in p.settings], "settings_id": p.settings_id,
        "settings_actions": [a.label for a in p.settings_actions], "run_label": p.run_label,
        "accounts": [[account, toggle] for account, toggle in p.accounts],
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
        "summary": (r.as_plain_text().splitlines() or ["Done"])[0],  # the line history shows, as on the phone
    }


def _problem(message: str) -> Any:
    from taskdeck.core import Problem

    return Problem(message, blocking=False, fix="recheck")


def _drive_url(location: Any) -> str | None:
    from taskdeck.shared.gdrive import ROOT, DriveLocation

    if not isinstance(location, DriveLocation):
        return None
    fid = location.folder_id
    return "https://drive.google.com/drive/my-drive" if fid == ROOT else f"https://drive.google.com/drive/folders/{fid}"


def _number(value: Any) -> float | None:
    try:
        return None if value is None else round(float(value), 3)
    except (TypeError, ValueError):
        return None


def task_json(t: Any, run: int, retry_label: str) -> dict[str, Any]:
    return {
        "event": "task", "id": t.id, "run": run, "job": t.job_id, "title": t.title, "status": t.status,
        "done": t.done, "total": t.total, "fraction": _number(t.fraction), "eta": _number(t.eta()),
        "speed": _number(t.speed()), "note": t.note, "message": t.message, "fix": t.fix,
        "phases": [{"label": p.label, "done": p.done, "total": p.total, "unit": p.unit, "fraction": _number(p.fraction)}
                   for p in t.phases],
        "retry_label": retry_label, "drive_url": _drive_url(t.result), "inputs": t.inputs,
    }


class Host:
    def __init__(self, registry: Any) -> None:
        from taskdeck.core import Context, TaskManager

        self.registry = registry
        self.Context = Context
        self.lock = threading.Lock()
        self.contexts: dict[int, Any] = {}   # running: run id -> Context (for cancel)
        self.payloads: dict[int, tuple[Any, Any]] = {}  # run id -> (plugin, payload) awaiting commit
        self.confirms: dict[int, Any] = {}   # run id -> payload a NeedsConfirmation offered
        # Background tasks (core/tasks.py), each reported as it changes. The task manager calls the listener on
        # its own thread (notify_in_background), so a task never waits on the pipe.
        self.tasks = TaskManager(start_thread=self._start_thread)
        self.tasks.notify_in_background = True
        self.tasks.listeners.append(self._on_task)
        self.task_info: dict[int, dict[str, Any]] = {}  # task id -> {"run", "retry": (title, work, label)}
        self._pending_info: dict[str, Any] | None = None
        self.google: dict[str, str] = {}     # the Google account: client_id, client_secret, refresh_token
        self._google_source: Any = None
        self.sign_ins: dict[Any, threading.Event] = {}  # google_connect request -> cancelled

    # ---- background tasks and accounts -------------------------------------------------------------

    def _start_thread(self, fn: Any) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _on_task(self, task: Any) -> None:
        info = self.task_info.get(task.id) or self._pending_info or {}
        retry = info.get("retry")
        send(task_json(task, info.get("run", 0), retry[2] if retry else ""))

    def start_task(self, run: int, job_id: str, inputs: dict[str, Any]) -> Any:
        """Context.services["start_task"] for one run: work(task) in the background, reported as "task" events.
        With ``retry_label`` a failed or stopped task offers that button, which runs ``work`` again."""
        def start(title: str, work: Any, retry_label: str = "", inputs_: dict[str, Any] | None = None) -> Any:
            return self._start(run, job_id, title, work, retry_label, dict(inputs if inputs_ is None else inputs_))
        return start

    def _start(self, run: int, job_id: str, title: str, work: Any, retry_label: str, inputs: dict[str, Any]) -> Any:
        info = {"run": run, "retry": (title, work, retry_label) if retry_label else None}
        with self.lock:  # the task's first notification may come before start() returns its id
            self._pending_info = info
            task = self.tasks.start(title, job_id, work, inputs=inputs)
            self.task_info[task.id] = info
            self._pending_info = None
        self._on_task(task)  # with its run and retry label, whatever the first notification had
        return task

    def _task(self, task_id: Any) -> Any:
        return next((t for t in self.tasks.tasks if t.id == task_id), None)

    def services(self, start_task: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"start_task": start_task}
        g = self.google
        if g.get("client_id") and g.get("client_secret") and g.get("refresh_token"):
            out["google_drive"] = self.drive  # only while connected, as on the phone
        return out

    def drive(self) -> Any:
        """A signed-in Drive client for the account as it is now (a retry after "Connect again" uses the new
        sign-in)."""
        from taskdeck.shared import gdrive, google_oauth

        g = self.google
        with self.lock:
            src = self._google_source
            if src is None or (src.client_id, src.client_secret, src.refresh_token) != \
                    (g.get("client_id"), g.get("client_secret"), g.get("refresh_token")):
                src = self._google_source = google_oauth.TokenSource(g["client_id"], g["client_secret"],
                                                                     g["refresh_token"])
        return gdrive.DriveClient(src.token)

    def google_connect(self, msg: dict[str, Any]) -> None:
        from taskdeck.shared import google_oauth

        req, cancelled = msg.get("req"), threading.Event()
        self.sign_ins[req] = cancelled
        done: dict[str, Any] = {"event": "google_done", "req": req, "ok": False, "refresh_token": "", "email": "",
                                "error": ""}
        try:
            client_id, secret = str(msg.get("client_id") or ""), str(msg.get("client_secret") or "")
            code = google_oauth.start(client_id)
            send({"event": "google_code", "req": req, "user_code": code.user_code, "url": code.verification_url})
            tokens = google_oauth.wait(client_id, secret, code, cancelled.is_set)
            if tokens is None:
                done["error"] = "Cancelled."
            else:
                done.update(ok=True, refresh_token=tokens.refresh_token, email=tokens.email)
        except Exception as exc:  # OAuthError messages are written for the user; network errors read the same
            done["error"] = str(exc) or type(exc).__name__
        finally:
            self.sign_ins.pop(req, None)
        send(done)

    def handle(self, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        if op in ("check", "test_settings", "settings_action"):  # these may use the network
            threading.Thread(target=self.answer, args=(msg,), daemon=True).start()
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
        elif op == "task_cancel":
            task = self._task(msg.get("task"))
            if task is not None:
                self.tasks.cancel(task)
        elif op == "task_forget":
            task = self._task(msg.get("task"))
            if task is not None and not task.running:
                self.tasks.forget(task)
                self.task_info.pop(task.id, None)
        elif op == "task_retry":
            task = self._task(msg.get("task"))
            info = self.task_info.get(task.id, {}) if task is not None else {}
            if task is not None and not task.running and info.get("retry"):
                title, work, label = info["retry"]
                self.tasks.forget(task)
                self.task_info.pop(task.id, None)
                self._start(info.get("run", 0), task.job_id, title, work, label, task.inputs)
        elif op == "accounts":
            google = msg.get("google") or {}
            self.google = {k: str(google.get(k) or "") for k in ("client_id", "client_secret", "refresh_token")}
        elif op == "google_connect":
            threading.Thread(target=self.google_connect, args=(msg,), daemon=True).start()
        elif op == "google_cancel":
            event = self.sign_ins.get(msg.get("req"))
            if event is not None:
                event.set()
        elif op == "google_revoke":
            from taskdeck.shared import google_oauth

            threading.Thread(target=google_oauth.revoke, args=(str(msg.get("token") or ""),), daemon=True).start()

    def answer(self, msg: dict[str, Any]) -> None:
        req, op = msg.get("req"), msg.get("op")
        plugin = self.registry.get(msg.get("job", ""))
        try:
            if plugin is None:
                raise LookupError(f"Unknown job: {msg.get('job')}")
            if op == "check":
                send({"reply": req, **self.check(plugin, msg.get("settings"), bool(msg.get("online")))})
            elif op == "test_settings":
                from taskdeck.core import check_settings

                found = check_settings(plugin, dict(msg.get("values") or {}))
                send({"reply": req, "settings": found.settings, "passed": found.passed,
                      "problems": [problem_json(p) for p in found.problems], "fixes": found.fixes})
            else:
                send({"reply": req, **self.settings_action(plugin, msg.get("index"), msg.get("values"))})
        except Exception as exc:  # a broken check must not break the screen, nor the host
            traceback.print_exc()
            message = str(exc) if isinstance(exc, LookupError) else f"Couldn't check: {type(exc).__name__}: {exc}"
            problem = {"message": message, "blocking": plugin is None, "fix": "recheck", "ok": False}
            send({"reply": req, "problems": [problem], "passed": [], "fixes": {}, "settings": msg.get("values") or {},
                  "text": message, "values": {}})

    @staticmethod
    def check(plugin: Any, stored: dict[str, Any] | None, online: bool) -> dict[str, Any]:
        settings = plugin.resolve_settings(stored)
        found = list(plugin.check_ready(settings))
        if online and not any(p.blocking for p in found):
            try:
                found += plugin.check_ready_online(settings)
            except Exception as exc:  # a broken check must not lock the job
                found.append(_problem(f"Couldn't finish checking: {type(exc).__name__}: {exc}"))
        fixes = {k: v for p in found if p.settings for k, v in p.settings.items()}
        return {"problems": [problem_json(p) for p in found], "fixes": fixes}

    @staticmethod
    def settings_action(plugin: Any, index: Any, values: dict[str, Any] | None) -> dict[str, Any]:
        from taskdeck.core import SettingsResult

        actions = plugin.settings_actions
        if not isinstance(index, int) or not 0 <= index < len(actions):
            return {"text": f"No such button: {index}", "values": {}}
        found = actions[index].run(plugin.resolve_settings(values))
        if isinstance(found, SettingsResult):
            return {"text": found.text, "values": dict(found.values)}
        return {"text": str(found or ""), "values": {}}

    def run(self, msg: dict[str, Any]) -> None:
        rid = msg.get("run")
        plugin = self.registry.get(msg.get("job", ""))
        if plugin is None:
            send({"event": "done", "run": rid, "ok": False, "cancelled": False, "result": None, "steps": [],
                  "seconds": 0, "error": f"Unknown job: {msg.get('job')}"})
            return
        ctx = self.Context({}, services=self.services(self.start_task(rid, plugin.id, msg.get("inputs") or {})))
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

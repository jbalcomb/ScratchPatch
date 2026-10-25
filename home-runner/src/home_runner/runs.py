"""Run history (SQLite) and the worker that runs queued jobs one at a time."""

import json
import logging
import os
import queue
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL,        -- queued, running, succeeded, failed, cancelled, interrupted
    exit_code INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
"""
ACTIVE = ("queued", "running")
# The runner is started with pythonw on Windows; without this every job would flash a console.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Runner:
    def __init__(self, jobs, data_dir):
        self.jobs = jobs
        self.data_dir = Path(data_dir)
        self.logs = self.data_dir / "runs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.data_dir / "runner.sqlite3", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._process = None  # (run id, Popen) of the queued run in progress
        self._cancelled = set()
        with self._lock:
            self._db.executescript(SCHEMA)
            # Anything still marked active belonged to a runner that's gone.
            self._db.execute("UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status IN (?, ?)",
                             (now(), *ACTIVE))
            self._db.commit()
        self._worker = threading.Thread(target=self._work, name="runner", daemon=True)
        self._worker.start()

    # ---- requests ----------------------------------------------------------

    def submit(self, name, params):
        """Validate and start a run. Quick jobs finish before this returns."""
        job = self.jobs[name]  # caller checks the name
        argv = job.argv(params)
        run_id = self._insert(name, params, "running" if job.quick else "queued")
        if job.quick:
            self._run_quick(run_id, job, argv)
        else:
            self._queue.put((run_id, job, argv))
        return self.get(run_id)

    def get(self, run_id, tail=40):
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            ahead = None
            if row is not None and row["status"] == "queued":
                ahead = self._db.execute("SELECT COUNT(*) FROM runs WHERE status IN (?, ?) AND id < ?",
                                         (*ACTIVE, run_id)).fetchone()[0]
        if row is None:
            return None
        run = _public(row)
        run["ahead"] = ahead
        lines = self._tail(run_id, tail)
        run["log"] = lines
        run["last_line"] = lines[-1] if lines else None
        if self.jobs.get(row["job"]) and self.jobs[row["job"]].quick and row["status"] == "succeeded":
            output = self.log_path(run_id).read_text(encoding="utf-8", errors="replace")
            run["output"] = output
            try:
                run["result"] = json.loads(output)
            except ValueError:
                pass
        return run

    def list(self, limit=20, include_quick=False):
        """Recent runs, newest first. Quick lookups are left out unless asked for:
        they'd bury the runs people care about."""
        hidden = [] if include_quick else [name for name, job in self.jobs.items() if job.quick]
        marks = ",".join("?" * len(hidden))
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM runs WHERE job NOT IN ({marks}) ORDER BY id DESC LIMIT ?",
                                    (*hidden, limit)).fetchall()
        runs = []
        for row in rows:
            run = _public(row)
            lines = self._tail(row["id"], 1)
            run["last_line"] = lines[-1] if lines else None
            runs.append(run)
        return runs

    def cancel(self, run_id):
        """Cancel a queued run, or stop the one that's running. Capture jobs commit as
        they go, so stopping one loses at most the page in flight."""
        with self._lock:
            row = self._db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            if row["status"] in ACTIVE:
                self._cancelled.add(run_id)
                if row["status"] == "queued":
                    self._finish(run_id, "cancelled", None)
                elif self._process and self._process[0] == run_id:
                    self._process[1].terminate()
        return self.get(run_id)

    def log_path(self, run_id):
        return self.logs / f"{run_id}.log"

    # ---- running -----------------------------------------------------------

    def _work(self):
        while True:
            run_id, job, argv = self._queue.get()
            log.info("run %d: %s", run_id, job.name)
            try:
                with open(self.log_path(run_id), "ab") as out:
                    # Start it and mark it running under one lock, so a cancel always
                    # finds either a queued run or a process it can stop.
                    with self._lock:
                        if run_id in self._cancelled:
                            continue
                        process = subprocess.Popen(argv, cwd=job.cwd, stdout=out, stderr=subprocess.STDOUT,
                                                   stdin=subprocess.DEVNULL, env=_child_env(),
                                                   creationflags=NO_WINDOW)
                        self._process = (run_id, process)
                        self._db.execute("UPDATE runs SET status = 'running', started_at = ? WHERE id = ?",
                                         (now(), run_id))
                        self._db.commit()
                    try:
                        code = process.wait(timeout=job.timeout)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        code = process.wait()
                        out.write(f"\n[runner] stopped after {job.timeout:g} s\n".encode())
            except OSError as error:  # couldn't start it at all
                self.log_path(run_id).write_text(f"[runner] couldn't start {argv[0]}: {error}\n", encoding="utf-8")
                code = None
            with self._lock:
                self._process = None
                status = "cancelled" if run_id in self._cancelled else ("succeeded" if code == 0 else "failed")
                self._finish(run_id, status, code)
            log.info("run %d: %s (exit %s)", run_id, status, code)

    def _run_quick(self, run_id, job, argv):
        with self._lock:
            self._db.execute("UPDATE runs SET started_at = ? WHERE id = ?", (now(), run_id))
            self._db.commit()
        try:
            done = subprocess.run(argv, cwd=job.cwd, capture_output=True, stdin=subprocess.DEVNULL,
                                  env=_child_env(), creationflags=NO_WINDOW, timeout=job.timeout)
            code, output = done.returncode, done.stdout + (done.stderr if done.returncode else b"")
        except subprocess.TimeoutExpired:
            code, output = None, f"[runner] stopped after {job.timeout:g} s\n".encode()
        except OSError as error:
            code, output = None, f"[runner] couldn't start {argv[0]}: {error}\n".encode()
        self.log_path(run_id).write_bytes(output)
        with self._lock:
            self._finish(run_id, "succeeded" if code == 0 else "failed", code)

    # ---- storage -----------------------------------------------------------

    def _insert(self, name, params, status):
        with self._lock:
            cursor = self._db.execute("INSERT INTO runs (job, params, status, created_at) VALUES (?, ?, ?, ?)",
                                      (name, json.dumps(params or {}), status, now()))
            self._db.commit()
            return cursor.lastrowid

    def _finish(self, run_id, status, code):  # caller holds the lock
        self._db.execute("UPDATE runs SET status = ?, exit_code = ?, finished_at = ? WHERE id = ?",
                         (status, code, now(), run_id))
        self._db.commit()

    def _tail(self, run_id, lines):
        path = self.log_path(run_id)
        if lines <= 0 or not path.is_file():
            return []
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            text = f.read().decode("utf-8", errors="replace")
        return [line for line in text.splitlines() if line.strip()][-lines:]


def _public(row):
    run = dict(row)
    run["params"] = json.loads(run["params"])
    return run


def _child_env():
    # Unbuffered, so the log shows progress while the job runs, not just when it ends.
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

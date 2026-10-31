"""Jobs: each request becomes a job that runs idaw.exe once (twice when the database must be built first).

States: queued -> running -> succeeded | failed | timed_out | cancelled. A job that was running when the
service stopped is reported as interrupted after the restart.

Folders under IDABRIDGE_HOME:
  db/<name>-<hash>/     one per input file (by content): a copy of the binary and its .idb
  jobs/<id>/            job.json, job.idc, result.tsv, ida.log, ida.out and any output files
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psutil

from . import ops as ops_mod
from . import winproc
from .config import Settings

UNPACKED = (".id0", ".id1", ".id2", ".nam", ".til")
FINISHED = ("succeeded", "failed", "timed_out", "cancelled", "interrupted")
MAX_INLINE_ROWS = 20000


class JobError(Exception):
    """Raised for requests that are refused before a job starts (bad file, disabled operation)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Step:
    name: str
    command: list[str]
    started: str
    finished: str | None = None
    seconds: float | None = None
    pid: int | None = None
    in_job_object: bool | None = None
    exit_code: int | None = None
    exit_meaning: str | None = None
    timed_out: bool = False
    cancelled: bool = False
    diagnostics: dict | None = None


@dataclass
class Job:
    id: str
    op: str
    params: dict
    file: str
    mode: str
    timeout: int
    created: str = field(default_factory=_now)
    state: str = "queued"
    started: str | None = None
    finished: str | None = None
    error: dict | None = None
    result: dict | None = None
    files: list[dict] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    ida_log_tail: str | None = None

    def to_dict(self, rows: bool = True) -> dict:
        d = asdict(self)
        if not rows and d.get("result"):
            d["result"] = {k: v for k, v in d["result"].items() if k != "rows"}
            d["result"]["row_count"] = len(self.result.get("rows", []))  # type: ignore[union-attr]
        return d


class JobManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.s.db_dir.mkdir(parents=True, exist_ok=True)
        self.s.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.events: dict[str, threading.Event] = {}
        self.procs: dict[str, winproc.KillGroup] = {}
        self.cancelled: set[str] = set()
        self.lock = threading.RLock()
        self.db_locks: dict[str, threading.Lock] = {}
        self.hash_cache: dict[tuple, str] = {}
        self.pool = ThreadPoolExecutor(max_workers=settings.max_parallel, thread_name_prefix="ida")
        self._last_cleanup = 0.0
        self._recover()

    # ------------------------------------------------------------ public

    def submit(self, op_name: str, params: ops_mod.Base) -> Job:
        op = ops_mod.OPS.get(op_name)
        if op is None:
            raise JobError(404, f"unknown operation {op_name!r}")
        if op_name == "run_idc" and not self.s.allow_raw_idc:
            raise JobError(403, "run_idc is disabled on this server (set IDABRIDGE_ALLOW_IDC=1 to allow it)")
        self.input_path(params.file)  # refuse a missing file now, not inside the job
        timeout = min(params.timeout or self.s.default_timeout, self.s.max_timeout)
        job = Job(id=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], op=op_name,
                  params=params.model_dump(), file=params.file, mode=ops_mod.mode_of(op, params), timeout=timeout)
        self.job_dir(job.id).mkdir(parents=True)
        with self.lock:
            self.jobs[job.id] = job
            self.events[job.id] = threading.Event()
        self._save(job)
        self.pool.submit(self._run, job, op, params)
        self._maybe_cleanup()
        return job

    def get(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise JobError(404, f"no job {job_id!r}")
        return job

    def wait(self, job_id: str, seconds: float) -> Job:
        job = self.get(job_id)
        event = self.events.get(job_id)
        if event is not None and seconds > 0:
            event.wait(seconds)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.state in FINISHED:
            return job
        with self.lock:
            self.cancelled.add(job_id)
            group = self.procs.get(job_id)
        if group is not None:
            group.kill()
        return job

    def list(self, limit: int = 50) -> list[Job]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.id, reverse=True)
        return jobs[:limit]

    def live(self, job: Job) -> dict | None:
        """Quick view of a running job: elapsed time, what the OS says (no sampling), last log lines."""
        if job.state != "running" or not job.steps:
            return None
        step = job.steps[-1]
        info: dict = {"step": step.name, "elapsed_seconds": round(time.time() - _ts(job.started), 1)}
        if step.pid:
            try:
                p = psutil.Process(step.pid)
                cpu = p.cpu_times()
                info["process"] = {"pid": step.pid, "status": p.status(), "cpu_seconds": round(cpu.user + cpu.system, 2),
                                   "memory_mb": round(p.memory_info().rss / 1048576, 1)}
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                info["process"] = {"pid": step.pid, "status": "gone"}
        info["ida_log_tail"] = self._tail(self.job_dir(job.id) / "ida.log", 1500)
        return info

    def diagnose(self, job_id: str) -> dict:
        """A sampled snapshot (CPU/disk activity, windows) of a running job's IDA process."""
        job = self.get(job_id)
        if job.state != "running" or not job.steps or not job.steps[-1].pid:
            return {"state": job.state, "note": "the job is not running"}
        return winproc.snapshot(job.steps[-1].pid)

    def job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[\w-]+", job_id):
            raise JobError(400, "bad job id")
        return self.s.jobs_dir / job_id

    def job_file(self, job_id: str, name: str) -> Path:
        job = self.get(job_id)
        base = {"result.tsv", "ida.log", "ida.out", "job.idc"}
        allowed = {f["name"] for f in job.files} | base | {"analyze." + b for b in base}
        if name not in allowed:
            raise JobError(404, f"job {job_id} has no file {name!r}")
        path = self.job_dir(job_id) / name
        if not path.is_file():
            raise JobError(404, f"{name} was not produced")
        return path

    def input_path(self, rel: str) -> Path:
        root = self.s.input_dir
        path = (root / rel).resolve()
        if not path.is_relative_to(root):
            raise JobError(400, "file is outside IDA_INPUT_DIR")
        if not path.is_file():
            raise JobError(404, f"{rel} not found in IDA_INPUT_DIR")
        return path

    def list_inputs(self, rel: str = "", pattern: str = "*", limit: int = 500) -> list[dict]:
        root = self.s.input_dir
        base = (root / rel).resolve()
        if not base.is_relative_to(root) or not base.is_dir():
            raise JobError(404, f"{rel or '.'} is not a folder in IDA_INPUT_DIR")
        out = []
        for p in sorted(base.glob(pattern)):
            if len(out) >= limit:
                break
            out.append({"path": p.relative_to(root).as_posix(), "type": "folder" if p.is_dir() else "file",
                        "size": p.stat().st_size if p.is_file() else None,
                        "analysed": p.is_file() and self._idb_for(p, compute=False) is not None})
        return out

    def shutdown(self) -> None:
        with self.lock:
            groups = list(self.procs.values())
        for g in groups:
            g.kill()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ databases

    def _hash(self, path: Path) -> str:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in self.hash_cache:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            self.hash_cache[key] = h.hexdigest()
        return self.hash_cache[key]

    def _db_folder(self, path: Path) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", path.name)[:60]
        return self.s.db_dir / f"{safe}-{self._hash(path)[:12]}"

    def _idb_for(self, path: Path, compute: bool = True) -> Path | None:
        if not compute:
            st = path.stat()
            if (str(path), st.st_size, st.st_mtime_ns) not in self.hash_cache:
                return None
        idb = self._db_folder(path) / (path.stem + ".idb")
        return idb if idb.is_file() else None

    def _db_lock(self, folder: Path) -> threading.Lock:
        with self.lock:
            return self.db_locks.setdefault(folder.name, threading.Lock())

    @staticmethod
    def _clean_unpacked(folder: Path, stem: str) -> None:
        for ext in UNPACKED:
            (folder / (stem + ext)).unlink(missing_ok=True)

    # ------------------------------------------------------------ running a job

    def _run(self, job: Job, op: ops_mod.Op, params: ops_mod.Base) -> None:
        job.state, job.started = "running", _now()
        self._save(job)
        jdir = self.job_dir(job.id)
        try:
            if job.id in self.cancelled:
                raise _Stop("cancelled")
            src = self.input_path(job.file)
            folder = self._db_folder(src)
            folder.mkdir(parents=True, exist_ok=True)
            idb = folder / (src.stem + ".idb")
            lock = self._db_lock(folder)

            force = bool(getattr(params, "force", False))
            processor = getattr(params, "processor", None)
            with lock:
                if force or not idb.is_file():
                    state, error = self._create_db(job, src, folder, idb, processor)
                    if state != "succeeded" or op.name == "analyze":
                        self._finish(job, state, error)
                        return
                if job.mode == "write":
                    self._finish(job, *self._run_write(job, op, params, idb))
                    return
                snap = self._snapshot(jdir, idb)  # copied under the lock, so never mid-write
            if op.name == "analyze":  # the database was already there: report on it
                op, params = ops_mod.OPS["info"], ops_mod.OPS["info"].params(file=job.file)
            self._finish(job, *self._ida(job, op.name, op, params, target=snap, cwd=snap.parent, save=False))
        except _Stop as stop:
            self._finish(job, stop.state, stop.error)
        except JobError as e:
            self._finish(job, "failed", {"kind": "bad_request", "message": e.message})
        except Exception as e:  # noqa: BLE001 - report anything to the caller rather than losing the job
            self._finish(job, "failed", {"kind": "server_error", "message": f"{type(e).__name__}: {e}"})
        finally:
            snap_dir = jdir / "db"
            if snap_dir.exists():
                shutil.rmtree(snap_dir, ignore_errors=True)
            if job.state in ("queued", "running"):
                self._finish(job, "failed", {"kind": "server_error", "message": "job ended without a result"})

    def _snapshot(self, jdir: Path, idb: Path) -> Path:
        snap_dir = jdir / "db"
        snap_dir.mkdir(exist_ok=True)
        snap = snap_dir / idb.name
        shutil.copy2(idb, snap)
        return snap

    def _create_db(self, job: Job, src: Path, folder: Path, idb: Path, processor: str | None) -> tuple[str, dict | None]:
        for p in folder.iterdir():
            if p.is_file():
                p.unlink()
        binary = folder / src.name
        shutil.copy2(src, binary)
        op = ops_mod.OPS["analyze"]
        extra = ["-c"] + ([f"-p{processor}"] if processor else [])
        state, error = self._ida(job, "analyze", op, op.params(file=job.file), target=binary, cwd=folder,
                                 save=True, extra=extra, final=(job.op == "analyze"))
        if state == "succeeded" and not idb.is_file():
            state, error = "failed", {"kind": "database_not_created",
                                      "message": "IDA ran but did not save a database for this file; see ida_log_tail."}
        if state != "succeeded":
            self._clean_unpacked(folder, src.stem)
            idb.unlink(missing_ok=True)
            error = dict(error or {}, step="building the database (first use of this file)")
        return state, error

    def _run_write(self, job: Job, op: ops_mod.Op, params: ops_mod.Base, idb: Path) -> tuple[str, dict | None]:
        backup = idb.with_suffix(".idb.bak")
        shutil.copy2(idb, backup)
        state, error = self._ida(job, op.name, op, params, target=idb, cwd=idb.parent, save=True)
        if state == "succeeded" and idb.is_file():
            backup.unlink(missing_ok=True)
        else:  # leave the database exactly as it was
            self._clean_unpacked(idb.parent, idb.stem)
            shutil.move(backup, idb)
            error = dict(error or {}, database="restored to its state before this job")
        return state, error

    def _ida(self, job: Job, step_name: str, op: ops_mod.Op, params: ops_mod.Base, *, target: Path, cwd: Path,
             save: bool, extra: list[str] | None = None, final: bool = True) -> tuple[str, dict | None]:
        """Run IDA once; returns (state, error). The result is kept on the job when `final` or on failure."""
        jdir = self.job_dir(job.id)
        prefix = "" if final else f"{step_name}."
        result_path = jdir / f"{prefix}result.tsv"
        script_path = jdir / f"{prefix}job.idc"
        log_path = jdir / f"{prefix}ida.log"
        out_path = jdir / f"{prefix}ida.out"
        script = ops_mod.build_script(op, params, ops_mod.Ctx(jdir), self.s.idc_dir, result_path, save)
        script_path.write_text(script, encoding="cp1252", newline="\r\n")

        cmd = [*self.s.ida_cmd, "-A", *(extra or []), f"-S{script_path}"]
        if self.s.use_log_switch:
            cmd.append(f"-L{log_path}")
        cmd.append(str(target))

        step = Step(name=step_name, command=cmd, started=_now())
        job.steps.append(step)
        self._save(job)
        env = dict(os.environ, TVHEADLESS="1")
        flags = subprocess.CREATE_NO_WINDOW if winproc.IS_WINDOWS else 0  # type: ignore[attr-defined]
        t0 = time.time()
        with open(out_path, "wb") as out:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    env=env, creationflags=flags)
            group = winproc.KillGroup(proc)
            step.pid, step.in_job_object = proc.pid, group.in_job_object
            with self.lock:
                self.procs[job.id] = group
            self._save(job)
            deadline = t0 + job.timeout
            try:
                while proc.poll() is None:
                    if job.id in self.cancelled:
                        step.cancelled = True
                        group.kill()
                        break
                    if time.time() > deadline:
                        step.timed_out = True
                        step.diagnostics = winproc.snapshot(proc.pid)
                        step.diagnostics["action"] = "IDA was stopped (process tree killed) after the timeout"
                        group.kill()
                        break
                    time.sleep(0.25)
                proc.wait()
                if job.id in self.cancelled:  # a cancel can kill IDA before this loop notices the flag
                    step.cancelled, step.timed_out = True, False
            finally:
                group.close()
                with self.lock:
                    self.procs.pop(job.id, None)

        step.finished = _now()
        step.seconds = round(time.time() - t0, 2)
        step.exit_code = proc.returncode
        step.exit_meaning = winproc.describe_exit_code(proc.returncode)
        job.ida_log_tail = self._tail(log_path, 4000) or self._tail(out_path, 4000)

        text = result_path.read_text(encoding="cp1252", errors="replace") if result_path.exists() else None
        parsed = ops_mod.parse_result(text, op.columns) if text is not None else None
        if parsed and len(parsed["rows"]) > MAX_INLINE_ROWS:
            parsed["rows"] = parsed["rows"][:MAX_INLINE_ROWS]
            parsed["truncated"] = True

        state, error = self._judge(step, parsed, proc.returncode)
        if final or state != "succeeded":
            job.result = parsed
            if state == "succeeded":
                job.files = self._collect(jdir, op.output_files(params), job)
                missing = [n for n in op.output_files(params) if not (jdir / n).is_file()]
                if missing:
                    state, error = "failed", {"kind": "output_missing",
                                              "message": f"IDA finished but did not write: {', '.join(missing)}"}
        self._save(job)
        return state, error

    @staticmethod
    def _judge(step: Step, parsed: dict | None, code: int | None) -> tuple[str, dict | None]:
        exit_text = f"exit code {code}" + (f" ({step.exit_meaning})" if step.exit_meaning else "")
        if step.cancelled:
            return "cancelled", {"kind": "cancelled", "message": "cancelled on request; IDA was stopped"}
        if step.timed_out:
            d = step.diagnostics or {}
            return "timed_out", {"kind": "timeout", "message": f"IDA did not finish in time. The OS said: {d.get('verdict')}",
                                 "diagnostics": d}
        if parsed is None:
            return "failed", {"kind": "script_not_run", "message": (
                f"IDA exited ({exit_text}) without running the operation's script. Usual causes: IDA could not load "
                "the file or database, the IDC did not compile (a function missing in IDA 5.5), a licence or "
                "first-run prompt, or the service account cannot start IDA. See ida_log_tail.")}
        if not parsed["completed"]:
            return "failed", {"kind": "script_aborted", "message": (
                f"The script started but stopped part-way ({exit_text}): an IDC runtime error or a crash. "
                "Partial output is in result.")}
        if parsed["rc"] != 0 or parsed["error"]:
            return "failed", {"kind": "ida_error", "message": parsed["error"] or f"operation returned {parsed['rc']}"}
        return "succeeded", None

    def _collect(self, jdir: Path, names: list[str], job: Job) -> list[dict]:
        out = []
        for n in names:
            p = jdir / n
            if p.is_file():
                out.append({"name": n, "size": p.stat().st_size, "url": f"/jobs/{job.id}/files/{n}"})
        return out

    def _finish(self, job: Job, state: str, error: dict | None) -> None:
        job.state, job.error, job.finished = state, error, _now()
        self._save(job)
        event = self.events.get(job.id)
        if event is not None:
            event.set()

    # ------------------------------------------------------------ persistence and housekeeping

    def _save(self, job: Job) -> None:
        path = self.job_dir(job.id) / "job.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job.to_dict(), indent=1), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _tail(path: Path, n: int) -> str | None:
        if not path.is_file():
            return None
        data = path.read_bytes()[-n:]
        return data.decode("cp1252", errors="replace")

    def _recover(self) -> None:
        # A write that was cut off leaves .idb.bak: put the database back as it was.
        for bak in self.s.db_dir.glob("*/*.idb.bak"):
            idb = bak.with_name(bak.name[:-4])
            self._clean_unpacked(bak.parent, idb.stem)
            shutil.move(bak, idb)
        for jf in self.s.jobs_dir.glob("*/job.json"):
            try:
                d = json.loads(jf.read_text(encoding="utf-8"))
                steps = [Step(**s) for s in d.pop("steps", [])]
                job = Job(**d, steps=steps)
            except (ValueError, TypeError):
                continue
            if job.state not in FINISHED:
                pid = steps[-1].pid if steps else None
                still = None
                if pid and psutil.pid_exists(pid):
                    still = winproc.snapshot(pid, sample_seconds=0.5)
                job.state, job.finished = "interrupted", _now()
                job.error = {"kind": "interrupted", "message": "the service restarted while this job was running",
                             "process_after_restart": still or {"alive": False, "pid": pid}}
                self._save(job)
            self.jobs[job.id] = job
            ev = threading.Event()
            ev.set()
            self.events[job.id] = ev
        self._maybe_cleanup(force=True)

    def _maybe_cleanup(self, force: bool = False) -> None:
        if not force and time.time() - self._last_cleanup < 3600:
            return
        self._last_cleanup = time.time()
        cutoff = time.time() - self.s.job_retention_hours * 3600
        for d in self.s.jobs_dir.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                with self.lock:
                    job = self.jobs.get(d.name)
                    if job is not None and job.state not in FINISHED:
                        continue
                    self.jobs.pop(d.name, None)
                    self.events.pop(d.name, None)
                shutil.rmtree(d, ignore_errors=True)


class _Stop(Exception):
    def __init__(self, state: str, error: dict | None = None):
        super().__init__(state)
        self.state = state
        self.error = error


def _ts(iso: str | None) -> float:
    return datetime.fromisoformat(iso).timestamp() if iso else time.time()

"""The REST API (FastAPI) and the MCP server (Streamable HTTP at /mcp), in one app.

HTTP status of an operation or job:
  200 succeeded · 202 queued/running (poll /jobs/{id}) · 502 IDA failed, was cancelled or interrupted ·
  504 IDA timed out · 4xx the request was refused before IDA ran. The body is always the job as JSON.
"""

from __future__ import annotations

import contextlib
import hmac
import inspect
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from . import ops as ops_mod
from .config import Settings
from .runner import FINISHED, Job, JobError, JobManager

STATUS = {"succeeded": 200, "queued": 202, "running": 202, "failed": 502, "cancelled": 502,
          "interrupted": 502, "timed_out": 504}
TEXT_LIMIT = 200_000


def job_body(mgr: JobManager, job: Job, rows: bool = True) -> dict:
    body = job.to_dict(rows=rows)
    if job.state not in FINISHED:
        body["live"] = mgr.live(job)
        body["next"] = f"GET /jobs/{job.id}?wait=50 (or the job_status tool) until state is final"
    return body


def job_response(mgr: JobManager, job: Job, rows: bool = True) -> JSONResponse:
    headers = {"Location": f"/jobs/{job.id}"} if job.state not in FINISHED else None
    return JSONResponse(job_body(mgr, job, rows), status_code=STATUS.get(job.state, 200), headers=headers)


def read_text_file(mgr: JobManager, job_id: str, name: str, offset_line: int, max_lines: int) -> dict:
    path = mgr.job_file(job_id, name)
    lines: list[str] = []
    total = 0
    size = 0
    with open(path, encoding="cp1252", errors="replace") as f:
        for i, line in enumerate(f):
            total = i + 1
            if i >= offset_line and len(lines) < max_lines and size < TEXT_LIMIT:
                lines.append(line.rstrip("\r\n"))
                size += len(line)
    return {"name": name, "offset_line": offset_line, "lines": lines, "line_count": len(lines),
            "total_lines": total, "more": offset_line + len(lines) < total}


class BearerAuth(BaseHTTPMiddleware):
    def __init__(self, app, token: str | None):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if self.token and request.url.path not in ("/health",):
            given = request.headers.get("authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {self.token}".encode()):
                return JSONResponse({"error": "missing or wrong bearer token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)


# ---------------------------------------------------------------- MCP

def build_mcp(mgr: JobManager, settings: Settings) -> MCPServer:
    mcp = MCPServer("ida-pro-5.5", instructions=(
        "Tools for IDA Pro 5.5 (32-bit) on a Windows machine. `file` is a path relative to the server's input "
        "folder (use list_inputs). The first use of a file builds its database, which can take minutes. Every "
        "tool waits up to wait_seconds; if the job is still running you get state 'running' and a job_id: call "
        "job_status with it. Output files (export, run_idc) are read with read_job_file. Read-only tools run "
        "in parallel; tools that change the database run one at a time per file and save it."))

    async def run_op(op_name: str, model: type[ops_mod.Base], kw: dict) -> dict:
        wait = kw.pop("wait_seconds", settings.default_wait)
        try:
            params = model(**kw)
            job = mgr.submit(op_name, params)
        except JobError as e:
            return {"state": "refused", "error": {"kind": "bad_request", "message": e.message}}
        except ValidationError as e:
            return {"state": "refused", "error": {"kind": "bad_request", "message": str(e)}}
        job = await anyio.to_thread.run_sync(mgr.wait, job.id, wait)
        return job_body(mgr, job)

    for op in ops_mod.OPS.values():
        _register_op_tool(mcp, op, run_op, settings)

    async def job_status(job_id: str, wait_seconds: Annotated[int, Field(ge=0, le=300)] = settings.default_wait) -> dict:
        """State and result of a job; waits up to wait_seconds for it to finish."""
        try:
            job = await anyio.to_thread.run_sync(mgr.wait, job_id, wait_seconds)
        except JobError as e:
            return {"error": e.message}
        return job_body(mgr, job)

    async def cancel_job(job_id: str) -> dict:
        """Stop a queued or running job (kills IDA)."""
        try:
            job = mgr.cancel(job_id)
            job = await anyio.to_thread.run_sync(mgr.wait, job_id, 15)
        except JobError as e:
            return {"error": e.message}
        return job_body(mgr, job, rows=False)

    async def diagnose_job(job_id: str) -> dict:
        """What Windows says about a running job's IDA process: busy or idle, CPU, memory, open windows."""
        try:
            return await anyio.to_thread.run_sync(mgr.diagnose, job_id)
        except JobError as e:
            return {"error": e.message}

    async def list_jobs(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> list[dict]:
        """Recent jobs, newest first (without their rows)."""
        return [{"id": j.id, "op": j.op, "file": j.file, "state": j.state, "created": j.created,
                 "finished": j.finished} for j in mgr.list(limit)]

    async def read_job_file(job_id: str, name: str, offset_line: Annotated[int, Field(ge=0)] = 0,
                            max_lines: Annotated[int, Field(ge=1, le=5000)] = 500) -> dict:
        """Read lines of a job's text file (an export such as an .asm, or ida.log). Page with offset_line."""
        try:
            return read_text_file(mgr, job_id, name, offset_line, max_lines)
        except JobError as e:
            return {"error": e.message}

    async def list_inputs(path: str = "", pattern: str = "*") -> list[dict] | dict:
        """Files and folders in the server's input folder (IDA_INPUT_DIR), relative to it."""
        try:
            return mgr.list_inputs(path, pattern)
        except JobError as e:
            return {"error": e.message}

    for fn in (job_status, cancel_job, diagnose_job, list_jobs, read_job_file, list_inputs):
        mcp.add_tool(fn, name=fn.__name__, description=fn.__doc__)
    return mcp


def _register_op_tool(mcp: MCPServer, op: ops_mod.Op, run_op, settings: Settings) -> None:
    model = op.params
    params = []
    for name, f in model.model_fields.items():
        # A default_factory stays on the FieldInfo in the annotation; a plain default goes on the parameter.
        default = inspect.Parameter.empty if f.is_required() or f.default_factory is not None else f.default
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default,
                                        annotation=Annotated[f.annotation, f]))
    params.append(inspect.Parameter(
        "wait_seconds", inspect.Parameter.KEYWORD_ONLY, default=settings.default_wait,
        annotation=Annotated[int, Field(ge=0, le=300, description="How long to wait for the result before "
                                                                   "returning the job_id to poll.")]))

    async def tool(**kw: Any) -> dict:
        return await run_op(op.name, model, kw)

    tool.__signature__ = inspect.Signature(params, return_annotation=dict)  # type: ignore[attr-defined]
    tool.__name__ = op.name
    desc = op.summary + (" Changes the database." if op.mode == "write" else "")
    mcp.add_tool(tool, name=op.name, description=desc)


# ---------------------------------------------------------------- REST

def create_app(settings: Settings, mgr: JobManager | None = None) -> FastAPI:
    mgr = mgr or JobManager(settings)
    mcp = build_mcp(mgr, settings)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield
        mgr.shutdown()

    app = FastAPI(title="IDA Pro 5.5 bridge", version="0.1.0", lifespan=lifespan, description=__doc__)
    app.add_middleware(BearerAuth, token=settings.token)
    app.state.manager = mgr

    @app.exception_handler(JobError)
    async def _job_error(request, exc: JobError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.get("/health")
    def health():
        exe = settings.ida_cmd[0]
        return {"ok": True, "ida": exe, "ida_found": Path(exe).is_file(),
                "input_dir": str(settings.input_dir), "input_dir_found": settings.input_dir.is_dir(),
                "running": sum(1 for j in mgr.list(1000) if j.state == "running"),
                "max_parallel": settings.max_parallel}

    @app.get("/ops")
    def list_ops():
        return [{"name": o.name, "summary": o.summary, "mode": o.mode, "endpoint": f"POST /ops/{o.name}",
                 "params": o.params.model_json_schema()} for o in ops_mod.OPS.values()]

    @app.get("/inputs")
    def inputs(path: str = "", pattern: str = "*", limit: int = Query(500, ge=1, le=5000)):
        return mgr.list_inputs(path, pattern, limit)

    for op in ops_mod.OPS.values():
        _add_op_route(app, mgr, op, settings)

    @app.get("/jobs")
    def jobs(limit: int = Query(50, ge=1, le=1000)):
        return [j.to_dict(rows=False) for j in mgr.list(limit)]

    @app.get("/jobs/{job_id}")
    def job(job_id: str, wait: int = Query(0, ge=0, le=600), rows: bool = True):
        return job_response(mgr, mgr.wait(job_id, wait), rows)

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        mgr.cancel(job_id)
        return job_response(mgr, mgr.wait(job_id, 15), rows=False)

    @app.get("/jobs/{job_id}/diagnose")
    def diagnose(job_id: str):
        return mgr.diagnose(job_id)

    @app.get("/jobs/{job_id}/files/{name}")
    def job_file(job_id: str, name: str):
        return FileResponse(mgr.job_file(job_id, name), filename=name)

    @app.get("/jobs/{job_id}/text/{name}")
    def job_text(job_id: str, name: str, offset_line: int = Query(0, ge=0), max_lines: int = Query(500, ge=1, le=20000)):
        return read_text_file(mgr, job_id, name, offset_line, max_lines)

    app.mount("/mcp", mcp_app)
    return app


def _add_op_route(app: FastAPI, mgr: JobManager, op: ops_mod.Op, settings: Settings) -> None:
    model = op.params

    def endpoint(params: Annotated[model, Body()],  # type: ignore[valid-type]
                 wait: int = Query(settings.default_wait, ge=0, le=600,
                                   description="Seconds to wait for the result before answering 202."),
                 respond: str = Query("json", pattern="^(json|file)$",
                                      description="file: when the job succeeds with one output file, send the file itself.")):
        job = mgr.submit(op.name, params)
        job = mgr.wait(job.id, wait)
        if respond == "file" and job.state == "succeeded" and len(job.files) == 1:
            name = job.files[0]["name"]
            return FileResponse(mgr.job_file(job.id, name), filename=name, headers={"X-Job-Id": job.id})
        return job_response(mgr, job)

    # The annotations are strings (from __future__) naming a local, so give FastAPI the real types.
    sig = inspect.signature(endpoint, eval_str=False)
    endpoint.__signature__ = sig.replace(parameters=[  # type: ignore[attr-defined]
        sig.parameters["params"].replace(annotation=Annotated[model, Body()]),
        sig.parameters["wait"].replace(annotation=int),
        sig.parameters["respond"].replace(annotation=str)])
    endpoint.__name__ = f"op_{op.name}"
    app.add_api_route(f"/ops/{op.name}", endpoint, methods=["POST"], summary=op.summary, tags=[op.mode])


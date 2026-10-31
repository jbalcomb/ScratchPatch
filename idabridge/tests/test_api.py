import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from idabridge.api import create_app

AUTH = {"Authorization": "Bearer secret"}


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_health_is_open_everything_else_needs_the_token(client):
    assert client.get("/health").status_code == 200
    assert client.get("/ops").status_code == 401
    assert client.get("/ops", headers={"Authorization": "Bearer nope"}).status_code == 401
    ops = client.get("/ops", headers=AUTH).json()
    assert {"list_functions", "export", "rename"} <= {o["name"] for o in ops}


def test_operation_status_codes(client):
    r = client.post("/ops/list_functions", json={"file": "game/MAGIC.EXE"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["state"] == "succeeded"
    assert client.post("/ops/info", json={"file": "game/missing.exe"}, headers=AUTH).status_code == 404
    assert client.post("/ops/info", json={"file": "../x"}, headers=AUTH).status_code == 422
    r = client.post("/ops/rename", json={"file": "game/MAGIC.EXE", "address": "0x401000", "new_name": "fail"}, headers=AUTH)
    assert r.status_code == 502 and r.json()["error"]["kind"] == "ida_error"
    r = client.post("/ops/info?wait=60", json={"file": "game/hang.exe", "timeout": 5}, headers=AUTH)
    assert r.status_code == 504 and r.json()["error"]["diagnostics"]["verdict"]


def test_running_job_answers_202_then_poll(client):
    r = client.post("/ops/info?wait=0", json={"file": "game/slow.exe"}, headers=AUTH)
    assert r.status_code == 202 and r.headers["location"].startswith("/jobs/")
    r = client.get(r.headers["location"] + "?wait=30", headers=AUTH)
    assert r.status_code == 200


def test_export_respond_with_the_file(client):
    r = client.post("/ops/export?respond=file", headers=AUTH,
                    json={"file": "game/MAGIC.EXE", "kind": "asm", "output_name": "magic.asm"})
    assert r.status_code == 200 and r.text.startswith("; fake asm")
    job_id = r.headers["x-job-id"]
    page = client.get(f"/jobs/{job_id}/text/magic.asm?offset_line=1&max_lines=5", headers=AUTH).json()
    assert page["lines"] == ["line 0", "line 1", "line 2", "line 3", "line 4"] and page["more"]
    assert client.get(f"/jobs/{job_id}/files/..%2Fjob.json", headers=AUTH).status_code == 404


def test_inputs(client):
    rows = client.get("/inputs?path=game", headers=AUTH).json()
    assert "game/MAGIC.EXE" in {r["path"] for r in rows}
    assert client.get("/inputs?path=..", headers=AUTH).status_code == 404


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_tools_end_to_end(settings):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)

    async def go():
        http = httpx.AsyncClient(headers=AUTH, timeout=60)
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp/", http_client=http) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert {"list_functions", "export", "job_status", "read_job_file"} <= names
                res = await s.call_tool("list_functions", {"file": "game/MAGIC.EXE", "filter": "Draw"})
                assert '"succeeded"' in res.content[0].text and "Draw" in res.content[0].text
                res = await s.call_tool("info", {"file": "game/slow.exe", "wait_seconds": 0})
                import json
                job = json.loads(res.content[0].text)
                assert job["state"] in ("queued", "running")
                res = await s.call_tool("job_status", {"job_id": job["id"], "wait_seconds": 30})
                assert json.loads(res.content[0].text)["state"] == "succeeded"
                res = await s.call_tool("export", {"file": "game/MAGIC.EXE", "kind": "asm", "output_name": "m.asm"})
                job = json.loads(res.content[0].text)
                res = await s.call_tool("read_job_file", {"job_id": job["id"], "name": "m.asm", "max_lines": 2})
                assert json.loads(res.content[0].text)["lines"] == ["; fake asm", "line 0"]
                res = await s.call_tool("run_idc", {"file": "game/MAGIC.EXE", "code": "return 0;"})
                assert "disabled" in res.content[0].text
        await http.aclose()

    try:
        asyncio.run(go())
    finally:
        server.should_exit = True
        t.join(10)


def test_mcp_needs_the_token(settings):
    with TestClient(create_app(settings)) as c:
        assert c.post("/mcp/", json={}).status_code == 401

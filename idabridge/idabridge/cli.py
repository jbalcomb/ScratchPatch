"""idactl: command-line client for the bridge. Standard library only, so it runs anywhere (Termux too).

    export IDABRIDGE_URL=https://ida-box.your-tailnet.ts.net   IDABRIDGE_TOKEN=...
    idactl inputs
    idactl run list_functions file=game/MAGIC.EXE filter=Draw limit=50
    idactl run export file=game/MAGIC.EXE kind=asm output_name=magic.asm -o ./out
    idactl job 20261031-120000-abc123 --wait 60
    idactl selftest game/MAGIC.EXE

Exit codes: 0 succeeded, 1 failed, 2 timed out, 3 refused or usage error, 4 cannot reach the server.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FINAL = ("succeeded", "failed", "timed_out", "cancelled", "interrupted")


class Client:
    def __init__(self, url: str, token: str | None):
        self.url = url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=700) as resp:
                payload = resp.read()
                return resp.status, (payload if raw else json.loads(payload or b"null"))
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, {"error": payload.decode(errors="replace")}
        except urllib.error.URLError as e:
            print(f"cannot reach {self.url}: {e.reason}", file=sys.stderr)
            sys.exit(4)


def coerce(params: list[str], schema: dict) -> dict:
    props = schema.get("properties", {})
    out: dict = {}
    for item in params:
        if "=" not in item:
            raise SystemExit(f"parameters are key=value, got {item!r}")
        key, value = item.split("=", 1)
        prop = props.get(key, {})
        types = {prop.get("type")} | {a.get("type") for a in prop.get("anyOf", [])}
        if "integer" in types and value.lstrip("-").isdigit():
            out[key] = int(value)
        elif "boolean" in types:
            out[key] = value.lower() in ("1", "true", "yes", "on")
        elif "array" in types:
            out[key] = [v for v in value.split(",") if v]
        else:
            out[key] = value
    return out


def follow(client: Client, status: int, job: dict, quiet: bool) -> dict:
    while job.get("state") not in FINAL and "id" in job:
        live = job.get("live") or {}
        proc = live.get("process") or {}
        if not quiet:
            print(f"... {job['state']} {live.get('step', '')} {live.get('elapsed_seconds', '')}s "
                  f"cpu={proc.get('cpu_seconds', '?')}s", file=sys.stderr)
        status, job = client.request("GET", f"/jobs/{job['id']}?wait=50")
    return job


def print_job(job: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(job, indent=1))
        return
    if "state" not in job:
        print(json.dumps(job, indent=1))
        return
    print(f"# job {job.get('id')} {job.get('op')} {job.get('file')}: {job['state']}", file=sys.stderr)
    err = job.get("error")
    if err:
        print(f"# error ({err.get('kind')}): {err.get('message')}", file=sys.stderr)
        diag = err.get("diagnostics")
        if diag:
            print(f"# OS: {diag.get('verdict')}", file=sys.stderr)
            for w in diag.get("windows", []):
                print(f"#   window: {w.get('title')!r} visible={w.get('visible')}", file=sys.stderr)
        if job.get("ida_log_tail"):
            print("# ida log tail:\n" + job["ida_log_tail"], file=sys.stderr)
    result = job.get("result") or {}
    for k, v in (result.get("info") or {}).items():
        print(f"{k}\t{v}")
    rows = result.get("rows") or []
    if rows and isinstance(rows[0], dict):
        cols = list(rows[0].keys())
        print("\t".join(cols))
        for r in rows:
            print("\t".join(str(r.get(c, "")).replace("\t", " ").replace("\n", "\\n") for c in cols))
    else:
        for r in rows:
            print(r)
    if result.get("total") is not None:
        print(f"# total {result['total']}", file=sys.stderr)
    for f in job.get("files") or []:
        print(f"# file {f['name']} ({f['size']} bytes)", file=sys.stderr)


def download(client: Client, job: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in job.get("files") or []:
        status, data = client.request("GET", f"/jobs/{job['id']}/files/{urllib.parse.quote(f['name'])}", raw=True)
        if status == 200:
            (out_dir / f["name"]).write_bytes(data)
            print(f"# saved {out_dir / f['name']}", file=sys.stderr)


def exit_code(job: dict) -> int:
    return {"succeeded": 0, "timed_out": 2}.get(job.get("state", ""), 3 if "state" not in job else 1)


SELFTEST = [
    ("info", []), ("list_segments", []), ("list_functions", ["limit=5"]), ("list_entry_points", []),
    ("list_names", ["limit=5"]), ("list_strings", ["limit=5"]), ("disassemble", ["address={start}", "count=5"]),
    ("function_info", ["address={start}"]), ("xrefs_to", ["address={start}"]), ("xrefs_from", ["address={start}"]),
    ("read_bytes", ["address={start}", "size=32"]), ("find_bytes", ["pattern=4D 5A", "limit=1"]),
    ("export", ["kind=map", "output_name=selftest.map"]),
]


def selftest(client: Client, file: str, schemas: dict) -> int:
    """Run every read-only operation once and say which work with this IDA install."""
    start = None
    failures = 0
    for name, args in SELFTEST:
        args = [a.replace("{start}", start or "0") for a in args]
        body = coerce([f"file={file}", *args], schemas[name])
        status, job = client.request("POST", f"/ops/{name}?wait=60", body)
        job = follow(client, status, job, quiet=True)
        state = job.get("state", "refused")
        err = (job.get("error") or {}).get("message", "") if state != "succeeded" else ""
        if name == "info" and state == "succeeded":
            start = job["result"]["info"].get("start_ea")
        failures += state != "succeeded"
        print(f"{'ok  ' if state == 'succeeded' else 'FAIL'} {name:18} {state:10} {err[:120]}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="idactl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("IDABRIDGE_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--token", default=os.environ.get("IDABRIDGE_TOKEN"))
    ap.add_argument("--json", action="store_true", help="print the raw JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ops", help="list operations and their parameters")
    p = sub.add_parser("inputs", help="list files in the server's input folder")
    p.add_argument("path", nargs="?", default="")
    p.add_argument("--pattern", default="*")
    p = sub.add_parser("run", help="run an operation: idactl run OP key=value ...")
    p.add_argument("op")
    p.add_argument("params", nargs="*")
    p.add_argument("--wait", type=int, default=50, help="seconds per poll")
    p.add_argument("--no-follow", action="store_true", help="return as soon as the job is queued/running")
    p.add_argument("-o", "--out", default=".", help="folder for output files (default: here)")
    for name in ("job", "cancel", "diagnose"):
        p = sub.add_parser(name)
        p.add_argument("id")
        if name == "job":
            p.add_argument("--wait", type=int, default=0)
            p.add_argument("--follow", action="store_true")
    p = sub.add_parser("get", help="download a job's file")
    p.add_argument("id")
    p.add_argument("name")
    p.add_argument("-o", "--out", default=None)
    sub.add_parser("jobs")
    p = sub.add_parser("selftest", help="try every read-only operation on a file")
    p.add_argument("file")
    a = ap.parse_args(argv)
    c = Client(a.url, a.token)

    if a.cmd == "ops":
        _, ops = c.request("GET", "/ops")
        if a.json:
            print(json.dumps(ops, indent=1))
        else:
            for o in ops:
                req = set(o["params"].get("required", []))
                params = " ".join(k if k in req else f"[{k}]" for k in o["params"].get("properties", {}))
                print(f"{o['name']:22} {o['mode']:6} {params}\n{'':29}{o['summary']}")
    elif a.cmd == "inputs":
        _, rows = c.request("GET", "/inputs?" + urllib.parse.urlencode({"path": a.path, "pattern": a.pattern}))
        print(json.dumps(rows, indent=1) if a.json or not isinstance(rows, list) else
              "\n".join(f"{r['type']:6} {r['size'] or '':>10} {r['path']}" for r in rows))
    elif a.cmd == "run":
        _, ops = c.request("GET", "/ops")
        schemas = {o["name"]: o["params"] for o in ops}
        if a.op not in schemas:
            raise SystemExit(f"unknown operation {a.op}; see `idactl ops`")
        status, job = c.request("POST", f"/ops/{a.op}?wait={a.wait}", coerce(a.params, schemas[a.op]))
        if not a.no_follow:
            job = follow(c, status, job, quiet=a.json)
        print_job(job, a.json)
        if job.get("state") == "succeeded":
            download(c, job, Path(a.out))
        sys.exit(exit_code(job))
    elif a.cmd == "job":
        status, job = c.request("GET", f"/jobs/{a.id}?wait={a.wait}")
        if a.follow:
            job = follow(c, status, job, quiet=a.json)
        print_job(job, a.json)
        sys.exit(exit_code(job) if job.get("state") in FINAL else 0)
    elif a.cmd == "cancel":
        _, job = c.request("POST", f"/jobs/{a.id}/cancel")
        print_job(job, a.json)
    elif a.cmd == "diagnose":
        _, d = c.request("GET", f"/jobs/{a.id}/diagnose")
        print(json.dumps(d, indent=1))
    elif a.cmd == "get":
        status, data = c.request("GET", f"/jobs/{a.id}/files/{urllib.parse.quote(a.name)}", raw=True)
        if status != 200:
            print(data, file=sys.stderr)
            sys.exit(1)
        Path(a.out or a.name).write_bytes(data)
    elif a.cmd == "jobs":
        _, jobs = c.request("GET", "/jobs")
        print(json.dumps(jobs, indent=1) if a.json else
              "\n".join(f"{j['id']}  {j['state']:11} {j['op']:18} {j['file']}" for j in jobs))
    elif a.cmd == "selftest":
        _, ops = c.request("GET", "/ops")
        sys.exit(selftest(c, a.file, {o["name"]: o["params"] for o in ops}))


if __name__ == "__main__":
    main()

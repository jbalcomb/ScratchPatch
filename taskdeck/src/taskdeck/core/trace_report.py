"""Summarize a task trace (diagnostics/*.jsonl, see taskdeck.core.trace).

Says what the task was, how it ended, where the time went (unpacking, uploading, waiting), upload
speed, the slowest requests, retries and errors, pauses, stalls (heartbeats with no progress), and
app / network / background-service changes, so a slow or stuck run can be explained afterwards.
The app uploads this summary next to each trace; ``tools/trace_report.py`` prints it for a file.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

STALL_BEATS = 2  # this many heartbeats in a row without progress count as a stall


def load(path: Path) -> list[dict[str, Any]]:
    events = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"ev": "corrupt", "line": n, "s": events[-1]["s"] if events else 0})
    return events


def mb(n: float) -> str:
    return f"{n / 1e6:,.1f} MB"


def rate(nbytes: float, secs: float) -> str:
    return f"{nbytes / secs / 1e6:.2f} MB/s" if secs > 0 else "-"


def clock(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def report(events: list[dict[str, Any]], per_minute: bool = False) -> str:
    out: list[str] = []
    say = out.append
    if not events:
        return "Empty trace."
    start = next((e for e in events if e["ev"] == "start"), events[0])
    end = next((e for e in reversed(events) if e["ev"] == "end"), None)
    last = events[-1]
    say(f"Task: {start.get('title', '?')}  [job {start.get('job', '?')}, app {start.get('app', '?')}, "
        f"Python {start.get('python', '?')}, {'Android' if start.get('android') else start.get('platform', '?')}]")
    if end:
        say(f"Ended: {end['status']} after {clock(end['s'])}: {end.get('message', '')}")
        if end.get("traceback"):
            say("Traceback:\n" + end["traceback"].rstrip())
    else:
        say(f"No end recorded: the trace stops at {clock(last['s'])} (app killed, or still running at upload).")
    arch = next((e for e in events if e["ev"] == "archive"), None)
    if arch:
        say(f"Archive: {arch['name']} ({arch.get('format')}, {mb(arch.get('size', 0))}) -> "
            f"{arch.get('files')} files, {mb(arch.get('bytes', 0))} unpacked")
    unpacked = next((e for e in events if e["ev"] == "unpacked"), None)
    if unpacked:
        say(f"Unpacking took {clock(unpacked['secs'])} ({rate(arch.get('bytes', 0) if arch else 0, unpacked['secs'])})")

    # ---- requests
    http = [e for e in events if e["ev"] == "http"]
    if http:
        uploads = [e for e in http if e.get("method") == "PUT" and e.get("sent")]
        ok_up = [e for e in uploads if e.get("status") in (200, 201, 308)]
        sent, busy = sum(e["sent"] for e in ok_up), sum(e["secs"] for e in ok_up)
        say("")
        say(f"Requests: {len(http)}  (errors: {sum(1 for e in http if 'error' in e)}, "
            f"statuses: {dict(Counter(e.get('status', 'ERR') for e in http))})")
        if ok_up:
            secs = sorted(e["secs"] for e in ok_up)
            say(f"Upload pieces: {len(ok_up)}, {mb(sent)} in {clock(busy)} of request time = {rate(sent, busy)} "
                f"while sending; piece time median {secs[len(secs) // 2]:.1f} s, max {secs[-1]:.1f} s")
        timed = [e for e in http if "wait" in e]
        if timed:
            def avg(key: str) -> float:
                vals = [e.get(key, 0) for e in timed]
                return sum(vals) / len(vals)
            new = [e["connect"] for e in timed if "connect" in e]
            say(f"Where a request's time goes (average of {len(timed)}): "
                f"connect {sum(new) / len(new) if new else 0:.2f} s ({len(new)} new connections, "
                f"{len(timed) - len(new)} reused), send {avg('send'):.2f} s, wait for Google {avg('wait'):.2f} s, "
                f"read {avg('read'):.2f} s; progress updates while sending {avg('callbacks'):.2f} s")
            kinds: dict[str, list[float]] = {}
            for e in timed:
                kind = "create file" if e.get("method") == "POST" and "/upload/" in e.get("path", "") else \
                    "upload piece" if e.get("method") == "PUT" else \
                    "list folder" if e.get("method") == "GET" else f"{e.get('method')} {e.get('path', '')[-20:]}"
                kinds.setdefault(kind, []).append(e["wait"])
            say("Google's reply time by request: " + ", ".join(
                f"{k} {sorted(v)[len(v) // 2]:.2f} s median ({len(v)})" for k, v in sorted(kinds.items())))
            between = [b["s"] - (a["s"]) for a, b in zip(http, http[1:])]
            gaps = [g - b["secs"] for g, b in zip(between, http[1:])]
            if gaps:
                say(f"Time between requests (TaskDeck's own work): average {sum(gaps) / len(gaps):.2f} s, "
                    f"max {max(gaps):.1f} s")
        slow = sorted(http, key=lambda e: -e["secs"])[:5]
        say("Slowest requests:")
        for e in slow:
            parts = " ".join(f"{k} {e[k]:.1f}" for k in ("connect", "send", "wait", "read", "callbacks") if k in e)
            say(f"  {clock(e['s'])}  {e['secs']:7.1f} s  {e.get('method')} {e.get('host')}{e.get('path', '')[:50]}"
                f"  sent {mb(e.get('sent', 0))}  -> {e.get('status', e.get('error', '?'))}  {parts}")
        errors = [e for e in http if "error" in e or (e.get("status") or 0) >= 400]
        if errors:
            say("Failed requests:")
            for e in errors[:20]:
                say(f"  {clock(e['s'])}  {e.get('method')} {e.get('host')}  {e.get('status', '')} {e.get('error', '')}")
            if len(errors) > 20:
                say(f"  … {len(errors) - 20} more")

    # ---- files
    skipped = [e for e in events if e["ev"] == "file" and e.get("skipped")]
    files = [e for e in events if e["ev"] == "file" and not e.get("skipped") and "secs" in e]
    if skipped:
        say("")
        say(f"Files skipped (already in Drive): {len(skipped)}, {mb(sum(e.get('bytes', 0) for e in skipped))}")
    if files:
        total = sum(e["bytes"] for e in files)
        secs = sum(e["secs"] for e in files)
        small = [e for e in files if e["bytes"] < 1_000_000]
        say("")
        say(f"Files uploaded: {len(files)}, {mb(total)} in {clock(secs)} = {rate(total, secs)} overall")
        if small:
            per = sum(e["secs"] for e in small) / len(small)
            say(f"  {len(small)} files under 1 MB: {per:.1f} s each on average (per-file overhead)")
        for e in sorted(files, key=lambda e: -e["secs"])[:5]:
            say(f"  {e['secs']:8.1f} s  {mb(e['bytes']):>10}  {rate(e['bytes'], e['secs']):>11}  {e['path']}")

    # ---- speed over time
    beats = [e for e in events if e["ev"] == "heartbeat"]
    if beats:
        say("")
        say("Progress (heartbeats):")
        stalls, run = [], []
        prev_s = start["s"]
        for e in beats:
            moved = e.get("moved", 0)
            if moved <= 0:
                run.append(e)
            else:
                if len(run) >= STALL_BEATS:
                    stalls.append(run)
                run = []
            if per_minute or moved <= 0 or e is beats[-1]:
                say(f"  {clock(e['s'])}  {e.get('phase', ''):<28} {rate(moved, e['s'] - prev_s):>11}  "
                    f"{mb(e.get('done', 0))} / {mb(e.get('total', 0))}  {e.get('note', '')}")
            prev_s = e["s"]
        if len(run) >= STALL_BEATS:
            stalls.append(run)
        for st in stalls:
            say(f"STALL: no progress from {clock(st[0]['s'])} to {clock(st[-1]['s'])} "
                f"({clock(st[-1]['s'] - st[0]['s'] + 30)}), phase {st[0].get('phase', '?')}, note {st[0].get('note', '') or '-'}")

    # ---- what happened around it
    context = [e for e in events if e["ev"] in ("phase", "note", "app", "network", "service", "cancel_requested",
                                                "corrupt")]
    if context:
        say("")
        say("Timeline:")
        if len(context) > 150:  # a long run: the start and the most recent part
            skipped = len(context) - 150
            context = context[:50] + [{"ev": f"… {skipped} more", "s": context[50]["s"]}] + context[-100:]
        for e in context:
            detail = {k: v for k, v in e.items() if k not in ("t", "s", "ev")}
            say(f"  {clock(e['s'])}  {e['ev']:<16} {json.dumps(detail, ensure_ascii=False)}")
    return "\n".join(out)

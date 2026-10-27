"""Summarize a TaskDeck task trace (diagnostics/*.jsonl, see taskdeck.core.trace):
``python tools/trace_report.py TRACE.jsonl [--minute]``.

Prints what the task was, how it ended, where the time went (unpacking, uploading, waiting), upload
speed over time, the slowest requests, retries and errors, pauses, stalls (heartbeats with no
progress), and app / network / background-service changes, so a slow or stuck run can be
explained afterwards.
"""

from __future__ import annotations

import argparse
import json
import sys
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
        slow = sorted(http, key=lambda e: -e["secs"])[:5]
        say("Slowest requests:")
        for e in slow:
            say(f"  {clock(e['s'])}  {e['secs']:7.1f} s  {e.get('method')} {e.get('host')}{e.get('path', '')[:50]}"
                f"  sent {mb(e.get('sent', 0))}  -> {e.get('status', e.get('error', '?'))}")
        errors = [e for e in http if "error" in e or (e.get("status") or 0) >= 400]
        if errors:
            say("Failed requests:")
            for e in errors[:20]:
                say(f"  {clock(e['s'])}  {e.get('method')} {e.get('host')}  {e.get('status', '')} {e.get('error', '')}")
            if len(errors) > 20:
                say(f"  … {len(errors) - 20} more")

    # ---- files
    files = [e for e in events if e["ev"] == "file"]
    if files:
        total = sum(e["bytes"] for e in files)
        secs = sum(e["secs"] for e in files)
        say("")
        say(f"Files uploaded: {len(files)}, {mb(total)} in {clock(secs)} = {rate(total, secs)} overall")
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
        for e in context:
            detail = {k: v for k, v in e.items() if k not in ("t", "s", "ev")}
            say(f"  {clock(e['s'])}  {e['ev']:<16} {json.dumps(detail, ensure_ascii=False)}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trace", type=Path, nargs="+")
    ap.add_argument("--minute", action="store_true", help="print every heartbeat, not only stalls")
    args = ap.parse_args(argv)
    for i, path in enumerate(args.trace):
        if i:
            print("\n" + "=" * 80 + "\n")
        print(f"{path.name}\n{report(load(path), args.minute)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

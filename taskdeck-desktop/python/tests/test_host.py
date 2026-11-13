"""Tests for the job host. Run from taskdeck-desktop/:  python -m unittest discover -s python/tests -v"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = HERE.parent / "taskdeck_host.py"
SRC = HERE.parents[2] / "taskdeck" / "src"  # the phone app's source in this repository
sys.path[:0] = [str(HERE.parent), str(SRC)]

import taskdeck_host  # noqa: E402
from taskdeck.core import (  # noqa: E402
    Field, JobPlugin, NeedsConfirmation, Pipeline, PluginRegistry, Problem, Result, Stage, step,
)


class Sent:
    """Captures what the host sends, instead of stdout."""

    def __init__(self) -> None:
        self.messages: queue.Queue = queue.Queue()

    def __call__(self, msg):
        self.messages.put(json.loads(json.dumps(msg, default=str)))  # must be JSON-serializable

    def wait(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                msg = self.messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if pred(msg):
                return msg
        raise AssertionError("no matching message")


@step(Stage.EXECUTE)
def slow(ctx):
    while True:
        ctx.check_cancelled()
        time.sleep(0.01)


@step(Stage.EXECUTE)
def preview(ctx):
    ctx.result = Result(title="Preview", rows=[("Rows", "2")], payload={"rows": 2})


class Slow(JobPlugin):
    id = "slow"
    title = "Slow"
    fields = (Field.text("x", "X"),)

    def build_pipeline(self):
        return Pipeline([slow])


class TwoPhase(JobPlugin):
    id = "two_phase"
    title = "Two phase"
    fields = (Field.text("name", "Name", required=True),)
    settings = (Field.text("sheet", "Sheet", required=True),)
    commit_label = "Write"

    def build_pipeline(self):
        return Pipeline([preview])

    def commit(self, payload, settings):
        if not payload.get("overwrite"):
            raise NeedsConfirmation("A row exists.", "Overwrite", {**payload, "overwrite": True})
        return f"Wrote {payload['rows']} rows"


class HostTest(unittest.TestCase):
    def setUp(self):
        self.sent = Sent()
        self._send = taskdeck_host.send
        taskdeck_host.send = self.sent
        reg = PluginRegistry()
        reg.register(Slow())
        reg.register(TwoPhase())
        self.host = taskdeck_host.Host(reg)

    def tearDown(self):
        taskdeck_host.send = self._send

    def test_job_description(self):
        job = taskdeck_host.job_json(TwoPhase())
        self.assertEqual(job["commit_label"], "Write")
        self.assertEqual([f["name"] for f in job["fields"]], ["name"])
        self.assertTrue(job["fields"][0]["required"])
        self.assertEqual(job["settings"][0]["kind"], "text")

    def test_check_reports_missing_settings(self):
        self.host.handle({"op": "check", "req": 1, "job": "two_phase", "settings": {}})
        reply = self.sent.wait(lambda m: m.get("reply") == 1)
        self.assertEqual(len(reply["problems"]), 1)
        self.assertTrue(reply["problems"][0]["blocking"])
        self.host.handle({"op": "check", "req": 2, "job": "two_phase", "settings": {"sheet": "S"}})
        self.assertEqual(self.sent.wait(lambda m: m.get("reply") == 2)["problems"], [])

    def test_missing_required_input(self):
        self.host.handle({"op": "run", "run": 1, "job": "two_phase", "inputs": {}, "settings": {}})
        done = self.sent.wait(lambda m: m.get("event") == "done")
        self.assertFalse(done["ok"])
        self.assertIn("Name is required", done["error"])

    def test_cancel(self):
        self.host.handle({"op": "run", "run": 5, "job": "slow", "inputs": {}, "settings": {}})
        self.sent.wait(lambda m: m.get("event") == "progress" and m["run"] == 5)
        self.host.handle({"op": "cancel", "run": 5})
        done = self.sent.wait(lambda m: m.get("event") == "done")
        self.assertTrue(done["cancelled"])
        self.assertFalse(done["ok"])

    def test_commit_with_confirmation(self):
        self.host.handle({"op": "run", "run": 2, "job": "two_phase", "inputs": {"name": "a"}, "settings": {}})
        done = self.sent.wait(lambda m: m.get("event") == "done")
        self.assertTrue(done["ok"])
        self.assertTrue(done["result"]["has_payload"])
        self.assertEqual(done["result"]["rows"], [["Rows", "2"]])
        self.host.handle({"op": "commit", "run": 2, "settings": {}})
        first = self.sent.wait(lambda m: m.get("event") == "committed")
        self.assertFalse(first["ok"])
        self.assertEqual(first["confirm_label"], "Overwrite")
        self.host.handle({"op": "commit", "run": 2, "settings": {}, "confirm": True})
        second = self.sent.wait(lambda m: m.get("event") == "committed")
        self.assertTrue(second["ok"])
        self.assertEqual(second["message"], "Wrote 2 rows")
        self.host.handle({"op": "commit", "run": 2, "settings": {}})  # payload is gone once committed
        self.assertFalse(self.sent.wait(lambda m: m.get("event") == "committed")["ok"])

    def test_unknown_job(self):
        self.host.handle({"op": "run", "run": 3, "job": "nope", "inputs": {}, "settings": {}})
        self.assertIn("Unknown job", self.sent.wait(lambda m: m.get("event") == "done")["error"])


class ProcessTest(unittest.TestCase):
    """The host as the app runs it: a child process, the phone app's real jobs, JSON lines."""

    def test_ready_and_run(self):
        proc = subprocess.Popen([sys.executable, "-u", str(HOST), "--src", str(SRC)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()
        try:
            ready = json.loads(lines.get(timeout=30))
            self.assertEqual(ready["event"], "ready")
            self.assertEqual(ready["errors"], {})
            ids = {j["id"] for j in ready["jobs"]}
            self.assertTrue({"exe_parse", "extract_archive", "text_stats"} <= ids)
            proc.stdin.write(json.dumps({"op": "run", "run": 1, "job": "case_convert",
                                         "inputs": {"text": "héllo wörld", "style": "UPPER CASE"}}) + "\n")
            proc.stdin.flush()
            while True:
                msg = json.loads(lines.get(timeout=30))
                if msg.get("event") == "done":
                    break
            self.assertTrue(msg["ok"], msg["error"])
            self.assertIn("HÉLLO WÖRLD", msg["result"]["text"])
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()

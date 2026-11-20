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
    Field, JobPlugin, NeedsConfirmation, Pipeline, PluginRegistry, Problem, Result, SettingsAction, SettingsResult,
    Stage, step,
)


class Sent:
    """Captures what the host sends, instead of stdout."""

    def __init__(self) -> None:
        self.messages: queue.Queue = queue.Queue()
        self.skipped: list = []  # passed over by an earlier wait: a later one may want them (they can come in any order)

    def __call__(self, msg):
        self.messages.put(json.loads(json.dumps(msg, default=str)))  # must be JSON-serializable

    def wait(self, pred, timeout=5.0):
        for i, msg in enumerate(self.skipped):
            if pred(msg):
                return self.skipped.pop(i)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                msg = self.messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if pred(msg):
                return msg
            self.skipped.append(msg)
        raise AssertionError("no matching message")


@step(Stage.EXECUTE)
def slow(ctx):
    ctx.status("waiting on the service", 1, 4)
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
    settings = (Field.text("sheet", "Sheet", required=True), Field.text("url", "Server"),
                Field.text("key", "API key", secret=True, importable=True))
    settings_actions = (SettingsAction("Find server", lambda s: SettingsResult(f"Found for {s['sheet']}",
                                                                               {"url": "https://found"})),
                        SettingsAction("Broken", lambda s: 1 / 0))
    commit_label = "Write"

    def clean_settings(self, values):
        url = str(values.get("url") or "")
        return {**values, "url": url if not url or url.startswith("https://") else "https://" + url}

    def check_ready_online(self, settings):
        if settings.get("url") == "https://old":
            return [Problem("The server moved.", blocking=False, settings={"url": "https://new"})]
        return [Problem("Server answers.", ok=True)]

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
        self.assertEqual(job["settings_id"], "two_phase")
        self.assertEqual(job["settings_actions"], ["Find server", "Broken"])
        self.assertTrue(job["settings"][2]["secret"] and job["settings"][2]["importable"])

    def test_check_online_offers_fixes(self):
        self.host.handle({"op": "check", "req": 1, "job": "two_phase", "settings": {"sheet": "S", "url": "https://old"},
                          "online": True})
        reply = self.sent.wait(lambda m: m.get("reply") == 1)
        self.assertEqual(reply["fixes"], {"url": "https://new"})
        self.host.handle({"op": "check", "req": 2, "job": "two_phase", "settings": {"url": "https://old"},
                          "online": True})  # a blocking offline problem: the online checks wait
        self.assertEqual(self.sent.wait(lambda m: m.get("reply") == 2)["fixes"], {})

    def test_test_settings(self):
        self.host.handle({"op": "test_settings", "req": 3, "job": "two_phase", "values": {"sheet": "S", "url": "srv"}})
        reply = self.sent.wait(lambda m: m.get("reply") == 3)
        self.assertEqual(reply["settings"]["url"], "https://srv")  # tidied by clean_settings
        self.assertEqual(reply["passed"], ["Server answers."])
        self.assertEqual(reply["problems"], [])
        self.host.handle({"op": "test_settings", "req": 4, "job": "two_phase", "values": {}})
        self.assertTrue(self.sent.wait(lambda m: m.get("reply") == 4)["problems"][0]["blocking"])

    def test_settings_actions(self):
        self.host.handle({"op": "settings_action", "req": 5, "job": "two_phase", "index": 0, "values": {"sheet": "S"}})
        reply = self.sent.wait(lambda m: m.get("reply") == 5)
        self.assertEqual(reply["text"], "Found for S")
        self.assertEqual(reply["values"], {"url": "https://found"})
        self.host.handle({"op": "settings_action", "req": 6, "job": "two_phase", "index": 1, "values": {}})
        self.assertIn("ZeroDivisionError", self.sent.wait(lambda m: m.get("reply") == 6)["text"])

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

    def test_status(self):
        self.host.handle({"op": "run", "run": 6, "job": "slow", "inputs": {}, "settings": {}})
        status = self.sent.wait(lambda m: m.get("event") == "status")
        self.assertEqual(status, {"event": "status", "run": 6, "text": "waiting on the service", "fraction": 0.25})
        self.host.handle({"op": "cancel", "run": 6})
        self.sent.wait(lambda m: m.get("event") == "done")

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
        self.assertEqual(done["result"]["summary"], "Preview")
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

"""The job host's background tasks and Google account. Run from taskdeck-desktop/:
python -m unittest discover -s python/tests -v"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from test_host import Sent  # noqa: F401  (also sets up sys.path)

import taskdeck_host  # noqa: E402
from taskdeck.core import Cancelled, JobPlugin, Pipeline, PluginRegistry, Stage, StepError, step  # noqa: E402
from taskdeck.shared import google_oauth  # noqa: E402

release = threading.Event()
attempts: list[int] = []


def upload(task):
    """A task with two phases that fails the first time (like an upload Google refused) and is cancellable."""
    attempts.append(1)
    task.set_phases(("Packing", 10), ("Uploading", 2, "files"))
    task.advance(0, 10)
    if len(attempts) == 1:
        err = RuntimeError("Google no longer accepts this sign-in.")
        err.fix = "account:google"
        raise err
    while not release.is_set():
        if task.cancel_requested:
            raise Cancelled("Stopped.")
        release.wait(0.01)
    task.advance(1, 2)
    return "Uploaded 2 files"


@step(Stage.EXECUTE)
def start_upload(ctx):
    if "google_drive" not in ctx.services:
        raise StepError("Connect Google Drive first.")
    ctx["task"] = ctx.services["start_task"]("Uploading things", upload, retry_label="Retry upload")


class Uploader(JobPlugin):
    id = "uploader"
    title = "Uploader"
    accounts = (("account:google", ""),)

    def build_pipeline(self):
        return Pipeline([start_upload])


class TaskTest(unittest.TestCase):
    def setUp(self):
        attempts.clear()
        release.clear()
        self.sent = Sent()
        self._send = taskdeck_host.send
        taskdeck_host.send = self.sent
        reg = PluginRegistry()
        reg.register(Uploader())
        self.host = taskdeck_host.Host(reg)

    def tearDown(self):
        release.set()
        taskdeck_host.send = self._send

    def task_event(self, status):
        return self.sent.wait(lambda m: m.get("event") == "task" and m["status"] == status)

    def test_job_lists_its_accounts(self):
        self.assertEqual(taskdeck_host.job_json(Uploader())["accounts"], [["account:google", ""]])

    def test_needs_the_account(self):
        self.host.handle({"op": "run", "run": 1, "job": "uploader", "inputs": {}, "settings": {}})
        done = self.sent.wait(lambda m: m.get("event") == "done")
        self.assertIn("Connect Google Drive", done["error"])

    def test_task_fails_retries_and_cancels(self):
        self.host.handle({"op": "accounts", "google": {"client_id": "id", "client_secret": "s", "refresh_token": "r"}})
        self.host.handle({"op": "run", "run": 1, "job": "uploader", "inputs": {"x": "y"}, "settings": {}})
        self.assertTrue(self.sent.wait(lambda m: m.get("event") == "done")["ok"])
        failed = self.task_event("failed")
        self.assertEqual((failed["run"], failed["job"], failed["retry_label"]), (1, "uploader", "Retry upload"))
        self.assertEqual(failed["fix"], "account:google")
        self.assertEqual([p["label"] for p in failed["phases"]], ["Packing", "Uploading"])
        self.assertEqual(failed["inputs"], {"x": "y"})

        self.host.handle({"op": "task_retry", "task": failed["id"]})
        running = self.sent.wait(lambda m: m.get("event") == "task" and m["id"] != failed["id"])
        self.assertEqual((running["run"], running["retry_label"]), (1, "Retry upload"))
        self.host.handle({"op": "task_cancel", "task": running["id"]})
        cancelled = self.task_event("cancelled")
        self.assertEqual(cancelled["message"], "Stopped.")

        self.host.handle({"op": "task_retry", "task": cancelled["id"]})
        release.set()
        done = self.task_event("done")
        self.assertEqual(done["message"], "Uploaded 2 files")
        self.assertEqual(done["fraction"], 1.0)
        self.host.handle({"op": "task_forget", "task": done["id"]})
        self.assertEqual(self.host.tasks.tasks, [])

    def test_google_sign_in(self):
        code = google_oauth.DeviceCode("dev", "ABCD-EFGH", "https://www.google.com/device", 0, 5)
        tokens = google_oauth.Tokens("access", 0, "refresh-1", "me@example.com")
        with mock.patch.object(google_oauth, "start", return_value=code), \
                mock.patch.object(google_oauth, "wait", return_value=tokens):
            self.host.handle({"op": "google_connect", "req": 4, "client_id": "id", "client_secret": "s"})
            shown = self.sent.wait(lambda m: m.get("event") == "google_code")
            done = self.sent.wait(lambda m: m.get("event") == "google_done")
        self.assertEqual(shown["user_code"], "ABCD-EFGH")
        self.assertTrue(done["ok"])
        self.assertEqual((done["refresh_token"], done["email"]), ("refresh-1", "me@example.com"))

    def test_google_sign_in_refused(self):
        with mock.patch.object(google_oauth, "start", side_effect=google_oauth.OAuthError("invalid_client")):
            self.host.handle({"op": "google_connect", "req": 5, "client_id": "id", "client_secret": "s"})
            done = self.sent.wait(lambda m: m.get("event") == "google_done")
        self.assertFalse(done["ok"])
        self.assertIn("Client ID", done["error"])


if __name__ == "__main__":
    unittest.main()

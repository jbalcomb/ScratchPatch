import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from home_runner import registry
from home_runner.runs import Runner
from home_runner.server import make_server

JOBS = """
[jobs.echo]
command = ["{python}", "-c", "import json, sys; print(json.dumps({'got': sys.argv[1]}))", "{word}"]
params = { word = '[a-z]+' }
quick = true

[jobs.slow]
command = ["{python}", "-c", "import time; print('started', flush=True); time.sleep(30)"]

[jobs.count]
command = ["{python}", "-c", "print('line 1'); print('line 2')"]

[jobs.fail]
command = ["{python}", "-c", "import sys; print('boom'); sys.exit(3)"]
"""
KEY = "test-key"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "jobs.toml").write_text(JOBS, encoding="utf-8")
        self.start()

    def start(self):
        self.runner = Runner(registry.load(self.dir / "jobs.toml"), self.dir / "data")
        self.server = make_server(self.runner, KEY, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for run in self.runner.list(50):
            if run["status"] == "running":
                self.runner.cancel(run["id"])

    def call(self, method, path, body=None, key=KEY):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {key}"} if key else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def wait_for(self, run_id, *statuses, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            run = self.call("GET", f"/runs/{run_id}")[1]
            if run["status"] in statuses:
                return run
            time.sleep(0.1)
        self.fail(f"run {run_id} still {run['status']}")

    def test_health_needs_no_key_but_everything_else_does(self):
        self.assertEqual(self.call("GET", "/health", key=None)[0], 200)
        self.assertEqual(self.call("GET", "/jobs", key="wrong")[0], 401)
        status, jobs = self.call("GET", "/jobs")
        self.assertEqual(status, 200)
        self.assertEqual({j["name"] for j in jobs}, {"echo", "slow", "count", "fail"})

    def test_quick_job_returns_parsed_output_and_stays_out_of_the_list(self):
        status, run = self.call("POST", "/runs", {"job": "echo", "params": {"word": "hello"}})
        self.assertEqual((status, run["status"], run["result"]), (200, "succeeded", {"got": "hello"}))
        self.assertEqual(self.call("GET", "/runs")[1], [])
        self.assertEqual([r["id"] for r in self.call("GET", "/runs?all=1")[1]], [run["id"]])

    def test_bad_params_and_unknown_jobs_are_refused(self):
        self.assertEqual(self.call("POST", "/runs", {"job": "echo", "params": {"word": "a; rm -rf /"}})[0], 400)
        self.assertEqual(self.call("POST", "/runs", {"job": "echo", "params": {}})[0], 400)
        self.assertEqual(self.call("POST", "/runs", {"job": "echo", "params": {"word": "a", "x": "y"}})[0], 400)
        self.assertEqual(self.call("POST", "/runs", {"job": "rm"})[0], 404)

    def test_queued_runs_go_one_at_a_time_and_can_be_cancelled(self):
        status, slow = self.call("POST", "/runs", {"job": "slow"})
        self.assertEqual((status, slow["status"]), (202, "queued"))
        self.wait_for(slow["id"], "running")
        _, waiting = self.call("POST", "/runs", {"job": "count"})
        waiting = self.call("GET", f"/runs/{waiting['id']}")[1]
        self.assertEqual((waiting["status"], waiting["ahead"]), ("queued", 1))

        self.call("POST", f"/runs/{slow['id']}/cancel")
        self.assertEqual(self.wait_for(slow["id"], "cancelled", "failed")["status"], "cancelled")
        done = self.wait_for(waiting["id"], "succeeded")
        self.assertEqual(done["log"], ["line 1", "line 2"])

    def test_failure_is_reported_with_its_log(self):
        _, run = self.call("POST", "/runs", {"job": "fail"})
        run = self.wait_for(run["id"], "failed")
        self.assertEqual((run["exit_code"], run["last_line"]), (3, "boom"))

    def test_runs_left_active_by_a_dead_runner_show_as_interrupted(self):
        # As the database looks after the runner died mid-run: a row still marked running.
        run_id = self.runner._insert("slow", {}, "running")
        self.server.shutdown()
        self.server.server_close()
        self.start()
        self.assertEqual(self.call("GET", f"/runs/{run_id}")[1]["status"], "interrupted")

    def test_config_with_an_undeclared_placeholder_is_rejected(self):
        bad = self.dir / "bad.toml"
        bad.write_text('[jobs.x]\ncommand = ["echo", "{oops}"]\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            registry.load(bad)


if __name__ == "__main__":
    unittest.main()

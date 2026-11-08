import base64
import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from courier.__main__ import main
from courier.agent import Seen, receive
from courier.envelope import make_submission
from courier.ntfy import PublishFailed, publisher
from courier.screen import Ruleset
from courier.server import RateLimit, Service, make_server
from courier.store import Senders, load_gateway_key, private_key_from, public_key_from

RULES = Ruleset.load()


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.gw_key = load_gateway_key(self.dir / "gateway.key")
        self.senders = Senders(self.dir / "senders.json")
        self.creds = self.senders.add("laptop-agent", "agent", ["clip", "agent-inbox"])
        self.published = []
        self.fail_publish = False
        self.clock = [0.0]
        self.service = Service(
            RULES, self.gw_key, self.senders, self.publish, self.dir / "data",
            rate=RateLimit(5, 60, clock=lambda: self.clock[0]),
        )
        self.server = make_server(self.service, "127.0.0.1", 0, auth_delay=0)
        self.url = serve(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def publish(self, env):
        if self.fail_publish:
            raise PublishFailed("down")
        self.published.append(env)

    def submission(self, body="git status", channel="clip", creds=None, **kw):
        return make_submission(private_key_from((creds or self.creds)["private_key"]), channel, body, **kw)

    def post(self, data, secret=None):
        request = urllib.request.Request(f"{self.url}/messages", data=json.dumps(data).encode(), method="POST")
        request.add_header("Authorization", f"Bearer {secret or self.creds['secret']}")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def get(self, path):
        with urllib.request.urlopen(self.url + path) as response:
            return json.loads(response.read())

    def audit(self):
        return [json.loads(line) for line in (self.dir / "data" / "audit.jsonl").read_text().splitlines()]

    def test_admitted_message_is_published_and_an_agent_accepts_it(self):
        status, body = self.post(self.submission("ls -la\n"))
        self.assertEqual(status, 202)
        self.assertEqual(body["labels"], ["origin_agent", "trailing_newline_removed"])
        gw_public = public_key_from(self.get("/gateway-key")["public_key"])
        delivery = receive(self.published[0].to_json(), [gw_public], RULES, Seen())
        self.assertEqual(delivery.body, "ls -la")

    def test_health_and_gateway_key_need_no_secret(self):
        self.assertTrue(self.get("/health")["ok"])
        self.assertEqual(self.get("/gateway-key")["ruleset"], RULES.digest)

    def test_unknown_secret(self):
        self.assertEqual(self.post(self.submission(), secret="guess")[0], 401)
        self.assertEqual(self.published, [])

    def test_channel_outside_scope_is_invalid(self):
        status, body = self.post(self.submission(channel="admin"))
        self.assertEqual(status, 400)
        self.assertIn("may not post", body["error"])

    def test_rejected_content(self):
        self.assertEqual(self.post(self.submission("<b>x</b>", content_type="text/html"))[0], 422)

    def test_duplicate_id_is_not_published_twice(self):
        sub = self.submission()
        self.assertEqual(self.post(sub)[0], 202)
        status, body = self.post(sub)
        self.assertEqual((status, body["duplicate"]), (200, True))
        self.assertEqual(len(self.published), 1)

    def test_rate_limit_refills(self):
        statuses = [self.post(self.submission(f"m{i}"))[0] for i in range(6)]
        self.assertEqual(statuses, [202] * 5 + [429])
        self.clock[0] += 12  # one message's worth at 5 per minute
        self.assertEqual(self.post(self.submission("later"))[0], 202)

    def test_broker_down_then_retry_succeeds(self):
        sub = self.submission()
        self.fail_publish = True
        self.assertEqual(self.post(sub)[0], 502)
        self.fail_publish = False
        self.assertEqual(self.post(sub)[0], 202)

    def test_envelope_over_the_broker_limit(self):
        status, body = self.post(self.submission("x" * 5000))
        self.assertEqual(status, 413)
        self.assertEqual(self.published, [])

    def test_audit_log_has_outcomes_but_never_bodies(self):
        self.post(self.submission("hunter2 is my password"))
        self.post(self.submission(channel="admin"))
        lines = self.audit()
        self.assertEqual([l["result"] for l in lines], ["admitted", "invalid"])
        self.assertNotIn("hunter2", (self.dir / "data" / "audit.jsonl").read_text())
        self.assertEqual(len(lines[0]["orig_sha256"]), 64)

    def test_senders_added_and_removed_while_running(self):
        other = Senders(self.dir / "senders.json").add("phone", "human", ["clip"])
        self.assertEqual(self.post(self.submission(creds=other), secret=other["secret"])[0], 202)
        Senders(self.dir / "senders.json").remove("phone")
        self.assertEqual(self.post(self.submission(creds=other), secret=other["secret"])[0], 401)

    def test_send_cli(self):
        main(["--config", str(self.dir), "add-sender", "cli", "--kind", "script", "--channels", "clip",
              "--gateway", self.url, "--out", str(self.dir / "cli.json")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--config", str(self.dir), "send", "clip", "hello\r\n", "--creds", str(self.dir / "cli.json")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["labels"], ["origin_script", "trailing_newline_removed"])
        self.assertEqual(self.published[-1].body, "hello")

    def test_paste_string_holds_the_credentials_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--config", str(self.dir), "add-sender", "phone", "--kind", "human", "--channels", "clip",
                  "--gateway", self.url, "--paste-string"])
        line = [l for l in out.getvalue().splitlines() if l.startswith("courier-sender:")][0]
        encoded = line.removeprefix("courier-sender:")
        decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(decoded, json.loads((self.dir / "phone.sender.json").read_text()))


class NtfyTests(unittest.TestCase):
    def test_envelope_is_posted_to_the_channel_topic(self):
        got = []

        class Ntfy(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                got.append((self.path, self.headers.get("Authorization"), json.loads(body)))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Ntfy)
        url = serve(server)
        try:
            from courier.gateway import Token, admit
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            key, gw = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
            env = admit(make_submission(key, "clip", "hi"), Token("s", "human", frozenset({"clip"}), key.public_key()),
                        RULES, gw)
            publisher(url, token="tk")(env)
        finally:
            server.shutdown()
            server.server_close()
        path, auth, body = got[0]
        self.assertEqual((path, auth, body["id"]), ("/clip", "Bearer tk", env.id))

    def test_unreachable_broker(self):
        from courier.envelope import Envelope

        env = Envelope(1, "0" * 26, "clip", "fanout", "", "", "", "text/plain", "hi")
        with self.assertRaises(PublishFailed):
            publisher("http://127.0.0.1:9", timeout=2)(env)


if __name__ == "__main__":
    unittest.main()

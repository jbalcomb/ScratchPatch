import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from crossmess.__main__ import main
from crossmess.device import ConfigError, DeviceAgent, DeviceConfig, subscribe
from crossmess.envelope import make_submission
from crossmess.gateway import Token, admit
from crossmess.screen import Ruleset
from crossmess.store import public_key_text

RULES = Ruleset.load()


class FakeClipboard:
    def __init__(self):
        self.text = None

    def set(self, text):
        self.text = text

    def get(self):
        return self.text

    def clear(self):
        self.text = None


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, title, body):
        self.sent.append((title, body))


class FakeTimer:
    def __init__(self, seconds, callback):
        self.seconds, self.callback, self.cancelled, self.daemon = seconds, callback, False, False

    def start(self):
        FakeTimer.last = self

    def cancel(self):
        self.cancelled = True


CONFIG = """
ntfy = "http://127.0.0.1:1/"
gateway_keys = [{key}]

[[subscriptions]]
channel = "clip"
action = "clipboard"
clear_after = 90

[[subscriptions]]
channel = "multi"
action = "clipboard"
allow_multiline = true

[[subscriptions]]
channel = "firstline"
action = "clipboard"
toppings = [{{ mutate = "single-line" }}]

[[subscriptions]]
channel = "humans"
action = "clipboard"
toppings = [{{ filter = "allow-origins", origins = ["human"] }}]

[[subscriptions]]
channel = "nosecrets"
action = "notify"
toppings = [{{ filter = "drop-labels", labels = ["secret"] }}]

[[subscriptions]]
channel = "alerts"
action = "notify"

[[subscriptions]]
channel = "links"
action = "open-url"

[[subscriptions]]
channel = "box"
action = "inbox"
"""


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.gw_key = Ed25519PrivateKey.generate()
        self.sender_key = Ed25519PrivateKey.generate()
        (self.dir / "ntfy-device.token").write_text("tk_" + "a" * 29)
        (self.dir / "device.toml").write_text(CONFIG.format(key=json.dumps(public_key_text(self.gw_key))))
        self.config = DeviceConfig.load(self.dir / "device.toml")
        self.clipboard, self.notifier, self.opened = FakeClipboard(), FakeNotifier(), []
        self.agent = self.make_agent()

    def make_agent(self, platform="win32"):
        return DeviceAgent(self.config, RULES, self.dir / "data", self.clipboard, self.notifier,
                           opener=self.opened.append, platform=platform, timer=FakeTimer)

    def envelope(self, channel, body, kind="script", **kw):
        token = Token("laptop", kind, frozenset({channel}), self.sender_key.public_key())
        env = admit(make_submission(self.sender_key, channel, body, **kw), token, RULES, self.gw_key)
        return json.dumps(env.to_json())

    def handle(self, channel, body, **kw):
        return self.agent.handle(channel, self.envelope(channel, body, **kw))

    def test_copies_to_the_clipboard_and_clears_it_later(self):
        out = self.handle("clip", "git status")
        self.assertEqual((out.result, self.clipboard.text), ("done", "git status"))
        self.assertEqual(FakeTimer.last.seconds, 90)
        FakeTimer.last.callback()
        self.assertIsNone(self.clipboard.text)

    def test_clear_leaves_what_you_copied_since(self):
        self.handle("clip", "git status")
        self.clipboard.text = "something you copied"
        FakeTimer.last.callback()
        self.assertEqual(self.clipboard.text, "something you copied")

    def test_multiline_is_refused_and_you_are_told(self):
        out = self.handle("clip", "echo hi\nrm -rf ~")
        self.assertEqual((out.result, out.reason), ("refused", "it has more than one line"))
        self.assertIsNone(self.clipboard.text)
        self.assertIn("not copied", self.notifier.sent[0][0])
        self.assertNotIn("rm -rf", self.notifier.sent[0][1])

    def test_multiline_allowed_becomes_crlf_on_windows_only(self):
        self.handle("multi", "one\ntwo")
        self.assertEqual(self.clipboard.text, "one\r\ntwo")
        self.agent = self.make_agent(platform="linux")
        self.handle("multi", "three\nfour")
        self.assertEqual(self.clipboard.text, "three\nfour")

    def test_single_line_topping_makes_a_multiline_message_copyable(self):
        out = self.handle("firstline", "git status\nrm -rf ~")
        self.assertEqual((out.result, self.clipboard.text), ("done", "git status"))
        self.assertIn("multiline", out.labels, "the label stays on the record")

    def test_shell_characters_and_secrets_are_refused(self):
        self.assertEqual(self.handle("clip", "ls | head").reason, "it has shell characters")
        self.assertEqual(self.handle("clip", "AKIAIOSFODNN7EXAMPLE").reason, "it looks like a secret")
        self.assertIsNone(self.clipboard.text)

    def test_filter_topping_drops(self):
        self.assertEqual(self.handle("humans", "hi").result, "dropped")
        self.assertEqual(self.handle("humans", "hi", kind="human").result, "done")

    def test_notification_hides_secrets_and_truncates(self):
        self.handle("alerts", "AKIAIOSFODNN7EXAMPLE")
        self.handle("alerts", "x" * 500)
        self.assertEqual(self.notifier.sent[0][1], "(hidden: it looks like a secret)")
        self.assertEqual(len(self.notifier.sent[1][1]), 240)
        self.assertEqual(self.notifier.sent[1][0], "alerts from laptop")

    def test_drop_labels_topping(self):
        self.assertEqual(self.handle("nosecrets", "AKIAIOSFODNN7EXAMPLE").result, "dropped")
        self.assertEqual(self.notifier.sent, [])

    def test_open_url_only_plain_https(self):
        self.assertEqual(self.handle("links", "https://example.com/a").result, "done")
        self.assertEqual(self.opened, ["https://example.com/a"])
        for body, reason in [
            ("javascript:alert(1)", "it is not a single https link"),
            ("https://xn--pple-43d.com", "the link's host uses non-ASCII or punycode"),
            ("https://bank.example@evil.example", "the link hides its host behind an @"),
            ("see https://example.com", "it is not a single https link"),
        ]:
            self.assertEqual(self.handle("links", body).reason, reason, body)
        self.assertEqual(len(self.opened), 1)

    def test_inbox_keeps_bodies_but_not_secrets(self):
        self.handle("box", "remember the milk")
        self.handle("box", "AKIAIOSFODNN7EXAMPLE")
        entries = [json.loads(l) for l in (self.dir / "data" / "inbox.jsonl").read_text().splitlines()]
        self.assertEqual([e["body"] for e in entries], ["remember the milk", None])

    def test_device_log_never_holds_bodies(self):
        self.handle("clip", "hunter2-is-the-password")
        self.handle("clip", "echo hi\nrm -rf ~")
        text = (self.dir / "data" / "device.jsonl").read_text()
        self.assertNotIn("hunter2", text)
        self.assertNotIn("rm -rf", text)

    def test_forged_replayed_and_misrouted_envelopes(self):
        other_gateway = Ed25519PrivateKey.generate()
        token = Token("x", "script", frozenset({"clip"}), self.sender_key.public_key())
        forged = admit(make_submission(self.sender_key, "clip", "hi"), token, RULES, other_gateway)
        self.assertEqual(self.agent.handle("clip", json.dumps(forged.to_json())).result, "invalid")
        good = self.envelope("clip", "hi")
        self.assertEqual(self.agent.handle("clip", good).result, "done")
        self.assertEqual(self.agent.handle("clip", good).reason, "envelope already delivered")
        self.assertEqual(self.agent.handle("multi", self.envelope("clip", "hi")).reason,
                         "channel does not match the topic")
        self.assertEqual(self.agent.handle("clip", "not json").result, "invalid")
        self.assertEqual(self.agent.handle("elsewhere", good).result, "ignored")


class ConfigTests(unittest.TestCase):
    def write(self, text):
        folder = Path(tempfile.mkdtemp())
        (folder / "ntfy-device.token").write_text("tk_x")
        key = json.dumps(public_key_text(Ed25519PrivateKey.generate()))
        (folder / "device.toml").write_text(f'ntfy = "http://x"\ngateway_keys = [{key}]\n{text}')
        return folder / "device.toml"

    def test_bad_settings_are_named(self):
        for text, words in [
            ('[[subscriptions]]\nchannel = "c"\naction = "run"', "action must be one of"),
            ('[[subscriptions]]\nchannel = "c"\ntoppings = [{ filter = "nope" }]', "unknown topping"),
            ('[[subscriptions]]\nchannel = "c"\nalow_multiline = true', "unknown settings alow_multiline"),
            ('[[subscriptions]]\nchannel = "c"\n[[subscriptions]]\nchannel = "c"', "subscribed twice"),
            ('[[subscriptions]]\nchannel = "C D"', "bad channel name"),
        ]:
            with self.assertRaises(ConfigError, msg=text) as caught:
                DeviceConfig.load(self.write(text))
            self.assertIn(words, str(caught.exception))

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            DeviceConfig.load(Path(tempfile.mkdtemp()) / "device.toml")

    def test_device_init_writes_a_loadable_config(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "ntfy-device.token").write_text("tk_" + "b" * 29)
        key = public_key_text(Ed25519PrivateKey.generate())
        self.assertEqual(main(["--config", str(folder), "device-init", "--ntfy", "https://h.ts.net:2586/",
                               "--gateway-key", key]), 0)
        config = DeviceConfig.load(folder / "device.toml")
        self.assertEqual(config.ntfy, "https://h.ts.net:2586")
        self.assertEqual(sorted(config.subscriptions), ["agent-inbox", "clip"])
        self.assertEqual(main(["--config", str(folder), "device-init", "--ntfy", "x", "--gateway-key", key]), 1)


class SubscribeTests(unittest.TestCase):
    def test_streams_messages_and_resumes_after_the_last_one(self):
        folder = Path(tempfile.mkdtemp())
        gw = Ed25519PrivateKey.generate()
        sender = Ed25519PrivateKey.generate()
        (folder / "ntfy-device.token").write_text("tk_" + "c" * 29)
        (folder / "device.toml").write_text(
            f'ntfy = "http://127.0.0.1:0"\ngateway_keys = [{json.dumps(public_key_text(gw))}]\n'
            '[[subscriptions]]\nchannel = "clip"\naction = "clipboard"\nclear_after = 0\n')
        token = Token("s", "human", frozenset({"clip"}), sender.public_key())
        envelopes = [json.dumps(admit(make_submission(sender, "clip", t), token, RULES, gw).to_json())
                     for t in ("first", "second")]
        requests, stop = [], threading.Event()

        class Ntfy(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                batch = envelopes[len(requests) - 1:len(requests)]
                lines = [json.dumps({"event": "open", "id": "o1"})]
                lines += [json.dumps({"event": "message", "id": f"m{len(requests)}", "topic": "clip", "message": e})
                          for e in batch]
                body = ("\n".join(lines) + "\n").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if len(requests) >= 3:
                    stop.set()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Ntfy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        text = (folder / "device.toml").read_text().replace("127.0.0.1:0", f"127.0.0.1:{server.server_address[1]}")
        (folder / "device.toml").write_text(text)
        config = DeviceConfig.load(folder / "device.toml")
        clipboard = FakeClipboard()
        agent = DeviceAgent(config, RULES, folder / "data", clipboard, FakeNotifier())
        try:
            subscribe(config, agent, folder / "state.json", stop=stop, max_backoff=0.05)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(clipboard.text, "second")
        self.assertTrue(requests[0][0].startswith("/clip/json?since="))
        self.assertEqual(requests[1][0], "/clip/json?since=m1")
        self.assertEqual(requests[2][0], "/clip/json?since=m2")
        self.assertEqual(requests[0][1], "Bearer tk_" + "c" * 29)
        self.assertEqual(json.loads((folder / "state.json").read_text()), {"since": "m2"})


if __name__ == "__main__":
    unittest.main()

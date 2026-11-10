import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from courier import client, mcp
from courier.__main__ import main
from courier.agent import Seen, receive
from courier.screen import Ruleset
from courier.server import Service, make_server
from courier.store import Senders, load_gateway_key

RULES = Ruleset.load()
SRC = Path(__file__).resolve().parents[1] / "src"
CREDS = client.Credentials("claude", "s", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "http://gw")


def rpc(server, method, params=None, msg_id=1):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.reply = {"id": "01J", "labels": ["origin_agent"]}
        self.fail = None

        def send(creds, channel, text, ttl_s):
            if self.fail:
                raise self.fail
            self.sent.append((channel, text, ttl_s))
            return self.reply

        self.server = mcp.Server(CREDS, ["clip", "agent-inbox"], send=send)

    def call(self, **arguments):
        return rpc(self.server, "tools/call", {"name": "send_to_devices", "arguments": arguments})["result"]

    def test_initialize_negotiates_a_known_version(self):
        result = rpc(self.server, "initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        newer = rpc(self.server, "initialize", {"protocolVersion": "2099-01-01"})["result"]
        self.assertEqual(newer["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])

    def test_tools_list_offers_only_the_senders_channels(self):
        (tool,) = rpc(self.server, "tools/list")["result"]["tools"]
        self.assertEqual(tool["name"], "send_to_devices")
        self.assertEqual(tool["inputSchema"]["properties"]["channel"]["enum"], ["agent-inbox", "clip"])

    def test_send(self):
        result = self.call(channel="clip", text="git status")
        self.assertFalse(result["isError"])
        self.assertEqual(self.sent, [("clip", "git status", 600)])
        self.assertEqual(result["structuredContent"]["id"], "01J")

    def test_screening_labels_are_reported_without_origin(self):
        self.reply = {"id": "01J", "labels": ["origin_agent", "shell_chars"]}
        text = self.call(channel="clip", text="a | b")["content"][0]["text"]
        self.assertIn("shell_chars", text)
        self.assertNotIn("origin_agent", text)

    def test_refused_before_sending(self):
        cases = [
            dict(channel="admin", text="x"),
            dict(channel="Clip!", text="x"),
            dict(channel="clip", text="   "),
            dict(channel="clip", text="x" * (mcp.MAX_TEXT + 1)),
            dict(channel="clip", text="x", ttl_seconds=5),
            dict(channel="clip", text="x", ttl_seconds=True),
            dict(channel="clip", text=["not", "text"]),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertTrue(self.call(**arguments)["isError"])
        self.assertEqual(self.sent, [])

    def test_gateway_refusal_is_a_tool_error(self):
        self.fail = client.SendFailed("screening rejected the content", 422)
        result = self.call(channel="clip", text="x")
        self.assertTrue(result["isError"])
        self.assertIn("screening rejected", result["content"][0]["text"])

    def test_unknown_tool_and_method(self):
        self.assertEqual(rpc(self.server, "tools/call", {"name": "rm", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(rpc(self.server, "resources/list")["error"]["code"], -32601)

    def test_notifications_get_no_reply(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_serve_reads_lines_and_survives_garbage(self):
        stdin = io.StringIO('not json\n\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
        stdout = io.StringIO()
        mcp.serve(self.server, stdin, stdout)
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 7, "result": {}})


class EndToEndTests(unittest.TestCase):
    """`python -m courier mcp` over real stdio, against a gateway running in this process."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.published = []
        senders = Senders(self.dir / "senders.json")
        self.gw_key = load_gateway_key(self.dir / "gateway.key")
        service = Service(RULES, self.gw_key, senders, self.published.append, self.dir / "data")
        self.server = make_server(service, "127.0.0.1", 0, auth_delay=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        main(["--config", str(self.dir), "add-sender", "claude", "--kind", "agent", "--channels", "clip,agent-inbox",
              "--gateway", url, "--out", str(self.dir / "claude.json")])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_agent_sends_and_a_device_accepts_it(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "send_to_devices", "arguments": {"channel": "clip", "text": "héllo"}}},
        ]
        stdin = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests).encode("utf-8")
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        proc = subprocess.run([sys.executable, "-m", "courier", "mcp", "--creds", str(self.dir / "claude.json")],
                              input=stdin, capture_output=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertNotIn(b"\r\n", proc.stdout)
        replies = {r["id"]: r for r in map(json.loads, proc.stdout.decode("utf-8").splitlines())}
        self.assertEqual(sorted(replies), [1, 2, 3])
        enum = replies[2]["result"]["tools"][0]["inputSchema"]["properties"]["channel"]["enum"]
        self.assertEqual(enum, ["agent-inbox", "clip"])
        self.assertFalse(replies[3]["result"]["isError"], replies[3])

        delivery = receive(self.published[0].to_json(), [self.gw_key.public_key()], RULES, Seen())
        self.assertEqual(delivery.body, "héllo")
        self.assertIn("origin_agent", delivery.labels)


if __name__ == "__main__":
    unittest.main()

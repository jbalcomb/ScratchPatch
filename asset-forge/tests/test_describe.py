import base64
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

from assetforge import describe, export
from assetforge.vision import VisionError, parse_json_reply
from tests.builders import build_flic, build_lbx

GOOD = {"subject": "red blob", "category": "unit", "description": "A red blob.", "animation": "",
        "style": "pixel art", "prompt": "a red blob, transparent background", "negative_prompt": "text",
        "tags": ["blob"]}


class FakeServer:
    """An OpenAI-compatible endpoint that records requests and answers with queued replies."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                reply = outer.replies.pop(0) if outer.replies else json.dumps(GOOD)
                data = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DescribeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        game, self.out = Path(self.tmp.name, "game"), Path(self.tmp.name, "out")
        game.mkdir()
        sprites = [build_flic(4, 3, [[5] * 12, [None] * 11 + [6]]), build_flic(2, 2, [[1, 2, 3, 4]])]
        (game / "UNITS.LBX").write_bytes(build_lbx(sprites, [("BLOB", "red blob"), ("DOT", "")]))
        (game / "SPELLS.LBX").write_bytes(build_lbx([build_flic(2, 2, [[7] * 4])], [("FIRE", "")]))
        export.main([str(game), str(self.out)])

    def tearDown(self):
        self.tmp.cleanup()

    def run_describe(self, replies, *extra):
        server = FakeServer(replies)
        try:
            code = describe.main([str(self.out), "--endpoint", server.url, "--model", "test-vl", *extra])
        finally:
            server.close()
        return code, server.requests

    def test_describes_sprites_and_records_provenance(self):
        code, requests = self.run_describe([], "--archive", "units")
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 2)
        path, body = requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["model"], "test-vl")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        text, image = body["messages"][1]["content"]
        self.assertIn('UNITS.LBX entry 0. Name in the archive: "BLOB". Archive note: "red blob"', text["text"])
        self.assertIn("2 animation frame(s)", text["text"])
        png = base64.b64decode(image["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(png)) as img:
            self.assertEqual(img.size, (384, 144))                   # 8x3 sheet enlarged 48x, nearest neighbour
            self.assertEqual(img.getpixel((383, 0)), (255, 0, 255))  # transparent shown as magenta

        result = json.loads((self.out / "UNITS/000_BLOB/describe.json").read_text())
        self.assertEqual(result["subject"], "red blob")
        self.assertEqual((result["archive"], result["entry"], result["frames"]), ("UNITS", 0, 2))
        self.assertEqual(result["provenance"]["model"], "test-vl")
        self.assertEqual(result["provenance"]["prompt_version"], describe.PROMPT_VERSION)
        lines = (self.out / "descriptions.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["name"] for l in lines], ["BLOB", "DOT"])
        self.assertFalse((self.out / "SPELLS/000_FIRE/describe.json").exists())

    def test_resumes_and_skips_bad_replies(self):
        code, requests = self.run_describe(["not json at all", "```json\n" + json.dumps(GOOD) + "\n```"])
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 3)
        self.assertFalse((self.out / "SPELLS/000_FIRE/describe.json").exists())  # the bad reply
        self.assertTrue((self.out / "UNITS/000_BLOB/describe.json").exists())    # fenced JSON is fine
        code, requests = self.run_describe([])                                   # second run: only the failed one
        self.assertEqual(len(requests), 1)
        self.assertTrue((self.out / "SPELLS/000_FIRE/describe.json").exists())

    def test_hints_and_tag_strings(self):
        (self.out / "hints.csv").write_text("archive,entry,hint\nUNITS,1,icon of a Dwarven Settlers unit\n")
        reply = dict(GOOD, tags="blob, red , ")
        code, requests = self.run_describe([json.dumps(reply)] * 3, "--archive", "units")
        texts = [b["messages"][1]["content"][0]["text"] for _, b in requests]
        self.assertNotIn("Known identity", texts[0])
        self.assertIn("Known identity, from the game's data (trust it): icon of a Dwarven Settlers unit.", texts[1])
        dot = json.loads((self.out / "UNITS/001_DOT/describe.json").read_text())
        self.assertEqual((dot["hint"], dot["tags"]), ("icon of a Dwarven Settlers unit", ["blob", "red"]))

    def test_bucket_filters(self):
        code, requests = self.run_describe([], "--skip-bucket", "ui")         # UNITS and SPELLS are both ui
        self.assertEqual((code, len(requests)), (0, 0))
        code, requests = self.run_describe([], "--bucket", "ui", "--skip-bucket", "figure")
        self.assertEqual((code, len(requests)), (0, 3))

    def test_limit_and_missing_keys(self):
        code, requests = self.run_describe([json.dumps({"subject": "x"})], "--limit", "1", "--no-json-mode")
        self.assertEqual(code, 1)
        self.assertNotIn("response_format", requests[0][1])
        self.assertEqual(len(requests), 1)

    def test_unreachable_server_stops_the_run(self):
        code = describe.main([str(self.out), "--endpoint", "http://127.0.0.1:9/v1"])
        self.assertEqual(code, 1)


class ParseTest(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(parse_json_reply('Sure! {"a": 1} hope that helps'), {"a": 1})
        with self.assertRaises(VisionError):
            parse_json_reply("no json here")


if __name__ == "__main__":
    unittest.main()

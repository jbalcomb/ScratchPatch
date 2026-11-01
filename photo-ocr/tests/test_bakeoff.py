import json
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bakeoff  # noqa: E402


def _png():
    """A valid 1x1 PNG, so no real receipt photos are needed."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


PNG = _png()
LABEL = {"store": ["Giant Eagle", "GetGo"], "date": "2026-10-12", "tax": "0.42", "total": "7.19",
         "items": [{"name": "BANANAS", "price": "1.29"}, {"name": "MILK 2% GAL", "price": "3.99"},
                   {"name": "BREAD WHT", "price": "2.49"}, {"name": "COUPON", "price": "-1.00"}]}


def test_to_cents():
    assert bakeoff.to_cents("12.34") == 1234
    assert bakeoff.to_cents("$1,234.5") == 123450
    assert bakeoff.to_cents(3.49) == 349
    assert bakeoff.to_cents("1.00-") == -100
    assert bakeoff.to_cents("(2.50)") == -250
    assert bakeoff.to_cents("-.99") == -99
    assert bakeoff.to_cents("abc") is None
    assert bakeoff.to_cents(None) is None
    assert bakeoff.to_cents(True) is None


def test_to_date():
    assert bakeoff.to_date("2026-10-12") == "2026-10-12"
    assert bakeoff.to_date("10/12/2026") == "2026-10-12"
    assert bakeoff.to_date("10/12/26") == "2026-10-12"
    assert bakeoff.to_date("Oct 12, 2026") == "2026-10-12"
    assert bakeoff.to_date("2026-10-12T14:03:00") == "2026-10-12"
    assert bakeoff.to_date("13/45/2026") is None
    assert bakeoff.to_date(None) is None


def test_extract_json_tolerates_fences_and_thinking():
    assert bakeoff.extract_json('<think>hmm {"x": 0}</think>Sure!\n```json\n{"total": 1}\n```') == {"total": 1}
    assert bakeoff.extract_json('Here: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        bakeoff.extract_json("no json here")


def test_score_perfect_and_partial():
    parsed = {"store": "GIANT EAGLE", "date": "10/12/2026", "tax": 0.42, "total": 7.19,
              "items": [{"name": "Bananas", "price": 1.29}, {"name": "MILK 2% GALLON", "price": 3.99},
                        {"name": "BREAD WHT", "price": 2.49}, {"name": "COUPON", "price": -1.0}]}
    s = bakeoff.score(LABEL, parsed)
    assert s["all"] and s["sum_check"] and s["items_matched"] == 4

    parsed["total"] = 7.91          # misread total
    parsed["items"][2]["price"] = 2.94  # misread price: that item no longer matches
    parsed["items"].append({"name": "BANANAS", "price": 1.29})  # duplicated where photos overlap
    s = bakeoff.score(LABEL, parsed)
    assert not s["total"] and s["date"] and s["store"]
    assert (s["items_matched"], s["items_truth"], s["items_pred"]) == (3, 4, 5)
    assert not s["items_all"] and not s["all"]


def test_score_store_must_be_a_known_spelling():
    assert not bakeoff.score(LABEL, {"store": "Giant Tiger", "total": 7.19})["store"]
    assert bakeoff.score(LABEL, {"store": "Get-Go", "total": 7.19})["store"]


def test_match_items_one_to_one():
    truth = [{"name": "MILK", "price": 399}, {"name": "MILK", "price": 399}]
    pred = [{"name": "MILK", "price": 399}]
    assert len(bakeoff.match_items(truth, pred)) == 1


def test_load_engines_fills_nested_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("RECEIPTS_LLM_URL", raising=False)
    monkeypatch.setenv("LLM_URL", "http://ws:8080")
    f = tmp_path / "e.json"
    f.write_text(json.dumps({"engines": [{"name": "a", "kind": "vlm", "url": "${RECEIPTS_LLM_URL:-${LLM_URL}}"}]}))
    assert bakeoff.load_engines(f)["a"]["url"] == "http://ws:8080"
    monkeypatch.setenv("RECEIPTS_LLM_URL", "http://swap:9292")
    assert bakeoff.load_engines(f)["a"]["url"] == "http://swap:9292"


def test_repo_engines_file_loads():
    engines = bakeoff.load_engines(bakeoff.HERE / "engines.json")
    assert {"qwen3-vl-8b", "tesseract+llm"} <= set(engines)
    assert all(e["kind"] in ("vlm", "ocr_llm") for e in engines.values())
    assert all(e["ocr"] in bakeoff.OCR for e in engines.values() if e["kind"] == "ocr_llm")


class FakeLlama(BaseHTTPRequestHandler):
    """Answers like llama-server: the right receipt, except a wrong total when the request has two photos."""
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLlama.requests.append(body)
        content = body["messages"][0]["content"]
        images = [c for c in content if isinstance(c, dict) and c.get("type") == "image_url"] if isinstance(content, list) else []
        reply = json.loads(json.dumps(LABEL))
        reply["store"] = "Giant Eagle"
        if len(images) == 2:
            reply["total"] = "9.99"
        text = "```json\n" + json.dumps(reply) + "\n```"
        data = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def llama():
    FakeLlama.requests = []
    server = HTTPServer(("127.0.0.1", 0), FakeLlama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_add_run_score_end_to_end(tmp_path, llama, capsys):
    home = tmp_path / "bakeoff"
    shots = tmp_path / "phone"
    shots.mkdir()
    for n in (1, 2, 3):
        (shots / f"IMG_{n}.png").write_bytes(PNG)
    assert bakeoff.main(["--home", str(home), "add", str(shots / "IMG_1.png"), "--tags", "thermal"]) == 0
    assert bakeoff.main(["--home", str(home), "add", str(shots / "IMG_2.png"), str(shots / "IMG_3.png")]) == 0
    r1, r2 = home / "set" / "r001", home / "set" / "r002"
    assert [p.name for p in bakeoff.photos(r2)] == ["1.png", "2.png"]
    assert bakeoff.tags(r1) == ["thermal"] and bakeoff.tags(r2) == ["multi-photo"]

    engines = tmp_path / "engines.json"
    engines.write_text(json.dumps({"engines": [{"name": "fake-vl", "kind": "vlm", "url": llama, "model": "m"}]}))
    assert bakeoff.main(["--home", str(home), "run", "fake-vl", "--engines", str(engines)]) == 0
    run = json.loads((home / "results" / "fake-vl" / "r002.json").read_text())
    assert run["error"] is None and run["parsed"]["total"] == "9.99"
    # Both photos of r002 went in one request, with the schema and the model name.
    assert len(FakeLlama.requests) == 2
    assert FakeLlama.requests[1]["model"] == "m" and "response_format" in FakeLlama.requests[1]

    # Kept between runs: nothing re-sent without --force.
    bakeoff.main(["--home", str(home), "run", "fake-vl", "--engines", str(engines)])
    assert len(FakeLlama.requests) == 2

    # Drafts come from the engine; one becomes the label, as if corrected by hand.
    bakeoff.main(["--home", str(home), "label", "fake-vl"])
    assert (r1 / "label.draft.json").exists()
    (r1 / "label.json").write_text(json.dumps(LABEL))
    (r2 / "label.json").write_text(json.dumps(LABEL))
    assert bakeoff.main(["--home", str(home), "check"]) == 0

    capsys.readouterr()
    bakeoff.main(["--home", str(home), "score"])
    report = (home / "results" / "report.md").read_text()
    row = next(line for line in report.splitlines() if line.startswith("| fake-vl |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[1:4] == ["2", "0", "50%"]  # 2 receipts, none failed, total right on 1
    assert "T✗" in report and "thermal" in report


def test_ocr_llm_engine_ocrs_everything_first(tmp_path, llama, monkeypatch):
    home = tmp_path / "bakeoff"
    for rid in ("r001", "r002"):
        (home / "set" / rid).mkdir(parents=True)
        (home / "set" / rid / "1.png").write_bytes(PNG)
    order = []

    def fake_ocr(paths, cfg):
        order.append(("ocr", paths[0].parent.name))
        return [{"text": "BANANAS 1.29", "conf": 0.9}, None]

    monkeypatch.setitem(bakeoff.OCR, "tesseract", fake_ocr)
    engine = {"name": "t+llm", "kind": "ocr_llm", "ocr": "tesseract", "url": llama}
    bakeoff.run_engine(engine, home / "set", home / "results" / "t+llm",
                       log=lambda msg: order.append(("log", msg)) if "OCR" not in msg else None)
    assert [o for o in order if o[0] == "ocr"] == [("ocr", "r001"), ("ocr", "r002")]
    assert order.index(("ocr", "r002")) < next(i for i, o in enumerate(order) if o[0] == "log")
    prompt = FakeLlama.requests[0]["messages"][0]["content"]
    assert "--- photo 1 ---\nBANANAS 1.29" in prompt
    run = json.loads((home / "results" / "t+llm" / "r001.json").read_text())
    assert run["ocr"][0]["conf"] == 0.9 and run["parsed"]


def test_check_flags_bad_labels(tmp_path, capsys):
    home = tmp_path / "bakeoff"
    r = home / "set" / "r001"
    r.mkdir(parents=True)
    (r / "1.png").write_bytes(PNG)
    bad = dict(LABEL, total="7.91", date="2026-13-01")
    (r / "label.json").write_text(json.dumps(bad))
    assert bakeoff.main(["--home", str(home), "check"]) == 1
    out = capsys.readouterr().out
    assert "doesn't parse" in out and "items + tax = 7.19, total = 7.91" in out


def test_ocr_text_marks_photo_breaks():
    lines = [{"text": "A", "conf": 1}, None, {"text": "B", "conf": 1}, None]
    assert bakeoff.ocr_text(lines) == "--- photo 1 ---\nA\n--- photo 2 ---\nB"

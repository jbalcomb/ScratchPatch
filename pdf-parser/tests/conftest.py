"""Test PDFs made on the spot with PyMuPDF and Pillow, and a stand-in for the llama.cpp server."""
import base64
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def chart(w=400, h=300, seed=0):
    """A bar chart with a margin all round, like a figure in a report."""
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    d.line((30, h - 30, w - 30, h - 30), fill="black", width=3)
    d.line((30, 30, 30, h - 30), fill="black", width=3)
    for i, bar in enumerate((120, 200, 80, 160, 220)):
        x = 50 + i * (w - 100) // 5
        d.rectangle((x, h - 30 - (bar + seed) * (h - 80) // 240, x + 40, h - 32), fill=(40 + 40 * i, 90, 160))
    return im


def text_image(lines, w=1700, h=2200):
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=48)
    for i, line in enumerate(lines):
        d.text((150, 200 + i * 90), line, fill=0, font=font)
    return im


@pytest.fixture
def sample_pdf(tmp_path):
    doc = pymupdf.open()
    # Page 1: text with two fonts, a table, a figure, the same figure again, a blank image and an icon.
    p = doc.new_page()
    p.insert_text((72, 72), "Quarterly Report", fontname="hebo", fontsize=20)
    p.insert_text((72, 100), "Sales grew in every region this quarter.", fontname="helv", fontsize=11)
    x0, y0, cw, rh = 72, 130, 120, 20
    rows = [("Region", "Q1", "Q2"), ("North", "10", "12"), ("South", "8", "11")]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
            p.draw_rect(rect, color=(0, 0, 0), width=0.8)
            p.insert_text((rect.x0 + 4, rect.y1 - 6), cell, fontname="helv", fontsize=10)
    figure = png(chart())
    p.insert_image(pymupdf.Rect(72, 220, 312, 400), stream=figure)
    p.insert_image(pymupdf.Rect(330, 220, 570, 400), stream=figure)          # same image again: duplicate
    p.insert_image(pymupdf.Rect(72, 420, 272, 560), stream=png(Image.new("RGB", (200, 140), (250, 250, 250))))
    p.insert_image(pymupdf.Rect(72, 600, 84, 612), stream=png(Image.new("RGB", (40, 40), (200, 30, 30))))  # an icon
    # Page 2: a scan (an image of text, no text layer).
    p2 = doc.new_page()
    p2.insert_image(p2.rect, stream=png(text_image(["INVOICE 2041", "Total due 125 dollars", "Thank you"])))
    path = tmp_path / "sample.pdf"
    doc.save(path)
    return path


@pytest.fixture
def fake_llm():
    """A llama.cpp-like server. ``answer(image)`` decides each verdict; ``fail`` makes requests fail."""
    state = {"answer": lambda im: {"complete": True, "content_type": "chart", "issue": None}, "fail": 0,
             "models": [{"id": "qwen3-vl-8b", "status": {"value": "unloaded",
                                                         "args": ["--mmproj", "/models/mmproj.gguf"]}}],
             "requests": [], "refuse_images": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/v1/models"):
                return self._send(200, {"data": state["models"]})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body)
            if state["refuse_images"]:
                return self._send(500, {"error": {"message": "image input is not supported - hint: if this is "
                                                             "unexpected, you may need to provide the mmproj"}})
            if state["fail"]:
                state["fail"] -= 1
                return self._send(503, {"error": "loading model"})
            url = body["messages"][0]["content"][1]["image_url"]["url"]
            im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
            return self._send(200, {"choices": [{"message": {"content": json.dumps(state["answer"](im))}}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    yield state
    server.shutdown()

import csv
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

import pymupdf
import pytest
from PIL import ImageStat

import images
import ocr
import parse_pdf
import vision
import web
from conftest import chart, png

needs_tesseract = pytest.mark.skipif(not ocr.available(), reason="tesseract not installed")


def by_id(manifest):
    return {e["id"]: e for e in manifest["elements"]}


def blank_or_chart(im):
    if ImageStat.Stat(im.convert("L")).stddev[0] < 4:
        return {"complete": False, "content_type": "blank", "issue": "nothing in it"}
    return {"complete": True, "content_type": "chart", "issue": None}


# ---------------------------------------------------------------- without the vision model

def test_bundle_without_vision(sample_pdf, tmp_path):
    m = parse_pdf.parse(sample_pdf, tmp_path / "out", use_vision=False)
    out = tmp_path / "out" / "sample"
    assert json.loads((out / "manifest.json").read_text()) == m
    s = m["summary"]
    assert (s["pages"], s["text_pages"], s["scanned_pages"], s["tables"]) == (2, 1, 1, 1)
    assert (s["images_kept"], s["images_needs_review"], s["images_duplicate"], s["images_small"]) == (1, 1, 1, 1)
    assert s["vision"]["available"] is False

    els = by_id(m)
    title = els["p1-text1"]
    assert title["text"] == "Quarterly Report" and title["order"] == 1 and title["source"] == "text_layer"
    span = title["lines"][0]["spans"][0]
    assert span["bold"] and span["size"] == 20 and span["color"] == "#000000" and len(span["bbox"]) == 4
    assert els["p1-text2"]["lines"][0]["spans"][0]["bold"] is False

    table = next(e for e in m["elements"] if e["type"] == "table")
    assert (table["rows"], table["cols"], table["header"]) == (3, 3, ["Region", "Q1", "Q2"])
    rows = list(csv.reader((out / table["file"]).open()))
    assert rows == [["Region", "Q1", "Q2"], ["North", "10", "12"], ["South", "8", "11"]]
    in_table = [e for e in m["elements"] if e.get("in_table") == table["file"]]
    assert in_table and all(e["order"] > table["order"] for e in in_table)

    assert els["p1-i1"]["status"] == "kept" and (out / els["p1-i1"]["file"]).exists()
    assert els["p1-i2"] == {**els["p1-i2"], "status": "duplicate", "duplicate_of": "p1-i1"}
    blank = els["p1-i3"]
    assert blank["status"] == "needs_review" and "blank" in blank["flags"]
    assert 1 < len(blank["attempts"]) <= images.MAX_ATTEMPTS
    assert all((out / a["file"]).exists() for a in blank["attempts"])
    assert blank["vision_skipped"] == "no vision model configured"
    assert els["p1-i4"]["status"] == "small"
    assert not (out / "images" / ".work").exists()

    orders = [e["order"] for e in m["elements"] if e["page"] == 1]
    assert orders == list(range(1, len(orders) + 1))
    assert all(e["type"] in ("text", "table", "image") and len(e["bbox"]) == 4 for e in m["elements"])


@needs_tesseract
def test_scanned_page_is_ocrd(sample_pdf, tmp_path):
    m = parse_pdf.parse(sample_pdf, tmp_path, use_vision=False)
    page = m["pages"][1]
    assert page["kind"] == "scanned" and page["ocr_confidence"] > 60 and (tmp_path / "sample" / page["render"]).exists()
    text = [e for e in m["elements"] if e["page"] == 2]
    assert text and all(e["type"] == "text" and e["source"] == "ocr" for e in text)
    assert "INVOICE" in text[0]["text"]
    assert not any(e["type"] == "image" for e in text)  # the scan itself isn't an image element
    assert "INVOICE 2041" in (tmp_path / "sample" / "text.txt").read_text()


# ---------------------------------------------------------------- with the vision model

def make_vision(fake):
    return vision.Vision(fake["url"], "qwen3-vl-8b")


def test_vision_check_reads_the_model_list(fake_llm):
    v = make_vision(fake_llm)
    assert v.check()[0] is True and v.multimodal is True
    fake_llm["models"][:] = [{"id": "coder-model"}, {"id": "qwen3-asr"}]
    ok, message = make_vision(fake_llm).check()
    assert ok is False and "no model 'qwen3-vl-8b'" in message and "coder-model" in message
    fake_llm["models"][:] = [{"id": "qwen3-vl-8b"}, {"id": "other"}]
    assert make_vision(fake_llm).check()[0] is None  # can't tell without loading it


def test_vision_check_without_a_server():
    v = vision.Vision("http://127.0.0.1:9/v1/chat/completions")
    ok, message = v.check()
    assert ok is False and "didn't answer" in message
    assert vision.Vision("").check() == (False, "no vision model configured (set PDF_PARSER_LLM_URL or LLM_URL)")


def test_config_from_env():
    c = vision.config({"LLM_URL": "http://h:1/v1/chat/completions", "LLM_API_KEY": "k"})
    assert c == {"url": "http://h:1/v1/chat/completions", "model": "qwen3-vl-8b", "api_key": "k"}
    assert vision.config({"LLM_URL": "a", "PDF_PARSER_LLM_URL": "b", "PDF_PARSER_VISION_MODEL": "m"})["url"] == "b"
    assert vision.base_url("http://h:1/v1/chat/completions") == "http://h:1"
    assert vision.base_url("http://h:1/") == "http://h:1"


def test_vision_rejects_the_blank_crop_after_retries(sample_pdf, tmp_path, fake_llm):
    fake_llm["answer"] = blank_or_chart
    m = parse_pdf.parse(sample_pdf, tmp_path, make_vision(fake_llm))
    blank = by_id(m)["p1-i3"]
    assert blank["status"] == "needs_review" and len(blank["attempts"]) == images.MAX_ATTEMPTS
    assert all(a["vision"]["content_type"] == "blank" for a in blank["attempts"])
    assert by_id(m)["p1-i1"]["vision"] is None  # not flagged: not asked
    assert len(fake_llm["requests"]) == images.MAX_ATTEMPTS
    assert m["summary"]["vision"] == {"requests": 3, "failed": 0, "available": True, "stopped": None,
                                      "model": "qwen3-vl-8b"}
    body = fake_llm["requests"][0]
    assert body["model"] == "qwen3-vl-8b" and body["response_format"]["type"] == "json_schema"


def test_vision_for_all_checks_unflagged_images(sample_pdf, tmp_path, fake_llm, monkeypatch):
    monkeypatch.setenv("PDF_PARSER_VISION_ALL", "1")
    fake_llm["answer"] = blank_or_chart
    m = parse_pdf.parse(sample_pdf, tmp_path, make_vision(fake_llm))
    assert by_id(m)["p1-i1"]["vision"] == {"complete": True, "content_type": "chart", "issue": None}


def test_vision_failures_stop_asking_and_are_logged(sample_pdf, tmp_path, fake_llm, monkeypatch):
    monkeypatch.setenv("PDF_PARSER_VISION_ALL", "1")
    fake_llm["fail"] = 99
    m = parse_pdf.parse(sample_pdf, tmp_path, make_vision(fake_llm))
    assert len(fake_llm["requests"]) == vision.MAX_CONSECUTIVE_FAILURES
    v = m["summary"]["vision"]
    assert v["available"] is False and "3 vision requests in a row failed" in v["stopped"]
    els = by_id(m)
    assert els["p1-i1"]["status"] == "kept"           # no hard flags: kept without the model
    assert els["p1-i3"]["status"] == "needs_review"   # blank: would have needed it
    events = [json.loads(line) for line in (tmp_path / "sample" / "qa_log.jsonl").read_text().splitlines()]
    assert [e["ok"] for e in events if e["event"] == "vision"] == [False] * 3
    assert sum(e["event"] == "vision_stopped" for e in events) == 1
    s = parse_pdf.stats(tmp_path)
    assert s["vision_requests"] == 3 and s["vision_availability"] == 0.0 and s["pdfs_that_stopped_asking"] == 1
    assert s["needs_review_without_vision"] == 1


def test_model_without_mmproj_stops_at_the_first_image(sample_pdf, tmp_path, fake_llm):
    fake_llm["refuse_images"] = True
    v = make_vision(fake_llm)
    m = parse_pdf.parse(sample_pdf, tmp_path, v)
    assert len(fake_llm["requests"]) == 1
    assert "can't take images" in m["summary"]["vision"]["stopped"] and v.multimodal is False
    assert v.state == "unavailable"


def test_a_transient_failure_is_retried_on_the_next_attempt(sample_pdf, tmp_path, fake_llm):
    fake_llm["answer"], fake_llm["fail"] = blank_or_chart, 1
    m = parse_pdf.parse(sample_pdf, tmp_path, make_vision(fake_llm))
    assert m["summary"]["vision"]["available"] is True and m["summary"]["vision"]["failed"] == 1


def fragments_pdf(tmp_path):
    """A figure stored as two halves side by side; each half alone is a fragment."""
    full = chart(1200, 600)
    left, right = full.crop((0, 0, 600, 600)), full.crop((600, 0, 1200, 600))
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "A figure in two pieces.", fontsize=11)
    p.insert_image(pymupdf.Rect(100, 200, 250, 350), stream=png(left))
    p.insert_image(pymupdf.Rect(250, 200, 400, 350), stream=png(right))
    path = tmp_path / "fragments.pdf"
    doc.save(path)
    return path


def test_fragments_are_merged(tmp_path, fake_llm, monkeypatch):
    monkeypatch.setenv("PDF_PARSER_VISION_ALL", "1")

    def answer(im):
        if im.width / im.height < 1.5:
            return {"complete": False, "content_type": "fragment", "issue": "the chart continues past the edge"}
        return {"complete": True, "content_type": "chart", "issue": None}

    fake_llm["answer"] = answer
    m = parse_pdf.parse(fragments_pdf(tmp_path), tmp_path / "out", make_vision(fake_llm))
    first, second = by_id(m)["p1-i1"], by_id(m)["p1-i2"]
    assert [a["strategy"] for a in first["attempts"]] == ["embedded", "pad", "merge"]
    assert first["status"] == "kept" and first["merged"] == ["p1-i2"]
    assert pymupdf.Rect(first["bbox"]).contains(pymupdf.Rect(100, 200, 400, 350))  # both halves (plus the padding)
    assert second["status"] == "merged" and second["merged_into"] == "p1-i1"
    assert m["summary"]["images_kept"] == 1 and m["summary"]["images_merged"] == 1


def test_next_strategy_order():
    a = {"flags": ["clipped"], "dpi": 150, "vision": None}
    assert images._next_strategy(set(), a, True) == "hidpi"
    assert images._next_strategy({"hidpi"}, a, True) == "pad"
    assert images._next_strategy({"hidpi", "pad"}, a, True) == "merge"
    assert images._next_strategy({"hidpi", "pad"}, a, False) == "enhance"
    assert images._next_strategy(set(images.STRATEGIES), a, True) is None
    low = {"flags": ["low_ocr"], "dpi": 300, "vision": None}
    assert images._next_strategy(set(), low, False) == "hidpi"
    assert images._next_strategy({"hidpi"}, low, False) == "enhance"
    sharp = {"flags": ["odd_aspect"], "dpi": 300, "vision": None}
    assert images._next_strategy(set(), sharp, False) == "hidpi"  # nothing fits: the next in order


def test_parse_verdict():
    assert vision.parse_verdict('ok: {"complete": true, "content_type": "Photo", "issue": "null"}') == \
        {"complete": True, "content_type": "photo", "issue": None}
    with pytest.raises(vision.VisionError):
        vision.parse_verdict('{"complete": "yes"}')


# ---------------------------------------------------------------- the service

@pytest.fixture
def service(tmp_path, fake_llm):
    v = make_vision(fake_llm)
    v.check()
    jobs = web.Jobs(tmp_path / "home", v)
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(jobs, v))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield jobs, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def upload(base, name, data):
    boundary = "xyz"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(base + "/api/jobs", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_service_upload_status_and_files(service, sample_pdf):
    jobs, base = service
    health = json.loads(get(base + "/api/health")[2])
    assert health["ok"] and health["vision"]["state"] == "ok" and health["vision"]["model"] == "qwen3-vl-8b"

    code, job = upload(base, "My Report.pdf", sample_pdf.read_bytes())
    assert code == 202 and job["status"] == "queued" and job["file"] == "My Report.pdf"
    assert json.loads(get(base + f"/api/jobs/{job['id']}")[2])["status"] == "queued"
    jobs.run_one(jobs.queue.get_nowait())
    done = json.loads(get(base + f"/api/jobs/{job['id']}")[2])
    assert done["status"] == "done" and done["summary"]["tables"] == 1 and done["summary"]["pages"] == 2

    _, kind, page = get(base + done["url"])
    assert kind.startswith("text/html") and b"manifest.json" in page and b"tables/p001-t1.csv" in page
    _, _, manifest = get(base + done["url"] + "manifest.json")
    assert json.loads(manifest)["source"]["file"] == "My Report.pdf"
    _, kind, z = get(base + done["zip"])
    names = zipfile.ZipFile(io.BytesIO(z)).namelist()
    assert kind == "application/zip" and "My Report/manifest.json" in names
    with pytest.raises(urllib.error.HTTPError):
        get(base + done["url"] + "..%2F..%2Fstatus.json")
    assert json.loads(get(base + "/api/jobs")[2])["jobs"][0]["id"] == job["id"]


def test_service_rejects_what_isnt_a_pdf(service):
    _, base = service
    code, body = upload(base, "notes.pdf", b"hello")
    assert code == 400 and "isn't a PDF" in body["error"]
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base + "/api/jobs/20260101-000000-abcdef")
    assert exc.value.code == 404


def test_a_broken_pdf_fails_its_job(service):
    jobs, base = service
    _, job = upload(base, "broken.pdf", b"%PDF-1.4 not really")
    jobs.run_one(jobs.queue.get_nowait())
    st = json.loads(get(base + f"/api/jobs/{job['id']}")[2])
    assert st["status"] == "failed" and st["error"]


def test_restart_requeues_unfinished_jobs(service, sample_pdf):
    jobs, base = service
    _, job = upload(base, "a.pdf", sample_pdf.read_bytes())
    jobs.queue.get_nowait()
    again = web.Jobs(jobs.root.parent, jobs.vision)
    again.requeue()
    assert again.queue.get_nowait() == job["id"]


def test_enhance_straightens_and_leaves_blank_alone(tmp_path):
    from conftest import text_image
    from PIL import Image
    tilted = text_image(["A line of text to straighten", "and another one under it"] * 4, 1200, 1200).rotate(
        3, expand=True, fillcolor=255)
    tilted.save(tmp_path / "t.png")
    _, angle = images._enhance(tmp_path / "t.png", tmp_path / "out")
    assert angle == pytest.approx(-3, abs=0.5)
    Image.new("L", (100, 100), 250).save(tmp_path / "b.png")
    path, angle = images._enhance(tmp_path / "b.png", tmp_path / "b-out")
    assert angle == 0 and ImageStat.Stat(Image.open(path)).stddev[0] < images.BLANK_STDDEV


def test_clipped_render_is_flagged(tmp_path):
    from PIL import Image, ImageDraw
    im = Image.new("L", (300, 200), 255)
    ImageDraw.Draw(im).rectangle((100, 40, 299, 160), fill=0)  # runs into the right edge only
    im.save(tmp_path / "c.png")
    rect, page = pymupdf.Rect(0, 0, 150, 100), pymupdf.Rect(0, 0, 600, 800)
    assert "clipped" in images.check(tmp_path / "c.png", rect, [], "clip", page)[0]
    assert "clipped" not in images.check(tmp_path / "c.png", rect, [], "embedded", page)[0]
    assert images.check(tmp_path / "c.png", rect, [pymupdf.Rect(0, 0, 150, 90)], "embedded", page)[0] == ["text_overlap"]

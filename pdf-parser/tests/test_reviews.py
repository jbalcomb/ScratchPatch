import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import reviews
import web

SHA = "a" * 64
TEXT = {"id": "p1-text1", "type": "text", "page": 1, "bbox": [10, 10, 110, 30], "source": "text_layer",
        "text": "Getting Started"}


def rv(by, decision, at, **kw):
    return {"element": TEXT["id"], "by": by, "decision": decision, "at": at, **kw}


def test_states_follow_the_tiebreaker_rule():
    assert reviews.state([], TEXT) == ("unreviewed", None)
    a = rv("ann", "accepted", "1")
    assert reviews.state([a], TEXT) == ("reviewed", a)
    b = rv("bob", "accepted", "2")
    assert reviews.state([a, b], TEXT) == ("confirmed", b)
    fix = rv("bob", "corrected", "2", text="Getting  Started!")
    assert reviews.state([a, fix], TEXT) == ("disputed", fix)               # the newest is shown
    tie = rv("cat", "accepted", "3")
    assert reviews.state([a, fix, tie], TEXT) == ("settled", tie)           # two of three agree
    other = rv("cat", "rejected", "3")
    assert reviews.state([a, fix, other], TEXT) == ("escalated", other)     # all three differ
    owner = rv("jim", "corrected", "4", text="Getting Started!")
    assert reviews.state([a, fix, other, owner], TEXT) == ("settled", owner)


def test_agreement():
    a = rv("ann", "corrected", "1", text="Getting   Started")
    b = rv("bob", "corrected", "2", text="Getting Started", bbox=[10, 10, 111, 30])
    assert reviews.agree(a, b, TEXT)                       # whitespace ignored; boxes overlap > 90%
    assert not reviews.agree(a, {**b, "bbox": [10, 10, 60, 30]}, TEXT)
    assert not reviews.agree(a, {**b, "text": "Getting Started."}, TEXT)
    assert not reviews.agree({**a, "rows": [["x"]]}, {**b, "rows": [["y"]]}, TEXT)
    assert not reviews.agree(rv("a", "accepted", "1"), rv("b", "rejected", "2"), TEXT)


def test_store(tmp_path):
    store = reviews.Store(tmp_path, SHA, "doc.pdf")
    ids = {"p1-text1", "p1-table2"}
    store.add({"element": "p1-text1", "decision": "accepted", "by": "ann"}, ids)
    store.add({"element": "p1-text1", "decision": "corrected", "by": "ann", "text": "x", "bbox": [1, 2, 3, 4]}, ids)
    data = store.load()
    assert [(r["by"], r["decision"]) for r in data["reviews"]] == [("ann", "corrected")]  # own review replaced
    assert data["document"] == {"file": "doc.pdf", "sha256": SHA}
    assert (tmp_path / "reviews" / f"{SHA}.json").exists()
    for bad, message in [({"element": "p1-text1", "decision": "accepted"}, "reviewer"),
                         ({"element": "nope", "decision": "accepted", "by": "a"}, "no element"),
                         ({"element": "p1-text1", "decision": "maybe", "by": "a"}, "decision"),
                         ({"element": "p1-text1", "decision": "corrected", "by": "a"}, "needs"),
                         ({"element": "p1-text1", "decision": "corrected", "by": "a", "bbox": [5, 5, 1, 1]}, "bbox")]:
        with pytest.raises(reviews.ReviewError, match=message):
            store.add(bad, ids)
    with pytest.raises(reviews.ReviewError):
        reviews.Store(tmp_path, "../etc")


MANIFEST = {"source": {"file": "doc.pdf", "sha256": SHA}, "pages": [{"page": 1}, {"page": 2}], "elements": [
    {**TEXT},
    {"id": "p1-i2", "type": "image", "page": 1, "bbox": [0, 40, 50, 90], "status": "kept",
     "vision": {"complete": True, "content_type": "chart", "issue": None}},
    {"id": "p2-text1", "type": "text", "page": 2, "bbox": [0, 0, 10, 10], "source": "ocr", "text": "abcd",
     "confidence": 40, "low_confidence": True},
    {"id": "p2-text2", "type": "text", "page": 2, "bbox": [0, 20, 10, 30], "source": "vision", "text": "Title",
     "ocr_text": "Tit1e"},
    {"id": "p2-table3", "type": "table", "page": 2, "bbox": [0, 40, 10, 50], "file": "tables/p002-t1.csv"},
    {"id": "p2-i4", "type": "image", "page": 2, "bbox": [0, 60, 10, 70], "status": "duplicate"},
]}


def test_queues_and_coverage():
    data = {"reviews": [rv("ann", "accepted", "1"),
                        {"element": "p1-i2", "by": "ann", "decision": "accepted", "at": "1"},
                        {"element": "p2-text2", "by": "ann", "decision": "corrected", "at": "1", "text": "Title!"},
                        {"element": "p2-text2", "by": "bob", "decision": "accepted", "at": "2"}]}
    els = reviews.annotate(MANIFEST, data)
    assert [e["id"] for e in els] == ["p1-text1", "p1-i2", "p2-text1", "p2-text2", "p2-table3"]  # no duplicate
    assert {e["id"]: e["queue"] for e in els} == {"p1-text1": "other", "p1-i2": "vision", "p2-text1": "needs_review",
                                                 "p2-text2": "vision", "p2-table3": "other"}
    c = reviews.coverage(MANIFEST, els)
    assert c["pages"] == 50.0 and c["pages_total"] == 2          # page 1 all checked; page 2 not
    assert c["text"] == round(100 * len("Getting Started") / len("Getting Startedabcd" + "Title"), 1)
    assert c["text_by_source"] == {"ocr": 0.0, "text_layer": 100.0, "vision": 0.0}
    assert (c["tables"], c["images"], c["mixed_pages"]) == (0.0, 100.0, 100.0)
    assert c["required"] == round(100 / 3, 1) and c["required_left"] == 2   # p2-text2 is disputed: not checked
    assert c["disputed"] == 1 and c["escalated"] == 0
    assert c["accuracy"]["text_layer"] == {"checked": 1, "accepted": 100.0}
    assert c["accuracy"]["vision"] == {"checked": 0, "accepted": None}


# ---------------------------------------------------------------- through the service

@pytest.fixture
def done_job(tmp_path, sample_pdf):
    jobs = web.Jobs(tmp_path / "home", vision=None)
    job = jobs.create("sample.pdf", sample_pdf.read_bytes())
    jobs.run_one(jobs.queue.get_nowait())
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(jobs, vision=None))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield jobs, f"http://127.0.0.1:{server.server_port}", job["id"]
    server.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.headers.get("Content-Type"), r.read()


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_review_page_data_and_saving(done_job):
    jobs, base, job = done_job
    kind, page = get(f"{base}/jobs/{job}/review/")
    assert kind.startswith("text/html") and b"Next in queue" in page
    assert b"/review/" in get(f"{base}/jobs/{job}/")[1]                    # linked from the bundle page
    kind, png = get(f"{base}/jobs/{job}/page/1.png")
    assert kind == "image/png" and png.startswith(b"\x89PNG")
    data = json.loads(get(f"{base}/api/jobs/{job}/review")[1])
    assert data["file"] == "sample.pdf" and len(data["pages"]) == 2
    title = data["elements"][0]
    assert title["review"] == {"state": "unreviewed", "applied": None, "reviews": []}
    assert data["coverage"]["pages_total"] == 2

    code, res = post(f"{base}/api/jobs/{job}/reviews", {"element": title["id"], "decision": "corrected",
                                                       "by": "ann", "text": "Quarterly report"})
    assert code == 200 and res["element"]["review"]["state"] == "reviewed"
    code, res = post(f"{base}/api/jobs/{job}/reviews", {"element": title["id"], "decision": "accepted", "by": "bob"})
    assert res["element"]["review"]["state"] == "disputed" and res["coverage"]["disputed"] == 1
    sha = json.loads((jobs.bundle_dir(job) / "manifest.json").read_text())["source"]["sha256"]
    assert len(json.loads((jobs.root.parent / "reviews" / f"{sha}.json").read_text())["reviews"]) == 2

    code, res = post(f"{base}/api/jobs/{job}/reviews", {"element": title["id"], "decision": "accepted"})
    assert code == 400 and "reviewer" in res["error"]
    with pytest.raises(urllib.error.HTTPError):
        get(f"{base}/jobs/{job}/page/99.png")


def test_the_same_pdf_uploaded_again_shares_its_reviews(done_job, sample_pdf):
    jobs, base, job = done_job
    first = json.loads(get(f"{base}/api/jobs/{job}/review")[1])["elements"][0]["id"]
    post(f"{base}/api/jobs/{job}/reviews", {"element": first, "decision": "accepted", "by": "ann"})
    again = jobs.create("sample copy.pdf", sample_pdf.read_bytes())
    jobs.run_one(jobs.queue.get_nowait())
    data = json.loads(get(f"{base}/api/jobs/{again['id']}/review")[1])
    assert data["elements"][0]["review"]["state"] == "reviewed"

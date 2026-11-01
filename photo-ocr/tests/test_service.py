import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

import pytest

from fakes import RECEIPT, FakeGoogle, FakeImmich, FakeModel, png

import bakeoff
import catalog
import checks
import immich
import reconcile
import web
from picker import Picker
from service import Service

TODAY = date(2026, 10, 31)


@pytest.fixture
def model():
    m = FakeModel()
    yield m
    m.close()


@pytest.fixture
def immich_fake():
    f = FakeImmich()
    yield f
    f.close()


def make_service(tmp_path, model, engines=("vl-a",), photos=None, **env):
    eng = tmp_path / "engines.json"
    eng.write_text(json.dumps({"engines": [
        {"name": "vl-a", "kind": "vlm", "url": model.url, "model": "a"},
        {"name": "vl-b", "kind": "vlm", "url": model.url, "model": "b"},
    ]}))
    env = {"RECEIPTS_ENGINE": ",".join(engines), "LLM_URL": model.url, "LLM_MODEL": "text", **env}
    return Service(tmp_path / "home", engines_path=eng, photos=photos, env=env)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    class D(date):
        @classmethod
        def today(cls):
            return TODAY
    monkeypatch.setattr(checks, "date", D)
    monkeypatch.setattr(catalog, "date", D)
    monkeypatch.setattr(reconcile, "date", D)


# ------------------------------------------------------------ checks and buckets

def test_buckets():
    known = {"gianteagle"}
    a = checks.assess(RECEIPT, {}, known)
    assert a["bucket"] == "auto" and a["suspect"] == [] and all(c["ok"] for c in a["checks"])

    a = checks.assess(RECEIPT, {}, set())  # a new store
    assert a["bucket"] == "review" and a["suspect"] == ["merchant"]

    bad_sum = dict(RECEIPT, total=7.91)
    a = checks.assess(bad_sum, {}, known)
    assert a["bucket"] == "review" and {"total", "items"} <= set(a["suspect"])
    assert next(c for c in a["checks"] if c["name"] == "sum")["detail"] == "items + tax = 7.19, total = 7.91"

    assert checks.assess(dict(RECEIPT, total=None), {}, known)["bucket"] == "retake"
    assert checks.assess(dict(RECEIPT, date="13/45/2026"), {}, known)["bucket"] == "retake"
    assert checks.assess(dict(RECEIPT, date="2019-01-01"), {}, known)["bucket"] == "review"
    assert checks.assess(dict(RECEIPT, tax="forty"), {}, known)["bucket"] == "review"


def test_disagreement_marks_fields():
    other = dict(RECEIPT, total=7.91)
    a = checks.assess(RECEIPT, {"a": RECEIPT, "b": other}, {"gianteagle"})
    assert a["bucket"] == "review"
    assert a["disagreements"] == {"total": {"a": 719, "b": 791}}
    assert a["confidence"]["total"] == 0.4 and a["suspect"] == ["total"]


def test_ocr_confidence_lowers_a_field():
    lines = [{"text": "TOTAL 7.19", "conf": 0.31}, {"text": "BANANAS 1.29", "conf": 0.95}, None]
    a = checks.assess(RECEIPT, {}, {"gianteagle"}, ocr_lines=lines)
    assert a["confidence"]["total"] == 0.31 and a["bucket"] == "review"


# ------------------------------------------------------------ capture, parse, review

def capture(svc, *shades):
    return svc.create_receipt([(f"IMG_{s}.jpg", png(s)) for s in shades])


def test_capture_parse_review_approve(tmp_path, model):
    svc = make_service(tmp_path, model)
    rid = capture(svc, 1, 2)
    assert svc.receipt(rid)["status"] == "pending"
    assert svc.parse_pending() == 1
    # Both photos went to the model in one request.
    req = json.loads(model.requests[0][2])
    assert sum(1 for c in req["messages"][0]["content"] if c.get("type") == "image_url") == 2

    r = svc.receipt(rid)
    assert (r["status"], r["bucket"], r["suspect"]) == ("parsed", "review", ["merchant"])  # new store
    assert (r["merchant"], r["total"], r["tax"], r["location"]) == ("Giant Eagle", 719, 42, "Mentor, OH")
    assert [i["raw_text"] for i in r["items"]] == ["BANANAS", "MILK 2% GAL", "BREAD WHT", "BONUS SAVINGS"]
    assert r["items"][3]["line_total"] == -100

    items = [{"raw_text": i["raw_text"], "quantity": i["quantity"], "line_total": i["line_total"]} for i in r["items"]]
    items[1]["raw_text"] = "MILK 2% GALLON"
    r = svc.edit(rid, {"merchant": "Giant Eagle #4", "purchase_date": "2026-10-12", "items": items, "approve": True})
    assert r["status"] == "approved"
    fixes = svc.store.q("SELECT field, before, after FROM corrections WHERE receipt_id = ?", rid)
    assert {f["field"] for f in fixes} == {"merchant", "items"}
    assert svc.known_stores() == {"gianteagle4", "gianteagle"}

    # The same store next time: everything checks out, so it's saved without review.
    rid2 = capture(svc, 3)
    svc.parse_pending()
    r2 = svc.receipt(rid2)
    assert (r2["status"], r2["bucket"], r2["merchant"]) == ("approved", "auto", "Giant Eagle #4")


def test_two_engines_disagree(tmp_path, model):
    model.by_model["b"] = dict(RECEIPT, total=7.91)
    svc = make_service(tmp_path, model, engines=("vl-a", "vl-b"))
    rid = capture(svc, 1)
    svc.parse_pending()
    r = svc.receipt(rid)
    assert r["bucket"] == "review" and "total" in r["suspect"]
    assert r["disagreements"]["total"] == {"vl-a": 719, "vl-b": 791}
    assert r["total"] == 719  # the first engine's reading is used
    assert {x["engine"] for x in r["runs"]} == {"vl-a", "vl-b"}


def test_retake_and_failure(tmp_path, model):
    model.receipt = dict(RECEIPT, total=None)
    svc = make_service(tmp_path, model)
    rid = capture(svc, 1)
    svc.parse_pending()
    assert svc.receipt(rid)["bucket"] == "retake"
    assert svc.counts()["retake"] == 1

    model.fail = True
    svc.add_photos(rid, [("again.jpg", png(9))])
    assert svc.receipt(rid)["photos"] == 2
    svc.parse_pending()
    r = svc.receipt(rid)
    assert r["status"] == "failed" and "HTTP 500" in r["error"]


def test_model_server_down_keeps_receipts_pending(tmp_path, model):
    svc = make_service(tmp_path, model)
    rid = capture(svc, 1)
    url = model.url
    model.close()
    svc.parse_pending()
    assert svc.receipt(rid)["status"] == "pending" and svc.store.q("SELECT * FROM engine_runs") == []
    assert url


def test_merge(tmp_path, model):
    svc = make_service(tmp_path, model)
    a, b = capture(svc, 1), capture(svc, 2)
    svc.parse_pending()
    svc.merge(b, a)
    r = svc.receipt(a)
    assert r["status"] == "pending" and [i["position"] for i in r["images"]] == [1, 2]
    with pytest.raises(Exception):
        svc.receipt(b)


def test_edit_rejects_bad_values(tmp_path, model):
    svc = make_service(tmp_path, model)
    rid = capture(svc, 1)
    with pytest.raises(ValueError):
        svc.edit(rid, {"total": "7.19"})
    with pytest.raises(ValueError):
        svc.edit(rid, {"purchase_date": "10/12/2026"})


# ------------------------------------------------------------ catalog

def approve_receipt(svc, rid):
    svc.edit(rid, {"approve": True})


def test_aliases_products_and_shopping_list(tmp_path, model):
    svc = make_service(tmp_path, model)
    milk = svc.create_product([("milk.jpg", png(50))])
    assert svc.product(milk)["status"] == "pending"
    svc.read_pending_products()
    p = svc.product(milk)
    assert (p["name"], p["brand"], p["size"], p["status"]) == ("Whole milk", "Great Value", "1 gal", "ready")

    # Three trips a week apart, the last 9 days before "today".
    days = [TODAY - timedelta(days=d) for d in (23, 16, 9)]
    # No alias yet, and the text is too abbreviated for a sure text match: the model is asked to pick.
    model.pick = milk
    rids = []
    for n, d in enumerate(days):
        model.receipt = dict(RECEIPT, date=d.isoformat(), items=[{"name": "GV WHL MLK GAL", "price": 3.29 + n * 0.1},
                                                                   {"name": "BAG FEE", "price": 0.10}],
                             tax=0, total=round(3.39 + n * 0.1, 2))
        rids.append(capture(svc, 10 + n))
        svc.parse_pending()
    unmatched = svc.unmatched_items()
    assert len(unmatched) == 6
    assert unmatched[0]["suggestion"]["product_id"] == milk and unmatched[0]["suggestion"]["by"] == "model"

    milk_item = next(i for i in unmatched if i["raw_text"] == "GV WHL MLK GAL")
    assert catalog.confirm_match(svc.store, milk_item["id"], product_id=milk)["matched"] == 3
    bag = next(i for i in unmatched if i["raw_text"] == "BAG FEE")
    assert catalog.confirm_match(svc.store, bag["id"], skip=True)["matched"] == 3
    assert svc.unmatched_items() == []

    for rid in rids:
        approve_receipt(svc, rid)
    p = svc.product(milk)
    assert p["aliases"] == ["GV WHL MLK GAL"] and [h["line_total"] for h in p["history"]] == [349, 339, 329]

    sl = catalog.shopping_list(svc.store)
    assert [(i["name"], i["every_days"], i["overdue_days"], i["usual_store"], i["best_price"]) for i in sl["items"]] == \
        [("Whole milk", 7, 2, "Giant Eagle", 329)]

    # The alias matches by itself next time.
    model.receipt = dict(RECEIPT, items=[{"name": "GV WHL MLK GAL", "price": 3.29}], tax=0, total=3.29)
    rid = capture(svc, 30)
    svc.parse_pending()
    assert svc.receipt(rid)["items"][0]["match"] == "alias"


def test_text_suggestion_and_product_merge(tmp_path, model):
    svc = make_service(tmp_path, model)
    bananas = svc.create_product([("b.jpg", png(60))], name="Bananas")
    dup = svc.create_product([("b2.jpg", png(61))], name="Banana bunch")
    model.receipt = dict(RECEIPT, items=[{"name": "BANANAS", "price": 1.29}], tax=0, total=1.29)
    capture(svc, 1)
    svc.parse_pending()
    s = svc.unmatched_items()[0]["suggestion"]
    assert s["product_id"] == bananas and s["by"] == "text" and s["score"] == 1.0
    merged = svc.merge_products(dup, bananas)
    assert len(merged["images"]) == 2


# ------------------------------------------------------------ Immich

def test_immich_upload_poll_and_syncthing(tmp_path, model, immich_fake):
    client = immich.Immich(immich_fake.url, "k1")
    svc = make_service(tmp_path, model, photos=client, IMMICH_LIBRARY_ID="lib1")
    rid = capture(svc, 1, 2)
    album = next(a for a in immich_fake.albums.values() if a["albumName"] == "Receipts")
    assert len(album["assets"]) == 2 and immich_fake.key_seen == {"k1"}

    # A photo put straight into the Receipts album, and one synced into the external library.
    immich_fake.add_asset(png(40), "IMG_9.jpg", album="Receipts")
    immich_fake.add_asset(png(41), "IMG_10.jpg", path="/sync/phone/Receipts/IMG_10.jpg", library="lib1")
    immich_fake.add_asset(png(42), "cat.jpg", path="/sync/phone/Camera/cat.jpg", library="lib1")
    immich_fake.add_asset(png(43), "soap.jpg", path="/sync/phone/Products/soap.jpg", library="lib1")
    for p in svc.cache.iterdir():  # photos come back from Immich, not the local copy
        p.unlink()
    done = svc.work_once()
    assert done["library"] == 2 and done["albums"] == 3 and done["receipts"] == 3 and done["products"] == 1
    vias = {r["via"] for r in svc.store.q("SELECT via FROM images WHERE receipt_id IS NOT NULL")}
    assert vias == {"capture", "immich", "syncthing"}
    assert svc.receipt(rid)["status"] == "parsed"
    assert svc.work_once()["albums"] == 0  # nothing new the second time


def test_upload_without_device_fields(tmp_path):
    """Newer Immich servers refuse the device fields; the upload is sent again without them."""
    calls = []

    def handle(method, path, query, headers, body):
        calls.append(body)
        if b"deviceAssetId" in body:
            return 400, {"message": ["property deviceAssetId should not exist"]}, None
        return 201, {"id": "a1", "status": "created"}, None

    from fakes import Server
    srv = Server(handle)
    try:
        assert immich.Immich(srv.url, "k").upload(png(), "x.jpg") == "a1"
        assert len(calls) == 2
    finally:
        srv.close()


# ------------------------------------------------------------ HTTP API and pages

@pytest.fixture
def api(tmp_path, model):
    svc = make_service(tmp_path, model)
    google = FakeGoogle([("g1.png", png(70)), ("g2.png", png(71))])
    pk = Picker(svc.store, "cid", "secret", "https://ws.example.ts.net", api=google.url + "/v1", token_url=google.url + "/token")
    app = web.App(svc, pk)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(method, path, body=None, ctype=None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data, method=method,
                                     headers={"Content-Type": ctype or "application/json"} if data is not None else {})
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers["Content-Type"] == "application/json" else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    call.svc, call.google, call.app, call.base = svc, google, app, base
    yield call
    httpd.shutdown()
    google.close()


def form(files, **fields):
    from immich import multipart
    return multipart(fields, [(name, fn, data, "image/png") for name, fn, data in files])


def test_api_receipt_flow(api):
    body, ctype = form([("photo", "a.png", png(1)), ("photo", "b.png", png(2))], note="lunch")
    status, r = api("POST", "/api/receipts", body, ctype)
    assert status == 201 and r == {"id": 1, "photos": 2, "status": "pending"}
    api.svc.parse_pending()
    status, r = api("GET", "/api/receipts?status=parsed")
    assert [x["id"] for x in r["receipts"]] == [1] and r["receipts"][0]["url"] == "/review/1"
    status, full = api("GET", "/api/receipts/1")
    assert full["note"] == "lunch" and len(full["images"]) == 2
    status, img = api("GET", full["images"][1]["url"])
    assert img == png(2)
    status, r = api("POST", "/api/receipts/1", {"total": 720, "approve": True})
    assert r["status"] == "approved" and r["total"] == 720
    status, s = api("GET", "/api/summary")
    assert s["approved"] == 1 and s["review"] == 0
    assert api("GET", "/api/health")[1] == {"ok": True, "immich": None, "engines": ["vl-a"], "version": 1}


def test_api_errors(api):
    assert api("GET", "/api/receipts/99") == (404, {"error": "no receipt 99"})
    assert api("POST", "/api/receipts", b"{}")[0] == 400
    body, ctype = form([("photo", "notes.txt", b"hi")])
    assert api("POST", "/api/receipts", body, ctype) == (400, {"error": "notes.txt: not a photo (JPEG, PNG, WebP or HEIC)"})
    assert api("DELETE", "/api/summary")[0] == 405
    assert api("GET", "/nope")[0] == 404
    assert api("POST", "/api/items/5/match", {})[0] == 400


def test_pages_render(api):
    api.svc.create_receipt([("a.png", png(1))])
    api.svc.create_product([("p.png", png(2))], name="Soap")
    for path in ["/", "/capture", "/review", "/review/1", "/items", "/products", "/products/1", "/shopping", "/reconcile",
                 "/picker", "/manifest.webmanifest", "/sw.js", "/icon.svg"]:
        status, body = api("GET", path)
        assert status == 200, path
    assert b"Add another photo" in api("GET", "/capture")[1]


def test_api_products_and_items(api, model):
    body, ctype = form([("photo", "milk.png", png(5))])
    assert api("POST", "/api/products", body, ctype) == (201, {"id": 1, "status": "pending"})
    api.svc.read_pending_products()
    api.svc.create_receipt([("r.png", png(6))])
    api.svc.parse_pending()
    items = api("GET", "/api/items/unmatched")[1]["items"]
    status, res = api("POST", f"/api/items/{items[0]['id']}/match", {"new_product": {"name": "Bananas"}})
    assert status == 200 and res["matched"] == 1
    assert {p["name"] for p in api("GET", "/api/products")[1]["products"]} == {"Whole milk", "Bananas"}
    assert api("GET", "/api/products?q=milk")[1]["products"][0]["brand"] == "Great Value"
    assert api("POST", "/api/products/1", {"size": "half gal"})[1]["size"] == "half gal"
    assert api("GET", "/api/shopping-list")[1]["items"] == []


def test_google_photos_picker(api):
    assert api("GET", "/api/picker/status")[1] == {"configured": True, "signed_in": False, "auth_url": "/picker/auth"}
    assert api("POST", "/api/picker/sessions", {"album": "receipts"})[0] == 400  # not signed in
    pk = api.app.picker
    url = pk.auth_url()
    assert "photospicker.mediaitems.readonly" in url and "access_type=offline" in url
    state = api.svc.store.get("google_oauth_state")
    with pytest.raises(Exception):
        pk.finish_auth("the-code", "forged")
    pk.finish_auth("the-code", state)
    assert pk.signed_in

    status, s = api("POST", "/api/picker/sessions", {"album": "receipts"})
    assert status == 200 and s["picker_uri"].startswith("https://photos.google.com/")
    api.app.picker_threads[s["id"]].join(10)
    assert api("GET", f"/api/picker/sessions/{s['id']}")[1] == {"state": "done", "imported": 2, "error": None}
    assert [r["via"] for r in api.svc.store.q("SELECT via FROM images")] == ["picker", "picker"]
    assert api.svc.counts()["pending"] == 2 and api.google.deleted == ["sess1"]


# ------------------------------------------------------------ reconciliation and orders

BOA = ('Description,,Summary Amt.\r\nBeginning balance as of 10/01/2026,,"100.00"\r\n\r\n'
       'Date,Description,Amount,Running Bal.\r\n'
       '10/01/2026,Beginning balance as of 10/01/2026,,"100.00"\r\n'
       '10/13/2026,"GIANT EAGLE #4 10/12 PURCHASE MENTOR OH","-7.19","92.81"\r\n'
       '10/14/2026,"SUNOCO 0408780500 PURCHASE","-20.00","72.81"\r\n'
       '10/16/2026,"INSTACART*ORDER SAN FRANCISCO CA","-52.10","20.71"\r\n')


def test_reconcile(api, model, tmp_path):
    api.svc.create_receipt([("r.png", png(1))])
    api.svc.parse_pending()
    api.svc.edit(1, {"approve": True})
    status, r = api("POST", "/api/bank/import?account=checking", BOA.encode(), "text/csv")
    assert r == {"rows": 3, "new": 3}
    assert api("POST", "/api/bank/import", BOA.encode(), "text/csv")[1] == {"rows": 3, "new": 0}

    model.order = {"store": "Instacart (Aldi)", "date": "2026-10-15", "tax": 1.10,
                   "items": [{"name": "Eggs", "price": 3.00}, {"name": "Service fee", "price": 48.00}], "total": 52.10}
    eml = (b"From: orders@instacart.com\r\nSubject: Your order receipt\r\nContent-Type: text/html\r\n\r\n"
           b"<html><body><p>Eggs $3.00</p><br>Total $52.10</body></html>")
    status, o = api("POST", "/api/orders", eml, "message/rfc822")
    assert status == 201
    api.svc.parse_pending()
    order = api.svc.receipt(o["id"])
    assert order["source"] == "instacart" and order["total"] == 5210 and "Eggs $3.00" in order["order_text"]
    api.svc.edit(o["id"], {"approve": True})

    assert api("POST", "/api/reconcile/run", {})[1] == {"matched": 2}
    rec = api("GET", "/api/reconcile")[1]
    assert rec["receipts_unmatched"] == [] and [b["description"] for b in rec["bank_unmatched"]] == ["SUNOCO 0408780500 PURCHASE"]
    assert api("POST", "/api/reconcile/match", {"receipt_id": 1, "unmatch": True})[1] == {"ok": True}
    assert len(api("GET", "/api/reconcile")[1]["receipts_unmatched"]) == 1

    budget = tmp_path / "budget.db"
    con = sqlite3.connect(budget)
    con.execute("CREATE TABLE transactions (account TEXT, date TEXT, description TEXT, amount INTEGER, balance INTEGER)")
    con.execute("INSERT INTO transactions VALUES ('bills', '2026-10-20', 'FIRSTENERGY', -9000, 1000)")
    con.commit()
    con.close()
    api.svc.budget_db = str(budget)
    assert api("POST", "/api/bank/sync-budget", {})[1] == {"rows": 1, "new": 1}


def test_bank_csv_needs_a_header():
    with pytest.raises(ValueError):
        reconcile.parse_csv("just,some\n1,2\n")
    rows = reconcile.parse_csv("Date,Description,Amount\n2026-10-01,COFFEE,-3.50\n")
    assert rows == [{"date": "2026-10-01", "description": "COFFEE", "amount": -350, "balance": None}]


# ------------------------------------------------------------ the labeled set grows

def test_export_labels_feeds_the_bakeoff(tmp_path, model, capsys):
    svc = make_service(tmp_path, model)
    rid = capture(svc, 1, 2)
    svc.parse_pending()
    svc.edit(rid, {"approve": True})
    set_dir = tmp_path / "bakeoff" / "set"
    assert svc.export_labels(set_dir) == 1 and svc.export_labels(set_dir) == 0
    folder = set_dir / f"s{rid:04d}"
    assert sorted(p.name for p in folder.iterdir()) == ["1.jpg", "2.jpg", "label.json", "tags.txt"]
    assert bakeoff.main(["--home", str(tmp_path / "bakeoff"), "check"]) == 0
    assert bakeoff.score(json.loads((folder / "label.json").read_text()), RECEIPT)["all"]

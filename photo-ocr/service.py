"""The photo OCR service's work, without the HTTP: photos of receipts and products in, records out (PRD.md).

web.py serves this over HTTP; a background worker (Service.work_once in a loop) parses what's pending and
watches Immich for new photos.
"""
import json
import logging
import os
import re
import shutil
import threading
from datetime import date
from pathlib import Path

import catalog
import timing
import checks
import immich
import reconcile
from db import Store, loads, now
from parsing import load_engines, norm_store, ocr_photos, parse_photos

log = logging.getLogger("photo-ocr")
HERE = Path(__file__).resolve().parent
ALBUMS = {"receipts": os.environ.get("PHOTO_OCR_RECEIPTS_ALBUM", "Receipts"),
          "products": os.environ.get("PHOTO_OCR_PRODUCTS_ALBUM", "Products")}


class NotFound(Exception):
    pass


class Service:
    def __init__(self, home, engines_path=None, photos=None, env=None):
        env = os.environ if env is None else env
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.cache = self.home / "cache"
        self.cache.mkdir(exist_ok=True)
        self.store = Store(self.home / "photo-ocr.db")
        self.photos = photos if photos is not None else immich.from_env(self.home)
        self.engines = load_engines(engines_path or env.get("PHOTO_OCR_ENGINES") or HERE / "engines.json")
        # Which engines read each kind of photo; the first one's reading is used, the others are second opinions.
        self.parse_engines = self._engine_list(env, "PHOTO_OCR_RECEIPT_ENGINE", [next(iter(self.engines))])
        self.product_engines = self._engine_list(env, "PHOTO_OCR_PRODUCT_ENGINE", [e["name"] for e in self.parse_engines])
        url = env.get("PHOTO_OCR_LLM_URL") or env.get("LLM_URL")
        self.text_engine = {"name": "text", "kind": "text", "url": url,
                            # The vision model also does text, so receipt work never swaps the coding model in.
                            "model": env.get("PHOTO_OCR_TEXT_MODEL") or "qwen3-vl-8b"} if url else None
        self.library_id = env.get("IMMICH_LIBRARY_ID")
        self.budget_db = env.get("BUDGET_DB") or str(Path.home() / "budget" / "budget.db")
        self.wake = threading.Event()
        self.work_lock = threading.Lock()

    def _engine_list(self, env, var, default):
        names = [n.strip() for n in (env.get(var) or "").split(",") if n.strip()] or default
        unknown = [n for n in names if n not in self.engines]
        if unknown:
            raise ValueError(f"{var} names {', '.join(unknown)}, not in engines.json ({', '.join(self.engines)})")
        return [self.engines[n] for n in names]

    # ------------------------------------------------------------ photos

    def _add_image(self, data, filename, album, via, receipt_id=None, product_id=None, position=None, asset_id=None):
        if asset_id is None:
            asset_id = self.photos.upload(data, filename, album=ALBUMS[album])
        if self.store.one("SELECT id FROM images WHERE immich_asset_id = ?", asset_id):
            raise ValueError(f"{filename} is already stored (Immich has the same photo)")
        cache_path = None
        if data is not None:
            cache_path = str(self.cache / f"{asset_id}{Path(filename).suffix.lower() or '.jpg'}")
            Path(cache_path).write_bytes(data)
        return self.store.x(
            "INSERT INTO images (receipt_id, product_id, position, immich_asset_id, album, via, source_path, cache_path, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", receipt_id, product_id, position, asset_id, album, via, filename, cache_path, now())

    def image_path(self, image_id):
        """A local file for a stored photo, fetched from Immich the first time."""
        img = self.store.one("SELECT * FROM images WHERE id = ?", image_id)
        if not img:
            raise NotFound(f"no image {image_id}")
        if img["cache_path"] and Path(img["cache_path"]).exists():
            return Path(img["cache_path"])
        suffix = Path(img["source_path"] or "").suffix.lower() or ".jpg"
        path = self.cache / f"{img['immich_asset_id']}{suffix}"
        with timing.Timer("photo from Immich"):
            path.write_bytes(self.photos.download(img["immich_asset_id"]))
        self.store.x("UPDATE images SET cache_path = ? WHERE id = ?", str(path), image_id)
        return path

    SIZES = (200, 400, 800, 1200, 1600)  # widths a page can ask for; anything else rounds up to one of these

    def image_resized(self, image_id, width):
        """A smaller copy of a stored photo for a page: at most ``width`` wide, turned upright (EXIF), JPEG.
        Made once, then kept in the cache. The original when Pillow can't (or the photo is already small)."""
        original = self.image_path(image_id)
        w = next((s for s in self.SIZES if s >= width), self.SIZES[-1])
        sized = self.cache / "sized" / f"{image_id}-{w}.jpg"
        if sized.exists():
            timing.note("ready-made")
            return sized
        try:
            from PIL import Image, ImageOps
        except ImportError:
            return original
        try:
            with timing.Timer("shrunk"), Image.open(original) as im:
                if im.format == "JPEG":
                    im.draft("RGB", (w, w))  # decode at 1/2, 1/4 or 1/8 size when that is still at least w: far faster
                im = ImageOps.exif_transpose(im)
                if im.width > w:
                    im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
                sized.parent.mkdir(exist_ok=True)
                tmp = sized.with_suffix(".part")
                im.convert("RGB").save(tmp, "JPEG", quality=82, optimize=True)
                tmp.replace(sized)
        except Exception as e:  # noqa: BLE001 - a photo Pillow can't read: send the original
            log.warning("image %s: no smaller copy: %s", image_id, e)
            return original
        return sized

    PAGE_SIZES = (400, 1200)  # what the pages ask for: the gallery's tiles and the photo beside the fields

    def make_page_copies(self, limit=50):
        """Background work: the copies the pages will ask for, made before anyone opens a page."""
        made = 0
        for img in self.store.q("SELECT id FROM images ORDER BY id DESC"):
            for w in self.PAGE_SIZES:
                if made >= limit:
                    return made
                if not (self.cache / "sized" / f"{img['id']}-{w}.jpg").exists():
                    try:
                        self.image_resized(img["id"], w)
                        made += 1
                    except Exception as e:  # noqa: BLE001 - e.g. Immich down: the page makes it later
                        log.warning("image %s: no copy for pages yet: %s", img["id"], e)
                        break
        return made

    def receipt_photos(self, receipt_id):
        return [self.image_path(i["id"]) for i in
                self.store.q("SELECT id FROM images WHERE receipt_id = ? ORDER BY position, id", receipt_id)]

    def product_photos(self, product_id):
        return [self.image_path(i["id"]) for i in
                self.store.q("SELECT id FROM images WHERE product_id = ? ORDER BY position, id", product_id)]

    # ------------------------------------------------------------ receipts in

    def create_receipt(self, photos, via="capture", source="in-store", note=None, asset_ids=None):
        """photos: [(filename, bytes)] in order, top of the receipt first -> receipt id. One receipt, many photos."""
        if not photos:
            raise ValueError("no photos")
        rid = self.store.x("INSERT INTO receipts (created_at, status, source, note) VALUES (?, 'pending', ?, ?)",
                           now(), source, note)
        try:
            for n, (filename, data) in enumerate(photos, 1):
                self._add_image(data, filename, "receipts", via, receipt_id=rid, position=n,
                                asset_id=asset_ids[n - 1] if asset_ids else None)
        except Exception:
            self.store.x("DELETE FROM receipts WHERE id = ?", rid)
            raise
        self.wake.set()
        return rid

    def add_photos(self, rid, photos, via="capture"):
        self._receipt_row(rid)
        last = self.store.one("SELECT COALESCE(MAX(position), 0) AS n FROM images WHERE receipt_id = ?", rid)["n"]
        for n, (filename, data) in enumerate(photos, last + 1):
            self._add_image(data, filename, "receipts", via, receipt_id=rid, position=n)
        self.reparse(rid)
        return self.store.one("SELECT COUNT(*) AS n FROM images WHERE receipt_id = ?", rid)["n"]

    def reparse(self, rid):
        self._receipt_row(rid)
        self.store.x("UPDATE receipts SET status = 'pending', bucket = NULL, error = NULL, approved_at = NULL WHERE id = ?", rid)
        self.wake.set()

    def merge(self, rid, into):
        """rid's photos go after into's; rid is removed and into parsed again."""
        if rid == into:
            raise ValueError("can't merge a receipt into itself")
        self._receipt_row(rid), self._receipt_row(into)
        with self.store.tx():
            last = self.store.one("SELECT COALESCE(MAX(position), 0) AS n FROM images WHERE receipt_id = ?", into)["n"]
            for n, img in enumerate(self.store.q("SELECT id FROM images WHERE receipt_id = ? ORDER BY position", rid), last + 1):
                self.store.x("UPDATE images SET receipt_id = ?, position = ? WHERE id = ?", into, n, img["id"])
            self.store.x("DELETE FROM receipts WHERE id = ?", rid)
        self.reparse(into)

    def delete(self, rid):
        self._receipt_row(rid)
        self.store.x("DELETE FROM receipts WHERE id = ?", rid)

    # ------------------------------------------------------------ parsing

    def known_stores(self):
        return {r["norm_name"] for r in self.store.q("SELECT norm_name FROM stores")}

    def _read_batch(self, rows, photos_of, kind, engines):
        """Read a batch of receipts or products: engine by engine (all OCR first), so the GPU swaps models as
        little as possible. -> {id: {engine: run}} for the rows whose photos could be fetched, and
        {id: error} for those with no photos."""
        paths, missing = {}, {}
        for r in rows:
            try:
                paths[r["id"]] = photos_of(r["id"])
            except Exception as e:  # noqa: BLE001 - e.g. Immich is down; try again later
                log.warning("%s %s: photos unavailable: %s", kind, r["id"], e)
                continue
            if not paths[r["id"]]:
                missing[r["id"]] = f"the {kind} has no photos"
                del paths[r["id"]]
        runs = {i: {} for i in paths}
        for engine in engines:
            ocr = {i: ocr_photos(engine, p) for i, p in paths.items()} if engine["kind"] == "ocr_llm" else {}
            for i, p in paths.items():
                runs[i][engine["name"]] = parse_photos(engine, p, kind, ocr=ocr.get(i))
        return runs, missing

    @staticmethod
    def _server_down(run):
        """The model server didn't answer: not the photo's fault, so leave it pending for the next round."""
        return run["parsed"] is None and re.match(r"(URLError|TimeoutError|ConnectionError|ConnectionRefusedError|"
                                                  r"RemoteDisconnected)\b", run["error"] or "")

    def _save_runs(self, runs, receipt_id=None, product_id=None):
        for run in runs.values():
            self.store.x("INSERT INTO engine_runs (receipt_id, product_id, engine, raw_output, parsed_json, ocr_json, error, "
                         "seconds, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", receipt_id, product_id, run["engine"],
                         run["raw"], json.dumps(run["parsed"]) if run["parsed"] is not None else None,
                         json.dumps(run.get("ocr")) if run.get("ocr") else None, run["error"], run["seconds"], now())

    def parse_pending(self, limit=20):
        """Read pending receipts."""
        pending = self.store.q("SELECT * FROM receipts WHERE status = 'pending' ORDER BY id LIMIT ?", limit)
        runs, missing = self._read_batch(pending, self.receipt_photos, "receipt", self.parse_engines)
        for rid, error in missing.items():
            self._fail(rid, error)
        for r in pending:
            if r["id"] in runs:
                self._apply_runs(r, runs[r["id"]])
        return len(pending)

    def _fail(self, rid, error):
        self.store.x("UPDATE receipts SET status = 'failed', error = ? WHERE id = ?", error, rid)

    def _apply_runs(self, receipt, runs):
        rid = receipt["id"]
        primary = next(iter(runs.values()))
        if self._server_down(primary):
            log.warning("receipt %s stays pending, the model server didn't answer: %s", rid, primary["error"])
            return
        self._save_runs(runs, receipt_id=rid)
        if primary["parsed"] is None:
            self._fail(rid, primary["error"] or "no reading")
            return
        others = {name: run["parsed"] for name, run in runs.items() if run["parsed"] is not None}
        a = checks.assess(primary["parsed"], others if len(others) > 1 else {}, self.known_stores(),
                          ocr_lines=primary.get("ocr"))
        rec = a["record"]
        store_name = rec["store"] if isinstance(rec["store"], str) else None
        known = self.store.one("SELECT * FROM stores WHERE norm_name = ?", norm_store(store_name)) if store_name else None
        location = (primary["parsed"].get("location") if isinstance(primary["parsed"], dict) else None) or (known or {}).get("location")
        status = "approved" if a["bucket"] == "auto" else "parsed"
        with self.store.tx():
            self.store.x("DELETE FROM receipt_items WHERE receipt_id = ?", rid)
            self.store.x(
                "UPDATE receipts SET status = ?, bucket = ?, merchant = ?, merchant_raw = ?, purchase_date = ?, total = ?, "
                "tax = ?, location = ?, engine = ?, checks = ?, confidence = ?, disagreements = ?, error = NULL, "
                "approved_at = ? WHERE id = ?",
                status, a["bucket"], known["name"] if known else store_name, store_name, rec["date"], rec["total"],
                rec["tax"], location, primary["engine"], json.dumps(a["checks"]), json.dumps(a["confidence"]),
                json.dumps(a["disagreements"]), now() if status == "approved" else None, rid)
            self._write_items(rid, [{"raw_text": i["name"], "quantity": i["quantity"], "line_total": i["price"]}
                                    for i in rec["items"]], a["confidence"]["items"])
        catalog.match_receipt_items(self.store, rid, self.text_engine)

    def _write_items(self, rid, items, confidence=None):
        for n, it in enumerate(items, 1):
            qty = it.get("quantity")
            qty = float(qty) if isinstance(qty, (int, float)) and not isinstance(qty, bool) else None
            total = it.get("line_total")
            unit = round(total / qty) if total is not None and qty and qty != 1 else None
            self.store.x("INSERT INTO receipt_items (receipt_id, position, raw_text, quantity, unit_price, line_total, confidence) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", rid, n, str(it.get("raw_text") or "").strip(), qty, unit, total, confidence)

    # ------------------------------------------------------------ review

    def _receipt_row(self, rid):
        r = self.store.one("SELECT * FROM receipts WHERE id = ?", rid)
        if not r:
            raise NotFound(f"no receipt {rid}")
        return r

    def summary(self, r):
        conf = loads(r["confidence"], {})
        counts = self.store.one("SELECT (SELECT COUNT(*) FROM images WHERE receipt_id = ?) AS photos, "
                                "(SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ?) AS items", r["id"], r["id"])
        return {"id": r["id"], "status": r["status"], "bucket": r["bucket"], "merchant": r["merchant"],
                "purchase_date": r["purchase_date"], "total": r["total"], "tax": r["tax"], "source": r["source"],
                "photos": counts["photos"], "items": counts["items"],
                "suspect": [f for f in checks.FIELDS if conf.get(f, 1) < 0.9] if r["status"] == "parsed" else [],
                "created_at": r["created_at"], "url": f"/review/{r['id']}"}

    def list_receipts(self, bucket=None, status=None, limit=50):
        sql, args = "SELECT * FROM receipts WHERE 1 = 1", []
        if bucket:
            sql, args = sql + " AND bucket = ?", args + [bucket]
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        return [self.summary(r) for r in self.store.q(sql + " ORDER BY id DESC LIMIT ?", *args, int(limit))]

    def receipt(self, rid):
        r = self._receipt_row(rid)
        out = self.summary(r)
        out.update({
            "location": r["location"], "error": r["error"], "note": r["note"], "merchant_raw": r["merchant_raw"],
            "images": [{"id": i["id"], "position": i["position"], "url": f"/api/images/{i['id']}"} for i in
                       self.store.q("SELECT id, position FROM images WHERE receipt_id = ? ORDER BY position", rid)],
            "items": self.store.q(
                "SELECT i.id, i.position, i.raw_text, i.quantity, i.unit_price, i.line_total, i.product_id, "
                "p.name AS product_name, i.match, i.confidence FROM receipt_items i LEFT JOIN products p ON p.id = i.product_id "
                "WHERE i.receipt_id = ? ORDER BY i.position", rid),
            "checks": loads(r["checks"], []), "confidence": loads(r["confidence"], {}),
            "disagreements": loads(r["disagreements"], {}),
            "runs": self.store.q("SELECT engine, seconds, error FROM engine_runs WHERE receipt_id = ? ORDER BY id DESC LIMIT 4", rid),
        })
        return out

    def edit(self, rid, data):
        """Hand edits from the review page; every change is kept as a correction. approve: accept it."""
        r = self._receipt_row(rid)
        fields = {"merchant": str, "purchase_date": str, "total": int, "tax": int, "location": str}
        with self.store.tx():
            for f, kind in fields.items():
                if f not in data:
                    continue
                v = data[f]
                if v in ("", None):
                    v = None
                elif kind is int:
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        raise ValueError(f"{f} must be a number of cents")
                    v = int(v)
                elif f == "purchase_date":
                    try:
                        v = date.fromisoformat(str(v)).isoformat()
                    except ValueError:
                        raise ValueError("purchase_date must be YYYY-MM-DD") from None
                else:
                    v = str(v).strip()
                if v != r[f]:
                    self.store.x("INSERT INTO corrections (receipt_id, field, before, after, at) VALUES (?, ?, ?, ?, ?)",
                                 rid, f, None if r[f] is None else str(r[f]), None if v is None else str(v), now())
                    self.store.x(f"UPDATE receipts SET {f} = ? WHERE id = ?", v, rid)
            if "items" in data:
                old = [{"raw_text": i["raw_text"], "quantity": i["quantity"], "line_total": i["line_total"]}
                       for i in self.store.q("SELECT * FROM receipt_items WHERE receipt_id = ? ORDER BY position", rid)]
                new = [{"raw_text": str(i.get("raw_text") or "").strip(), "quantity": i.get("quantity"),
                        "line_total": i.get("line_total")} for i in data["items"]]
                for i in new:
                    lt = i["line_total"]
                    if lt is not None and (isinstance(lt, bool) or not isinstance(lt, (int, float))):
                        raise ValueError("item line_total must be a number of cents")
                    i["line_total"] = None if lt is None else int(lt)
                if new != old:
                    keep = {i["raw_text"]: i for i in self.store.q(
                        "SELECT raw_text, product_id, match FROM receipt_items WHERE receipt_id = ? AND match IS NOT NULL", rid)}
                    self.store.x("INSERT INTO corrections (receipt_id, field, before, after, at) VALUES (?, 'items', ?, ?, ?)",
                                 rid, json.dumps(old), json.dumps(new), now())
                    self.store.x("DELETE FROM receipt_items WHERE receipt_id = ?", rid)
                    self._write_items(rid, new, 1.0)
                    for raw, k in keep.items():
                        self.store.x("UPDATE receipt_items SET product_id = ?, match = ? WHERE receipt_id = ? AND raw_text = ?",
                                     k["product_id"], k["match"], rid, raw)
            if data.get("approve"):
                r = self._receipt_row(rid)
                self.store.x("UPDATE receipts SET status = 'approved', approved_at = ? WHERE id = ?", now(), rid)
                if r["merchant"]:
                    self.store.x("INSERT INTO stores (norm_name, name, location) VALUES (?, ?, ?) ON CONFLICT(norm_name) "
                                 "DO UPDATE SET location = COALESCE(excluded.location, stores.location)",
                                 norm_store(r["merchant"]), r["merchant"], r["location"])
                    if r["merchant_raw"] and norm_store(r["merchant_raw"]) != norm_store(r["merchant"]):
                        # The spelling the engine read is this store from now on, too.
                        self.store.x("INSERT OR IGNORE INTO stores (norm_name, name, location) VALUES (?, ?, ?)",
                                     norm_store(r["merchant_raw"]), r["merchant"], r["location"])
        catalog.match_receipt_items(self.store, rid, self.text_engine)
        return self.receipt(rid)

    def counts(self):
        c = self.store.one(
            "SELECT SUM(status = 'pending') AS pending, SUM(status = 'parsed' AND bucket = 'review') AS review, "
            "SUM(status = 'parsed' AND bucket = 'retake') AS retake, SUM(status = 'approved') AS approved, "
            "SUM(status = 'failed') AS failed FROM receipts")
        c = {k: v or 0 for k, v in c.items()}
        c["unmatched_items"] = self.store.one("SELECT COUNT(*) AS n FROM receipt_items WHERE match IS NULL")["n"]
        c["products"] = self.store.one("SELECT COUNT(*) AS n FROM products")["n"]
        c["products_review"] = self.store.one("SELECT COUNT(*) AS n FROM products WHERE status = 'review'")["n"]
        c["products_pending"] = self.store.one("SELECT COUNT(*) AS n FROM products WHERE status = 'pending'")["n"]
        c["due"] = len(catalog.shopping_list(self.store)["items"])
        receipts, bank, _ = reconcile.report(self.store)
        c["unreconciled_receipts"], c["unreconciled_bank"] = len(receipts), len(bank)
        return c

    # ------------------------------------------------------------ items and products

    def unmatched_items(self, limit=50):
        rows = self.store.q(
            "SELECT i.id, i.receipt_id, i.raw_text, i.line_total, r.merchant, r.purchase_date, i.suggested_product_id, "
            "i.suggested_by, i.suggestion_score, p.name AS s_name, p.brand AS s_brand, p.size AS s_size "
            "FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id LEFT JOIN products p ON p.id = i.suggested_product_id "
            "WHERE i.match IS NULL AND r.status != 'pending' ORDER BY r.purchase_date DESC, i.receipt_id DESC, i.position LIMIT ?", int(limit))
        return [{"id": r["id"], "receipt_id": r["receipt_id"], "raw_text": r["raw_text"], "line_total": r["line_total"],
                 "merchant": r["merchant"], "purchase_date": r["purchase_date"],
                 "suggestion": {"product_id": r["suggested_product_id"],
                                "name": catalog.product_label({"name": r["s_name"], "brand": r["s_brand"], "size": r["s_size"]}),
                                "by": r["suggested_by"], "score": r["suggestion_score"]} if r["suggested_product_id"] else None}
                for r in rows]

    def create_product(self, photos, via="capture", asset_ids=None, name=None):
        pid = self.store.x("INSERT INTO products (name, status, created_at) VALUES (?, ?, ?)", name,
                           "ready" if name else "pending", now())
        try:
            for n, (filename, data) in enumerate(photos, 1):
                self._add_image(data, filename, "products", via, product_id=pid, position=n,
                                asset_id=asset_ids[n - 1] if asset_ids else None)
        except Exception:
            self.store.x("DELETE FROM products WHERE id = ?", pid)
            raise
        self.wake.set()
        return pid

    def parse_pending_products(self, limit=20):
        """Read pending product photos with the product engines, then check them like receipts."""
        pending = self.store.q("SELECT * FROM products WHERE status = 'pending' ORDER BY id LIMIT ?", limit)
        runs, missing = self._read_batch(pending, self.product_photos, "product", self.product_engines)
        for pid, error in missing.items():
            self.store.x("UPDATE products SET status = 'failed', error = ? WHERE id = ?", error, pid)
        for p in pending:
            if p["id"] in runs:
                self._apply_product_runs(p["id"], runs[p["id"]])
        return len(pending)

    def _apply_product_runs(self, pid, runs):
        primary = next(iter(runs.values()))
        if self._server_down(primary):
            log.warning("product %s stays pending, the model server didn't answer: %s", pid, primary["error"])
            return
        self._save_runs(runs, product_id=pid)
        if primary["parsed"] is None:
            self.store.x("UPDATE products SET status = 'failed', error = ? WHERE id = ?", primary["error"] or "no reading", pid)
            return
        others = {name: run["parsed"] for name, run in runs.items() if run["parsed"] is not None}
        a = checks.assess_product(primary["parsed"], others if len(others) > 1 else {})
        rec, auto = a["record"], a["bucket"] == "auto"
        self.store.x("UPDATE products SET name = ?, brand = ?, size = ?, status = ?, bucket = ?, engine = ?, checks = ?, "
                     "confidence = ?, disagreements = ?, error = NULL, approved_at = ? WHERE id = ?",
                     rec["name"], rec["brand"], rec["size"], "ready" if auto else "review", a["bucket"], primary["engine"],
                     json.dumps(a["checks"]), json.dumps(a["confidence"]), json.dumps(a["disagreements"]),
                     now() if auto else None, pid)

    def reparse_product(self, pid):
        self.product(pid)
        self.store.x("UPDATE products SET status = 'pending', bucket = NULL, error = NULL, approved_at = NULL WHERE id = ?", pid)
        self.wake.set()

    def product(self, pid):
        p = self.store.one("SELECT * FROM products WHERE id = ?", pid)
        if not p:
            raise NotFound(f"no product {pid}")
        out = catalog.product_summary(self.store, p)
        out["aliases"] = [a["raw_text"] for a in self.store.q("SELECT raw_text FROM product_aliases WHERE product_id = ?", pid)]
        out["images"] = [{"id": i["id"], "url": f"/api/images/{i['id']}"} for i in
                         self.store.q("SELECT id FROM images WHERE product_id = ? ORDER BY position, id", pid)]
        out["history"] = catalog.history(self.store, pid)
        out.update({"checks": loads(p["checks"], []), "confidence": loads(p["confidence"], {}),
                    "disagreements": loads(p["disagreements"], {}), "error": p["error"],
                    "runs": self.store.q("SELECT engine, seconds, error FROM engine_runs WHERE product_id = ? "
                                         "ORDER BY id DESC LIMIT 4", pid)})
        return out

    def products(self, q=None, status=None):
        rows = self.store.q("SELECT * FROM products ORDER BY name COLLATE NOCASE")
        if status:
            rows = [p for p in rows if p["status"] == status]
        if q:
            words = q.lower().split()
            rows = [p for p in rows if all(w in catalog.product_label(p).lower() for w in words)]
        return [catalog.product_summary(self.store, p) for p in rows]

    def edit_product(self, pid, data):
        """Hand edits; every change is kept as a correction. approve: accept the product (it needs a name)."""
        p = self.store.one("SELECT * FROM products WHERE id = ?", pid)
        if not p:
            raise NotFound(f"no product {pid}")
        with self.store.tx():
            for f in ("name", "brand", "size"):
                if f not in data:
                    continue
                v = str(data[f] or "").strip() or None
                if v != p[f]:
                    self.store.x("INSERT INTO corrections (product_id, field, before, after, at) VALUES (?, ?, ?, ?, ?)",
                                 pid, f, p[f], v, now())
                    self.store.x(f"UPDATE products SET {f} = ? WHERE id = ?", v, pid)
            if data.get("approve"):
                if not self.store.one("SELECT name FROM products WHERE id = ?", pid)["name"]:
                    raise ValueError("a product needs a name before it can be approved")
                self.store.x("UPDATE products SET status = 'ready', approved_at = ? WHERE id = ?", now(), pid)
        return self.product(pid)

    def merge_products(self, pid, into):
        if pid == into:
            raise ValueError("can't merge a product into itself")
        self.product(pid), self.product(into)
        with self.store.tx():
            for table in ("images", "receipt_items", "product_aliases"):
                self.store.x(f"UPDATE {table} SET product_id = ? WHERE product_id = ?", into, pid)
            self.store.x("UPDATE receipt_items SET suggested_product_id = ? WHERE suggested_product_id = ?", into, pid)
            self.store.x("DELETE FROM products WHERE id = ?", pid)
        return self.product(into)

    # ------------------------------------------------------------ watching Immich

    def poll_albums(self):
        """New photos put straight into the Immich albums: each Receipts photo is one receipt (merge on the
        review page if a receipt was photographed in parts); each Products photo one product."""
        n = 0
        for album in ("receipts", "products"):
            assets = self.photos.album_assets(ALBUMS[album])
            known = {r["immich_asset_id"] for r in self.store.q("SELECT immich_asset_id FROM images")}
            for a in sorted((a for a in assets if a["id"] not in known), key=lambda a: a.get("fileCreatedAt") or ""):
                name = a.get("originalFileName") or a["id"]
                via = "syncthing" if a["id"] in set(self.store.get("syncthing_assets", [])) else "immich"
                if album == "receipts":
                    self.create_receipt([(name, None)], via=via, asset_ids=[a["id"]])
                else:
                    self.create_product([(name, None)], via=via, asset_ids=[a["id"]])
                n += 1
        return n

    def sync_library(self):
        """Syncthing: the phone folder is an Immich external library (IMMICH_LIBRARY_ID). Photos under a
        Receipts/ or Products/ folder in it are added to that album; poll_albums then picks them up."""
        if not self.library_id:
            return 0
        seen = set(self.store.get("syncthing_assets", []))
        add = {"receipts": [], "products": []}
        for a in self.photos.library_assets(self.library_id):
            if a["id"] in seen:
                continue
            parts = [p.lower() for p in re.split(r"[\\/]", a.get("originalPath") or "")]
            for album in add:
                if ALBUMS[album].lower() in parts[:-1]:
                    add[album].append(a["id"])
        for album, ids in add.items():
            self.photos.add_to_album(ALBUMS[album], ids)
            seen.update(ids)
        self.store.put("syncthing_assets", sorted(seen))
        return sum(len(v) for v in add.values())

    def work_once(self):
        """One round of background work. Returns what it did."""
        done = {}
        with self.work_lock:
            for name, fn in (("library", self.sync_library), ("albums", self.poll_albums),
                             ("receipts", self.parse_pending), ("products", self.parse_pending_products),
                             ("page copies", self.make_page_copies)):
                try:
                    done[name] = fn()
                except Exception as e:  # noqa: BLE001 - keep the worker alive; log and go on
                    log.warning("%s: %s", name, e)
                    done[name] = f"error: {e}"
        return done

    def run_worker(self, interval=60, stop=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            result = self.work_once()
            if any(v for v in result.values()):
                log.info("work: %s", result)
            self.wake.wait(interval)
            self.wake.clear()

    # ------------------------------------------------------------ labeled set for the bake-off

    def export_labels(self, set_dir):
        """Approved receipts and products -> bake-off test-set folders (photos + label.json), so corrections grow
        it. Receipts go to sNNNN folders, products to psNNNN (the bake-off reads a p... folder as a product)."""
        set_dir = Path(set_dir)
        cents = lambda v: None if v is None else f"{v / 100:.2f}"  # noqa: E731
        n = 0
        for r in self.store.q("SELECT * FROM receipts WHERE status = 'approved'"):
            items = self.store.q("SELECT raw_text, quantity, line_total FROM receipt_items WHERE receipt_id = ? ORDER BY position", r["id"])
            label = {"store": [r["merchant"]] + ([r["merchant_raw"]] if r["merchant_raw"] and r["merchant_raw"] != r["merchant"] else []),
                     "date": r["purchase_date"], "tax": cents(r["tax"]), "total": cents(r["total"]),
                     "items": [{"name": i["raw_text"], "quantity": i["quantity"], "price": cents(i["line_total"])} for i in items]}
            n += self._export(set_dir / f"s{r['id']:04d}", self.receipt_photos(r["id"]), label, r["bucket"] != "auto")
        for p in self.store.q("SELECT * FROM products WHERE status = 'ready' AND approved_at IS NOT NULL"):
            photos = self.product_photos(p["id"])
            if photos:
                n += self._export(set_dir / f"ps{p['id']:04d}", photos,
                                  {"name": p["name"], "brand": p["brand"], "size": p["size"]}, p["bucket"] != "auto")
        return n

    @staticmethod
    def _export(dest, photos, label, reviewed):
        if dest.exists():
            return 0
        dest.mkdir(parents=True)
        for n_photo, path in enumerate(photos, 1):
            shutil.copy2(path, dest / f"{n_photo}{path.suffix}")
        (dest / "label.json").write_text(json.dumps(label, indent=2) + "\n")
        if reviewed:
            (dest / "tags.txt").write_text("reviewed\n")
        return 1

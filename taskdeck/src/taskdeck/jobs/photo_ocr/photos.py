"""Receipt capture and product photos: upload photos from the phone to the photo OCR service, wait for the
local models to read them, and show the reading beside the photos so a person can judge it at a glance."""

from __future__ import annotations

import os
import time

from taskdeck.core import Field, Pipeline, Result, Stage, StepError, step
from taskdeck.shared import files

from . import api

UPLOAD_TIMEOUT = 180  # several full-size phone photos over Tailscale
READ_POLL_SECONDS = 3  # how often to ask whether the photos have been read
READ_TIMEOUT_SECONDS = 300  # a model swap on the workstation can take a minute; then the result says "still reading"


def _photo_paths(ctx) -> list[str]:
    paths = ctx["photos"]
    if not paths:
        raise StepError("Add at least one photo.")
    return paths


@step(Stage.VALIDATE)
def read_photos(ctx):
    ctx["parts"] = api.read_files(_photo_paths(ctx), api.PHOTO_EXTENSIONS)


@step(Stage.EXECUTE, name="send receipt")
def send_receipt(ctx):
    note = (ctx.get("note") or "").strip()
    ctx["sent_all"] = [api.call(ctx.settings, "POST", "/api/receipts", fields=[("note", note)] if note else [],
                                files=ctx["parts"], timeout=UPLOAD_TIMEOUT)]
    ctx["send_errors"] = [None]
    _delete_camera_photos(ctx["photos"])


def _delete_camera_photos(paths: list[str]) -> None:
    """Photos from TaskDeck's camera are kept only until sent (the service keeps them, in Immich); photos the
    user picked from the phone stay where they are."""
    for p in paths:
        if files.is_camera_photo(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _sent_images(parts) -> list[bytes]:
    """The photos sent, as bytes: thumbnails for the result even when the files are deleted."""
    return [data for _, _, data in parts]


def wait_for_reading(path: str):
    """A step that asks the service every READ_POLL_SECONDS whether everything sent (``ctx["sent_all"]``) has
    been read (``path`` with ``{id}``), up to READ_TIMEOUT_SECONDS. Leaves ``ctx["readings"]``, one per thing
    sent: its record, or None if not read in time (or not sent); ``ctx["reading_errors"]`` when asking failed."""

    @step(Stage.EXECUTE, name="wait for reading")
    def wait(ctx):
        sent = ctx["sent_all"]
        ctx["readings"], ctx["reading_errors"] = [None] * len(sent), [None] * len(sent)
        waiting = {i for i, s in enumerate(sent) if s}
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        while waiting:
            for i in sorted(waiting):
                ctx.check_cancelled()
                try:
                    record = api.call(ctx.settings, "GET", path.format(id=sent[i].get("id")))
                except StepError as exc:  # the photos are sent; only the wait for this one stops
                    ctx["reading_errors"][i] = str(exc)
                    waiting.discard(i)
                    continue
                if record.get("status") != "pending":
                    ctx["readings"][i] = record
                    waiting.discard(i)
            if not waiting or time.monotonic() >= deadline:
                return
            pause_until = time.monotonic() + READ_POLL_SECONDS
            while time.monotonic() < pause_until:
                ctx.check_cancelled()
                time.sleep(0.2)

    return wait


def _verdict(reading: dict, ok_status: str, retake_what: str) -> tuple[str, str | None]:
    """The reading's outcome in words, and a warning when a person has to act."""
    if reading.get("status") == "failed":
        return "Reading failed", f"Reading failed: {reading.get('error') or 'no reason given'}."
    if reading.get("bucket") == "retake":
        return "Retake", f"{retake_what} couldn't be read: add a new photo or type it on the page."
    if reading.get("status") == ok_status:
        return "Saved: every check passed", None
    return "Needs a look", "Some of this reading looks doubtful: check the marked fields on the page."


def _not_read(ctx, i: int, rows: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str]:
    error = ctx["reading_errors"][i]
    if error:
        return rows + [("Status", "sent")], f"Sent, but the reading couldn't be checked: {error}"
    return rows + [("Status", "still being read")], "Still being read on the workstation; the page shows it when it's done."


@step(Stage.PRESENT)
def show_receipt(ctx):
    sent, reading = ctx["sent_all"][0], ctx["readings"][0]
    rid = sent.get("id")
    rows = [("Receipt", f"#{rid}"), ("Photos", str(sent.get("photos", len(ctx["parts"]))))]
    page = ("Review page", api.page_url(ctx.settings, f"/review/{rid}"))
    if not reading:
        rows, text = _not_read(ctx, 0, rows)
        ctx.result = Result(title=f"Receipt #{rid} sent", rows=rows + [page], text=text,
                            images=_sent_images(ctx["parts"]))
        return
    status, warning = _verdict(reading, "approved", "The total or date")
    suspect = [api.FIELD_NAMES.get(f, f) for f in reading.get("suspect") or []]
    items = reading.get("items")
    rows += [("Store", reading.get("merchant") or "—"), ("Date", reading.get("purchase_date") or "—"),
             ("Total", api.dollars(reading.get("total"))), ("Tax", api.dollars(reading.get("tax"))),
             ("Items", str(len(items) if isinstance(items, list) else items or 0)), ("Status", status)]
    if suspect:
        rows.append(("Check", ", ".join(suspect)))
    ctx.result = Result(
        title=f"Receipt #{rid}: {reading.get('merchant') or 'store not read'} · {api.dollars(reading.get('total'))}",
        rows=rows + [page], warning=warning, images=_sent_images(ctx["parts"]))


# ---- products: each group of photos (``Field.grouped``) is one product -------------------------------------

@step(Stage.VALIDATE)
def read_product_photos(ctx):
    groups = ctx["photos"]
    if not groups:
        raise StepError("Add at least one photo.")
    ctx["groups"] = groups
    ctx["parts_by_group"] = [api.read_files(g, api.PHOTO_EXTENSIONS) for g in groups]


@step(Stage.EXECUTE, name="send product photos")
def send_products(ctx):
    """One product per group. A product the service refuses is reported; the others still go."""
    ctx["sent_all"], ctx["send_errors"] = [], []
    for parts in ctx["parts_by_group"]:
        ctx.check_cancelled()
        try:
            ctx["sent_all"].append(api.call(ctx.settings, "POST", "/api/products", files=parts, timeout=UPLOAD_TIMEOUT))
            ctx["send_errors"].append(None)
        except StepError as exc:
            ctx["sent_all"].append(None)
            ctx["send_errors"].append(str(exc))
    if not any(ctx["sent_all"]):
        raise StepError(ctx["send_errors"][0])


def _product_label(reading: dict) -> str:
    return " ".join(x for x in (reading.get("brand"), reading.get("name"), reading.get("size")) if x) or "name not read"


@step(Stage.PRESENT)
def show_product(ctx):
    if len(ctx["groups"]) > 1:
        _show_products(ctx)
        return
    sent, reading = ctx["sent_all"][0], ctx["readings"][0]
    pid = sent.get("id")
    photos = ctx["groups"][0]
    rows = [("Product", f"#{pid}"), ("Photos", str(len(photos)))]
    page = ("Product page", api.page_url(ctx.settings, f"/products/{pid}"))
    if not reading:
        rows, text = _not_read(ctx, 0, rows)
        ctx.result = Result(title=f"Product #{pid} added", rows=rows + [page], text=text, images=list(photos))
        return
    status, warning = _verdict(reading, "ready", "The product name")
    rows += [("Name", reading.get("name") or "—"), ("Brand", reading.get("brand") or "—"),
             ("Size", reading.get("size") or "—"), ("Status", status)]
    if reading.get("suspect"):
        rows.append(("Check", ", ".join(reading["suspect"])))
    ctx.result = Result(title=f"Product #{pid}: {_product_label(reading)}", rows=rows + [page], warning=warning,
                        images=list(photos))


def _show_products(ctx):
    """Several products sent at once: one row each, in the order picked, beside the first photo of each."""
    rows, sent_n, read_n, look_n = [], 0, 0, 0
    for i, group in enumerate(ctx["groups"]):
        sent, reading, error = ctx["sent_all"][i], ctx["readings"][i], ctx["send_errors"][i]
        if not sent:
            rows.append((f"Product {i + 1}", f"not sent: {error}"))
            continue
        sent_n += 1
        key = f"{i + 1}. #{sent.get('id')}"
        if not reading:
            rows.append((key, "sent; couldn't check the reading" if ctx["reading_errors"][i] else "still being read"))
            continue
        read_n += 1
        status, warning = _verdict(reading, "ready", "The product name")
        look_n += warning is not None
        check = f" · check {', '.join(reading['suspect'])}" if reading.get("suspect") else ""
        rows.append((key, f"{_product_label(reading)} · {status}{check}"))
    n = len(ctx["groups"])
    parts = [f"{read_n} read"] + ([f"{sent_n - read_n} still being read"] if sent_n > read_n else [])
    rows.append(("Products page", api.page_url(ctx.settings, "/products")))
    sent_words = f"{n} products sent" if sent_n == n else f"{sent_n} of {n} products sent"
    ctx.result = Result(
        title=f"{sent_words} · {', '.join(parts)}",
        rows=rows, images=[g[0] for g in ctx["groups"]],
        warning=f"{look_n} need{'s' if look_n == 1 else ''} a look: check {'it' if look_n == 1 else 'them'} on the review "
                f"page." if look_n else None)


class ReceiptCapture(api.PhotoOcrJob):
    id = api.SETTINGS_ID
    title = "Receipt capture"
    description = "Send photos of one receipt to the photo OCR service, top to bottom, and see what it read."
    icon = "RECEIPT_LONG"
    tags = ("receipt", "photo", "scan", "ocr", "capture", "groceries", "immich")
    fields = (
        Field.files("photos", "Receipt photos", required=True, extensions=api.PHOTO_EXTENSIONS, preview=True,
                    camera="receipt",
                    help="Take photos with the camera, or Add… photos you took, in order from the top of the "
                         "receipt to the bottom. All the photos become one receipt."),
        Field.text("note", "Note"),
    )
    uses_immich = True
    run_label = "Send receipt"

    def build_pipeline(self):
        return Pipeline([api.service_configured, read_photos, send_receipt, wait_for_reading("/api/receipts/{id}"),
                         show_receipt])


class ProductPhoto(api.PhotoOcrJob):
    id = "photo_ocr_product_photo"
    title = "Product photo"
    description = ("Add products to the catalog from their photos, one product or several at once; the service reads "
                   "name, brand and size.")
    icon = "ADD_A_PHOTO_OUTLINED"
    tags = ("product", "photo", "catalog", "gallery", "groceries", "receipts", "immich")
    fields = (Field.files("photos", "Product photos", required=True, extensions=api.PHOTO_EXTENSIONS, preview=True,
                          grouped="product",
                          help="Add… several photos, then 1 product (front and label of one product) or N products "
                               "(each photo its own product). Products are separated by a blank line."),)
    uses_immich = True
    run_label = "Send products"

    def build_pipeline(self):
        return Pipeline([api.service_configured, read_product_photos, send_products,
                         wait_for_reading("/api/products/{id}"), show_product])


CAPTURE = ReceiptCapture()
PRODUCT = ProductPhoto()

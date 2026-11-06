"""Receipt capture and product photos: upload photos from the phone to the photo OCR service, wait for the
local models to read them, and show the reading beside the photos so a person can judge it at a glance."""

from __future__ import annotations

import time

from taskdeck.core import Field, Pipeline, Result, Stage, StepError, step

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
    ctx["sent"] = api.call(ctx.settings, "POST", "/api/receipts", fields=[("note", note)] if note else [],
                           files=ctx["parts"], timeout=UPLOAD_TIMEOUT)


def wait_for_reading(path: str):
    """A step that asks the service every READ_POLL_SECONDS whether the photos sent are read yet (``path`` with
    ``{id}``), up to READ_TIMEOUT_SECONDS. Leaves ``ctx["reading"]`` = the record, or None if not read in time."""

    @step(Stage.EXECUTE, name="wait for reading")
    def wait(ctx):
        ctx["reading"], ctx["reading_error"] = None, None
        url_path = path.format(id=ctx["sent"].get("id"))
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        while True:
            ctx.check_cancelled()
            try:
                record = api.call(ctx.settings, "GET", url_path)
            except StepError as exc:  # the photos are sent; only the wait stops
                ctx["reading_error"] = str(exc)
                return
            if record.get("status") != "pending":
                ctx["reading"] = record
                return
            if time.monotonic() >= deadline:
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


def _not_read(ctx, rows: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str]:
    if ctx.get("reading_error"):
        return rows + [("Status", "sent")], f"Sent, but the reading couldn't be checked: {ctx['reading_error']}"
    return rows + [("Status", "still being read")], "Still being read on the workstation; the page shows it when it's done."


@step(Stage.PRESENT)
def show_receipt(ctx):
    sent, reading = ctx["sent"], ctx.get("reading")
    rid = sent.get("id")
    rows = [("Receipt", f"#{rid}"), ("Photos", str(sent.get("photos", len(ctx["parts"]))))]
    page = ("Review page", api.page_url(ctx.settings, f"/review/{rid}"))
    if not reading:
        rows, text = _not_read(ctx, rows)
        ctx.result = Result(title=f"Receipt #{rid} sent", rows=rows + [page], text=text, images=list(ctx["photos"]))
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
        rows=rows + [page], warning=warning, images=list(ctx["photos"]))


@step(Stage.EXECUTE, name="send product photos")
def send_product(ctx):
    ctx["sent"] = api.call(ctx.settings, "POST", "/api/products", files=ctx["parts"], timeout=UPLOAD_TIMEOUT)


@step(Stage.PRESENT)
def show_product(ctx):
    sent, reading = ctx["sent"], ctx.get("reading")
    pid = sent.get("id")
    rows = [("Product", f"#{pid}"), ("Photos", str(len(ctx["parts"])))]
    page = ("Product page", api.page_url(ctx.settings, f"/products/{pid}"))
    if not reading:
        rows, text = _not_read(ctx, rows)
        ctx.result = Result(title=f"Product #{pid} added", rows=rows + [page], text=text, images=list(ctx["photos"]))
        return
    status, warning = _verdict(reading, "ready", "The product name")
    rows += [("Name", reading.get("name") or "—"), ("Brand", reading.get("brand") or "—"),
             ("Size", reading.get("size") or "—"), ("Status", status)]
    if reading.get("suspect"):
        rows.append(("Check", ", ".join(reading["suspect"])))
    label = " ".join(x for x in (reading.get("brand"), reading.get("name"), reading.get("size")) if x) or "name not read"
    ctx.result = Result(title=f"Product #{pid}: {label}", rows=rows + [page], warning=warning,
                        images=list(ctx["photos"]))


class ReceiptCapture(api.PhotoOcrJob):
    id = api.SETTINGS_ID
    title = "Receipt capture"
    description = "Send photos of one receipt to the photo OCR service, top to bottom, and see what it read."
    icon = "RECEIPT_LONG"
    tags = ("receipt", "photo", "scan", "ocr", "capture", "groceries", "immich")
    fields = (
        Field.files("photos", "Receipt photos", required=True, extensions=api.PHOTO_EXTENSIONS, preview=True,
                    help="In order from the top of the receipt to the bottom. All the photos become one receipt."),
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
    description = "Add a product to the catalog from one or more photos of it; the service reads name, brand and size."
    icon = "ADD_A_PHOTO_OUTLINED"
    tags = ("product", "photo", "catalog", "gallery", "groceries", "receipts", "immich")
    fields = (Field.files("photos", "Product photos", required=True, extensions=api.PHOTO_EXTENSIONS, preview=True,
                          help="One or more photos of one product, e.g. the front and the label."),)
    uses_immich = True
    run_label = "Send photos"

    def build_pipeline(self):
        return Pipeline([api.service_configured, read_photos, send_product, wait_for_reading("/api/products/{id}"),
                         show_product])


CAPTURE = ReceiptCapture()
PRODUCT = ProductPhoto()

"""Receipt capture and product photos: upload photos from the phone to the receipts service."""

from __future__ import annotations

from taskdeck.core import Field, Pipeline, Result, Stage, StepError, step

from . import api

UPLOAD_TIMEOUT = 180  # several full-size phone photos over Tailscale


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


@step(Stage.PRESENT)
def show_receipt(ctx):
    sent = ctx["sent"]
    ctx.result = Result(
        title=f"Receipt #{sent.get('id')} sent",
        rows=[("Receipt", f"#{sent.get('id')}"), ("Photos", str(sent.get("photos", len(ctx["parts"])))),
              ("Status", str(sent.get("status", ""))), ("Review page", api.page_url(ctx.settings, f"/review/{sent.get('id')}"))],
        text="It is parsed on the workstation in the background.",
    )


@step(Stage.EXECUTE, name="send product photos")
def send_product(ctx):
    ctx["sent"] = api.call(ctx.settings, "POST", "/api/products", files=ctx["parts"], timeout=UPLOAD_TIMEOUT)


@step(Stage.PRESENT)
def show_product(ctx):
    sent = ctx["sent"]
    ctx.result = Result(
        title=f"Product #{sent.get('id')} added",
        rows=[("Product", f"#{sent.get('id')}"), ("Photos", str(len(ctx["parts"]))),
              ("Status", str(sent.get("status", ""))), ("Product page", api.page_url(ctx.settings, f"/products/{sent.get('id')}"))],
        text="The name, brand and size are read from the photos in the background.",
    )


class ReceiptCapture(api.ReceiptsJob):
    id = api.SETTINGS_ID
    title = "Receipt capture"
    description = "Send photos of one receipt to the receipts service, top to bottom, to be parsed and reviewed."
    icon = "RECEIPT_LONG"
    tags = ("receipt", "photo", "scan", "ocr", "capture", "groceries", "immich")
    fields = (
        Field.files("photos", "Receipt photos", required=True, extensions=api.PHOTO_EXTENSIONS,
                    help="In order from the top of the receipt to the bottom. All the photos become one receipt."),
        Field.text("note", "Note"),
    )
    uses_immich = True
    run_label = "Send receipt"

    def build_pipeline(self):
        return Pipeline([api.service_configured, read_photos, send_receipt, show_receipt])


class ProductPhoto(api.ReceiptsJob):
    id = "receipts_product_photo"
    title = "Product photo"
    description = "Add a product to the receipts catalog from one or more photos of it."
    icon = "ADD_A_PHOTO_OUTLINED"
    tags = ("product", "photo", "catalog", "gallery", "groceries", "receipts", "immich")
    fields = (Field.files("photos", "Product photos", required=True, extensions=api.PHOTO_EXTENSIONS,
                          help="One or more photos of one product, e.g. the front and the label."),)
    uses_immich = True
    run_label = "Send photos"

    def build_pipeline(self):
        return Pipeline([api.service_configured, read_photos, send_product, show_product])


CAPTURE = ReceiptCapture()
PRODUCT = ProductPhoto()

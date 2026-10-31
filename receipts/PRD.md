# Receipt OCR & Product Catalog — PRD

Oct 31, 2026 · @Jim Balcomb

## Overview

Phone photos of receipts and products are read by a local model on the home AI workstation, parsed into structured records, reviewed when uncertain, and cataloged for purchase history and shopping lists.

The workstation runs everything in Docker and is reached from the phone over Tailscale. No cloud OCR or vision API is used. The phone side is two TaskDeck plug-ins: one for capture and review, one for reconciliation.

## Goals and non-goals

The first versions turn receipt photos into reviewed, structured records and decide which parsing engine is worth keeping.

**Goals**

- Capture receipts from the phone, including receipts that need several photos.
- Extract store, date, tax, total and line items into structured records.
- Compare a vision-language model against classic OCR on real receipts, then choose.
- Score confidence per receipt and send suspect results to a review page.
- Keep a product photo gallery and match products to receipt line items.
- Record purchase history, price and location so shopping lists can be generated.

**Non-goals for now**

- Bank statement and Instacart reconciliation are designed for but not built (see Later).
- No cloud OCR or vision APIs.
- Google Photos is not an automatic source; it is used through manual selection only.

## Architecture

Immich is the single landing spot for photos, and one Docker service reads from it, parses with local models, and serves both plug-ins.

&#91;embedded content: architecture · phone, Immich, one service, local models\]

The phone reaches the workstation only over Tailscale. Besides the TaskDeck plug-ins, the service is the only piece to build; Immich, the model server and the database are existing software.

## Photo ingest

All three capture paths end in Immich, and the service watches Immich for new photos.

- **Immich hub.** Two albums: Receipts and Products. The service polls the Immich API for new assets in them.
- **Capture PWA.** A camera page served by the service over Tailscale. "Add another photo" groups photos into one receipt; "Done" submits the group. Photos upload to Immich through its API, and the grouping and order are stored in the service database.
- **Syncthing.** A phone folder syncs to the workstation and is added to Immich as an external library.
- **Google Photos Picker.** The person selects photos in Google Photos, and the service downloads the selected items and imports them into Immich. This needs a Google Cloud project and OAuth setup.

The Picker is the only option for Google Photos. Google's Library API now lists and reads only items an app created itself, so the existing library cannot be scanned automatically ([Google Photos API updates](https://developers.google.com/photos/support/updates), page last updated 2025-08-28).

A multi-photo receipt is one record: the photos are kept in order and sent to the model together in one request. Lines repeated where photos overlap must be deduplicated.

## Parsing bake-off

Build the comparison harness first, because its results decide which engine the pipeline uses. The success rates of a vision model versus classic OCR on these receipts are not yet known.

Candidates come from web roundups and are unverified; the bake-off decides. The GPU is an RTX 5060 Ti with 16 GB of VRAM, and everything runs through llama.cpp or Python.

| Engine | Type | Notes |
| --- | --- | --- |
| Qwen3-VL-8B | Vision-language model | Image in, structured JSON out; fits 16 GB at Q4 or Q8 |
| PaddleOCR-VL | Small document model | Under 1B parameters; built for document OCR |
| PaddleOCR or docTR + text LLM | Classic OCR | Raw text with word confidence; an LLM structures it |
| Tesseract + text LLM | Classic OCR | Baseline |

**Test set.** 25 to 30 real receipts, hand-labeled for store, date, tax, total and line items. Include crumpled, faded, thermal-paper and long multi-photo receipts.

**Scoring.** Exact match for total, date and store. Fuzzy match on name and price for line items. Record seconds per receipt.

**Output.** A results table per engine and a written decision on which engine to use, or whether to run two permanently so their disagreements feed confidence scoring.

Other local-AI jobs share this GPU, so only one model is loaded at a time. Use a model-swapping layer such as llama-swap, or equivalent.

## Confidence and review

Every receipt lands in one of three buckets, and only the second and third need a person.

| Bucket | Rule | Action |
| --- | --- | --- |
| Auto-accept | All checks pass and the engines agree | Save without review |
| Review | One check failed or the engines disagree on a field | Show on the review page with the suspect fields marked |
| Retake | Total or date unreadable | Ask for a new photo |

**Checks that work on any engine**

- Line items plus tax equal the total, within rounding.
- The date parses and is plausible.
- The store matches a known store, or is flagged as new.
- Prices match a currency format.

**Engine signals**

- **Disagreement.** Run two engines and flag fields where they differ. Likely the strongest signal.
- **Native confidence.** Classic OCR returns per-word confidence. llama.cpp can return token log-probabilities for vision models.

Store a confidence per field and a bucket per receipt. The review page shows the photo beside the parsed fields, lets the person edit them, and approves. Keep every correction so the labeled set grows over time.

## Data model

Six tables hold everything, and receipts carry the fields that later bank and Instacart matching will need. SQLite is enough to start.

| Table | Holds | Key fields |
| --- | --- | --- |
| images | One row per photo | Immich asset id, receipt id, position, source path |
| receipts | One row per purchase | normalized merchant, purchase date, total, tax, location, source (in-store, Instacart, other), bucket, status |
| receipt\_items | Line items | raw text, quantity, unit price, line total, product id (nullable), confidence |
| engine\_runs | Each engine's output per receipt | engine, raw output, parsed JSON, seconds |
| products | The catalog | name, brand, size, Immich asset ids |
| product\_aliases | Receipt text mapped to a product | raw text, product id |

The engine\_runs table feeds both the bake-off and the disagreement check, so keep every run, not just the winner. Store amounts in cents.

## Product gallery and matching

Product photos become catalog entries, and catalog entries get linked to receipt line items so purchases build a history.

1. A photo added to the Products album is read by the vision model for name, brand and size, then saved as a catalog entry linked to its Immich asset.
2. Each receipt line item is matched to a catalog entry by normalized text, with the model assisting on hard cases.
3. The first confirmed match saves an alias (receipt text to product), so the same text matches automatically next time.
4. Unmatched items go to the review page as their own queue.

Every matched purchase records price, date and store location, so price history and "where I buy it" can be shown later.

**Shopping lists.** Generated from the catalog plus purchase frequency and the date of the last purchase. The exact rules for what makes an item due are still to be defined.

## TaskDeck plug-ins

Both plug-ins are thin screens over the service's HTTP API, reached over Tailscale.

**Receipts plug-in**

- Capture: take one or several photos, then submit.
- Review queue: receipts in the Review and Retake buckets, with the photo beside the parsed fields, editable and approvable.
- Unmatched items queue for product matching.
- Product gallery: browse the catalog and add new product photos.

**Reconciliation plug-in**

- Built later (see Later). It lists receipts against bank and Instacart records and shows what is unmatched on either side.

The plug-in registration details follow whatever contract the TaskDeck app already defines for jobs.

## Later: bank and Instacart reconciliation

Nothing here is built in the first versions; the data model only has to leave room for it.

- **Bank statements.** Import CSV exports and match each charge to a receipt on merchant, date within a few days, and amount.
- **Instacart and other online orders.** The bank shows one charge for an order, while the order itself lists the items. Treat each order as a receipt with source Instacart, with items taken from the order confirmation or its email. I assume Instacart has no consumer API, which should be verified before building.
- **Output.** A reconciliation screen showing receipts with no bank match and bank charges with no receipt.

## Build phases

Build in this order; each phase ends with something usable.

1. **Bake-off harness.** Labeled receipt set, scoring script, results table. Done when an engine, or a pair of engines, is chosen.
2. **Hub and capture.** Immich with the two albums, the service skeleton, and the capture PWA with multi-photo grouping.
3. **Parse and review.** The chosen engine in the pipeline, the checks, confidence buckets, and the review page in the Receipts plug-in.
4. **Other ingest paths.** Syncthing into Immich and the Google Photos Picker import.
5. **Products.** Product gallery, catalog extraction, line-item matching with aliases, and shopping lists.
6. **Reconciliation.** Bank CSV import, online orders, and the Reconciliation plug-in.

## Open questions

- Which engine wins the bake-off, and is it worth running two permanently for the disagreement signal?
- SQLite or Postgres for the service database?
- How are models swapped on the shared GPU: llama-swap or another approach?
- How long are original photos kept, and how much Immich storage is budgeted?
- What rules make a product due on a shopping list?
- Does Instacart expose order data in a usable form, such as email or export?

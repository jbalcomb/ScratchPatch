# Photo OCR

Six jobs in the **Photo OCR** category are thin screens over the photo OCR service on the AI Workstation
([photo-ocr/API.md](../../../photo-ocr/API.md)), reached over Tailscale. The service reads photos of receipts
and products with the local models, checks the readings and stores them; the phone sends photos and shows lists.
The design is in [photo-ocr/PRD.md](../../../photo-ocr/PRD.md).

## Setup

1. On the workstation, start the service and expose it with Tailscale Serve, as in the Tailscale Serve step of
   [photo-ocr/README.md](../../../photo-ocr/README.md). Note the HTTPS address it prints.
2. On the phone, connect Tailscale.
3. Open **Receipt capture**, tap the **tune** icon and set **Photo OCR service URL** to that address, e.g.
   `https://<workstation>.<tailnet>.ts.net:8444`. All six jobs use this one setting.

When a job opens, it checks `GET /api/health`. If the service doesn't answer, the job says so, and says when
the reason is that the phone isn't on Tailscale. Errors from the service are shown as the job's error.

## Jobs

| Job | What it does |
|---|---|
| **Receipt capture** | Tap **Add…** to pick photos (.jpg, .jpeg, .png, .webp) from a grid of the phone's photos, like Google Photos: it opens at Photos (the camera folder, DCIM), newest first under day headings, with Camera, Screenshots and other folders as buttons. Tap photos in order from the top of the receipt to the bottom; each gets a numbered check, and the top bar shows how many with **Add**, clear and select all. Long-press a photo to see it large. Add an optional note. Thumbnails appear under the field as soon as photos are chosen. **Send receipt** uploads them as one receipt, then waits (up to 5 minutes, asking every 3 seconds; Cancel stops the wait) while the workstation reads it. The result shows the photos as thumbnails beside what was read: store, date, total, tax, number of items, whether every check passed, the fields to check, and the review page. Tap a thumbnail to compare the reading with the photo at full size. If the reading isn't done in time, the result says so; the review page shows it later. |
| **Review queue** | Counts, then every receipt and product whose reading needs a person: for a receipt its store, date, total, bucket and the fields to check; for a product its name, brand, size and the fields to check. Readings in the retake bucket say a new photo is needed. Review them on the `/review` page. |
| **Unmatched items** | Receipt lines not yet matched to a product, as "text (store, date, price) → suggestion". **Accept suggestions** matches every item whose suggestion scores 90% or more; the preview lists exactly those first. The service then matches the same text automatically, on other receipts too. Match the rest on the `/items` page. |
| **Product photo** | Pick photos with the same photo grid. With several selected, the top bar offers **1 product** (they are all one product, e.g. its front and label) and **N products** (each photo is its own product); picking again adds more products. The field shows the thumbnails under "Product 1", "Product 2", …. **Send products** sends each product, waits for all the readings together, and shows them: for one product, the photos beside the name, brand and size, whether every check passed and the fields to check; for several, one line each, in the order picked, beside the first photo of each. A product the service refuses is listed as not sent; the others still go. A doubtful reading also shows up in the review queue. |
| **Shopping list** | Products that are due, with how many days overdue, the usual store, the last price and the best price and store. |
| **Reconciliation** | Matches receipts to bank charges, then lists receipts with no bank charge and charges with no receipt. Bank charges come from the budget store, which the service reads on every run; bank exports are imported in the budget project, not here. No inputs. Match the leftovers by hand on the `/reconcile` page. |

Amounts come from the service in cents and are shown in dollars.

# Receipts service HTTP API

The service (`receipts/web.py`) listens on the workstation's `localhost:8093`. Tailscale Serve exposes it
over HTTPS to your own devices only. Nothing here needs a login, so keep it off the open internet.

All JSON. Amounts are **integer cents**, dates `YYYY-MM-DD`. Errors are `{"error": "message"}` with a
4xx/5xx status. The TaskDeck plug-ins (`taskdeck/src/taskdeck/jobs/receipts/`) and the web pages use
this API only.

## Health and counts

`GET /api/health` → `{"ok": true, "immich": true|false|null, "engines": ["qwen3-vl-8b"], "version": 1}`
(`immich` is null when Immich isn't set up and photos are kept locally, false when it doesn't answer.)

`GET /api/summary` →
```json
{"pending": 2, "review": 3, "retake": 1, "approved": 40, "unmatched_items": 12,
 "products": 25, "due": 4, "unreconciled_receipts": 5, "unreconciled_bank": 7}
```

## Receipts

`POST /api/receipts`: multipart/form-data. Repeat the `photo` field once per photo, in order from the top of
the receipt to the bottom. Optional fields are `source` (`in-store` by default) and `note`. All the photos
become one receipt; each goes to the Immich *Receipts* album and parsing follows in the background.
→ `201 {"id": 17, "photos": 3, "status": "pending"}`

`POST /api/receipts/{id}/photos`: multipart, `photo` repeated. Adds photos (a retake, or a missed part),
appended after the existing ones, and parses again. → `{"id": 17, "photos": 4, "status": "pending"}`

`GET /api/receipts?bucket=review&status=parsed&limit=50` → `{"receipts": [Receipt summary, ...]}`
(each filter optional; newest first).

Receipt summary:
```json
{"id": 17, "status": "parsed", "bucket": "review", "merchant": "Giant Eagle", "purchase_date": "2026-10-12",
 "total": 4519, "tax": 112, "source": "in-store", "photos": 2, "items": 14, "suspect": ["total", "merchant"],
 "created_at": "2026-10-31T18:20:00", "url": "/review/17"}
```

- `status`: `pending` (waiting to be parsed), `parsed` (needs a person), `approved` (accepted, by hand or
  automatically), `failed` (the engine errored; the reason is in `error`).
- `bucket`: `auto`, `review` or `retake` (PRD *Confidence and review*); null while pending.
- `suspect`: the fields whose confidence is low, out of `merchant`, `purchase_date`, `total`, `tax` and
  `items`.

`GET /api/receipts/{id}` → the summary fields plus:
```json
{"location": "Mentor, OH", "error": null, "note": "",
 "images": [{"id": 31, "position": 1, "url": "/api/images/31"}],
 "items": [{"id": 201, "position": 1, "raw_text": "BANANAS", "quantity": 1, "unit_price": null,
            "line_total": 129, "product_id": 5, "product_name": "Bananas", "match": "alias", "confidence": 1.0}],
 "checks": [{"name": "sum", "ok": false, "detail": "items + tax = 45.91, total = 45.19"}],
 "confidence": {"merchant": 0.9, "purchase_date": 1.0, "total": 0.3, "tax": 1.0, "items": 0.6},
 "disagreements": {"total": {"qwen3-vl-8b": 4519, "tesseract+llm": 4591}},
 "runs": [{"engine": "qwen3-vl-8b", "seconds": 8.2, "error": null}]}
```

`POST /api/receipts/{id}`: a JSON edit, all keys optional:
```json
{"merchant": "Giant Eagle", "purchase_date": "2026-10-12", "total": 4519, "tax": 112, "location": "Mentor, OH",
 "items": [{"raw_text": "BANANAS", "quantity": 1, "line_total": 129}], "approve": true}
```
`items`, when given, replaces the whole list. Every changed value is kept as a correction, and with
`approve`, the receipt becomes `approved` and its store becomes a known store. → the full receipt.

`POST /api/receipts/{id}/reparse` → `{"status": "pending"}`
`POST /api/receipts/{id}/merge` `{"into": 16}`: appends this receipt's photos to receipt 16 (for a receipt
photographed as separate album photos), removes this one and re-parses 16. → receipt 16.
`DELETE /api/receipts/{id}` removes the record. The photos stay in Immich.

`GET /api/images/{id}` → the photo (JPEG), from the local cache or from Immich.

## Line items and the catalog

`GET /api/items/unmatched?limit=50` →
```json
{"items": [{"id": 201, "receipt_id": 17, "raw_text": "GV WHL MLK GAL", "line_total": 329,
            "merchant": "Walmart", "purchase_date": "2026-10-12",
            "suggestion": {"product_id": 5, "name": "Whole milk", "by": "text|model", "score": 0.82}}]}
```
`suggestion` is null when nothing is close.

`POST /api/items/{id}/match`, with `{"product_id": 5}`, `{"new_product": {"name": "...", "brand": "...",
"size": "..."}}` or `{"skip": true}` (not a product, e.g. a bag fee): saves the alias, so the same receipt
text matches automatically next time, and applies it to other unmatched items with the same text.
→ `{"matched": 3, "product_id": 5}`

`GET /api/products?q=milk` → `{"products": [{"id": 5, "name": "Whole milk", "brand": "Great Value",
"size": "1 gal", "image_url": "/api/images/40", "purchases": 6, "last_bought": "2026-10-12",
"last_price": 329, "last_store": "Walmart"}]}`

`GET /api/products/{id}` → the product plus `aliases: [..]`, `images: [..]` and
`history: [{"date", "merchant", "location", "line_total", "quantity", "receipt_id"}]`.

`POST /api/products`: multipart with `photo` (one or more photos of one product). The photos go to the
Immich *Products* album, and the vision model reads the name, brand and size in the background.
→ `201 {"id": 26, "status": "pending"}`
`POST /api/products/{id}`: JSON `{"name", "brand", "size"}`. → the product.
`POST /api/products/{id}/merge` `{"into": 5}`: moves photos, aliases and purchases to 5. → product 5.

`GET /api/shopping-list` →
```json
{"rule": "due when the days since the last purchase reach 90% of the usual days between purchases",
 "items": [{"product_id": 5, "name": "Whole milk", "brand": "Great Value", "size": "1 gal",
            "last_bought": "2026-10-20", "every_days": 7, "overdue_days": 4,
            "usual_store": "Walmart", "last_price": 329, "best_price": 309, "best_store": "Aldi"}]}
```

## Google Photos Picker

`GET /api/picker/status` → `{"configured": true, "signed_in": false, "auth_url": "/picker/auth"}`
`POST /api/picker/sessions` `{"album": "receipts"|"products"}` → `{"id": "...", "picker_uri": "https://photos.google.com/..."}`.
Open `picker_uri`, choose the photos, then poll:
`GET /api/picker/sessions/{id}` → `{"state": "waiting"|"importing"|"done"|"error", "imported": 3, "error": null}`.
Photos chosen for `receipts` become **one receipt each**. Use merge for one receipt photographed in parts.

## Reconciliation (bank and online orders)

`POST /api/bank/import`: a bank CSV as the request body, or multipart `file`. Bank of America exports and
any CSV with Date, Description and Amount columns are accepted; overlapping imports add no duplicates.
→ `{"rows": 120, "new": 31}`
`POST /api/bank/sync-budget` copies new transactions from the budget project's store (`BUDGET_DB`).
→ `{"rows": 640, "new": 12}`

`POST /api/orders`: an online order (Instacart and others). The body is the order confirmation email (`.eml`,
or text pasted in as JSON `{"text": "...", "source": "instacart"}`). The text model structures it into a
receipt with `source` `instacart`, with no photos, which then goes through the same checks.
→ `201 {"id": 18, "status": "pending"}`

`POST /api/reconcile/run` matches automatically (merchant, amount, bank date 1 day before to 5 days after
the purchase). → `{"matched": 9}`
`GET /api/reconcile` →
```json
{"receipts_unmatched": [Receipt summary, ...],
 "bank_unmatched": [{"id": 3, "date": "2026-10-13", "description": "GIANT EAGLE #4 ...", "amount": -4519, "account": "checking"}],
 "matched": [{"receipt_id": 17, "bank_id": 3, "method": "auto|manual"}]}
```
`POST /api/reconcile/match` `{"receipt_id": 17, "bank_id": 3}` (or `"unmatch": true`) → `{"ok": true}`

## Pages (for a phone browser)

`/` home with counts · `/capture` camera PWA · `/review` queue · `/review/{id}` photo beside the fields ·
`/items` unmatched items · `/products` gallery · `/products/{id}` · `/shopping` · `/reconcile` · `/picker`

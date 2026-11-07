# Photo OCR

Phone photos read by local models on the AI Workstation and turned into checked, structured records. Two kinds
of photo go through the same pipeline:

- **Receipts:** store, date, tax, total and line items.
- **Products:** name, brand and size from the packaging.

Two things are built on those records:

- **Shopping list:** receipt lines are matched to products, which gives purchase history, prices and where
  you buy things, and from that what is due again.
- **Bank reconciliation:** receipts are matched to the bank transactions in the budget project's store.

The design is in [PRD.md](PRD.md); the HTTP API is in [API.md](API.md).

## How it fits together

```
photos (capture page, Immich albums, Syncthing, Google Photos Picker)
  -> Immich, and one row per photo in the store
  -> read by each configured engine (engines.json, through the workstation's llama.cpp router)
  -> checks + engine agreement -> auto-accepted, or the review queue (or "retake")
  -> receipts and products
       -> receipt lines matched to products -> purchase history -> shopping list
       -> receipts matched to bank charges read from budget.db
```

| Part | Where |
|---|---|
| Reading photos: prompts and schemas per kind, model and OCR calls | `parsing.py` |
| Checks, engine disagreement, confidence and buckets, per kind | `checks.py` |
| The pipeline: ingest, batches per engine, review, corrections | `service.py` |
| Store: SQLite, the PRD's tables plus corrections, stores, bank copy | `db.py` |
| Immich client (or a local folder without Immich) | `immich.py` |
| Google Photos Picker | `picker.py` |
| Catalog matching, purchase history, shopping list | `catalog.py` |
| Bank reconciliation against budget | `reconcile.py` |
| HTTP API, phone pages, background worker | `web.py`, `pages.py` |
| Bake-off: compare engines on labeled receipts and products | `bakeoff.py` |

The TaskDeck jobs are in `taskdeck/src/taskdeck/jobs/photo_ocr/`
([taskdeck/docs/jobs/photo_ocr.md](../taskdeck/docs/jobs/photo_ocr.md)). The screens that need the photo
beside the editable fields are web pages served by the service; the TaskDeck jobs list what needs doing and
link to them.

## Status

Built: everything above. Not yet run against the real Immich, Google, or on real photos; the tests use
stand-ins for those (see **Not yet tried for real** at the end). The models are on the workstation and load
(`qwen3-vl-8b`, `paddleocr-vl`).

**Until the bake-off picks engines,** both kinds use the first engine in `engines.json`.
`PHOTO_OCR_RECEIPT_ENGINE` and `PHOTO_OCR_PRODUCT_ENGINE` choose; two names, comma-separated, run both and
send readings they disagree on to review.

## The service

One process: the API, the phone pages and a background worker. The worker checks Immich every minute (and
right away after an upload), reads pending receipts and products engine by engine (so the model server loads
each model once per batch), and matches receipt lines to the catalog. Python 3.10+, standard library;
Pillow recommended.

```bash
python web.py                      # http://127.0.0.1:8093, data in ~/photo-ocr (PHOTO_OCR_HOME)
python web.py work                 # one round of background work, printed (handy over SSH)
python web.py export-labels        # approved receipts and products -> the bake-off set
```

### On the workstation (Docker)

1. **Settings** (optional to start). Put any of these in `~/.config/photo-ocr.env`; the model server and key
   come from `~/.config/secrets.env` (`LLM_URL`, `LLM_API_KEY`), like budget:

   | Variable | What |
   |---|---|
   | `IMMICH_URL`, `IMMICH_API_KEY` | Immich, e.g. `http://localhost:2283`. The key needs asset upload/read and album read/create/update. Without them, photos stay in `~/photo-ocr/photos/`. |
   | `PHOTO_OCR_RECEIPT_ENGINE` | engine(s) from `engines.json` for receipts, e.g. `qwen3-vl-8b` or `qwen3-vl-8b,tesseract+llm` |
   | `PHOTO_OCR_PRODUCT_ENGINE` | engine(s) for product photos (default: the receipt engines) |
   | `PHOTO_OCR_LLM_URL`, `PHOTO_OCR_TEXT_MODEL` | model server and text model for the OCR engines' text step and hard catalog matches (default `LLM_URL` and `qwen3-vl-8b`, so this work doesn't load the coding model) |
   | `PHOTO_OCR_RECEIPTS_ALBUM`, `PHOTO_OCR_PRODUCTS_ALBUM` | Immich album names (default `Receipts`, `Products`; created if missing) |
   | `IMMICH_LIBRARY_ID` | the Syncthing external library (below) |
   | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PHOTO_OCR_PUBLIC_URL` | Google Photos Picker (below) |

2. **Start it:**

   ```bash
   mkdir -p ~/photo-ocr && cd ~/develai/ScratchPatch/photo-ocr && docker compose up -d --build photo-ocr
   ```

   Logs: `docker logs -f photo-ocr`. Check: `curl -s http://127.0.0.1:8093/api/health`.

3. **Reach it from the phone** with Tailscale Serve, HTTPS and your own devices only:

   ```bash
   sudo tailscale serve status                      # see what already uses which port
   sudo tailscale serve --bg --https=8444 8093      # budget already has 443
   ```

   The address is like `https://<workstation>.<tailnet>.ts.net:8444`. Open `/capture` on the phone and use
   the browser's *Add to Home screen* to install it like an app. Put the same address in the TaskDeck Photo
   OCR jobs' settings.

**After pulling new code:** `git pull && docker compose up -d --build photo-ocr`.

### Getting photos in

- **Capture page** (`/capture`): choose *Receipt* or *Product*, take one or more photos (*Add another photo*
  between shots of a long receipt, or of a product's front and back), then *Done*. All photos before *Done*
  are one receipt or one product.
- **Immich albums:** a photo added to the *Receipts* album in the Immich app becomes a receipt, and one added
  to *Products* becomes a product (one photo each; *Merge into* on the receipt page joins a receipt
  photographed in parts, and on the product page joins two photos of one product).
- **Syncthing:** sync a phone folder to the workstation, with `Receipts/` and `Products/` subfolders. In
  Immich, add the synced folder as an external library (Administration → External libraries) and set
  `IMMICH_LIBRARY_ID` to its id (it's in the library page's URL). The service adds new photos from those
  subfolders to the matching album.
- **Google Photos Picker** (`/picker`): pick photos in Google Photos; they're copied into Immich.
  One-time setup in the Google Cloud console:
  1. Create a project, enable the **Google Photos Picker API**.
  2. OAuth consent screen: External, add yourself as a test user.
  3. Credentials → OAuth client ID → **Web application**, with authorized redirect URI
     `https://<your Tailscale Serve address>/picker/callback`. Set `PHOTO_OCR_PUBLIC_URL` to that address
     (without `/picker/callback`) and the client id and secret in `photo-ocr.env`.
  4. Open `/picker` and sign in. While the consent screen is in *Testing*, Google expires the sign-in
     after 7 days; sign in again then.

### Checks and review

Every reading, receipt or product, lands in one of three buckets:

| Bucket | Receipt | Product |
|---|---|---|
| **auto** (saved without review) | items + tax = total, plausible date, known store, prices look like prices, engines agree | the name says what it is, the size has an amount and a unit, engines agree |
| **review** | a check failed (including a store seen for the first time) or the engines disagree | a check failed or the engines disagree on name, brand or size |
| **retake** | total or date unreadable | no name readable |

`/review` shows receipts that need a look and, under *Products to check*, products. Suspect fields are
highlighted and each engine's reading is shown where they differ. *Approve* saves the edits, keeps every
change as a correction (so `export-labels` grows the bake-off set) and moves to the next one. *Read again*
re-reads with the current engines.

### Catalog, shopping, reconciliation

- **`/items`:** receipt lines with no product yet, with a suggestion (text similarity, or the text model for
  abbreviations). Confirming saves an alias, so the same receipt text matches by itself next time.
  *Not a product* is for bag fees and the like. Products waiting for review can already be matched.
- **`/products`:** the gallery; each product page has its readings, checks, aliases and purchase history.
- **`/shopping`:** products due again. The rule is a placeholder until the PRD's open question is settled:
  due when the days since the last purchase reach 90% of the usual days between purchases (two or more
  purchases needed).
- **`/reconcile`:** *Match* reads the latest bank transactions from the budget store (`~/budget/budget.db`,
  read-only; bank exports are imported in budget, and more accounts are added there), then matches each
  approved receipt to a charge with the same amount, a similar merchant, and a bank date from one day before
  to five days after the purchase.

## The bake-off (phase 1)

`bakeoff.py` runs each candidate engine on hand-labeled photos, 25 to 30 receipts plus some products, and
scores the results. It needs Python 3.10+ and the standard library; Pillow is optional but recommended,
because without it phone photos go to vision models unrotated and full size.

| Engine (`engines.json`) | How it reads |
|---|---|
| `qwen3-vl-8b` | All photos of the receipt or product in one request to a vision model; JSON back |
| `paddleocr-vl+llm` | PaddleOCR-VL transcribes each photo (`OCR:` prompt); a text model structures it |
| `paddleocr+llm` | PaddleOCR text with per-line confidence; a text model structures it |
| `doctr+llm` | docTR, the same way |
| `tesseract+llm` | Tesseract, the same way (baseline) |

These candidates came from web roundups and haven't been tried on these photos; the bake-off decides, and
it may pick different engines for receipts and products. Every model is reached through the workstation's
llama.cpp server in router mode (one endpoint; the request's `model` field picks a section of its preset
file, and `--models-max 1` keeps one model on the GPU at a time). The `model` names in `engines.json` must
match the preset's section names; `qwen3-vl-8b` and `paddleocr-vl` are both there.

### 1. Build the test set

```bash
cd ~/develai/ScratchPatch/photo-ocr
python bakeoff.py add ~/Downloads/IMG_0412.jpg --tags thermal,faded
python bakeoff.py add IMG_0413.jpg IMG_0414.jpg IMG_0415.jpg --tags long   # one receipt, top to bottom
python bakeoff.py add --product front.jpg back.jpg                          # one product
```

Each receipt gets a folder `r001`, `r002`, …, and each product `p001`, `p002`, …, under
`~/photo-ocr/bakeoff/set/` (or `PHOTO_OCR_BAKEOFF_HOME`), with the photos numbered in order. Items with more
than one photo are tagged `multi-photo` automatically. Include crumpled, faded, thermal-paper and long
receipts. Photos must be JPEG, PNG or WebP, so export HEIC as JPEG first. They stay out of the repo.

### 2. Label it

A receipt's `label.json`:

```json
{
  "store": ["Giant Eagle", "GIANT EAGLE #4"],
  "date": "2026-10-12",
  "tax": "0.42",
  "total": "7.19",
  "items": [
    {"name": "BANANAS", "quantity": 1, "price": "1.29"},
    {"name": "MILK 2% GAL", "price": "3.99"},
    {"name": "BONUS SAVINGS", "price": "-1.00"}
  ]
}
```

- `store`: one name, or a list of accepted spellings. Matching ignores case and punctuation.
- `price`: the amount printed on that line (the line total). Discounts and coupons are items with a
  negative price, and deposits and bag fees are items too, so that items + tax = total.
- Leave out payment, change and loyalty lines.

A product's `label.json`:

```json
{"name": "Whole milk", "brand": "Great Value", "size": "1 gal"}
```

- `name`: what the product is, without brand or size. `brand` and `size` may be `null`.
- `size`: amount and unit as printed; "12 FL. OZ" and "12 fl oz" count as the same, and a pack keeps its
  count ("12 x 12 fl oz").

To save typing, run one engine first and start from its output: `python bakeoff.py label qwen3-vl-8b`
writes `label.draft.json` next to each unlabeled item. Correct the draft against the photos, then rename it
to `label.json`. Then run:

```bash
python bakeoff.py check      # unparseable values, implausible dates, items + tax != total, product sizes
```

Approved readings from the service join the set too: `python web.py export-labels` writes `sNNNN`
(receipts) and `psNNNN` (products) folders.

### 3. Run and score

```bash
python bakeoff.py run qwen3-vl-8b          # one engine
python bakeoff.py run --all                # every engine in engines.json, one after another
python bakeoff.py score                    # writes ~/photo-ocr/bakeoff/results/report.md
```

The model server comes from `PHOTO_OCR_LLM_URL`, falling back to `LLM_URL`; the OCR engines' text model from
`PHOTO_OCR_TEXT_MODEL`, default `qwen3-vl-8b`; and the key from `LLM_API_KEY`.

Each engine's output is saved per item in `results/<engine>/<item>.json`, holding the raw reply, the parsed
JSON, the seconds taken and any OCR lines with confidence (the `engine_runs` table of the PRD). Re-running
skips items that are already done; use `--force` to redo them, or `--receipt r007` (or `p002`) for one. OCR
engines first OCR every item and then structure every item, so the GPU swaps models once per run. Because
the first request also loads the model, compare engines on median seconds.

The report has a section per kind:

- **Receipts** (scoring follows the PRD): total, date, store and tax must match exactly; a line item counts
  as found when its name is at least 60% similar (`--name-threshold`) and its price is within a cent. Per
  engine: field accuracy, line-item precision / recall / F1, receipts fully right, seconds, and how far the
  items + tax = total check alone can be trusted. Then accuracy by condition (thermal, faded, …), engine
  pairs (are they right when they agree?) and per receipt.
- **Products:** name at least 80% similar, brand and size equal once normalized. Per engine: name, brand,
  size, all right, seconds; then per product.

### Bake-off with Docker

```bash
cd ~/develai/ScratchPatch/photo-ocr
docker compose build                                 # Tesseract built in; see Dockerfile for the others
docker compose run --rm bakeoff run --all
docker compose run --rm bakeoff score
```

### Installing the OCR back ends (outside Docker)

- Tesseract: `sudo apt install tesseract-ocr`
- docTR: `pip install "python-doctr[torch]"`
- PaddleOCR: `pip install paddleocr paddlepaddle-gpu` (the `paddlepaddle` CPU build also works). Both
  PaddleOCR 2.x and 3.x APIs are handled, but neither has been run here yet.
- PaddleOCR-VL: in the workstation's llama.cpp preset (`[paddleocr-vl]`). It takes one image per request and
  fixed task prompts such as `OCR:`, which is how `engines.json` calls it.

## Tests

```bash
cd photo-ocr && pytest -q
```

They use stand-ins for llama-server, Immich and Google (`tests/fakes.py`), so they need no GPU, models,
accounts or OCR installs.

## Not yet tried for real

These were written from the services' documentation and tested only against the stand-ins:

- **Immich API calls** (`immich.py`): written for Immich v3 (the workstation runs v3.3.6). An album's photos
  are listed with `POST /search/metadata` filtered by the album, since v3's album response carries only
  `assetCount`. The `albumIds`, `libraryId` and `page` search fields used there were deprecated in v3.2 (still
  accepted on v3.3.6); their replacements are a filter object and cursor paging. The upload retries without
  the `deviceAssetId`/`deviceId` fields if the server refuses them.
- **Google Photos Picker** (`picker.py`): OAuth, sessions, media items, download.
- **The engines on real photos:** `qwen3-vl-8b` and `paddleocr-vl` load on the workstation but haven't read
  a receipt or product yet; PaddleOCR and docTR haven't been run (Tesseract has). The checks and buckets are
  only as good as the readings; the bake-off measures that.

## Later

- **Budget on Postgres, with receipt tags** (noted 2026-11-02): migrate the budget project to a Postgres
  database, and have budget read this project's receipt data so it can tag each bank transaction as having a
  receipt photo or not. That work belongs in budget. Open with it: whether photo-ocr's store moves to the
  same Postgres (the PRD's "SQLite or Postgres?" question), which would let budget read receipts directly
  instead of through this service's API. No Postgres runs on the workstation yet.
- **Several receipts at once: grouping in the photo picker** (option C, noted 2026-11-07): receipts are
  usually several photos each and sometimes come in batches, so Receipt capture should let you group photos
  in the picker. Select a receipt's photos top to bottom, tap **+ Next receipt**, select the next, and so on;
  the check circles show 1a, 1b, 2a, … and one **Add** sends every group. It reuses what Product photo already
  has (`Field.grouped`, one group per item, sent and read as one batch). Product photo keeps its two buttons
  (**1 product** / **N products**, option A), which were tried on the phone and kept.

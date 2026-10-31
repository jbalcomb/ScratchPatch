# Receipts

Phone photos of receipts and products, read by local models on the AI Workstation, turned into structured
records and cataloged: purchase history, prices, where you buy things, shopping lists and bank
reconciliation. The design is in [PRD.md](PRD.md); the HTTP API is in [API.md](API.md).

## Status

Every phase in the PRD is built. Nothing has run yet against the real Immich, Google, models or receipts:
the tests use stand-ins for those (see **Not yet tried for real** at the end).

| Phase | What | Where |
|---|---|---|
| 1 | Parsing bake-off: run candidate engines on labeled receipts, score, compare | `bakeoff.py`, `parsing.py` |
| 2 | Service, Immich albums, capture PWA with multi-photo receipts | `web.py`, `service.py`, `immich.py`, `pages.py` |
| 3 | Parsing, checks, confidence buckets, review page | `checks.py`, `/review` |
| 4 | Syncthing (Immich external library), Google Photos Picker | `service.py`, `picker.py`, `/picker` |
| 5 | Product gallery, catalog, line-item matching with aliases, shopping list | `catalog.py`, `/items`, `/products`, `/shopping` |
| 6 | Bank CSV and budget import, online orders, reconciliation | `reconcile.py`, `/reconcile` |

The TaskDeck plug-ins (Receipts and Reconciliation jobs) are in `taskdeck/src/taskdeck/jobs/receipts/`
([taskdeck/docs/jobs/receipts.md](../taskdeck/docs/jobs/receipts.md)). The heavy screens, such as the photo
beside the editable fields, are web pages served by the service; the TaskDeck jobs list what needs doing
and link to them.

**Until the bake-off picks an engine,** the service uses the first engine in `engines.json`
(`RECEIPTS_ENGINE` chooses; two names, comma-separated, runs both and uses their disagreement for review).

## The service

One process: the API, the phone pages and a background worker. The worker checks Immich every minute (and
right away after an upload), parses pending receipts engine by engine (so llama-swap loads each model once
per batch), reads new product photos and matches line items to the catalog. Python 3.10+, standard
library; Pillow recommended.

```bash
python web.py                      # http://127.0.0.1:8093, data in ~/receipts (RECEIPTS_HOME)
python web.py work                 # one round of background work, printed (handy over SSH)
python web.py export-labels        # approved receipts -> the bake-off set, so corrections grow it
```

### On the workstation (Docker)

1. **Settings.** Put these in `~/.config/receipts.env` (model settings come from `~/.config/secrets.env`,
   like budget):

   | Variable | What |
   |---|---|
   | `IMMICH_URL`, `IMMICH_API_KEY` | Immich, e.g. `http://localhost:2283`. The key needs asset upload/read and album read/create/update. Without them, photos stay in `~/receipts/photos/`. |
   | `RECEIPTS_ENGINE` | engine(s) from `engines.json`, e.g. `qwen3-vl-8b` or `qwen3-vl-8b,tesseract+llm` |
   | `RECEIPTS_PRODUCT_ENGINE` | vision engine for product photos (default: the first `vlm` engine) |
   | `RECEIPTS_LLM_URL`, `RECEIPTS_TEXT_MODEL` | text model for orders and hard catalog matches (default `LLM_URL`, `LLM_MODEL`) |
   | `RECEIPTS_ALBUM`, `PRODUCTS_ALBUM` | album names (default `Receipts`, `Products`; created if missing) |
   | `IMMICH_LIBRARY_ID` | the Syncthing external library (below) |
   | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `RECEIPTS_PUBLIC_URL` | Google Photos Picker (below) |

2. **Start it:** `cd ~/ScratchPatch/receipts && docker compose up -d --build`. Logs: `docker logs -f receipts`.

3. **Reach it from the phone** with Tailscale Serve, HTTPS and your own devices only:

   ```bash
   sudo tailscale serve status                      # see what already uses which port
   sudo tailscale serve --bg --https=8444 8093      # budget may already have 443 or 8443
   ```

   The address is like `https://ai-workstation.tail1234.ts.net:8444`. Open `/capture` on the phone and
   use the browser's *Add to Home screen* to install it like an app. Put the same address in the TaskDeck
   Receipts jobs' settings.

**After pulling new code:** `git pull && docker compose up -d --build`.

### Getting photos in

- **Capture page** (`/capture`): take one or more photos, *Add another photo* between shots of a long
  receipt, then *Done*. All photos before *Done* are one receipt. Switch to *Product* for product photos.
- **Immich albums:** a photo added to the *Receipts* album in the Immich app becomes a receipt (one photo
  each; on the review page, *Merge into* joins a receipt photographed in parts). The *Products* album works
  the same way for products.
- **Syncthing:** sync a phone folder to the workstation, with `Receipts/` and `Products/` subfolders. In
  Immich, add the synced folder as an external library (Administration → External libraries) and set
  `IMMICH_LIBRARY_ID` to its id (it's in the library page's URL). The service adds new photos from those
  subfolders to the matching album.
- **Google Photos Picker** (`/picker`): pick photos in Google Photos; they're copied into Immich.
  One-time setup in the Google Cloud console:
  1. Create a project, enable the **Google Photos Picker API**.
  2. OAuth consent screen: External, add yourself as a test user.
  3. Credentials → OAuth client ID → **Web application**, with authorized redirect URI
     `https://<your Tailscale Serve address>/picker/callback`. Set `RECEIPTS_PUBLIC_URL` to that address
     (without `/picker/callback`) and the client id and secret in `receipts.env`.
  4. Open `/picker` and sign in. While the consent screen is in *Testing*, Google expires the sign-in
     after 7 days; sign in again then.

### Review, catalog, shopping, reconciliation

- **`/review`:** receipts that need a person: *review* (a check failed, a new store, or the engines
  disagree; suspect fields are highlighted) and *retake* (total or date unreadable: add a photo or type
  them). *Approve* saves the edits, keeps every change as a correction and moves to the next receipt.
  Receipts that pass every check from a known store are approved without review. Every store starts as new,
  so the first receipt from each store gets a look.
- **`/items`:** line items with no product yet, with a suggestion (text similarity, or the text model for
  abbreviations). Confirming saves an alias, so the same receipt text matches by itself next time.
  *Not a product* is for bag fees and the like.
- **`/products`:** the gallery. A product photo is read by the vision model for name, brand and size;
  fix them on the product page, merge duplicates, see the purchase history.
- **`/shopping`:** products due again. The rule is a placeholder until the PRD's open question is settled:
  due when the days since the last purchase reach 90% of the usual days between purchases (two or more
  purchases needed).
- **`/reconcile`:** import a bank CSV or sync from the budget store, then *Match*: each approved receipt is
  matched to a charge with the same amount, a similar merchant, and a bank date from one day before to five
  days after the purchase. Paste an Instacart order confirmation email to add it as a receipt (source
  *instacart*); it goes through the same checks.

## The bake-off (phase 1)

`bakeoff.py` runs each candidate engine on 25 to 30 real, hand-labeled receipts and scores the results. It
needs Python 3.10+ and the standard library; Pillow is optional but recommended, because without it phone
photos go to vision models unrotated and full size.

| Engine (`engines.json`) | How it reads a receipt |
|---|---|
| `qwen3-vl-8b` | All photos of the receipt in one request to a vision model; JSON back |
| `paddleocr-vl+llm` | PaddleOCR-VL transcribes each photo (`OCR:` prompt); a text LLM structures it |
| `paddleocr+llm` | PaddleOCR text with per-line confidence; a text LLM structures it |
| `doctr+llm` | docTR, the same way |
| `tesseract+llm` | Tesseract, the same way (baseline) |

These candidates came from web roundups and haven't been tried on these receipts; the bake-off decides.
Every model is reached through an OpenAI-compatible endpoint (llama-server, or llama-swap in front of it,
which keeps one model on the shared GPU at a time). The `model` names in `engines.json` must match your
llama-swap config. Edit the file to add, drop or retune candidates.

### 1. Build the test set

```bash
cd ~/ScratchPatch/receipts
python bakeoff.py add ~/Downloads/IMG_0412.jpg --tags thermal,faded
python bakeoff.py add IMG_0413.jpg IMG_0414.jpg IMG_0415.jpg --tags long   # one receipt, top to bottom
```

Each receipt gets a folder under `~/receipts/bakeoff/set/` (`r001`, `r002`, …; or set `RECEIPTS_BAKEOFF_HOME`), with its
photos numbered in order. Receipts with more than one photo are tagged `multi-photo` automatically. Include
crumpled, faded, thermal-paper and long receipts. Photos must be JPEG, PNG or WebP, so export HEIC as JPEG
first. They stay out of the repo.

### 2. Label it

Write `label.json` in each receipt folder:

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

To save typing, run one engine first and start from its output: `python bakeoff.py label qwen3-vl-8b`
writes `label.draft.json` next to each unlabeled receipt. Correct the draft against the photos, then
rename it to `label.json`. Then run:

```bash
python bakeoff.py check      # unparseable values, implausible dates, items + tax != total
```

### 3. Run and score

```bash
python bakeoff.py run qwen3-vl-8b          # one engine
python bakeoff.py run --all                # every engine in engines.json, one after another
python bakeoff.py score                    # writes ~/receipts/bakeoff/results/report.md
```

The model server comes from `RECEIPTS_LLM_URL`, falling back to `LLM_URL`. The text model for the
OCR + LLM engines comes from `RECEIPTS_TEXT_MODEL`, falling back to `LLM_MODEL`, and the key from
`LLM_API_KEY`. These are the same `~/.config/secrets.env` names that budget uses.

Each engine's output is saved per receipt in `results/<engine>/<receipt>.json`, holding the raw reply,
the parsed JSON, the seconds taken and any OCR lines with confidence (the `engine_runs` table of the PRD).
Re-running skips receipts that are already done; use `--force` to redo them, or `--receipt r007` for one.
OCR + LLM engines first OCR every receipt and then structure every receipt, so the GPU swaps models once
per run rather than once per receipt. Because the first request also loads the model, compare engines on
median seconds.

Scoring follows the PRD. Total, date, store and tax must match exactly. A line item counts as found when
its name is at least 60% similar (`--name-threshold`) and its price is within a cent. The report has:

- **Per engine:** field accuracy, line-item precision / recall / F1, receipts fully right, and seconds.
  It also shows how often the engine's own items + tax = total check passes, and how often the receipt
  is fully right when that check passes, which tells you whether the check can be trusted for auto-accept.
- **By condition:** total accuracy for each tag (thermal, faded, multi-photo, …).
- **Engine pairs:** how often two engines agree on total and date, and whether they are right when they
  agree. This answers whether to run two engines permanently and use their disagreement for confidence.
- **Per receipt:** what each engine got right or wrong.

### Bake-off with Docker

```bash
cd ~/ScratchPatch/receipts
docker compose build                                 # Tesseract built in; see Dockerfile for the others
docker compose run --rm bakeoff run --all
docker compose run --rm bakeoff score
```

### Installing the OCR back ends (outside Docker)

- Tesseract: `sudo apt install tesseract-ocr`
- docTR: `pip install "python-doctr[torch]"`
- PaddleOCR: `pip install paddleocr paddlepaddle-gpu` (the `paddlepaddle` CPU build also works). Both
  PaddleOCR 2.x and 3.x APIs are handled, but neither has been run here yet.
- PaddleOCR-VL needs a server that speaks the OpenAI chat API, behind llama-swap like the others. Whether
  llama.cpp serves it, or it needs vLLM, still has to be checked on the workstation.

## Tests

```bash
cd receipts && pytest -q
```

They use stand-ins for llama-server, Immich and Google (`tests/fakes.py`), so they need no GPU, models,
accounts or OCR installs.

## Not yet tried for real

These were written from the services' documentation and tested only against the stand-ins. Check them
first tonight:

- **Immich API calls** (`immich.py`): upload, albums, original download, and metadata search for the external
  library. The upload retries without the `deviceAssetId`/`deviceId` fields if the server refuses them.
- **Google Photos Picker** (`picker.py`): OAuth, sessions, media items, download.
- **The engines:** model names in `engines.json` must match your llama-swap config; PaddleOCR and docTR
  haven't been run (Tesseract has); whether llama.cpp can serve PaddleOCR-VL is unknown.
- **Instacart:** order emails are read by the text model; there's no Instacart API (PRD open question).
- **Model output on real receipts:** the checks and buckets are only as good as the readings. The bake-off
  measures that.

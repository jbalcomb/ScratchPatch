# PDF Parser

A PDF in, a folder out: all the text, the tables, the images, and OCR of the images and of scanned pages, with a
`manifest.json` that says where every piece sits on its page. Runs on the AI Workstation; the TaskDeck job
**PDF Parser** sends PDFs from the phone and shows the result
([taskdeck/docs/jobs/pdf_parser.md](../taskdeck/docs/jobs/pdf_parser.md)).

```bash
pip install -r requirements.txt          # PyMuPDF, Pillow; plus the tesseract program (apt install tesseract-ocr)
python parse_pdf.py file.pdf             # -> ./file/
python parse_pdf.py file.pdf -o out/     # -> out/file/
python parse_pdf.py file.pdf --no-vision # tier-1 image checks only
python parse_pdf.py --stats out/         # how much the vision model was needed (from the QA logs)
python web.py                            # the service: http://127.0.0.1:8096, jobs in ~/pdf-parser
pytest                                   # tests (stand-in vision server; OCR tests skip without tesseract)
```

## What happens to a PDF

| Step | How | Where |
|---|---|---|
| Rotation | A page stored rotated (`/Rotate`) is read in its own coordinates (tables from an unrotated copy of it, where PyMuPDF's table finder agrees with the text), and every box is then turned into the page as displayed, inside its CropBox: printer's marks outside the trim stay out. Rotated tables are re-read row by row as displayed. A table whose columns are separated only by white space (no ruled lines) isn't found on a rotated page; its text still comes out in order. | `parse_pdf.parse`, `_grid` |
| Triage | A page with a text layer is a **text** page. One with almost none and a page-sized image is OCR'd: if that reads 40+ words at 60%+ confidence it is **scanned** (the page's picture isn't an image element); otherwise it is an **image** page, like a title page with a full-page drawing, whose picture is kept as an image and whose few words are kept as OCR text. | `parse_pdf.triage` |
| OCR text | OCR blocks read at under 60% mean confidence are marked `low_confidence` and kept. On **image** pages the vision model reads every OCR block again from the page render (Tesseract does badly on decorative lettering, and its confidence doesn't always show it): its reading becomes the block's `text` (`source: "vision"`), Tesseract's stays as `ocr_text`, and a block it finds no text in is left with empty text. Without the vision model the Tesseract text stays. | `parse_pdf.reread_with_vision` |
| Text | PyMuPDF blocks → lines → spans in reading order, with bbox, font, size, bold/italic, colour. Scanned pages: rendered at 300 DPI and OCR'd by Tesseract, as blocks with boxes and confidence. | `parse_pdf.text_blocks`, `ocr_page` |
| Tables | PyMuPDF `find_tables` finds them; each cell's text comes from the text layer's words in it (PyMuPDF's own table text can lose its spaces). Each table is a CSV, plus its bbox, header and cell boxes in the manifest. One-column "tables" (framed lists) are left as text. Text blocks inside a table are marked `in_table`. | `parse_pdf.tables` |
| Images | The embedded image first; the page rendered under its box when it can't be taken out. Then the QA loop below. | `images.py` |
| OCR | Tesseract on every image attempt: text, mean word confidence, word count. | `ocr.py` |

Vector drawings (charts drawn with lines rather than placed as images) aren't extracted as images yet.

### The image QA loop

**Tier 1, heuristics** (`images.check`). An attempt is flagged when it is:

| Flag | Test | Weight |
|---|---|---|
| `blank` | grey-level spread under 4 | 0.6, hard |
| `too_small` | under 48 px on a side | 0.4, hard |
| `clipped` | a page render whose content runs into 1–3 of its edges (a photo fills all 4), or a box past the page edge | 0.3, hard |
| `text_overlap` | text blocks cover over half its box | 0.2 |
| `odd_aspect` | longer side over 8× the shorter | 0.15 |
| `low_ocr` | 3+ words read at under 50% mean confidence | 0.15 |

Score = 1 − the weights of its flags. Two more checks don't retry: the same image placed again, or one whose
difference hash is within 4 bits of an image already kept, is a **duplicate** (`duplicate_of`); a box under
24 pt is an icon or bullet, saved as **small** and not checked.

**Tier 2, the vision model** (`vision.py`). Flagged attempts (every attempt with `PDF_PARSER_VISION_ALL=1`) go
to the llama.cpp server's `/v1/chat/completions` with the image, asking for
`{complete: bool, content_type: str, issue: str|null}` (a JSON schema). Complete and meaningful (not blank,
fragment, decoration or background) passes, and the score rises to at least 0.9; otherwise the score halves.

**Retries.** A failed attempt is retried with the next strategy, in the order **hidpi** (page render at
300 DPI) → **pad** (box grown 10% each side) → **merge** (with image boxes touching it) → **enhance**
(deskew + contrast), picking the first that fits the problem (pad and merge for a cut-off crop, enhance for poor
OCR, hidpi below 200 DPI or for poor OCR / small images) and otherwise the next in order. Once the vision model
has turned an attempt down, every retry of that image is shown to it too, even with no flags. At most **3 attempts**
per image; the best-scoring one that passed is kept. If none passed, the best is saved with status
**needs_review**, and every attempt is in `images/attempts/` so a person can pick.

### When the vision model isn't there

Parsing never fails because of it. Each PDF asks it until **3 requests in a row fail**, **5 minutes** have gone
on failed requests, or it answers that it can't take images; after that the PDF stops asking
(`summary.vision.stopped` says why). Without a verdict, an attempt passes if it has no hard flag, so images
it would have checked go to needs_review only when they look wrong (`vision_skipped` on the image says why).
The next PDF tries again.

Every request (time, how long, outcome, verdict or text read) and every image's outcome is written to the bundle's
`qa_log.jsonl`. `python parse_pdf.py --stats DIR` adds them up over all bundles under DIR (on the workstation:
`~/pdf-parser/jobs`): how many images tier 1 flagged, how many needed the model, its availability and median
time, how many images it rescued (`kept_by_vision`) and how many went to review because it wasn't there
(`needs_review_without_vision`). Those numbers decide whether the model is worth keeping in the loop.

**At startup** (`Vision.check`) the service asks the server for its model list and makes sure the model is in
it and was started with an mmproj (router mode lists each model's arguments; a single-model server reports
`modalities` on `/props`). It only looks, so the router doesn't load the model and push another off the GPU. If
it can't tell, the first image request settles it: a "can't take images" answer stops the PDF asking at once,
with a message that says to add the model's mmproj. `GET /api/health` reports the state (`ok`, `unknown`,
`unavailable`) and checks again every 5 minutes while it isn't `ok`.

## The bundle

```
<name>/
  manifest.json   pages and elements (below)
  text.txt        all text in reading order, page by page
  tables/         p001-t1.csv, ...
  images/         p001-i1.png|jpg, ...; attempts/ for needs_review
  pages/          p002.png: renders of scanned pages (what the OCR read)
  qa_log.jsonl    vision requests and image outcomes
```

`manifest.json` is meant to be enough to rebuild the document in another format later (not built yet):

```jsonc
{
  "schema": "pdf-parser/1",
  "units": "PDF points (1/72 in); bbox is [x0, y0, x1, y1] from the page's top-left corner",
  "source": {"file": "report.pdf", "sha256": "…", "pages": 12, "metadata": {"title": "…"}},
  "pages": [{"page": 1, "width": 612, "height": 792, "rotation": 0, "kind": "text"},
            {"page": 2, "rotation": 90, "kind": "scanned|image", "render": "pages/p002.png", "ocr_confidence": 91.4, "ocr_words": 310, …}],
  "elements": [   // per page in reading order: "order" 1, 2, 3, …
    {"id": "p1-text1", "type": "text", "page": 1, "order": 1, "bbox": […], "source": "text_layer|ocr|vision",
     "text": "…", "in_table": null, "confidence": null,   // confidence, low_confidence: OCR only
     "ocr_text": "…",                                     // source "vision": what Tesseract had read
     "lines": [{"bbox": […], "dir": [1, 0], "spans": [{"text": "…", "bbox": […], "font": "Helvetica-Bold",
                "size": 20, "bold": true, "italic": false, "monospace": false, "serif": false,
                "superscript": false, "color": "#000000"}]}]},
    {"id": "p1-table3", "type": "table", "page": 1, "order": 3, "bbox": […], "file": "tables/p001-t1.csv",
     "rows": 3, "cols": 3, "header": ["Region", "Q1", "Q2"], "cells": [[[x0, y0, x1, y1], …], …]},
    {"id": "p1-i1", "type": "image", "page": 1, "order": 7, "bbox": […], "file": "images/p1-i1.png",
     "status": "kept|needs_review|duplicate|merged|small", "source": "embedded|render", "xref": 27,
     "width": 400, "height": 300, "dpi": 120, "score": 1.0, "flags": [],
     "vision": {"complete": true, "content_type": "chart", "issue": null},
     "ocr": {"text": "…", "confidence": 88.0, "words": 12},
     "attempts": [{"n": 1, "strategy": "embedded", "dpi": 120, "bbox": […], "score": 1.0, "flags": [], "vision": null}],
     "best_attempt": 1, "duplicate_of": null, "merged": ["p1-i2"], "merged_into": null, "vision_skipped": null}
  ],
  "summary": {"pages": 12, "text_pages": 11, "scanned_pages": 1, "image_pages": 0, "text_blocks": 140, "tables": 3,
              "images_kept": 8, "images_needs_review": 1, "images_duplicate": 2, "images_merged": 0,
              "images_small": 5, "vision": {"requests": 4, "failed": 0, "available": true, "stopped": null,
                                            "model": "qwen3-vl-8b"}}
}
```

Keys that don't apply to an element are left out.

## The service

`web.py`, one process: the HTTP API and one worker thread that parses one PDF at a time. Each job is a folder
`~/pdf-parser/jobs/<id>/` with the PDF, `status.json`, the bundle in `out/` and `bundle.zip`. A restart picks
unfinished jobs up again.

| Request | Answer |
|---|---|
| `GET /` | a page with the latest jobs, newest first, each with links to its files, review and .zip |
| `GET /api/health` | `{ok, vision: {state, model, multimodal, message, requests, failures, …}, tesseract, queued}` |
| `POST /api/jobs` | multipart, the PDF as field `pdf` (at most 200 MB) → `202 {id, file, status: "queued", url, zip}` |
| `GET /api/jobs/<id>` | `{id, file, status: queued/running/done/failed, step ("page 3 of 12"), error, summary, url, zip}` |
| `GET /api/jobs` | the latest 50 jobs |
| `GET /jobs/<id>/` | a page listing the bundle's files, with links; `/jobs/<id>/<path>` is one file |
| `GET /jobs/<id>.zip` | the whole bundle |
| `GET /jobs/<id>/review/` | the review page (below) |
| `GET /api/jobs/<id>/review` | pages, the reviewable elements with their reviews and state, coverage scores |
| `POST /api/jobs/<id>/reviews` | one review, JSON: `{element, decision: accepted\|corrected\|rejected, by, text?, rows?, bbox?, reason?}` |
| `GET /jobs/<id>/page/<n>.png` | page *n* as displayed, at 144 DPI (made once, kept in the job folder) |

Errors are `{"error": "..."}`. No login: keep it behind Tailscale.

### Reviewing a parse (proof of concept)

Open the bundle page (`/jobs/<id>/`) and follow **Review**, or go to `/jobs/<id>/review/`. It's on the tailnet only
for now. The design, including what's still to come, is in [REVIEW_SPEC.md](REVIEW_SPEC.md).

- **The page:** every reviewable element is a box over the page: blue text layer, orange OCR, purple vision,
  green table, teal image. A dashed box isn't checked yet; a red box is disputed.
- **An element:** click a box to see the page under it beside what was parsed.
  - **Accept** it.
  - **Save correction** after editing the text, editing the table's cells (rows and columns can be added and
    removed), or moving or resizing the box (drag it, or its corners).
  - **Reject** it as noise.
- **Queues:** *Disputed*, *Vision model* and *Needs review* (the two required ones), *OCR text*, and
  *Everything not checked*. **Next in queue** walks one.
- **Reviewer:** type your name; the browser remembers it.
- **Several reviews:** a second review that disagrees makes the element **disputed**. It waits for a third
  reviewer, and the page shows the newest review with its differences from the earlier ones marked. Two of three
  that agree settle it; three different reviews escalate it to the owner. Your own newer review replaces your
  earlier one.
- **Coverage, along the top:** the required queues, whole pages, text characters, tables, images, mixed pages,
  disputed and escalated elements, and how often each source was accepted as parsed.

Reviews are kept per PDF (by its SHA-256) in `~/pdf-parser/reviews/`, so re-uploading the same PDF keeps them.

**Not built yet:**
- applying reviews to a new parse (`--reviews`: re-cutting images and re-reading tables from corrected boxes);
- drawing a box for something the parser missed;
- the owner's role for escalations;
- review state in `manifest.json` for the rebuild;
- access from outside the tailnet.

### On the workstation (Docker)

1. **Settings.** The model server and key come from `~/.config/secrets.env` (`LLM_URL`, `LLM_API_KEY`), like
   budget and photo-ocr. Optional, in `~/.config/pdf-parser.env`:

   | Variable | What |
   |---|---|
   | `PDF_PARSER_LLM_URL` | another server than `LLM_URL` (a base URL or a `.../v1/chat/completions` URL) |
   | `PDF_PARSER_VISION_MODEL` | the vision model: a section name in the router's preset, with its mmproj (default `qwen3-vl-8b`) |
   | `PDF_PARSER_VISION_ALL` | `1`: ask the model about every image, not only flagged ones (to measure how much it's needed) |
   | `PDF_PARSER_OCR_LANG` | Tesseract languages (default `eng`; others need their `tesseract-ocr-<lang>` package in the image) |

   The router keeps one model on the GPU at a time, so the first image request of a PDF can wait while the
   vision model loads (the request timeout is 3 minutes).

2. **Start it:**

   ```bash
   mkdir -p ~/pdf-parser && cd ~/develai/ScratchPatch/pdf-parser && docker compose up -d --build
   ```

   Logs: `docker logs -f pdf-parser` (the first lines say whether the vision model was found and takes images).
   Check: `curl -s http://127.0.0.1:8096/api/health`. If `id -u` isn't 1000, start it with
   `UID=$(id -u) GID=$(id -g) docker compose up -d --build`.

3. **Reach it from the phone** at **https://pdf-parser.tailc54221.ts.net** (the TaskDeck job's default). HTTPS
   comes from a Tailscale sidecar container, `ts-pdf-parser`, defined in `~/develai/docker-compose.yml` on the
   workstation (outside this repo), which forwards to `localhost:8096`. Change or restart it there, not here.
   Check it from another device on the tailnet: `curl -s https://pdf-parser.tailc54221.ts.net/api/health`.

Port 8096 is the first one above those already taken on the workstation (8090, 8092–8095).

**After pulling new code:** `git pull && docker compose up -d --build`.

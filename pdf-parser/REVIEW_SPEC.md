# PDF Parser: human review — specification (draft)

**Status:** draft for discussion; nothing here is built yet. The open decisions are listed at the end.

## Why

The parser's own signals don't establish that a parse is correct. Tesseract's confidence is self-reported
(it read "Building Asn Army" at 77%). The vision model gives no confidence at all, and can return fluent text that
isn't on the page. A text layer has the right characters, but order, spacing, and where a table starts and ends can
still be wrong. The only check of a parse is a person comparing it with the page. This spec covers:

1. a review site where people check each element of a parsed document against the page, and correct it;
2. corrections (text, and the location and size of images and tables) fed back through the pipeline;
3. coverage scores: how much of the document a person has checked, measured several ways;
4. the review state and confidence of every element, kept in the manifest for the future rebuild to show.

## Terms

- **Bundle:** one parsed PDF (`manifest.json`, `text.txt`, `tables/`, `images/`, `pages/`, `qa_log.jsonl`).
- **Element:** one entry in `manifest.json` `elements[]`: a text block, a table or an image.
- **Source:** where an element's content came from: `text_layer`, `ocr` (Tesseract), `vision` (the vision
  model), or `human` (a reviewer's correction).
- **Review:** one person's judgement of one element: **accepted** (correct as parsed), **corrected** (they fixed
  it), or **rejected** (not real content: noise, a printer's mark, a duplicate).

## 1. What must be reviewed

| Queue | What | Rule |
|---|---|---|
| **Vision (required)** | Every element whose content or status the vision model decided: text re-reads (`source: "vision"`), and images whose kept/needs_review status came from a vision verdict | Every one is reviewed. Until it is, the element is marked unverified, and the document can't be called reviewed. |
| **Needs review** | Images with `status: "needs_review"`; OCR blocks with `low_confidence` | Every one is reviewed. |
| **OCR** | All other OCR text | Every one is reviewed. |
| **Text layer, tables, other images** | Everything else | Reviewed as coverage goals require (section 4). The review site shows these after the queues above. |

## 2. The review site

The site is static: HTML, JavaScript, and the bundle's files. It can be served from GitHub Pages or from the
pdf-parser service. One site per bundle, built by `python parse_pdf.py --review-site BUNDLE OUT`.

**Page view.** On the left, the page render. On top of it, a box for every element, coloured by source and
outlined by review state. On the right, the selected element:

- **Text:** the parsed text, editable, beside a crop of the page under its box. For OCR and vision text, the
  other readings are shown too (Tesseract's `ocr_text`, the model's reading).
- **Table:** the CSV as an editable grid, beside the crop. Rows and columns can be added, removed, merged and
  split.
- **Image:** the extracted image beside the page under its box, with every attempt for needs_review images.
- **The box itself:** drag to move, drag the corners to resize, draw a new box for something the parser missed
  (type: text, table or image), or mark the element rejected.
- Buttons: **Accept**, **Save correction**, **Reject**, then next in the queue.

**Lists.** The queues from section 1, and per-page progress, showing what is left.

**Reviewer.** Each review records who made it (`by`) and when (`at`).

## 3. Reviews, and feeding them back

Reviews are stored as one JSON file per document, next to the bundle (`reviews.json`), and never edited by the
pipeline:

```jsonc
{"document": {"file": "25934_game_extra_1.pdf", "sha256": "…"},
 "reviews": [
   {"element": "p7-text1", "decision": "corrected", "by": "jim", "at": "2026-11-15T10:12:00Z",
    "text": "Getting Started"},
   {"element": "p42-i1", "decision": "corrected", "by": "jim", "at": "…", "bbox": [96.5, 49.6, 288.5, 200.0]},
   {"element": "p77-text3", "decision": "rejected", "by": "jim", "at": "…", "reason": "noise in the drawing"},
   {"element": "new:p145-table", "decision": "added", "type": "table", "page": 145, "bbox": […],
    "rows": [["Creature", "Magic", …], …], "by": "jim", "at": "…"},
   {"element": "p121-table3", "decision": "accepted", "by": "jim", "at": "…"}]}
```

Reviews are tied to the PDF's sha256 and to element ids. When a document is parsed again, each review is
matched to the element with the same id. If none matches, it goes to the element whose bbox overlaps most on the
same page. A review with no match is kept and shown as orphaned. It is never silently dropped.

**Feeding back** (`python parse_pdf.py file.pdf --reviews reviews.json`): the parse runs as usual, then the
reviews are applied.

- **A corrected text, or an edited table grid:** replaces the content; `source` becomes `human`.
- **A corrected bbox on an image:** the image is cut again from the page at that box, at 300 DPI. A corrected
  bbox on a table: the table is read again inside the box, from the text-layer words or, without them, OCR.
  Unless the reviewer also edited the content, the re-cut image or re-read table goes back into the review queue.
- **A rejected element:** stays in the manifest with `review.decision: "rejected"`, and is left out of
  `text.txt`, `tables/` and `images/`.
- **An added element:** is created from the reviewer's box and content.

So yes: the location and size of images and tables go back through the process, and the extracted images and
tables are produced again from the corrected boxes.

## 4. Coverage scores

These are computed from the manifest and `reviews.json`. They're shown on the review site, and written to
`summary.review` in the manifest. An element counts as **checked** when its latest review is accepted, corrected
or rejected.

| Score | Definition |
|---|---|
| **Pages** | % of pages whose every element is checked |
| **Text** | % of all text characters (text layer, OCR and vision) in checked text blocks; also per source |
| **Tables** | % of tables checked |
| **Images** | % of images checked (kept and needs_review; duplicates and icons are counted separately) |
| **Mixed pages** | % of pages with at least one image and one text block whose every element is checked |
| **Required queues** | % of the vision and needs-review queues checked. The document can't be called reviewed below 100%. |

**Measured accuracy.** These come from the same reviews, and are the real check of the parser's confidence: of
the checked elements from each source, the share **accepted** without change. For example: text layer 99%, OCR
62%, vision 91%; tables 80%. Broken down by confidence band, this shows whether Tesseract's confidence means
anything, and gives the vision model a measured accuracy in place of no number at all.

## 5. In the manifest, for the rebuild

Each element gains:

```jsonc
"review": {"decision": "accepted|corrected|rejected|added|null", "by": "jim", "at": "…", "reviews": 2},
"confidence": {"source": "text_layer|ocr|vision|human",
               "score": 0.62,              // ocr: Tesseract's mean confidence / 100; text_layer: none;
                                           // vision: none (the model gives none)
               "measured": 0.91}           // from section 4: share of this source's checked elements accepted
```

When the document is rebuilt (not built yet), each section shows whether a person has checked it, and the
confidence from its source (text layer, OCR or vision) and how that source has measured.

## 6. Hosting and submitting reviews

A static site can't write anything, so reviews come back another way.

- **GitHub Pages:** the review site for a document is published as a static folder. **Submit** opens a new GitHub
  issue, pre-filled with the reviews as JSON and labelled `pdf-review`. A larger batch is downloaded as a file and
  attached to an issue. A GitHub Action checks the JSON (right document hash, known elements) and commits it to the
  document's `reviews.json` in the repository. That commit is what the next parse reads. Reviewers need a GitHub
  account; that is also what identifies them.
- **The pdf-parser service:** the same site served at `/jobs/<id>/review/`, posting reviews to the service
  directly. It's reachable only on the tailnet.

## Open decisions

1. **Copyright and public hosting.** GitHub Pages sites are public, and this repository is public. Publishing a
   review site puts the document's page images and text on the internet: fine for your own documents, not for a
   commercial manual such as Master of Magic. Options:
   - (a) Pages only for documents you may publish; others reviewed through the service, on the tailnet;
   - (b) a separate private repository with Pages; Pages sites of private repositories are still public, unless
     on GitHub Enterprise.
2. **Who reviews, and how reviews come in:** GitHub accounts with issue-based submission (as above), or only
   people on your tailnet through the service?
3. **Where `reviews.json` lives:** in this repository (`pdf-parser/reviews/<sha256>.json`), so reviews are
   versioned and shared, or on the workstation next to the jobs.
4. **When there are several reviews of one element:** the latest wins (proposed), or two must agree before it
   counts as checked.

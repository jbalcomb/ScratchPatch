# PDF Parser: human review — specification (draft)

**Status:** draft, with the decisions of 2026-11-14 folded in. Proof of concept built: sections 1–2, 3a
and 4, on the tailnet (`reviews.py`, `review.html`; see the README). Not yet built:
- feeding reviews back (section 3);
- adding missed elements;
- the owner role;
- section 5;
- section 6, which is the final phase.

What is still open is listed at the end.

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

The site is served by the pdf-parser service, one per job, at `/jobs/<id>/review/`. Reviews are saved
through the service. There is no GitHub Pages copy, so no document is published.

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

**Reviewer.** Each review records who made it (`by`: the reviewer's signed-in identity, section 6) and when
(`at`).

## 3. Reviews, and feeding them back

Reviews are stored as one JSON file per document on the workstation: `~/pdf-parser/reviews/<sha256>.json`,
keyed by the PDF's hash, so that every upload of the same PDF shares its reviews. Only the service appends to
it, and the pipeline never edits it:

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

## 3a. More than one review of an element

| Reviews so far | State | Counts as checked? |
|---|---|---|
| none | **unreviewed** | no |
| one | **reviewed** | yes |
| two that agree | **confirmed** | yes |
| two that disagree | **disputed**: a third review is needed as a tiebreaker | **no** |
| three, two of which agree | **settled**: the two that agree win | yes |
| three, all different | **escalated**: the document's owner decides | no, until the owner's review |

- **Agree** means:
  - the same decision;
  - for a correction, the same content: text equal after trimming and collapsing whitespace, the same table
    grid, and boxes whose overlap (intersection over union) is at least 0.9.
- **The tiebreaker must be someone else.** It can't be either of the first two reviewers. A reviewer who changes
  their mind replaces their own earlier review rather than adding one.
- **While disputed:**
  - the element shows the **newest** review's version, the one the pipeline applies until the dispute is settled;
  - the element is in a **Disputed** queue at the top of the review site;
  - the review screen highlights where the earlier reviews differ from it: changed characters in the text, changed
    table cells, and the earlier boxes drawn over the page beside the newest one.
- **Measured accuracy** (section 4) uses the settled outcome. A disputed element counts toward neither accepted
  nor corrected until it is settled.

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
| **Disputed / escalated** | how many elements are waiting for a tiebreaker or the owner (not counted as checked above) |

**Measured accuracy.** These come from the same reviews, and are the real check of the parser's confidence: of
the checked elements from each source, the share **accepted** without change. For example: text layer 99%, OCR
62%, vision 91%; tables 80%. Broken down by confidence band, this shows whether Tesseract's confidence means
anything, and gives the vision model a measured accuracy in place of no number at all.

## 5. In the manifest, for the rebuild

Each element gains:

```jsonc
"review": {"state": "unreviewed|reviewed|confirmed|disputed|settled|escalated",
           "decision": "accepted|corrected|rejected|added|null",   // of the version applied
           "by": ["jim", "alex"], "at": "…", "reviews": 2},
"confidence": {"source": "text_layer|ocr|vision|human",
               "score": 0.62,              // ocr: Tesseract's mean confidence / 100; text_layer: none;
                                           // vision: none (the model gives none)
               "measured": 0.91}           // from section 4: share of this source's checked elements accepted
```

When the document is rebuilt (not built yet), each section shows whether a person has checked it, and the
confidence from its source (text layer, OCR or vision) and how that source has measured.

## 6. Access for reviewers

The service stays on the tailnet for everything it does today. For reviewers who aren't on the tailnet, only the
review pages are published through **Cloudflare**:

- **Cloudflare Tunnel** (`cloudflared`, another sidecar on the workstation) publishes the review site at a
  hostname on your Cloudflare domain, for example `review.<your-domain>`. No port is opened on the workstation.
  The tunnel forwards only `/review/...` and the review API. Uploads, job status, bundles and health stay
  tailnet-only.
- **Cloudflare Access** in front of that hostname makes reviewers sign in before any request reaches the service.
  - It supports Google and GitHub logins directly.
  - Discord isn't a built-in login. It can be added as a generic OpenID Connect provider through a small
    Cloudflare Worker that bridges Discord's OAuth (for example the community discord-oidc-worker); that is
    third-party code to review before use.
  - An Access policy lists who may get in, by e-mail address (or GitHub organization or Discord server, through the
    bridge).
  - Check the current Zero Trust plan limits for the number of free users.
- **The service trusts the sign-in, not the request.** Each request through Access carries a signed
  `Cf-Access-Jwt-Assertion` header. The service checks its signature (against your Access team's public keys) and
  its audience, and takes the reviewer's identity (`by`) from it. A review request without a valid assertion is
  refused, so the review API can't be reached around Access.
- **Unique URL per reviewer:** the owner creates an invitation in the service for one person and one or more
  documents, `review.<your-domain>/r/<random token>`. Opening it requires signing in through Access. The first
  sign-in binds the invitation to that identity, so a forwarded link is useless to anyone else. Invitations can
  expire and can be revoked. The owner on the tailnet needs no invitation.

Reviews are saved by the service as soon as a reviewer submits them (Accept, Save correction, Reject).

## Still open

1. **Cloudflare details:** the domain or hostname for the review site, and whether the Zero Trust free plan's user
   limit covers your reviewers.
2. **Discord:** worth the Worker bridge, or start with Google and GitHub only?
3. **The owner:** who decides escalated elements (three different reviews): you only, or anyone you name per
   document?
4. **`reviews.json` on the workstation:** assumed above, since reviews go through the service. They should
   be backed up like the jobs.

# Handwritten Notes & Cork Boards — PRD

Drafted 2026-11-15. Photo OCR's third kind of photo, after receipts and products (PRD.md).

## Overview

Jim takes handwritten notes at events and scribbles things out at home to get them out of his head before
organizing them. Four cork boards on the wall hold pinned cards and pages, Sherlock Holmes style: a visual map
of a problem space. This project photographs both, archives the photos, reads them with the local models, and
files what they say into the same knowledge base the voice notes already go to (voice-rec): Obsidian for
reading and linking, Neo4j for the knowledge graph. Voice notes, handwritten notes and boards share one set
of categories, tags and named things, so a memo, a card and a board about the same project meet in one place.

## Goals and non-goals

Goals:

- Capture from the phone in a few taps: one or more pages of one note, or a board, with an optional event
  name and a date you can change (the date it was photographed isn't always the date it was written).
- Archive every original photo, full size, in Postgres.
- Read each photo into a transcript that keeps the layout's meaning: arrows, boxes, emphasis, crossed-out
  words, and words the model couldn't read marked as such rather than guessed.
- Review: the photo beside an editable transcript; approving files it.
- File into Obsidian (one Markdown note each) and Neo4j, with the categories, tags and names shared with
  voice notes.
- Measure reading accuracy from the first note on, the way receipts are measured (error log, bake-off), so
  the model and prompt are tuned on Jim's own handwriting.

Non-goals (for now):

- Images in the Obsidian vault (Jim's call: the vault stays text; notes link to the photo instead).
- Drawing recognition beyond the text in and around drawings (a drawing is kept as the archived photo).
- Cloud OCR. Everything runs on the AI Workstation, as for receipts.
- Linking notes by meaning (embeddings): later, once there is enough to link.

## What the first two samples showed (2026-11-15)

Two index cards photographed on dark fabric: a concept map (Pi, OpenClaw, Claude Code → Open Code → Pi,
a boxed list: Docker, MCP, Tailscale; HERMES; "chat channels" (UI), "the Gateway") and a daily schedule
(0500 Wake-Up, coffee (+ mushroom powder), 0800 M–F work, Sa–Su fun, an arrow to "noon").

1. **Sideways, both ways.** The phone was upright, the card turned, so the photo's own rotation (EXIF) says
   nothing. Tesseract's orientation detection (in photo-ocr's image) failed on both: one low-confidence
   guess, one error.
2. **Small in the frame.** 12-megapixel photos, the card a part of each; the model sees a shrunk copy. A
   naive crop (the bright area) fails on the specks of the fabric.
3. **Personal vocabulary.** Pi, OpenClaw, Hermes, MCP, Tailscale can't be guessed from a scribble.
4. **Layout carries meaning.** Arrows, a box grouping four things, underlines, stars, sizes.
5. **Uncertain words** even for a strong reader ("DEV"?, a word in the box, "Satisfaction"?), and a word
   scribbled over.

## Architecture

```
 Phone (TaskDeck → Handwritten notes)         AI Workstation
 ───────────────────────────────────          ────────────────────────────────────────────────────────
 pages of one note, or a board,     ──POST──▶ photo-ocr: originals → Postgres (notes database)
 event name, date                             upright + crop → read (qwen3-vl via llama.cpp)
                                              review page: photo beside the transcript
                                    ◀──────── status, link to review
                                              approve → knowledge module (shared with voice-rec):
                                                 title, category, tags, names, actions (llama.cpp)
                                                 → Obsidian: Handwritten Notes/<Category>/…md
                                                 → Neo4j: (:Note:HandwrittenNote)-[…]->(Category, Tag, Entity, Day)
```

- **Capture:** a TaskDeck job plug-in, using the receipt camera and photo picker and the shared
  `api.SERVICE` client (`taskdeck/src/taskdeck/shared/service.py`; waiting with `ctx.sleep`).
- **Reading and review:** in photo-ocr, as new kinds next to receipt and product (`parsing.KINDS`), so notes
  get the review page, corrections, the error log and the bake-off.
- **Archive:** the Postgres server voice-rec runs (`voice-rec-db`), a database of its own (`notes`). Receipts
  and products stay in photo-ocr's SQLite until the move to Postgres noted in README, Later.
- **Filing:** a shared Python package, `knowledge/` at the repository root, holding what voice-rec does today
  in `organize.py`, `obsidian.py` and `graph.py`, generalized to any kind of note. voice-rec and photo-ocr both
  import it (each Dockerfile copies it in: compose `additional_contexts: knowledge: ../knowledge`), and both
  read one `categories.txt`.

## Capture (TaskDeck plug-in)

- **Handwritten note:** pick photos (Photos, the DCIM picker) or take them with TaskDeck's camera; the pages
  of one note in order, several notes in one go (the grouping of Product photo, `Field.grouped`). Optional
  event name; date defaults to the photo's and can be changed.
- **Cork board:** which board (four, named once in the plug-in's settings), one or more photos.
- Sends to photo-ocr, then follows the reading like Receipt capture (`wait_for_reading`), ending with a link
  to the review page.
- Google Photos: the picker shows what is on the phone. Photos only in the cloud need *Download* in Google
  Photos first; if the downloads land outside DCIM, the picker gets that folder too (to check on the phone).

## Reading

**Before the model** (each tested in the bake-off before it is kept):

- *Upright:* turn the photo so the writing reads normally. Candidates: ask the vision model which way is up
  (one short question, the image small); or read all four ways and keep the most confident. Tesseract OSD is
  out (it failed on both samples).
- *Crop:* to the paper: the largest bright region (not every bright pixel), or the four corners from the
  camera's guides at capture.

**The prompt** asks for Markdown that keeps the structure:

- lines in reading order; `→` for arrows (with what they connect); a boxed group as a list under a
  `[box]` line; underlined or starred words in bold; large writing as a heading;
- `[?]` around a word it can't read with confidence (`[DEV?]`), `~~word~~` for crossed-out words, `[drawing:
  short description]` for a drawing, nothing guessed;
- a **glossary**: names already in the knowledge base (voice-rec's entities, the tags in use), given as words
  that may appear. The more is filed, the better notes are read.

**Cork boards:** first cut: the whole board read as one transcript, item by item (each card or page as a
section), with where it sits (top left, middle, …). Later: items as their own records with positions,
clusters by distance, and what changed since the last photo of the same board.

## Review

The review page of receipts, for notes: the photo (zoomable, upright and cropped) beside the transcript in an
editable box, `[?]` spans highlighted, the event and date editable. *Approve* files it; *Read again* re-reads
(with the reading's prompt version shown, as for receipts). Every edit is kept as a correction.

## Measuring

As for receipts (README, Reading errors): each reading is compared with the approved transcript, from the
model's own output, by prompt version and engine:

- **word error rate** (words changed, added, dropped over the approved word count) and **character error
  rate**;
- **names misread**: words of the approved transcript that are in the glossary, read differently;
- **uncertain marks**: `[?]` spans, and how many were right anyway (honest uncertainty vs. noise);
- **structure**: arrows and boxes found vs. approved.

Approved notes join the bake-off set (`export-labels`), so preprocessing (as-is / upright / cropped),
engines (qwen3-vl-8b, paddleocr-vl), prompt versions and the glossary on or off are compared on Jim's own
handwriting before anything is changed in the service.

## Filing (shared knowledge module)

From voice-rec, unchanged in spirit:

- **Organizing** (`organize.py`): title, one category from `categories.txt`, tags, summary, actions, people /
  projects / places / organizations / topics. New, for every kind: the **tags already in use** are offered so
  the model reuses them ("job-search", not also "jobsearch"); new tags are allowed.
- **Obsidian** (`obsidian.py`): one note per handwritten note in `Handwritten Notes/<Category>/<date>
  <title>.md`, per board snapshot in `Cork Boards/<Category>/<date> <board>.md`; front matter as voice notes
  (`type: handwritten-note` / `cork-board`, `written`, `photographed`, `event`, `category`, `tags`, `people`,
  `projects`), actions as tasks, names as `[[links]]`, the transcript, a link to the photos on the service
  (no images in the vault), and the "Your notes" section kept when re-filed. Written straight to the final
  name (the vault syncs to the phone).
- **Neo4j** (`graph.py`): `(:Note:HandwrittenNote)`, `(:Note:CorkBoard)`, and `:Note` added to voice-rec's
  `:VoiceNote`; the same `Category`, `Tag`, `Entity`, `Day`, `Action` nodes, `[:WRITTEN_ON]` for the day
  (voice: `[:RECORDED_ON]`), `[:AT_EVENT]->(:Event {name})`, `[:PINNED_ON]->(:Board {name})`. Re-filing
  replaces a note's edges, as today.

## Data model (Postgres, database `notes`)

- `notes`: id, kind (handwritten / board), board, event, written_on, created_at, status (pending / read /
  approved / filed / failed), transcript (approved), title, category, tags, summary, actions, entities,
  note_path (in the vault), full-text index over the transcript.
- `note_photos`: note id, position, the original (`bytea`), mime, size, width, height, taken_at, sha256 (no
  duplicates), upright turn and crop box once worked out.
- `note_readings`: note id, engine, prompt, prompt version, preprocessing, raw reply, transcript, seconds,
  error (the engine runs, kept for measuring).
- `note_corrections`, `note_reading_errors`: as for receipts.

A 3–8 MB photo per page in `bytea` is well within what voice-rec already does with audio; a nightly
`pg_dump` (voice-rec's plan, Next 4) then covers notes too.

## Build phases

1. **Capture, archive, read, review.** The plug-in; photo-ocr's note kinds (the current prompt, no
   preprocessing yet); originals in Postgres; the review page; corrections and the error log. Ends with Jim
   able to send pages and approve transcripts, which builds the test set.
2. **Handwriting bake-off.** Word and character error rates; preprocessing, engines, prompt and glossary
   compared on the approved notes; the winners go into the service.
3. **Shared knowledge module.** Move voice-rec's organizing, Obsidian and graph code into `knowledge/`,
   voice-rec switched over with its tests passing and its notes unchanged; tags in use offered to the model;
   `:Note` on voice notes. Coordinated with whoever works on voice-rec.
4. **Filing notes.** Approved notes and boards into Obsidian and Neo4j through the module. Neo4j installed on
   the workstation (with voice-rec's `GRAPH_*` settings pointed at it).
5. **Cork boards, properly.** Items as records with positions, clusters, changes between snapshots.
6. **Later.** Linking by meaning (an embedding model in the llama.cpp presets); search from TaskDeck.

## Open questions

- **Neo4j:** who installs and runs it (a compose of its own, or in voice-rec's), and its memory next to
  llama.cpp's.
- **voice-rec:** agreeing the move to `knowledge/` with that project's session before phase 3.
- **The vault:** voice-rec's setup knows its path (`OBSIDIAN_VAULT_DIR`); photo-ocr mounts the same one.
- **Links to photos:** the service's Tailscale address in each note works on Jim's devices only, which is
  the intent; confirm.
- **Board names:** the four boards' names, for the plug-in's settings.
- **Notebook pages:** spreads (two pages in one photo) split into two, or kept as one?
- **Writing date:** asked at capture, or read off the page when one is written on it?

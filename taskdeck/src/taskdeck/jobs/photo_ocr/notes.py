"""Handwritten notes and cork boards (photo-ocr/NOTES-PRD.md): send photos of notes or a board to the photo OCR
service, which keeps the originals, reads them with the local vision model, and shows the transcript on a review
page to check against the photo and approve."""

from __future__ import annotations

import re
from datetime import date

from taskdeck.core import Field, Pipeline, Result, Stage, StepError, step

from . import api
from .photos import UPLOAD_TIMEOUT, _delete_camera_photos, wait_for_reading

UNSURE = re.compile(r"\[([^\[\]\n]+?)\?\]")  # a word the model wasn't sure of: [DEV?]


def _written_on(ctx) -> str | None:
    raw = (ctx.get("written_on") or "").strip()
    if not raw:
        return None  # the service takes the photo's date
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise StepError("Written on: use the form 2026-11-15, or leave it empty for the photo's date.") from None


@step(Stage.VALIDATE)
def read_note_photos(ctx):
    photos = ctx["photos"]
    if not photos:
        raise StepError("Add at least one photo.")
    groups = photos if isinstance(photos[0], list) else [photos]  # a board's photos aren't grouped: one board
    ctx["groups"] = groups
    ctx["parts_by_group"] = [api.read_files(g, api.PHOTO_EXTENSIONS) for g in groups]
    ctx["written"] = _written_on(ctx)


def _send(ctx, kind: str, extra: list[tuple[str, str]]):
    """One note per group of photos. One the service refuses is reported; the others still go. Photos from
    TaskDeck's camera are deleted once their note is sent (the service keeps the originals)."""
    fields = [("kind", kind)] + extra + ([("written_on", ctx["written"])] if ctx["written"] else [])
    ctx["sent_all"], ctx["send_errors"] = [], []
    for group, parts in zip(ctx["groups"], ctx["parts_by_group"]):
        ctx.check_cancelled()
        try:
            ctx["sent_all"].append(api.call(ctx.settings, "POST", "/api/notes", fields=fields, files=parts,
                                            timeout=UPLOAD_TIMEOUT))
            ctx["send_errors"].append(None)
            _delete_camera_photos(group)
        except StepError as exc:
            ctx["sent_all"].append(None)
            ctx["send_errors"].append(str(exc))
    if not any(ctx["sent_all"]):
        raise StepError(ctx["send_errors"][0])


@step(Stage.EXECUTE, name="send notes")
def send_notes(ctx):
    event = (ctx.get("event") or "").strip()
    _send(ctx, "handwritten", [("event", event)] if event else [])


@step(Stage.EXECUTE, name="send board")
def send_board(ctx):
    board = (ctx.get("board") or "").strip()
    if not board:
        raise StepError("Which board? Give it a name, e.g. Job search.")
    _send(ctx, "board", [("board", board)])


def _first_line(text: str) -> str:
    return next((ln.strip("#*-> ").strip() for ln in (text or "").splitlines() if re.search(r"\w", ln)), "")


def _unsure_words(text: str) -> list[str]:
    return UNSURE.findall(text or "")


@step(Stage.PRESENT)
def show_notes(ctx):
    what = "Board" if ctx.get("board") else "Note"
    images = [data for parts in ctx["parts_by_group"] for _, _, data in parts]
    if len(ctx["groups"]) == 1:
        sent, reading = ctx["sent_all"][0], ctx["readings"][0]
        nid = sent.get("id")
        rows = [(what, f"#{nid}"), ("Photos", str(sent.get("photos", len(ctx["groups"][0]))))]
        page = ("Review page", api.page_url(ctx.settings, f"/notes/{nid}"))
        if not reading:
            error = ctx["reading_errors"][0]
            ctx.result = Result(
                title=f"{what} #{nid} sent", rows=rows + [("Status", "sent" if error else "being read"), page],
                text=f"Sent, but the reading couldn't be checked: {error}" if error else
                "Being read on the workstation; the review page shows it when it's done.", images=images)
            return
        if reading.get("status") == "failed":
            ctx.result = Result(title=f"{what} #{nid}: reading failed", rows=rows + [page], images=images,
                                warning=f"Reading failed: {reading.get('error') or 'no reason given'}.")
            return
        text = reading.get("transcript") or ""
        unsure = _unsure_words(text)
        rows += [("Unsure words", ", ".join(unsure) if unsure else "none"), page]
        ctx.result = Result(
            title=f"{what} #{nid}: {_first_line(text) or 'read'}", rows=rows, text=text, images=images,
            warning=(f"The model wasn't sure of {len(unsure)} word{'s' * (len(unsure) != 1)}: fix "
                     f"{'it' if len(unsure) == 1 else 'them'} on the review page, then approve.") if unsure else
            "Check it against the photo on the review page, then approve.")
        return
    rows, sent_n = [], 0
    for i in range(len(ctx["groups"])):
        sent, reading, error = ctx["sent_all"][i], ctx["readings"][i], ctx["send_errors"][i]
        if not sent:
            rows.append((f"Note {i + 1}", f"not sent: {error}"))
            continue
        sent_n += 1
        key = f"{i + 1}. #{sent.get('id')}"
        if not reading:
            rows.append((key, "sent; couldn't check the reading" if ctx["reading_errors"][i] else "being read"))
        elif reading.get("status") == "failed":
            rows.append((key, f"reading failed: {reading.get('error') or 'no reason given'}"))
        else:
            unsure = _unsure_words(reading.get("transcript"))
            rows.append((key, (_first_line(reading.get("transcript")) or "read")
                         + (f" · {len(unsure)} unsure" if unsure else "")))
    rows.append(("Notes page", api.page_url(ctx.settings, "/notes")))
    n = len(ctx["groups"])
    ctx.result = Result(title=f"{n} notes sent" if sent_n == n else f"{sent_n} of {n} notes sent", rows=rows,
                        images=[parts[0][2] for parts in ctx["parts_by_group"] if parts],
                        warning="Check each against its photo on the notes page, then approve.")


class HandwrittenNote(api.PhotoOcrJob):
    id = "photo_ocr_handwritten_note"
    title = "Handwritten note"
    description = ("Send photos of handwritten notes; the workstation keeps the originals, reads them, and the "
                   "review page shows the transcript beside the photo.")
    icon = "EDIT_NOTE"
    tags = ("note", "notes", "handwriting", "handwritten", "notebook", "index card", "photo", "ocr", "knowledge")
    fields = (
        Field.files("photos", "Note photos", required=True, extensions=api.PHOTO_EXTENSIONS, preview=True,
                    grouped="note", camera="page",
                    help="Take photos, or Add… photos you took: the pages of one note in order. 1 note keeps "
                         "the photos together; N notes makes each photo its own note."),
        Field.text("event", "Event", help="Where it was written, if anywhere in particular (e.g. AI meetup)."),
        Field.text("written_on", "Written on", help="YYYY-MM-DD. Empty: the date the photo was taken."),
    )
    run_label = "Send notes"

    def build_pipeline(self):
        return Pipeline([api.service_configured, read_note_photos, send_notes, wait_for_reading("/api/notes/{id}"),
                         show_notes])


class CorkBoard(api.PhotoOcrJob):
    id = "photo_ocr_cork_board"
    title = "Cork board"
    description = "Archive a cork board from photos; the workstation reads every card and page pinned to it."
    icon = "DASHBOARD_OUTLINED"
    tags = ("cork board", "board", "pinboard", "notes", "cards", "photo", "ocr", "knowledge", "map")
    fields = (
        Field.text("board", "Board", required=True, help="Which board, e.g. Job search. Use the same name each time."),
        Field.files("photos", "Board photos", required=True, extensions=api.PHOTO_EXTENSIONS, preview=True,
                    camera="page",
                    help="The whole board in one photo if it reads, or several photos of its parts."),
        Field.text("written_on", "As of", help="YYYY-MM-DD. Empty: the date the photo was taken."),
    )
    run_label = "Send board"

    def build_pipeline(self):
        return Pipeline([api.service_configured, read_note_photos, send_board, wait_for_reading("/api/notes/{id}"),
                         show_notes])


NOTE = HandwrittenNote()
BOARD = CorkBoard()

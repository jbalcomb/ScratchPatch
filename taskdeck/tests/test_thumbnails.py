"""Thumbnails under a photo field and above a job's result."""

import flet as ft

from taskdeck.core import Field
from taskdeck.ui import thumbnails
from taskdeck.ui.forms import FieldControl


def photos(tmp_path, *names):
    out = []
    for name in names:
        p = tmp_path / name
        p.write_bytes(b"not really " + name.encode())
        out.append(str(p))
    return out


def test_strip_shows_existing_images_only(tmp_path):
    a, b, notes = photos(tmp_path, "a.jpg", "b.PNG", "notes.txt")
    row = thumbnails.strip("\n".join([a, "  ", b, notes, str(tmp_path / "gone.jpg")]))
    assert isinstance(row, ft.Row) and [c.content.src for c in row.controls] == [b"not really a.jpg", b"not really b.PNG"]
    assert thumbnails.strip("") is None and thumbnails.strip([notes]) is None


def test_strip_counts_what_it_leaves_out(tmp_path):
    many = photos(tmp_path, *[f"p{i}.jpg" for i in range(thumbnails.MOST + 3)])
    row = thumbnails.strip(many)
    assert len(row.controls) == thumbnails.MOST + 1 and row.controls[-1].value == "+3 more"


def test_preview_field_draws_thumbnails_as_files_are_chosen(tmp_path):
    a, b = photos(tmp_path, "top.jpg", "bottom.jpg")
    changed = []
    fc = FieldControl(Field.files("photos", "Photos", preview=True), a, on_change=lambda e: changed.append(e))
    assert fc.thumbs in fc.control.controls and len(fc.thumbs.content.controls) == 1
    fc.value = f"{a}\n{b}"
    assert len(fc.thumbs.content.controls) == 2
    fc.input.on_change(None)  # typing in the box: the job's own handler still runs
    assert changed == [None]


def test_fields_without_preview_have_no_thumbnails(tmp_path):
    [a] = photos(tmp_path, "a.jpg")
    fc = FieldControl(Field.files("archives", "Archives"), a)
    assert fc.thumbs not in fc.control.controls and fc.thumbs.content is None

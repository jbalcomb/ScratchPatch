"""The photo picker: finding photos, the thumbnails stored in them, day headings, and multi-select."""

import os
import struct
import time
from pathlib import Path

import flet as ft
import pytest

from taskdeck.core import Field
from taskdeck.shared import files, photos
from taskdeck.ui import photo_picker
from taskdeck.ui.forms import FieldControl
from taskdeck.ui.photo_picker import PhotoPicker

THUMB = b"\xff\xd8thumbnail-jpeg\xff\xd9"


def camera_jpeg(path: Path, orientation: int = 1, thumb: bytes | None = THUMB, endian: str = "<") -> Path:
    """A JPEG laid out like a phone camera's: SOI, an EXIF APP1 with an orientation tag and a thumbnail, image data."""
    e = endian
    ifd0 = struct.pack(e + "H", 1) + struct.pack(e + "HHIHH", 0x0112, 3, 1, orientation, 0)
    ifd0_at = 8
    ifd1_at = ifd0_at + len(ifd0) + 4
    thumb_at = ifd1_at + 2 + 2 * 12 + 4
    ifd1 = struct.pack(e + "H", 2) + struct.pack(e + "HHII", 0x0201, 4, 1, thumb_at) + \
        struct.pack(e + "HHII", 0x0202, 4, 1, len(thumb or b"")) + struct.pack(e + "I", 0)
    tiff = (b"II" if e == "<" else b"MM") + struct.pack(e + "HI", 42, ifd0_at) + ifd0 + \
        struct.pack(e + "I", ifd1_at if thumb else 0) + (ifd1 + thumb if thumb else b"")
    app1 = b"Exif\x00\x00" + tiff
    data = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xda\x00\x02image-data\xff\xd9"
    path.write_bytes(data)
    return path


def test_exif_thumbnail_and_orientation(tmp_path):
    assert photos.exif_thumbnail(camera_jpeg(tmp_path / "a.jpg", 6)) == (THUMB, 6)
    assert photos.exif_thumbnail(camera_jpeg(tmp_path / "b.jpg", 3, endian=">")) == (THUMB, 3)
    assert photos.exif_thumbnail(camera_jpeg(tmp_path / "c.jpg", 8, thumb=None)) == (None, 8)
    (tmp_path / "d.png").write_bytes(b"\x89PNG\r\n\x1a\n...")
    assert photos.exif_thumbnail(tmp_path / "d.png") == (None, 1)
    (tmp_path / "e.jpg").write_bytes(b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00II*\x00garbage")
    assert photos.exif_thumbnail(tmp_path / "e.jpg") == (None, 1)
    assert photos.exif_thumbnail(tmp_path / "gone.jpg") == (None, 1)


def make_dcim(root: Path) -> Path:
    """DCIM/Camera with three photos a day apart, DCIM/Screenshots with one, plus things to skip."""
    dcim = root / "DCIM"
    for sub in ("Camera", "Screenshots", ".thumbnails", "Camera/.trashed"):
        (dcim / sub).mkdir(parents=True)
    now = time.time()
    for name, days_ago in (("Camera/IMG_1.jpg", 2), ("Camera/IMG_2.jpg", 1), ("Camera/IMG_3.jpg", 0),
                           ("Screenshots/Screen.png", 0.5), (".thumbnails/t.jpg", 0), ("Camera/.trashed/x.jpg", 0),
                           ("Camera/notes.txt", 0), ("Camera/clip.heic", 3)):
        p = dcim / name
        camera_jpeg(p) if p.suffix == ".jpg" else p.write_bytes(b"x")
        os.utime(p, (now - days_ago * 86400, now - days_ago * 86400))
    return dcim


def test_scan_newest_first_skipping_hidden(tmp_path):
    dcim = make_dcim(tmp_path)
    assert [p.name for p in photos.scan(dcim)] == ["IMG_3.jpg", "Screen.png", "IMG_2.jpg", "IMG_1.jpg", "clip.heic"]
    assert [p.name for p in photos.subfolders(dcim)] == ["Camera", "Screenshots"]
    assert photos.scan(tmp_path / "nowhere") == []


def test_day_label():
    now = time.mktime((2026, 11, 6, 15, 0, 0, 0, 0, -1))
    at = lambda *ymd: time.mktime(ymd + (12, 0, 0, 0, 0, -1))  # noqa: E731
    assert photos.day_label(at(2026, 11, 6), now) == "Today"
    assert photos.day_label(at(2026, 11, 5), now) == "Yesterday"
    assert photos.day_label(at(2026, 11, 3), now) == "Tuesday"
    assert photos.day_label(at(2026, 10, 12), now) == "Oct 12"
    assert photos.day_label(at(2025, 12, 31), now) == "Dec 31, 2025"


def test_photos_location_on_a_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert files.photos_location().path == tmp_path
    (tmp_path / "Pictures").mkdir()
    assert files.photos_location().path == tmp_path / "Pictures"
    assert [loc.label for loc in files.locations()] == ["Photos", "Home"]


class FakePage:
    width = 412

    def __init__(self):
        self.updates = 0

    def update(self):
        self.updates += 1


class FakeApp:
    def __init__(self):
        self.page, self.pushed, self.backs = FakePage(), [], 0

    def push(self, screen):
        self.pushed.append(screen)

    def back(self):
        self.backs += 1


@pytest.fixture
def picker(tmp_path, monkeypatch):
    dcim = make_dcim(tmp_path)
    monkeypatch.setattr(files, "photos_location", lambda: files.Location("Photos", dcim, "PHOTO_LIBRARY"))
    monkeypatch.setattr(files, "locations", lambda: [files.Location("Downloads", tmp_path, "DOWNLOAD")])
    picked = []
    accept = lambda name: name.lower().endswith((".jpg", ".png"))  # noqa: E731
    p = PhotoPicker.pick(FakeApp(), lambda paths, separate: picked.append((paths, separate)),
                         title="Choose Receipt photos", accept=accept)
    p.picked = picked
    return p


def test_picker_opens_at_photos_with_a_grid_under_day_headings(picker):
    assert picker.app.pushed == [picker] and picker.folder.name == "DCIM"
    assert [c.label.value for c in picker.chips.controls] == ["Photos", "Downloads"]
    assert picker.chips.controls[0].selected
    assert [b.content for b in picker.folders.controls] == ["Camera", "Screenshots"]
    assert [p.name for p in picker.all] == ["IMG_3.jpg", "Screen.png", "IMG_2.jpg", "IMG_1.jpg"]  # .heic not accepted
    headings = [c.value for c in picker.grid.controls if isinstance(c, ft.Text)]
    assert headings[0] == "Today" and headings[1] == "Yesterday" and len(headings) == 3
    tiles = [t for row in picker.grid.controls if isinstance(row, ft.Row) for t in row.controls]
    assert len(tiles) == 4
    first_image = tiles[0].content.controls[0]
    assert first_image.src == THUMB  # the camera's stored thumbnail, not the photo


def test_multi_select_numbers_photos_in_the_order_picked(picker):
    a, b, c = (p.path for p in picker.all[:3])
    assert picker.view.appbar.title.value == "Choose Receipt photos"
    picker.toggle(c)
    picker.toggle(a)
    picker.toggle(b)
    assert picker.view.appbar.title.value == "3"
    assert [picker.circles[x].content.value for x in (c, a, b)] == ["1", "2", "3"]
    picker.toggle(a)  # unselected: the others renumber
    assert picker.circles[a].content is None and picker.circles[b].content.value == "2"
    add = picker.view.appbar.actions[-1].content
    assert add.content == "Add 2"
    add.on_click(None)
    assert picker.app.backs == 1 and picker.picked == [([c, b], False)]


def test_clear_and_select_all(picker):
    picker.toggle(picker.all[1].path)
    picker.select_all()
    assert picker.selected[0] == picker.all[1].path and len(picker.selected) == 4
    picker.clear()
    assert picker.selected == [] and picker.view.appbar.title.value == "Choose Receipt photos"


def test_folders_and_show_more(picker, monkeypatch):
    picker.go(picker.folder / "Camera")
    assert [p.name for p in picker.all] == ["IMG_3.jpg", "IMG_2.jpg", "IMG_1.jpg"]
    assert picker.folders.controls[1].value == "Camera"  # up button, then where we are
    monkeypatch.setattr(photo_picker, "PAGE", 2)
    picker.load()
    assert picker.shown == 2 and picker.grid.controls[-1].data == "more"
    picker.grid.controls[-1].controls[0].on_click(None)
    assert picker.shown == 3 and picker.grid.controls[-1].data != "more"


def test_field_takes_the_picked_photos_in_order(tmp_path, monkeypatch):
    a, b, c = (camera_jpeg(tmp_path / n) for n in ("a.jpg", "b.jpg", "c.jpg"))
    fc = FieldControl(Field.files("photos", "Photos", preview=True), str(a))
    monkeypatch.setattr(fc.input, "update", lambda: None)
    fc.picked_many([str(c), str(a), str(b)])
    assert fc.value.splitlines() == [str(a), str(c), str(b)]
    assert len(fc.thumbs.content.controls) == 3


def test_grouped_fields_get_two_add_buttons(picker):
    picker.noun = "product"
    a, b, c = (p.path for p in picker.all[:3])
    picker.toggle(a)
    assert "Add 1" in [getattr(x.content, "content", None) for x in picker.view.appbar.actions if isinstance(x, ft.Container)]
    picker.toggle(b)
    picker.toggle(c)
    one, many = picker.view.appbar.actions[-2], picker.view.appbar.actions[-1].content
    assert (one.content, many.content) == ("1 product", "3 products")
    assert all(not isinstance(x, ft.IconButton) or x.icon != ft.Icons.ZOOM_IN for x in picker.view.appbar.actions)
    many.on_click(None)
    assert picker.picked == [([a, b, c], True)]


def test_grouped_field_adds_products(tmp_path, monkeypatch):
    a, b, c = (str(camera_jpeg(tmp_path / n)) for n in ("a.jpg", "b.jpg", "c.jpg"))
    fc = FieldControl(Field.files("photos", "Product photos", preview=True, grouped="product"), "")
    monkeypatch.setattr(fc.input, "update", lambda: None)
    fc.picked_many([a, b])              # 1 product: front and label
    fc.picked_many([c, a], separate=True)  # 2 products, one photo each
    assert fc.value == f"{a}\n{b}\n\n{c}\n\n{a}"
    texts = [x.value for x in fc.thumbs.content.controls if isinstance(x, ft.Text)]
    assert texts == ["Product 1", "Product 2", "Product 3"]
    assert fc.field.coerce(fc.value) == [[a, b], [c], [a]]

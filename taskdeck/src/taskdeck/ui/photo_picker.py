"""A Google Photos style picker for photo fields (a pushed screen).

The grid shows the photos in a folder and its subfolders, newest first, under day headings, starting at
Photos (the camera's DCIM folder). Each photo has a check circle in its upper right corner; tapping a photo
selects it and the circle shows its number, the order the photos are added in (a receipt goes top to
bottom). Once anything is selected, the top bar shows how many, with Clear, Select all and **Add**.
Long-press a photo to see it full size.

Thumbnails come from the small JPEG a camera stores inside each photo (``shared.photos.exif_thumbnail``),
so a folder of hundreds of photos is drawn without reading the photos themselves.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import flet as ft

from taskdeck.shared import files, photos

from . import thumbnails
from .common import icon_for

if TYPE_CHECKING:
    from .app import TaskDeckApp

PAGE = 60          # photos drawn at first, and each time "Show more" is tapped
DECODE_WIDTH = 256  # a photo with no stored thumbnail is decoded this small
TURNS = {3: math.pi, 6: math.pi / 2, 8: -math.pi / 2}  # EXIF orientation -> rotation of its stored thumbnail


class PhotoPicker:
    def __init__(self, app: "TaskDeckApp", title: str, on_pick: Callable[[list[Path]], None],
                 accept: Callable[[str], bool] = photos.is_image, start: Path | None = None):
        self.app, self.title, self.on_pick, self.accept = app, title, on_pick, accept
        self.photos_loc = files.photos_location()
        self.locations = [self.photos_loc] + [loc for loc in files.locations() if loc.path != self.photos_loc.path]
        self.folder = start if start and start.is_dir() else self.photos_loc.path
        self.selected: list[Path] = []
        self.all: list[photos.Photo] = []
        self.shown = 0
        self.circles: dict[Path, ft.Container] = {}
        self.chips = ft.Row(wrap=True, spacing=8)
        self.folders = ft.Row(wrap=True, spacing=6)
        self.grid = ft.ListView(expand=True, spacing=4)
        body = ft.Column([self.chips, self.folders, ft.Divider(height=1), self.grid], expand=True, spacing=8,
                         horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.view = ft.View(
            route="/photos", appbar=self._appbar(), padding=0,
            controls=[ft.SafeArea(ft.Container(body, padding=ft.Padding.only(left=12, top=8, right=12, bottom=8)),
                                  expand=True)],
        )
        self.load(update=False)

    @classmethod
    def pick(cls, app: "TaskDeckApp", on_pick: Callable[[list[Path]], None], title: str = "Choose photos",
             accept: Callable[[str], bool] = photos.is_image, start: Path | None = None) -> "PhotoPicker":
        picker = cls(app, title, on_pick, accept, start)
        app.push(picker)
        return picker

    # ---- the folder ----------------------------------------------------------------------

    def load(self, update: bool = True) -> None:
        self.all = [p for p in photos.scan(self.folder) if self.accept(p.name)]
        self.shown = 0
        self.circles = {}
        self.grid.controls = []
        self._render_folders()
        if not self.all:
            self.grid.controls = [ft.Container(ft.Column(
                [ft.Icon(ft.Icons.PHOTO_LIBRARY_OUTLINED, size=36, color=ft.Colors.OUTLINE),
                 ft.Text("No photos here", weight=ft.FontWeight.W_600),
                 ft.Text("Pick another place above. If nothing shows anywhere, allow file access when TaskDeck asks.",
                         color=ft.Colors.ON_SURFACE_VARIANT, text_align=ft.TextAlign.CENTER)],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6), padding=24, alignment=ft.Alignment.CENTER)]
        else:
            self.show_more(update=False)
        if update:
            self.app.page.update()

    def go(self, folder: Path) -> None:
        self.folder = folder
        self.load()

    def _render_folders(self) -> None:
        here = max((loc for loc in self.locations if _is_under(self.folder, loc.path)), key=lambda l: len(str(l.path)),
                   default=None)
        self.chips.controls = [
            ft.Chip(label=ft.Text(loc.label), leading=ft.Icon(icon_for(loc.icon), size=18), selected=loc is here,
                    on_click=lambda e, p=loc.path: self.go(p)) for loc in self.locations]
        subs: list[ft.Control] = []
        if here and self.folder != here.path:
            subs.append(ft.TextButton(f"‹ {self.folder.parent.name or here.label}", icon=ft.Icons.ARROW_UPWARD,
                                      on_click=lambda e: self.go(self.folder.parent)))
            subs.append(ft.Text(self.folder.name, weight=ft.FontWeight.W_600))
        subs += [ft.OutlinedButton(p.name, icon=ft.Icons.FOLDER_OUTLINED, on_click=lambda e, p=p: self.go(p))
                 for p in photos.subfolders(self.folder)]
        self.folders.controls = subs

    def show_more(self, update: bool = True) -> None:
        """Draw the next PAGE photos, under their day headings."""
        if self.grid.controls and isinstance(self.grid.controls[-1], ft.Row) and self.grid.controls[-1].data == "more":
            self.grid.controls.pop()
        size = self._tile_size()
        batch = self.all[self.shown:self.shown + PAGE]
        last_day = photos.day_label(self.all[self.shown - 1].modified) if self.shown else None
        row: ft.Row | None = None
        for p in batch:
            day = photos.day_label(p.modified)
            if day != last_day or row is None:
                if day != last_day:
                    self.grid.controls.append(ft.Text(day, weight=ft.FontWeight.W_600, size=15))
                row = ft.Row(wrap=True, spacing=3, run_spacing=3)
                self.grid.controls.append(row)
                last_day = day
            row.controls.append(self._tile(p, size))
        self.shown += len(batch)
        if self.shown < len(self.all):
            self.grid.controls.append(ft.Row([ft.TextButton(f"Show more ({len(self.all) - self.shown} older)",
                                                            on_click=lambda e: self.show_more())],
                                              alignment=ft.MainAxisAlignment.CENTER, data="more"))
        if update:
            self.app.page.update()

    def _tile_size(self) -> int:
        width = getattr(self.app.page, "width", None) or 412
        across = 3 if width < 600 else 5
        return max(80, int((width - 24 - 3 * (across - 1)) / across) - 1)

    # ---- one photo -----------------------------------------------------------------------

    def _tile(self, p: photos.Photo, size: int) -> ft.Control:
        thumb, orientation = photos.exif_thumbnail(p.path)
        if thumb is not None:
            image = ft.Image(src=thumb, width=size, height=size, fit=ft.BoxFit.COVER,
                             rotate=ft.Rotate(TURNS[orientation]) if orientation in TURNS else None)
        else:  # no stored thumbnail (a screenshot, a PNG): the photo, decoded small
            try:
                data = p.path.read_bytes()
            except OSError:
                data = None
            image = ft.Image(src=data, width=size, height=size, fit=ft.BoxFit.COVER, cache_width=DECODE_WIDTH,
                             error_content=ft.Icon(ft.Icons.BROKEN_IMAGE_OUTLINED)) if data else \
                ft.Icon(ft.Icons.BROKEN_IMAGE_OUTLINED)
        circle = ft.Container(width=26, height=26, border_radius=13, alignment=ft.Alignment.CENTER)
        self.circles[p.path] = circle
        self._paint(p.path)
        return ft.Container(
            ft.Stack([image, ft.Container(circle, right=6, top=6)], width=size, height=size),
            width=size, height=size, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, clip_behavior=ft.ClipBehavior.HARD_EDGE,
            on_click=lambda e, path=p.path: self.toggle(path),
            on_long_press=lambda e, path=p.path: self._enlarge(path),
            tooltip=p.name,
        )

    def _paint(self, path: Path) -> None:
        """The check circle: an outline when not selected; filled, with the photo's number, when selected."""
        circle = self.circles.get(path)
        if circle is None:
            return
        if path in self.selected:
            circle.bgcolor = ft.Colors.PRIMARY
            circle.border = ft.Border.all(2, ft.Colors.WHITE)
            circle.content = ft.Text(str(self.selected.index(path) + 1), size=13, weight=ft.FontWeight.W_700,
                                     color=ft.Colors.ON_PRIMARY)
        else:
            circle.bgcolor = ft.Colors.with_opacity(0.25, ft.Colors.BLACK)
            circle.border = ft.Border.all(2, ft.Colors.WHITE)
            circle.content = None

    def _enlarge(self, path: Path) -> None:
        try:
            thumbnails.enlarge(self.app.page, path.read_bytes(), path.name)
        except OSError:
            pass

    # ---- selection -----------------------------------------------------------------------

    def toggle(self, path: Path) -> None:
        if path in self.selected:
            self.selected.remove(path)
        else:
            self.selected.append(path)
        self._selection_changed()

    def clear(self, e=None) -> None:
        self.selected = []
        self._selection_changed()

    def select_all(self, e=None) -> None:
        """Every photo drawn so far, in the order shown, after those already chosen."""
        self.selected += [p.path for p in self.all[:self.shown] if p.path not in self.selected]
        self._selection_changed()

    def _selection_changed(self) -> None:
        for path in self.circles:
            self._paint(path)
        self.view.appbar = self._appbar()
        self.app.page.update()

    def _appbar(self) -> ft.AppBar:
        n = len(self.selected)
        if not n:
            return ft.AppBar(title=ft.Text(self.title))
        return ft.AppBar(
            leading=ft.IconButton(ft.Icons.CLOSE, tooltip="Clear the selection", on_click=self.clear),
            title=ft.Text(f"{n} selected"),
            bgcolor=ft.Colors.SECONDARY_CONTAINER,
            actions=[
                ft.IconButton(ft.Icons.SELECT_ALL, tooltip="Select all shown", on_click=self.select_all),
                ft.IconButton(ft.Icons.ZOOM_IN, tooltip="View the selected photo", disabled=n != 1,
                              on_click=lambda e: self._enlarge(self.selected[0])),
                ft.Container(ft.FilledButton(f"Add {n}", icon=ft.Icons.CHECK, on_click=self.submit),
                             padding=ft.Padding.only(right=8)),
            ],
        )

    def submit(self, e=None) -> None:
        chosen = list(self.selected)
        self.app.back()
        if chosen:
            self.on_pick(chosen)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

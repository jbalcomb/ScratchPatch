"""Thumbnails of local image files: under a photo field as soon as a photo is chosen, and above a job's result."""

from __future__ import annotations

from pathlib import Path

import flet as ft

from taskdeck.core import split_groups

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
WIDTH, HEIGHT = 120, 160  # tall, for receipts; the whole photo is shown, not a crop
MOST = 8  # a strip shows at most this many; more are counted


def image_paths(value: str | list[str] | None) -> list[Path]:
    """The existing image files in a FILE(S) field's value (one path per line) or a list of paths."""
    lines = value if isinstance(value, list) else str(value or "").splitlines()
    out = []
    for line in lines:
        path = Path(str(line).strip()).expanduser()
        if str(line).strip() and path.suffix.lower() in IMAGE_EXTENSIONS and path.is_file():
            out.append(path)
    return out


def strip(value: str | list[str] | None) -> ft.Control | None:
    """A wrapping row of square thumbnails, or None when there is no image to show."""
    paths = image_paths(value)
    if not paths:
        return None
    tiles: list[ft.Control] = []
    for path in paths[:MOST]:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        tiles.append(ft.Container(
            ft.Image(src=data, width=WIDTH, height=HEIGHT, fit=ft.BoxFit.CONTAIN, border_radius=6,
                     semantics_label=path.name,
                     error_content=ft.Icon(ft.Icons.BROKEN_IMAGE_OUTLINED)),
            width=WIDTH, height=HEIGHT, border_radius=8, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            alignment=ft.Alignment.CENTER, tooltip=f"{path.name}: tap to enlarge", ink=True,
            on_click=lambda e, data=data, name=path.name: enlarge(e.page, data, name)))
    if len(paths) > MOST:
        tiles.append(ft.Text(f"+{len(paths) - MOST} more"))
    return ft.Row(tiles, wrap=True, spacing=8, run_spacing=8) if tiles else None


def grouped(value: str | None, noun: str) -> ft.Control | None:
    """A grouped field's thumbnails: one labelled strip per group ("Product 1", "Product 2", ...); one group is
    shown like an ungrouped field."""
    groups = [g for g in split_groups(value) if image_paths(g)]
    if len(groups) <= 1:
        return strip(groups[0] if groups else None)
    parts: list[ft.Control] = []
    for n, group in enumerate(groups, 1):
        row = strip(group)
        if row:
            parts += [ft.Text(f"{noun.capitalize()} {n}", size=13, weight=ft.FontWeight.W_600), row]
    return ft.Column(parts, spacing=4)


def enlarge(page: ft.Page, data: bytes, name: str) -> None:
    """The photo full size, to pinch and zoom: small print on a receipt can be checked against the reading."""
    dialog = ft.AlertDialog(
        title=ft.Text(name, size=14),
        content=ft.InteractiveViewer(ft.Image(src=data, fit=ft.BoxFit.CONTAIN), min_scale=1, max_scale=6),
        actions=[ft.TextButton("Close", on_click=lambda e: page.pop_dialog())],
        inset_padding=8,
    )
    page.show_dialog(dialog)

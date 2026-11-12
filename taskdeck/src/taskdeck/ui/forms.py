"""Build Flet input controls from core ``Field`` definitions (job inputs and job settings)."""

from __future__ import annotations

from typing import Any, Callable

import flet as ft

from taskdeck.core import Field, FieldKind, join_groups, split_groups

from . import thumbnails

Handler = Callable[..., Any] | None


class FieldControl:
    """One rendered field: ``control`` goes in the layout, ``value`` reads it back."""

    def __init__(self, field: Field, value: Any, on_change: Handler = None, on_submit: Handler = None,
                 browse: Callable[["FieldControl"], None] | None = None,
                 camera: Callable[["FieldControl"], None] | None = None):
        """``browse``: what Browse does for FILE fields (the job screen opens TaskDeck's own file browser,
        which gives real paths; Android's picker hands back a copy in the app's cache). ``camera``: what Take
        photos does, for fields with ``Field.camera``."""
        self.field = field
        self._browse_with = browse
        self._camera_with = camera
        self._shown: str | None = None  # the value the thumbnails were drawn for
        self.thumbs = ft.Container()
        if field.preview:
            user_change = on_change

            def on_change(e=None):  # the thumbnails follow typed and pasted paths too
                self.refresh_thumbnails()
                if user_change:
                    user_change(e)
        self.input = _build(field, value, on_change, on_submit)
        self.control: ft.Control = self.input
        if isinstance(self.input, ft.Dropdown):
            self.control = ft.Row([self.input])
        if field.kind == FieldKind.MULTILINE or (field.secret and not field.importable):
            self.control = ft.Column(
                [self.input, ft.Row([ft.TextButton("Paste", icon=ft.Icons.CONTENT_PASTE, on_click=self._paste)])],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )
        if field.kind in (FieldKind.FILE, FieldKind.FILES):
            button = "Add…" if field.kind == FieldKind.FILES else "Browse…"
            buttons = [ft.TextButton(button, icon=ft.Icons.FOLDER_OPEN, on_click=self._browse)]
            if field.camera and camera:
                buttons.insert(0, ft.FilledTonalButton("Take photos", icon=ft.Icons.PHOTO_CAMERA_OUTLINED,
                                                       on_click=lambda e: self._camera_with(self)))
            self.control = ft.Column(
                [self.input, ft.Row(buttons)] + ([self.thumbs] if field.preview else []),
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )
            if field.preview:
                self.refresh_thumbnails()
        if field.importable:
            self.control = ft.Column(
                [self.input, ft.Row([
                    ft.TextButton("Paste", icon=ft.Icons.CONTENT_PASTE, on_click=self._paste),
                    ft.TextButton("Import file…", icon=ft.Icons.UPLOAD_FILE, on_click=self._import),
                ])],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )
        self._on_change = on_change

    def refresh_thumbnails(self) -> None:
        """Redraw the thumbnails when the chosen files changed (preview fields only)."""
        if not self.field.preview:
            return
        value = str(self.input.value or "") if hasattr(self, "input") else ""
        if value == self._shown:
            return
        self._shown = value
        self.thumbs.content = thumbnails.grouped(value, self.field.grouped) if self.field.grouped else \
            thumbnails.strip(value)
        try:
            self.thumbs.update()
        except RuntimeError:  # not on screen yet: it is drawn with the form
            pass

    @property
    def value(self) -> Any:
        return self.input.value

    @value.setter
    def value(self, v: Any) -> None:
        self.input.value = v if isinstance(self.input, ft.Switch) else display(v)
        self.refresh_thumbnails()

    async def _paste(self, e) -> None:
        text = await ft.Clipboard().get()
        if text:
            self.input.value = text
            self.input.update()
            if self._on_change:
                self._on_change(None)

    def picked(self, path: str) -> None:
        """A file chosen with Browse (replaces the value) or Add (FILES: added as a new line, once)."""
        if self.field.kind == FieldKind.FILES:
            lines = [line for line in str(self.input.value or "").splitlines() if line.strip()]
            if path not in lines:
                lines.append(path)
            path = "\n".join(lines)
        self.input.value = path
        self.input.update()
        self.refresh_thumbnails()
        if self._on_change:
            self._on_change(None)

    def picked_many(self, paths: list[str], separate: bool = False) -> None:
        """Several files chosen at once (the photo picker), in order. A grouped field gets them as one new group,
        or with ``separate`` one group each (one product per photo); any other FILES field adds them as lines."""
        if self.field.kind != FieldKind.FILES:
            if paths:
                self.picked(paths[0])
            return
        if self.field.grouped:
            groups = split_groups(self.input.value)
            new = [[p] for p in paths] if separate else [list(paths)]
            self.input.value = join_groups(groups + [g for g in new if g])
        else:
            lines = [line for line in str(self.input.value or "").splitlines() if line.strip()]
            lines += [p for p in paths if p not in lines]
            self.input.value = "\n".join(lines)
        self.input.update()
        self.refresh_thumbnails()
        if self._on_change:
            self._on_change(None)

    async def _browse(self, e) -> None:
        if self._browse_with:
            self._browse_with(self)
            return
        files = await ft.FilePicker().pick_files(dialog_title=f"Choose {self.field.label}")
        if files and files[0].path:
            self.input.value = files[0].path
            self.input.update()
            self.refresh_thumbnails()
            if self._on_change:
                self._on_change(None)

    async def _import(self, e) -> None:
        files = await ft.FilePicker().pick_files(dialog_title=f"Choose {self.field.label}", with_data=True)
        if not files:
            return
        f = files[0]
        data = f.bytes
        if data is None and f.path:
            with open(f.path, "rb") as fh:
                data = fh.read()
        if data is None:
            return
        self.input.value = data.decode("utf-8", errors="replace")
        self.input.update()
        if self._on_change:
            self._on_change(None)


def _build(field: Field, value: Any, on_change: Handler, on_submit: Handler) -> ft.Control:
    label = field.label + (" *" if field.required else "")
    helper = field.help or None
    if field.kind == FieldKind.TOGGLE:
        return ft.Switch(label=field.label, value=bool(value), on_change=on_change)
    if field.kind == FieldKind.CHOICE:
        return ft.Dropdown(
            label=label,
            value=value,
            options=[ft.DropdownOption(c) for c in field.choices],
            enable_filter=len(field.choices) > 8,
            editable=len(field.choices) > 8,
            expand=True,
            helper_text=helper,
            on_select=on_change,
        )
    if field.kind in (FieldKind.MULTILINE, FieldKind.FILES):
        return ft.TextField(label=label, value=display(value), multiline=True, min_lines=field.lines,
                            max_lines=max(field.lines, 10) + 10, helper=helper, on_change=on_change,
                            text_size=16)
    return ft.TextField(
        label=label,
        value=display(value),
        keyboard_type=ft.KeyboardType.NUMBER if field.kind == FieldKind.NUMBER else ft.KeyboardType.TEXT,
        password=field.secret,
        can_reveal_password=field.secret,
        helper=helper,
        helper_max_lines=4,
        on_change=on_change,
        on_submit=on_submit,
        text_size=16,
    )


def display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)

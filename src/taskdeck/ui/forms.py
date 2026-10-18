"""Build Flet input controls from core ``Field`` definitions (job inputs and job settings)."""

from __future__ import annotations

from typing import Any, Callable

import flet as ft

from taskdeck.core import Field, FieldKind

Handler = Callable[..., Any] | None


class FieldControl:
    """One rendered field: ``control`` goes in the layout, ``value`` reads it back."""

    def __init__(self, field: Field, value: Any, on_change: Handler = None, on_submit: Handler = None):
        self.field = field
        self.input = _build(field, value, on_change, on_submit)
        self.control: ft.Control = self.input
        if isinstance(self.input, ft.Dropdown):
            self.control = ft.Row([self.input])
        if field.kind == FieldKind.MULTILINE:
            self.control = ft.Column(
                [self.input, ft.Row([ft.TextButton("Paste", icon=ft.Icons.CONTENT_PASTE, on_click=self._paste)])],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )
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

    @property
    def value(self) -> Any:
        return self.input.value

    @value.setter
    def value(self, v: Any) -> None:
        self.input.value = v if isinstance(self.input, ft.Switch) else display(v)

    async def _paste(self, e) -> None:
        text = await ft.Clipboard().get()
        if text:
            self.input.value = text
            self.input.update()
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
    if field.kind == FieldKind.MULTILINE:
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

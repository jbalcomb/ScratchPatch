"""The command palette: one search box that reaches every job and action."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from taskdeck.core import CommandKind

from .common import highlight, icon_for

if TYPE_CHECKING:
    from .app import TaskDeckApp

KIND_LABEL = {CommandKind.JOB: "Job", CommandKind.QUICK: "Preset", CommandKind.ACTION: "Action"}


class CommandPalette:
    def __init__(self, app: "TaskDeckApp"):
        self.app = app
        self.matches = []
        self.selected = 0
        self.query = ft.TextField(
            hint_text="Type to search  ( > actions, @ jobs )",
            prefix_icon=ft.Icons.SEARCH,
            autofocus=True,
            border_radius=12,
            dense=True,
            on_change=lambda e: self.refresh(),
            on_submit=lambda e: self.run_selected(),
        )
        self.results = ft.ListView(spacing=0, expand=True)
        self.dialog: ft.AlertDialog | None = None

    @property
    def open(self) -> bool:
        return bool(self.dialog and self.dialog.open)

    def show(self, prefix: str = "") -> None:
        if self.open:
            return
        self.query.value = prefix
        self.selected = 0
        self.refresh(update=False)
        # A fresh dialog each time: Flet keeps a closing dialog registered until its
        # exit animation ends, so reusing one would fail on a quick reopen.
        self.dialog = ft.AlertDialog(
            content=ft.Container(
                ft.Column([self.query, self.results], spacing=8, tight=True,
                          horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                width=560,
                height=480,
            ),
            content_padding=ft.Padding.all(12),
            inset_padding=ft.Padding.symmetric(horizontal=12, vertical=24),
            alignment=ft.Alignment.TOP_CENTER,
        )
        self.app.page.show_dialog(self.dialog)
        # autofocus only fires on first mount; refocus every time the palette reopens
        self.app.page.run_task(self.query.focus)

    def close(self) -> None:
        if self.open:
            self.dialog.open = False
            self.dialog.update()

    def refresh(self, update: bool = True) -> None:
        self.matches = self.app.commands.search(self.query.value or "", usage=self.app.command_usage())
        self.selected = min(self.selected, max(len(self.matches) - 1, 0))
        self.results.controls = [self._tile(i, m) for i, m in enumerate(self.matches)] or [
            ft.Container(ft.Text("No matches", color=ft.Colors.ON_SURFACE_VARIANT), padding=16)
        ]
        if update:
            self.results.update()

    def move(self, delta: int) -> None:
        if not self.matches:
            return
        self.selected = (self.selected + delta) % len(self.matches)
        for i, tile in enumerate(self.results.controls):
            if isinstance(tile, ft.ListTile):
                tile.selected = i == self.selected
        self.results.update()
        self.app.page.run_task(self.results.scroll_to, offset=max(0, self.selected - 3) * 56, duration=100)

    def run_selected(self) -> None:
        if self.matches:
            self.run(self.selected)

    def run(self, index: int) -> None:
        command = self.matches[index].command
        self.close()
        self.app.record_command(command.id)
        command.run()

    def _tile(self, i: int, match) -> ft.ListTile:
        c = match.command
        trailing = ft.Text(c.shortcut or KIND_LABEL[c.kind], size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        return ft.ListTile(
            leading=ft.Icon(icon_for(c.icon)),
            title=highlight(c.title, match.positions),
            subtitle=ft.Text(c.subtitle, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS) if c.subtitle else None,
            trailing=trailing,
            dense=True,
            selected=i == self.selected,
            shape=ft.RoundedRectangleBorder(radius=10),
            on_click=lambda e, i=i: self.run(i),
        )

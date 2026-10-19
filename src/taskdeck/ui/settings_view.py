"""Per-job settings screen, generated from the plugin's ``settings`` fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from taskdeck.core import JobPlugin

from .common import screen_view
from .forms import FieldControl

if TYPE_CHECKING:
    from .app import TaskDeckApp


class JobSettingsScreen:
    def __init__(self, app: "TaskDeckApp", plugin: JobPlugin):
        self.app = app
        self.plugin = plugin
        values = plugin.resolve_settings(app.job_settings(plugin.id))
        self.fields = {f.name: FieldControl(f, values.get(f.name)) for f in plugin.settings}
        self.view = screen_view(
            route=f"/job/{plugin.id}/settings",
            # Save is also in the top bar so it is reachable without scrolling past the form.
            appbar=ft.AppBar(
                title=ft.Text(f"Settings · {plugin.title}", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                actions=[ft.TextButton("Save", icon=ft.Icons.CHECK, on_click=self.save)],
            ),
            spacing=16,
            controls=[
                ft.Text("Stored only on this device.", color=ft.Colors.ON_SURFACE_VARIANT),
                *[fc.control for fc in self.fields.values()],
                ft.Row(
                    [
                        ft.FilledButton("Save", icon=ft.Icons.SAVE_OUTLINED, on_click=self.save, height=52),
                        ft.TextButton("Reset to defaults", on_click=self.reset, height=52),
                    ]
                ),
            ],
        )

    def save(self, e=None) -> None:
        values = {name: (fc.value.strip() if isinstance(fc.value, str) else fc.value) for name, fc in self.fields.items()}
        self.app.save_job_settings(self.plugin.id, values)
        self.app.toast("Settings saved")
        self.app.back()

    def reset(self, e=None) -> None:
        for fc in self.fields.values():
            fc.value = fc.field.default
        self.view.update()

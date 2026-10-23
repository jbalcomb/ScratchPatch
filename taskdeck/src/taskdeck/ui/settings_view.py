"""Per-job settings screen, generated from the plugin's ``settings`` fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from taskdeck.core import JobPlugin

from .common import icon_for, screen_view
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
                *self._actions(),
                ft.Row(
                    [
                        ft.FilledButton("Save", icon=ft.Icons.SAVE_OUTLINED, on_click=self.save, height=52),
                        ft.TextButton("Reset to defaults", on_click=self.reset, height=52),
                    ]
                ),
            ],
        )

    def current_values(self) -> dict:
        return {name: (fc.value.strip() if isinstance(fc.value, str) else fc.value) for name, fc in self.fields.items()}

    def _actions(self) -> list[ft.Control]:
        out: list[ft.Control] = []
        for action in self.plugin.settings_actions:
            result = ft.Text("", selectable=True, size=13)
            button = ft.OutlinedButton(action.label, icon=icon_for(action.icon), height=48)
            button.on_click = lambda e, a=action, r=result, b=button: self._run_action(a, r, b)
            out.append(ft.Column([button, result], spacing=6, horizontal_alignment=ft.CrossAxisAlignment.START))
        return out

    def _run_action(self, action, result: ft.Text, button: ft.Control) -> None:
        values = self.plugin.resolve_settings(self.current_values())
        button.disabled = True
        result.value = "Working…"
        self.view.update()

        def work():
            try:
                result.value = action.run(values)
            except Exception as exc:  # a broken check must not break the screen
                result.value = f"{type(exc).__name__}: {exc}"
            button.disabled = False
            self.view.update()

        self.app.page.run_thread(work)

    def save(self, e=None) -> None:
        values = self.current_values()
        self.app.save_job_settings(self.plugin.id, values)
        self.app.toast("Settings saved")
        self.app.back()

    def reset(self, e=None) -> None:
        for fc in self.fields.values():
            fc.value = fc.field.default
        self.view.update()

"""Generic job screen: a form built from the plugin's fields, a run button,
live pipeline progress and the result."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import flet as ft

from taskdeck.core import Context, JobPlugin, RunReport, Stage

from .common import plugin_icon
from .forms import FieldControl

if TYPE_CHECKING:
    from .app import TaskDeckApp

STAGE_COLOR = {
    Stage.VALIDATE: ft.Colors.AMBER,
    Stage.PREPARE: ft.Colors.BLUE,
    Stage.EXECUTE: ft.Colors.GREEN,
    Stage.PRESENT: ft.Colors.PURPLE,
}


class JobScreen:
    def __init__(self, app: "TaskDeckApp", plugin: JobPlugin, preset: dict[str, Any] | None = None):
        self.app = app
        self.plugin = plugin
        self.pipeline = plugin.build_pipeline()
        self.ctx: Context | None = None
        # Bumped whenever an input changes; a preview from an older generation can't be committed.
        self.generation = 0
        self.payload: Any = None
        self.has_output = False
        preset = preset or {}

        self.fields = {
            f.name: FieldControl(f, preset.get(f.name, f.default), on_change=self.invalidate, on_submit=self.run)
            for f in plugin.fields
        }
        self.run_button = ft.FilledButton(plugin.run_label, icon=ft.Icons.PLAY_ARROW, on_click=self.run, height=52)
        self.cancel_button = ft.TextButton("Cancel", visible=False, on_click=self.cancel, height=52)
        self.clear_button = ft.TextButton("Clear", icon=ft.Icons.CLEAR_ALL, on_click=self.clear, height=52)
        self.progress = ft.ProgressBar(value=0, visible=False)
        self.status = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.output = ft.Column(spacing=8)
        self.steps_list = ft.Column([self._step_row(s) for s in self.pipeline], spacing=2)

        self.pin_button = ft.IconButton(
            icon=ft.Icons.PUSH_PIN_OUTLINED,
            selected_icon=ft.Icons.PUSH_PIN,
            selected=app.favorites.is_pinned(plugin.id),
            tooltip="Pin to Home",
            on_click=self.toggle_pin,
        )
        actions: list[ft.Control] = [self.pin_button]
        if plugin.settings:
            actions.append(ft.IconButton(ft.Icons.TUNE, tooltip="Job settings", on_click=lambda e: app.open_settings(plugin.id)))
        actions.append(ft.IconButton(ft.Icons.SEARCH, tooltip="Command palette (Ctrl+K)", on_click=lambda e: app.palette.show()))

        self.view = ft.View(
            route=f"/job/{plugin.id}",
            appbar=ft.AppBar(title=ft.Text(plugin.title), actions=actions),
            scroll=ft.ScrollMode.AUTO,
            padding=16,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    [
                        ft.Icon(plugin_icon(plugin), color=ft.Colors.PRIMARY),
                        ft.Text(plugin.description, color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                    ]
                ),
                *[fc.control for fc in self.fields.values()],
                ft.Row([self.run_button, self.cancel_button, self.clear_button]),
                self.progress,
                ft.Semantics(content=self.status, live_region=True),
                ft.Semantics(content=self.output, live_region=True),
                ft.ExpansionTile(
                    title=ft.Text("Pipeline", size=14),
                    subtitle=ft.Text(" › ".join(s.value for s in self.pipeline.shape), size=12),
                    controls=[ft.Container(self.steps_list, padding=ft.Padding.only(left=16, bottom=8))],
                    leading=ft.Icon(ft.Icons.ACCOUNT_TREE_OUTLINED),
                ),
            ],
        )

    # ---- form -----------------------------------------------------------------

    def raw_inputs(self) -> dict[str, Any]:
        return {name: fc.value for name, fc in self.fields.items()}

    def invalidate(self, e=None) -> None:
        """Inputs changed: drop the result so a stale preview can never be committed."""
        self.generation += 1
        self.payload = None
        if self.has_output:
            self.has_output = False
            self.output.controls = []
            self.status.value = ""
            self.view.update()

    def clear(self, e=None) -> None:
        for fc in self.fields.values():
            fc.value = fc.field.default
        self.invalidate()
        self.view.update()

    # ---- pipeline display ---------------------------------------------------------

    def _step_row(self, s, state: str = "", seconds: float | None = None) -> ft.Control:
        icon = {"ok": ft.Icons.CHECK_CIRCLE, "fail": ft.Icons.ERROR, "run": ft.Icons.PENDING}.get(state, ft.Icons.CIRCLE_OUTLINED)
        color = {"ok": ft.Colors.GREEN, "fail": ft.Colors.ERROR, "run": ft.Colors.PRIMARY}.get(state, ft.Colors.OUTLINE)
        timing = f"{seconds * 1000:.1f} ms" if seconds is not None else ""
        return ft.Row(
            [
                ft.Icon(icon, size=16, color=color),
                ft.Container(
                    ft.Text(s.stage.value, size=10, color=ft.Colors.WHITE),
                    bgcolor=STAGE_COLOR[s.stage],
                    border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                ),
                ft.Text(s.name, size=13, expand=True),
                ft.Text(timing, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            ],
            spacing=8,
        )

    # ---- actions ------------------------------------------------------------------

    def toggle_pin(self, e=None) -> None:
        pinned = self.app.favorites.toggle(self.plugin.id)
        self.pin_button.selected = pinned
        self.pin_button.update()
        self.app.on_favorites_changed()
        self.app.toast("Pinned to Home" if pinned else "Unpinned")

    def cancel(self, e=None) -> None:
        if self.ctx:
            self.ctx.cancel()

    async def run(self, e=None) -> None:
        if self.run_button.disabled:
            return
        raw = self.raw_inputs()
        generation = self.generation
        self.ctx = Context({})
        self._set_running(True)
        steps = list(self.pipeline)

        def on_progress(i: int, total: int, s) -> None:
            # Called from the worker thread; only touch plain attributes and schedule a redraw.
            self.progress.value = i / total if total else 1
            self.status.value = f"Step {min(i + 1, total)}/{total}: {s.name}" if i < total else "Done"
            self.steps_list.controls = [
                self._step_row(st, "ok" if j < i else "run" if j == i else "") for j, st in enumerate(steps)
            ]
            self.app.page.schedule_update()

        settings = self.app.job_settings(self.plugin.id)
        report = await asyncio.to_thread(self.plugin.run, raw, on_progress, self.ctx, settings)
        if generation != self.generation:  # inputs were edited while this was running
            self._set_running(False)
            self.status.value = "Input changed while running; run again."
            self.view.update()
            return
        self._show_report(report)
        self._set_running(False)
        self.app.history.add(self.plugin.id, report.ok, _summary(report), raw)
        self.app.on_history_changed()
        self.view.update()

    def _set_running(self, running: bool) -> None:
        self.run_button.disabled = running
        self.cancel_button.visible = running
        self.progress.visible = running
        self.progress.value = 0 if running else None
        if running:
            self.output.controls = []
            self.payload = None
        self.view.update()

    def _show_report(self, report: RunReport) -> None:
        rows = []
        for j, s in enumerate(self.pipeline):
            r = report.steps[j] if j < len(report.steps) else None
            rows.append(self._step_row(s, "" if r is None else "ok" if r.ok else "fail", r.seconds if r else None))
        self.steps_list.controls = rows
        self.has_output = True
        self.status.value = f"Finished in {report.seconds * 1000:.1f} ms" if report.ok else ""
        if not report.ok:
            self.output.controls = [_error_card(report.error or "Failed")]
            return
        result = report.result
        if result is None:
            self.output.controls = [ft.Text("Done (no output).")]
            return
        parts: list[ft.Control] = []
        if result.title:
            parts.append(ft.Text(result.title, weight=ft.FontWeight.W_600))
        if result.rows:
            parts.append(
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text(k, color=ft.Colors.ON_SURFACE_VARIANT, width=150),
                                ft.Text(v, selectable=True, expand=True, weight=ft.FontWeight.W_500),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.START,
                        )
                        for k, v in result.rows
                    ],
                    spacing=6,
                )
            )
        if result.text is not None:
            parts.append(ft.Text(result.text, selectable=True, font_family="monospace"))
        if result.warning:
            parts.append(ft.Text(result.warning, color=ft.Colors.ERROR))
        text = result.as_plain_text()
        parts.append(
            ft.Row(
                [ft.TextButton("Copy", icon=ft.Icons.COPY, on_click=lambda e: self.app.copy(text))],
                alignment=ft.MainAxisAlignment.END,
            )
        )
        self.output.controls = [ft.Card(ft.Container(ft.Column(parts, spacing=8), padding=16), variant=ft.CardVariant.OUTLINED)]
        if self.plugin.has_commit:
            self.payload = result.payload
            self.commit_button = ft.FilledButton(self.plugin.commit_label, icon=ft.Icons.SAVE_OUTLINED,
                                                 on_click=self.commit, height=52)
            self.commit_error = ft.Column()
            self.output.controls += [self.commit_button, self.commit_error]

    async def commit(self, e=None) -> None:
        """The only step with side effects. Disabled while running and after success."""
        if self.payload is None or self.commit_button.disabled:
            return
        payload, generation = self.payload, self.generation
        self.commit_button.disabled = True
        self.commit_button.content = "Saving…"
        self.commit_error.controls = []
        self.view.update()
        ok, message = await asyncio.to_thread(self.plugin.try_commit, payload, self.app.job_settings(self.plugin.id))
        if ok:
            self.commit_button.content = self.plugin.committed_label
            self.commit_button.icon = ft.Icons.CHECK
            self.app.history.add(self.plugin.id, True, message, self.raw_inputs())
            self.app.on_history_changed()
            self.app.toast(message)
        else:
            self.commit_button.content = self.plugin.commit_label
            # Re-enable only if the preview is still current.
            self.commit_button.disabled = generation != self.generation
            self.commit_error.controls = [_error_card(message)]
        self.view.update()


def _error_card(message: str) -> ft.Control:
    return ft.Container(
        ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE, color=ft.Colors.ON_ERROR_CONTAINER),
                ft.Text(message, color=ft.Colors.ON_ERROR_CONTAINER, expand=True, selectable=True)]),
        bgcolor=ft.Colors.ERROR_CONTAINER,
        border_radius=12,
        padding=12,
    )


def _summary(report: RunReport) -> str:
    if not report.ok:
        return report.error or "Failed"
    if report.result is None:
        return "Done"
    return report.result.as_plain_text().splitlines()[0] if report.result.as_plain_text() else "Done"


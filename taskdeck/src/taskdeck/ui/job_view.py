"""Generic job screen: a form built from the plugin's fields, a run button,
live pipeline progress and the result."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import flet as ft

from taskdeck.core import Context, JobPlugin, Problem, RunReport, Stage, Task
from taskdeck.shared.gdrive import DriveLocation

from . import task_view
from .common import plugin_icon, screen_view
from .file_browser import FileBrowser
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
        # A preset (quick action, history entry) wins; otherwise restore what was typed last time.
        draft = {} if preset else app.drafts.get(plugin.id)
        initial = preset or draft

        self.fields = {
            f.name: FieldControl(f, initial.get(f.name, f.default), on_change=self.invalidate, on_submit=self.run,
                                 browse=self._browse_file)
            for f in plugin.fields
        }
        # Notes under fields that depend on the inputs (JobPlugin.field_notes), e.g. where the output goes.
        self.notes = {name: ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT, selectable=True, visible=False)
                      for name in self.fields}
        restored = any(initial.get(f.name) not in (None, "", f.default) for f in plugin.fields) and bool(draft)
        self.run_button = ft.FilledButton(plugin.run_label, icon=ft.Icons.PLAY_ARROW, on_click=self.run, height=52)
        # Readiness: problems found before running (missing settings, unreadable key, sheet headers...).
        self.readiness = ft.Column(spacing=8)
        self.problems: list[Problem] = []
        self.checking = False
        self.cancel_button = ft.TextButton("Cancel", visible=False, on_click=self.cancel, height=52)
        self.clear_button = ft.TextButton("Clear", icon=ft.Icons.CLEAR_ALL, on_click=self.clear, height=52)
        self.progress = ft.ProgressBar(value=0, visible=False)
        self.status = ft.Text("Restored what you entered last time." if restored else "", size=12,
                              color=ft.Colors.ON_SURFACE_VARIANT)
        self.output = ft.Column(spacing=8)
        # Long work a job handed to a background task (e.g. a Google Drive upload): its progress, then outcome.
        self.task_box = ft.Column(spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        tasks = getattr(app, "tasks", None)
        latest = tasks.latest(plugin.id) if tasks else None
        self.task: Task | None = latest if latest and latest.running else None
        if tasks:
            tasks.listeners.append(self._on_task)
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

        self.view = screen_view(
            route=f"/job/{plugin.id}",
            appbar=ft.AppBar(title=ft.Text(plugin.title), actions=actions),
            controls=[
                ft.Row(
                    [
                        ft.Icon(plugin_icon(plugin), color=ft.Colors.PRIMARY),
                        ft.Text(plugin.description, color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                    ]
                ),
                self.readiness,
                *[c for name, fc in self.fields.items() for c in (fc.control, self.notes[name])],
                ft.Row([self.run_button, self.cancel_button, self.clear_button]),
                self.progress,
                ft.Semantics(content=self.status, live_region=True),
                ft.Semantics(content=self.output, live_region=True),
                self.task_box,
                ft.ExpansionTile(
                    title=ft.Text("Pipeline", size=14),
                    subtitle=ft.Text(" › ".join(s.value for s in self.pipeline.shape), size=12),
                    controls=[ft.Container(self.steps_list, padding=ft.Padding.only(left=16, bottom=8))],
                    leading=ft.Icon(ft.Icons.ACCOUNT_TREE_OUTLINED),
                ),
            ],
        )
        self._render_task()
        self._render_notes()

    # ---- readiness ------------------------------------------------------------------

    @property
    def blocked(self) -> bool:
        return self.checking or any(p.blocking for p in self.problems) or bool(self.task and self.task.running)

    def check_ready(self) -> None:
        """Run the job's checks: offline ones now, network ones in the background."""
        self._render_notes()  # also called on return from the job's settings, which notes may use
        vault = getattr(self.app, "vault", None)
        if vault and vault.needs_unlock(self.plugin.settings_id):
            self.problems = [Problem("Your saved credentials are locked. Unlock them with your fingerprint.", fix="unlock")]
            self.checking = False
            self._render_readiness()
            return
        settings = self.plugin.resolve_settings(self.app.job_settings(self.plugin.id))
        self.problems = self.plugin.check_ready(settings)
        has_online = type(self.plugin).check_ready_online is not JobPlugin.check_ready_online
        network = getattr(self.app, "network", None)
        if has_online and network and network.wifi_only(self.plugin.settings_id) and not network.on_wifi:
            has_online = False  # Wi-Fi only: the online checks wait too
            self.problems = self.problems + [Problem("Wi-Fi only is on: connect to Wi-Fi to check the connection. "
                                                     "Running waits for Wi-Fi.", blocking=False, fix="recheck")]
        self.checking = has_online and not any(p.blocking for p in self.problems)
        self._render_readiness()
        if self.checking:
            def work():
                try:
                    found = self.plugin.check_ready_online(settings)
                except Exception as exc:  # a broken check must not lock the job
                    found = [Problem(f"Couldn't finish checking: {type(exc).__name__}: {exc}", blocking=False, fix="recheck")]
                self.problems = self.problems + found
                self.checking = False
                self._render_readiness()
                self._update()

            self.app.page.run_thread(work)

    def _render_readiness(self) -> None:
        self.run_button.disabled = self.blocked
        if self.checking:
            self.readiness.controls = [ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2),
                                               ft.Text("Checking that everything this job needs is ready…", size=13)])]
            return
        if not self.problems:
            self.readiness.controls = []
            return
        blocking = [p for p in self.problems if p.blocking]
        title = "Before you can run this job:" if blocking else "Heads up:"
        buttons = []
        if any(p.fix == "unlock" for p in self.problems):
            buttons.append(ft.FilledButton("Unlock", icon=ft.Icons.FINGERPRINT,
                                           on_click=lambda e: self.app.page.run_task(self.app.unlock, self.recheck)))
        if any(p.fix == "settings" for p in self.problems) and self.plugin.settings:
            buttons.append(ft.FilledTonalButton("Open settings", icon=ft.Icons.TUNE,
                                                on_click=lambda e: self.app.open_settings(self.plugin.id)))
        for job_id in dict.fromkeys(p.fix[4:] for p in self.problems if p.fix.startswith("job:")):
            other = self.app.registry.get(job_id)
            if other:
                buttons.append(ft.FilledTonalButton(other.title, icon=plugin_icon(other),
                                                    on_click=lambda e, j=job_id: self.app.open_job(j)))
        buttons.append(ft.TextButton("Check again", icon=ft.Icons.REFRESH, on_click=lambda e: self.recheck()))
        color = ft.Colors.ON_ERROR_CONTAINER if blocking else ft.Colors.ON_SURFACE
        self.readiness.controls = [ft.Container(
            ft.Column([
                ft.Text(title, weight=ft.FontWeight.W_600, color=color),
                *[ft.Text(f"• {p.message}", color=color, selectable=True) for p in self.problems],
                ft.Row(buttons, wrap=True),
            ], spacing=6),
            bgcolor=ft.Colors.ERROR_CONTAINER if blocking else ft.Colors.SURFACE_CONTAINER_HIGH,
            border_radius=12,
            padding=12,
        )]

    def recheck(self) -> None:
        self.check_ready()
        self._update()

    def _update(self) -> None:
        try:
            self.view.update()
        except Exception:  # not on screen (yet / any more)
            pass

    # ---- form -----------------------------------------------------------------

    def raw_inputs(self) -> dict[str, Any]:
        return {name: fc.value for name, fc in self.fields.items()}

    def invalidate(self, e=None) -> None:
        """Inputs changed: keep a draft, and drop the result so a stale preview can never be committed."""
        self.app.drafts.save(self.plugin.id, self.raw_inputs())
        self._render_notes()
        self._update()
        self.generation += 1
        self.payload = None
        if self.has_output:
            self.has_output = False
            self.output.controls = []
            self.status.value = ""
            self.view.update()

    def _render_notes(self) -> None:
        try:
            settings = self.plugin.resolve_settings(self.app.job_settings(self.plugin.id))
            notes = self.plugin.field_notes(self.raw_inputs(), settings)
        except Exception:  # a broken note must not break the screen
            notes = {}
        for name, text in self.notes.items():
            text.value = notes.get(name, "")
            text.visible = bool(text.value)

    def _browse_file(self, fc: FieldControl) -> None:
        """Browse for a FILE field with TaskDeck's own browser (real paths), starting where the current one is."""
        exts = tuple(e.lower() for e in fc.field.extensions)
        current = Path(str(fc.value or "")).expanduser()
        FileBrowser.pick_file(self.app, lambda path: fc.picked(str(path)),
                              start=current.parent if fc.value and current.parent.is_dir() else None,
                              title=f"Choose {fc.field.label}",
                              accept=(lambda name: name.lower().endswith(exts)) if exts else (lambda name: True),
                              kind=" or ".join(exts) + " files" if exts else "files")

    def clear(self, e=None) -> None:
        for fc in self.fields.values():
            fc.value = fc.field.default
        self.invalidate()
        self.app.drafts.clear(self.plugin.id)
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
        if self.run_button.disabled or self.blocked:
            return
        raw = self.raw_inputs()
        self.app.drafts.save(self.plugin.id, raw)
        if not await self._unlock_accounts(raw):
            return
        generation = self.generation
        self.ctx = Context({}, services={**self.app.services(), "start_task": self._start_task})
        self._set_running(True)
        steps = list(self.pipeline)

        def on_progress(i: int, total: int, s) -> None:
            # Called from the worker thread; only touch plain attributes and schedule a redraw.
            self.progress.value = i / total if total else 1
            self.status.value = f"Step {min(i + 1, total)}/{total}: {s.name}" if i < total else "Done"
            self.steps_list.controls = [
                self._step_row(st, "ok" if j < i else "run" if j == i else "") for j, st in enumerate(steps)
            ]
            self.app.page.update()

        settings = self.app.job_settings(self.plugin.id)
        report = await asyncio.to_thread(self._network_scoped(self.plugin.run, self.ctx), raw, on_progress,
                                         self.ctx, settings)
        if generation != self.generation:  # inputs were edited while this was running
            self._set_running(False)
            self.status.value = "Input changed while running; run again."
            self.view.update()
            return
        self._show_report(report)
        self._set_running(False)
        if report.ok and not self.plugin.has_commit:
            # The draft is kept until the job is done (for jobs with a save step: until it saves).
            self.app.drafts.clear(self.plugin.id)
        self.app.history.add(self.plugin.id, report.ok, _summary(report), raw)
        self.app.on_history_changed()
        self.view.update()

    # ---- background tasks ------------------------------------------------------------

    def _start_task(self, title: str, work) -> Task:
        """``Context.services["start_task"]``: run work(task) in the background under the job's Wi-Fi-only
        setting, shown on this screen (and in the notification) like Extract Archive."""
        network, settings_id = getattr(self.app, "network", None), self.plugin.settings_id

        def run(t: Task) -> str:
            if network is None:
                return work(t)
            with network.scope(settings_id, lambda: t.cancel_requested, t.set_note):  # Wi-Fi only
                return work(t)

        self.task = self.app.tasks.start(title, self.plugin.id, run, inputs=self.raw_inputs())
        return self.task

    def _on_task(self, task: Task) -> None:
        if task.job_id != self.plugin.id or (self.task and task is not self.task and self.task.running):
            return
        self.task = task
        self._render_task()
        self._render_readiness()
        self._update()

    def _render_task(self) -> None:
        t = self.task
        if not t:
            self.task_box.controls = []
        elif t.running:
            self.task_box.controls = task_view.running_rows(t, self.cancel_task)
        else:
            buttons: list[ft.Control] = [ft.TextButton("Close", on_click=self.dismiss_task)]
            if isinstance(t.result, DriveLocation):
                url = task_view.drive_url(t.result)
                buttons.insert(0, ft.FilledTonalButton("Open in Drive", icon=ft.Icons.OPEN_IN_NEW,
                                                       on_click=lambda e: self.app.open_url(url)))
            self.task_box.controls = [task_view.finished_card(t, buttons)]

    def cancel_task(self, e=None) -> None:
        task = self.task
        if not task or not task.running:
            return

        def stop(e=None):
            self.app.page.pop_dialog()
            self.app.tasks.cancel(task)

        self.app.page.show_dialog(ft.AlertDialog(
            title=ft.Text(f"Stop {task.title[:1].lower()}{task.title[1:]}?"),
            content=ft.Text("What's already done stays."),
            actions=[ft.TextButton("Keep going", on_click=lambda e: self.app.page.pop_dialog()),
                     ft.FilledButton("Stop", on_click=stop)]))

    def dismiss_task(self, e=None) -> None:
        if self.task and not self.task.running:
            self.app.tasks.forget(self.task)
            self.task = None
        self._render_task()
        self._update()

    async def _unlock_accounts(self, raw: dict[str, Any]) -> bool:
        """Ask for the fingerprint when the run signs in with an account whose credentials are locked
        (e.g. Parse executable with "Push to Google Drive" on). False if they stay locked."""
        vault = getattr(self.app, "vault", None)
        if not vault or not any(vault.needs_unlock(a) for a in self.plugin.accounts_needed(raw)):
            return True
        if await self.app.unlock():
            return True
        self.status.value = "Your credentials are still locked: unlock them to sign in to your account."
        self._update()
        return False

    def _network_scoped(self, fn, ctx):
        """fn, run under the job's Wi-Fi-only setting (in the worker thread): network requests wait for Wi-Fi,
        with "Waiting for Wi-Fi…" in the status line, and Cancel stops the wait."""
        network = getattr(self.app, "network", None)
        if network is None:
            return fn

        def on_wait(text: str) -> None:
            self.status.value = text or "Continuing…"
            self.app.page.update()

        def run(*args):
            cancelled = (lambda: ctx._cancelled) if ctx else (lambda: False)
            with network.scope(self.plugin.settings_id, cancelled, on_wait):
                return fn(*args)
        return run

    def _set_running(self, running: bool) -> None:
        self.run_button.disabled = running or self.blocked
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
        if self.plugin.has_commit and result.payload is not None:
            self.payload = result.payload
            self.commit_button = ft.FilledButton(self.plugin.commit_label, icon=ft.Icons.SAVE_OUTLINED,
                                                 on_click=self.commit, height=52)
            self.commit_error = ft.Column()
            self.output.controls += [self.commit_button, self.commit_error]

    async def commit(self, e=None, payload: Any = None) -> None:
        """The only step with side effects. Disabled while running and after success.

        ``payload`` overrides the preview's when the user confirms a follow-up (e.g. "Replace row 44")."""
        if self.payload is None or (self.commit_button.disabled and payload is None):
            return
        payload, generation = payload if payload is not None else self.payload, self.generation
        self.commit_button.disabled = True
        self.commit_button.content = "Saving…"
        self.commit_error.controls = []
        self.view.update()
        outcome = await asyncio.to_thread(self._network_scoped(self.plugin.attempt_commit, None), payload,
                                          self.app.job_settings(self.plugin.id))
        ok, message = outcome.ok, outcome.message
        if ok:
            self.commit_button.content = self.plugin.committed_label
            self.commit_button.icon = ft.Icons.CHECK
            if generation == self.generation:
                self.app.drafts.clear(self.plugin.id)
            self.app.history.add(self.plugin.id, True, message, self.raw_inputs())
            self.app.on_history_changed()
            self.app.toast(message)
        else:
            self.commit_button.content = self.plugin.commit_label
            # Re-enable only if the preview is still current.
            self.commit_button.disabled = generation != self.generation
            self.commit_error.controls = [_error_card(message)]
            if outcome.confirm and generation == self.generation:
                confirm = outcome.confirm
                self.commit_error.controls.append(ft.OutlinedButton(
                    confirm.confirm_label, icon=ft.Icons.WARNING_AMBER,
                    on_click=lambda e: self.app.page.run_task(self.commit, None, confirm.payload)))
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


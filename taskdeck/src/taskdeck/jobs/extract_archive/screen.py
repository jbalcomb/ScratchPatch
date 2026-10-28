"""The Extract Archive screen (see docs/jobs/extract_archive_SPEC.md §3)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from taskdeck.core import Task
from taskdeck.core.tasks import CANCELLED, DONE, FAILED
from taskdeck.shared import files
from taskdeck.shared.gdrive import ROOT, DriveLocation
from taskdeck.ui import file_access
from taskdeck.ui.common import plugin_icon, screen_view, section_title
from taskdeck.ui.file_browser import FileBrowser

from . import logic

if TYPE_CHECKING:
    from taskdeck.ui.app import TaskDeckApp

RECENT_KEY = "extract:recent"


class ExtractScreen:
    def __init__(self, app: "TaskDeckApp", plugin):
        self.app = app
        self.plugin = plugin
        self.archive: Path | None = None
        self.scan: logic.Scan | None = None
        self.scanning = False
        self.parent: Path | None = None
        self.name: str | None = None
        self.access_ok = not files.is_android()
        task = app.tasks.latest(plugin.id)
        self.task: Task | None = task if task and task.running else None

        self.access_box = ft.Column(spacing=8)
        self.archive_box = ft.Column(spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.password = ft.TextField(label="Password", password=True, can_reveal_password=True, text_size=16,
                                     visible=False, on_submit=lambda e: self.rescan(), on_change=lambda e: self.render())
        self.password_note = ft.Text("", size=13, visible=False)
        self.dest_box = ft.Column(spacing=8)
        self.problems_box = ft.Column(spacing=6)
        self.extract_button = ft.FilledButton("Extract", icon=ft.Icons.UNARCHIVE, height=52, on_click=self.extract)
        self.overwrite_note = ft.Text("Existing files with the same name are replaced.", size=13,
                                      color=ft.Colors.ON_SURFACE_VARIANT)
        self.form = ft.Column([
            section_title("Archive"), self.archive_box, self.password, self.password_note,
            section_title("Extract to"), self.dest_box, self.problems_box, self.extract_button, self.overwrite_note,
        ], spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.progress_box = ft.Column(spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self.pin_button = ft.IconButton(icon=ft.Icons.PUSH_PIN_OUTLINED, selected_icon=ft.Icons.PUSH_PIN,
                                        selected=app.favorites.is_pinned(plugin.id), tooltip="Pin to Home",
                                        on_click=self.toggle_pin)
        self.view = screen_view(
            route=f"/job/{plugin.id}",
            appbar=ft.AppBar(title=ft.Text(plugin.title), actions=[
                self.pin_button,
                ft.IconButton(ft.Icons.SEARCH, tooltip="Command palette (Ctrl+K)", on_click=lambda e: app.palette.show()),
            ]),
            controls=[
                ft.Row([ft.Icon(plugin_icon(plugin), color=ft.Colors.PRIMARY),
                        ft.Text(plugin.description, color=ft.Colors.ON_SURFACE_VARIANT, expand=True)]),
                self.access_box, self.progress_box, self.form,
            ],
        )
        app.tasks.listeners.append(self._on_task)
        self.render(update=False)

    # ---- lifecycle ---------------------------------------------------------------------

    def check_ready(self) -> None:
        """Called by the app after the screen is shown: check file access first."""
        self.app.page.run_task(self._check_access)

    async def _check_access(self) -> None:
        self.access_ok = await file_access.granted()
        self.render()

    async def _request_access(self) -> None:
        self.access_ok = await file_access.request()
        if not self.access_ok:
            self.app.toast("File access is still off. Turn on “Allow access to manage all files” for TaskDeck.")
        self.render()

    def toggle_pin(self, e=None) -> None:
        self.app.toggle_pin(self.plugin.id)
        self.pin_button.selected = self.app.favorites.is_pinned(self.plugin.id)
        self._update()

    def _update(self) -> None:
        try:
            self.view.update()
        except Exception:  # not on screen (yet / any more)
            pass

    # ---- choosing --------------------------------------------------------------------------

    def browse(self, e=None) -> None:
        start = self.archive.parent if self.archive else None
        FileBrowser.pick_file(self.app, self.set_archive, start=start)

    def set_archive(self, path: Path) -> None:
        self.archive = path
        self.parent, self.name = logic.default_destination(path)
        self.password.value = ""
        recent = [p for p in self.app.store.get(RECENT_KEY, []) if p != str(path)]
        self.app.store.set(RECENT_KEY, [str(path)] + recent[:4])
        self.rescan()

    def rescan(self) -> None:
        if not self.archive:
            return
        self.scanning = True
        self.render()
        path, password = self.archive, self.password.value or None

        def work():
            result = logic.scan(path, password)
            if path == self.archive:  # still the chosen archive
                self.scan = result
                self.scanning = False
                self.render()

        self.app.page.run_thread(work)

    def change_destination(self, e=None) -> None:
        FileBrowser.pick_folder(self.app, self.set_destination, start=self.parent,
                                new_folder_name=self.name if self.name is not None else
                                files.archive_stem(self.archive.name))

    def set_destination(self, parent: "Path | DriveLocation", name: str | None) -> None:
        self.parent, self.name = parent, name
        self.render()

    # ---- extracting ----------------------------------------------------------------------------

    @property
    def to_drive(self) -> bool:
        return isinstance(self.parent, DriveLocation)

    def destination(self) -> Path:
        return self.parent / self.name if self.name else self.parent

    def destination_text(self) -> str:
        if self.to_drive:
            return self.parent.display(self.name) + ("/" if self.name else "")
        return files.display_path(self.destination()) + ("/" if self.name else "")

    def extract(self, e=None) -> None:
        if not self.scan or self.task or self.problems():
            return
        path, password = self.archive, self.password.value or None
        if self.to_drive:
            drive, location, name, temp = self.app.google.drive(), self.parent, self.name, self.app.temp_dir
            network, settings_id = self.app.network, self.plugin.settings_id

            def work(t):
                with network.scope(settings_id, lambda: t.cancel_requested, t.set_note):  # Wi-Fi only
                    return logic.extract_to_drive(t, path, password, drive, location, name, temp)

            self.task = self.app.tasks.start(f"Extracting {path.name} to Google Drive", self.plugin.id, work,
                                             result=location)
        else:
            dest = self.destination()
            self.task = self.app.tasks.start(f"Extracting {path.name}", self.plugin.id,
                                             lambda t: logic.extract(t, path, dest, password), result=dest)
        self.render()

    def cancel(self, e=None) -> None:
        task = self.task
        if not task or not task.running:
            return

        def stop(e=None):
            self.app.page.pop_dialog()
            self.app.tasks.cancel(task)

        self.app.page.show_dialog(ft.AlertDialog(
            title=ft.Text("Stop extracting?"),
            content=ft.Text("Files already uploaded stay in Google Drive." if isinstance(task.result, DriveLocation)
                            else f"Files already extracted stay in {files.display_path(task.result)}."),
            actions=[ft.TextButton("Keep extracting", on_click=lambda e: self.app.page.pop_dialog()),
                     ft.FilledButton("Stop", on_click=stop)]))

    def _on_task(self, task: Task) -> None:
        if task.job_id != self.plugin.id or (self.task and task is not self.task):
            return
        self.task = task
        self.render(update=False)
        try:
            self.app.page.update()
        except Exception:
            pass

    def problems(self) -> list[str]:
        if not self.access_ok:
            return ["Allow file access first."]
        if not self.scan or self.scanning:
            return []
        if self.scan.names_hidden and not self.scan.error and self.password.value:
            return ["Tap “Unlock” to check the password."]
        google = self.app.google
        return logic.problems(self.scan, self.parent, self.name, self.password.value or "",
                              drive_connected=google.connected and not google.locked, temp_root=self.app.temp_dir)

    # ---- rendering ------------------------------------------------------------------------------

    def render(self, update: bool = True) -> None:
        self._render_access()
        self._render_archive()
        self._render_destination()
        self._render_progress()
        busy = bool(self.task and self.task.running)
        problems = self.problems() if self.archive else []
        self.problems_box.controls = [_notice(problems)] if problems and self.scan and not self.scanning else []
        self.extract_button.disabled = busy or not self.scan or self.scanning or bool(problems)
        self.form.visible = not busy
        if update:
            self._update()

    def _render_access(self) -> None:
        if self.access_ok:
            self.access_box.controls = []
            return
        if not file_access.supported():
            self.access_box.controls = [_notice([file_access.NEEDS_APK])]
            return
        self.access_box.controls = [ft.Container(ft.Column([
            ft.Text("Allow file access", weight=ft.FontWeight.W_600),
            ft.Text(file_access.EXPLAIN),
            ft.Row([ft.FilledButton("Allow file access", icon=ft.Icons.FOLDER_SHARED,
                                    on_click=lambda e: self.app.page.run_task(self._request_access)),
                    ft.TextButton("Check again", icon=ft.Icons.REFRESH,
                                  on_click=lambda e: self.app.page.run_task(self._check_access))], wrap=True),
        ], spacing=8), bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH, border_radius=12, padding=12)]

    def _render_archive(self) -> None:
        if not self.archive:
            recent = [Path(p) for p in self.app.store.get(RECENT_KEY, []) if Path(p).is_file()]
            self.archive_box.controls = [
                ft.FilledTonalButton("Browse for an archive", icon=ft.Icons.FOLDER_OPEN, height=56,
                                     on_click=self.browse, disabled=not self.access_ok),
                *([ft.Text("Recent archives", size=13, color=ft.Colors.ON_SURFACE_VARIANT)] if recent else []),
                *[ft.ListTile(leading=ft.Icon(ft.Icons.FOLDER_ZIP_OUTLINED), title=ft.Text(p.name, max_lines=2),
                              subtitle=ft.Text(files.display_path(p.parent), max_lines=1),
                              on_click=lambda e, p=p: self.set_archive(p), disabled=not self.access_ok)
                  for p in recent],
            ]
            self.password.visible = self.password_note.visible = False
            return
        s = self.scan
        lines: list[ft.Control] = [ft.Text(self.archive.name, weight=ft.FontWeight.W_600, selectable=True)]
        if self.scanning:
            lines.append(ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2), ft.Text("Reading the archive…")]))
        elif s and s.error and not s.names_hidden:
            lines.append(ft.Text(s.error, color=ft.Colors.ERROR))
        elif s:
            lines.append(ft.Text(s.summary, color=ft.Colors.ON_SURFACE_VARIANT))
            if s.names_hidden:
                lines.append(ft.Text("Encrypted archive: enter the password to see what's inside.",
                                     color=ft.Colors.ON_SURFACE_VARIANT))
            else:
                lines.append(ft.Text(f"Unpacks to {files.human_size(s.unpacked)}", color=ft.Colors.ON_SURFACE_VARIANT))
        lines.append(ft.Row([ft.TextButton("Change…", icon=ft.Icons.FOLDER_OPEN, on_click=self.browse)],
                            alignment=ft.MainAxisAlignment.END))
        self.archive_box.controls = [ft.Card(ft.Container(ft.Column(lines, spacing=4), padding=12),
                                             variant=ft.CardVariant.OUTLINED)]
        needs_pw = bool(s and s.encrypted)
        self.password.visible = needs_pw
        self.password_note.visible = needs_pw
        if needs_pw:
            if s.names_hidden and s.error:
                self.password_note.value, self.password_note.color = s.error, ft.Colors.ERROR
            elif s.names_hidden:
                self.password_note.value = "Press Enter (or Unlock) after typing it to open the archive."
                self.password_note.color = ft.Colors.ON_SURFACE_VARIANT
            else:
                self.password_note.value = "Checked when extracting."
                self.password_note.color = ft.Colors.ON_SURFACE_VARIANT
            self.password.suffix = ft.TextButton("Unlock", on_click=lambda e: self.rescan()) if s.names_hidden else None

    def _render_destination(self) -> None:
        if not self.archive or not self.parent:
            self.dest_box.controls = [ft.Text("Choose an archive first.", color=ft.Colors.ON_SURFACE_VARIANT)]
            return
        if self.to_drive:
            icon = ft.Icons.ADD_TO_DRIVE
            note = ("a folder named after the archive (reused if it exists)" if self.name else "straight into this folder") \
                + " · unpacked on the phone first, then uploaded"
        else:
            icon = ft.Icons.FOLDER_OUTLINED
            note = "new folder" if self.name and not self.destination().exists() else \
                "existing folder" if self.name else "straight into this folder"
        self.dest_box.controls = [ft.Card(ft.Container(ft.Column([
            ft.Row([ft.Icon(icon), ft.Text(self.destination_text(), expand=True, selectable=True)]),
            ft.Text(note, size=13, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Row([ft.TextButton("Change…", icon=ft.Icons.EDIT_LOCATION_ALT_OUTLINED, on_click=self.change_destination)],
                   alignment=ft.MainAxisAlignment.END),
        ], spacing=4), padding=12), variant=ft.CardVariant.OUTLINED)]

    def _render_progress(self) -> None:
        t = self.task
        if not t:
            self.progress_box.controls = []
            return
        if t.running:
            if t.phases:  # e.g. Unpacking, then Uploading to Google Drive: one bar each
                lines = [ft.Text(t.title, weight=ft.FontWeight.W_600, max_lines=2)]
                for phase in t.phases:
                    lines += _phase_rows(phase, active=phase is t.current_phase)
            else:
                pct = f" {t.fraction * 100:.0f}%" if t.fraction is not None else ""
                lines = [ft.Text(f"Extracting…{pct}", weight=ft.FontWeight.W_600),
                         ft.ProgressBar(value=t.fraction),
                         ft.Text(f"{files.human_size(t.done)} of {files.human_size(t.total)}{_speed(t)}"
                                 if t.total else "Starting…")]
                eta = t.eta()
                if eta is not None:
                    lines.append(ft.Text(f"{files.human_duration(eta)} left".capitalize(),
                                         color=ft.Colors.ON_SURFACE_VARIANT))
            if t.note and not t.cancel_requested:
                lines.append(ft.Row([ft.Icon(ft.Icons.CLOUD_OFF, size=18, color=ft.Colors.ERROR),
                                     ft.Text(t.note, expand=True, color=ft.Colors.ERROR)]))
            if t.cancel_requested:
                lines.append(ft.Text("Stopping…", color=ft.Colors.ON_SURFACE_VARIANT))
            lines += [ft.Row([ft.OutlinedButton("Cancel", icon=ft.Icons.STOP, on_click=self.cancel,
                                                disabled=t.cancel_requested)]),
                      ft.Text("You can keep using TaskDeck.", size=13, color=ft.Colors.ON_SURFACE_VARIANT)]
            self.progress_box.controls = lines
            return
        color = {DONE: ft.Colors.PRIMARY_CONTAINER, FAILED: ft.Colors.ERROR_CONTAINER}.get(t.status, ft.Colors.SURFACE_CONTAINER_HIGH)
        icon = {DONE: ft.Icons.CHECK_CIRCLE, FAILED: ft.Icons.ERROR_OUTLINE, CANCELLED: ft.Icons.STOP_CIRCLE}[t.status]
        buttons = [ft.TextButton("Close", on_click=self.dismiss)]
        if t.status == DONE and isinstance(t.result, Path):
            buttons.insert(0, ft.FilledTonalButton("Show files", icon=ft.Icons.FOLDER_OPEN,
                                                   on_click=lambda e: FileBrowser.show(self.app, t.result, self.set_archive)))
        elif t.status == DONE and isinstance(t.result, DriveLocation):
            fid = t.result.folder_id
            url = "https://drive.google.com/drive/my-drive" if fid == ROOT else f"https://drive.google.com/drive/folders/{fid}"
            buttons.insert(0, ft.FilledTonalButton("Open in Drive", icon=ft.Icons.OPEN_IN_NEW,
                                                   on_click=lambda e: self.app.open_url(url)))
        self.progress_box.controls = [ft.Container(ft.Column([
            ft.Row([ft.Icon(icon), ft.Text(t.message, expand=True, selectable=True)]),
            ft.Row(buttons, wrap=True),
        ], spacing=8), bgcolor=color, border_radius=12, padding=12)]

    def dismiss(self, e=None) -> None:
        if self.task and not self.task.running:
            self.app.tasks.forget(self.task)
            self.task = None
        self.render()


def _speed(progress) -> str:
    """The current speed of a Task or Phase as " · 1.2 MB/s", or nothing until it is known."""
    speed = progress.speed()
    return "" if speed is None else f" · {files.human_speed(speed)}"


def _phase_rows(phase, active: bool) -> list[ft.Control]:
    """A phase's label and state, its bar, and (while it runs) size and time left."""
    if phase.complete:
        state = "✓ Done"
    elif active and phase.started is not None:
        state = f"{(phase.fraction or 0) * 100:.0f}%"
    else:
        state = "Waiting"
    rows: list[ft.Control] = [
        ft.Row([ft.Text(phase.label, expand=True, weight=ft.FontWeight.W_500 if active else None),
                ft.Text(state, color=ft.Colors.ON_SURFACE_VARIANT)]),
        ft.ProgressBar(value=phase.fraction if phase.started is not None or phase.complete else 0),
    ]
    if active and phase.started is not None:
        line = f"{files.human_size(phase.done)} of {files.human_size(phase.total)}{_speed(phase)}"
        eta = phase.eta()
        if eta is not None:
            line += f" · {files.human_duration(eta)} left"
        rows.append(ft.Text(line, size=13, color=ft.Colors.ON_SURFACE_VARIANT))
    return rows


def _notice(lines: list[str]) -> ft.Control:
    return ft.Container(ft.Column([ft.Text(f"• {line}", color=ft.Colors.ON_ERROR_CONTAINER, selectable=True) for line in lines],
                                  spacing=4),
                        bgcolor=ft.Colors.ERROR_CONTAINER, border_radius=12, padding=12)

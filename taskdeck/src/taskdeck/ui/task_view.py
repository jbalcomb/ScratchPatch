"""How a background task looks on a job screen: a bar per phase while it runs, then its outcome.

Used by Extract Archive's screen and by the generic job screen (jobs that hand long work, such as a Google
Drive upload, to a background task).
"""

from __future__ import annotations

from typing import Callable

import flet as ft

from taskdeck.core import Task
from taskdeck.core.tasks import CANCELLED, DONE, FAILED
from taskdeck.shared import files
from taskdeck.shared.gdrive import ROOT, DriveLocation


def speed(progress) -> str:
    """The current speed of a Task or Phase as " · 1.2 MB/s", or nothing until it is known."""
    s = progress.speed()
    return "" if s is None else f" · {files.human_speed(s)}"


def phase_rows(phase, active: bool) -> list[ft.Control]:
    """A phase's label and state, its bar, and (while it runs) size and time left."""
    if phase.complete:
        state = "✓ Done"
    elif active and phase.started is not None and not phase.total:
        state = "…"  # total not known yet (e.g. still checking what's already in Drive)
    elif active and phase.started is not None:
        state = f"{(phase.fraction or 0) * 100:.0f}%"
    else:
        state = "Waiting"
    rows: list[ft.Control] = [
        ft.Row([ft.Text(phase.label, expand=True, weight=ft.FontWeight.W_500 if active else None),
                ft.Text(state, color=ft.Colors.ON_SURFACE_VARIANT)]),
        ft.ProgressBar(value=phase.fraction if phase.started is not None or phase.complete else 0),
    ]
    if active and phase.started is not None and phase.unit == "files":
        s = phase.speed()
        line = f"{phase.done:,} of {phase.total:,} files" + (f" · {files.human_rate(s)}" if s else "")
        eta = phase.eta()
        if eta is not None:
            line += f" · {files.human_duration(eta)} left"
        rows.append(ft.Text(line, size=13, color=ft.Colors.ON_SURFACE_VARIANT))
    elif active and phase.started is not None:
        line = f"{files.human_size(phase.done)} of {files.human_size(phase.total)}{speed(phase)}"
        eta = phase.eta()
        if eta is not None:
            line += f" · {files.human_duration(eta)} left"
        rows.append(ft.Text(line, size=13, color=ft.Colors.ON_SURFACE_VARIANT))
    return rows


def running_rows(t: Task, on_cancel: Callable, heading: str = "Working…") -> list[ft.Control]:
    """A running task: one bar per phase (or one overall bar), a connection note, Cancel."""
    if t.phases:  # e.g. Unpacking, then Uploading to Google Drive: one bar each
        lines: list[ft.Control] = [ft.Text(t.title, weight=ft.FontWeight.W_600, max_lines=2)]
        for phase in t.phases:
            lines += phase_rows(phase, active=phase is t.current_phase)
    else:
        pct = f" {t.fraction * 100:.0f}%" if t.fraction is not None else ""
        lines = [ft.Text(f"{heading}{pct}", weight=ft.FontWeight.W_600),
                 ft.ProgressBar(value=t.fraction),
                 ft.Text(f"{files.human_size(t.done)} of {files.human_size(t.total)}{speed(t)}"
                         if t.total else "Starting…")]
        eta = t.eta()
        if eta is not None:
            lines.append(ft.Text(f"{files.human_duration(eta)} left".capitalize(), color=ft.Colors.ON_SURFACE_VARIANT))
    if t.note and not t.cancel_requested:
        lines.append(ft.Row([ft.Icon(ft.Icons.CLOUD_OFF, size=18, color=ft.Colors.ERROR),
                             ft.Text(t.note, expand=True, color=ft.Colors.ERROR)]))
    if t.cancel_requested:
        lines.append(ft.Text("Stopping…", color=ft.Colors.ON_SURFACE_VARIANT))
    lines += [ft.Row([ft.OutlinedButton("Cancel", icon=ft.Icons.STOP, on_click=on_cancel, disabled=t.cancel_requested)]),
              ft.Text("You can keep using TaskDeck.", size=13, color=ft.Colors.ON_SURFACE_VARIANT)]
    return lines


def drive_url(location: DriveLocation) -> str:
    fid = location.folder_id
    return "https://drive.google.com/drive/my-drive" if fid == ROOT else f"https://drive.google.com/drive/folders/{fid}"


def finished_card(t: Task, buttons: list[ft.Control]) -> ft.Control:
    """A finished task's outcome with its buttons (e.g. Open in Drive, Close)."""
    color = {DONE: ft.Colors.PRIMARY_CONTAINER, FAILED: ft.Colors.ERROR_CONTAINER}.get(t.status, ft.Colors.SURFACE_CONTAINER_HIGH)
    icon = {DONE: ft.Icons.CHECK_CIRCLE, FAILED: ft.Icons.ERROR_OUTLINE, CANCELLED: ft.Icons.STOP_CIRCLE}[t.status]
    return ft.Container(ft.Column([
        ft.Row([ft.Icon(icon), ft.Text(t.message, expand=True, selectable=True)]),
        ft.Row(buttons, wrap=True),
    ], spacing=8), bgcolor=color, border_radius=12, padding=12)

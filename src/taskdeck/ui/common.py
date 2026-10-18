"""Small UI helpers shared by the screens."""

from __future__ import annotations

import flet as ft

from taskdeck.core import JobPlugin


def icon_for(name: str) -> ft.IconData:
    return getattr(ft.Icons, name, None) or ft.Icons.WORK_OUTLINE


def plugin_icon(plugin: JobPlugin) -> ft.IconData:
    return icon_for(plugin.icon)


def section_title(text: str, action: ft.Control | None = None) -> ft.Control:
    label = ft.Text(text, size=14, weight=ft.FontWeight.W_600, color=ft.Colors.ON_SURFACE_VARIANT)
    return ft.Row([label, action] if action else [label], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)


def empty_state(icon: ft.IconData, title: str, hint: str) -> ft.Control:
    return ft.Container(
        ft.Column(
            [
                ft.Icon(icon, size=40, color=ft.Colors.OUTLINE),
                ft.Text(title, weight=ft.FontWeight.W_600),
                ft.Text(hint, color=ft.Colors.ON_SURFACE_VARIANT, text_align=ft.TextAlign.CENTER),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=6,
        ),
        padding=24,
        alignment=ft.Alignment.CENTER,
    )


def highlight(text: str, positions: list[int]) -> ft.Text:
    """Bold the characters the fuzzy matcher hit, like VS Code's quick open."""
    if not positions:
        return ft.Text(text, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
    hit = set(positions)
    spans: list[ft.TextSpan] = []
    run, run_hit = "", None
    for i, ch in enumerate(text):
        is_hit = i in hit
        if run and is_hit != run_hit:
            spans.append(_span(run, run_hit))
            run = ""
        run += ch
        run_hit = is_hit
    if run:
        spans.append(_span(run, run_hit))
    return ft.Text(spans=spans, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)


def _span(text: str, bold: bool | None) -> ft.TextSpan:
    style = ft.TextStyle(weight=ft.FontWeight.BOLD, color=ft.Colors.PRIMARY) if bold else None
    return ft.TextSpan(text, style=style)

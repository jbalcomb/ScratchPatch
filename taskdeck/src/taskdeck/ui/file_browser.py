"""TaskDeck's own file browser for phone storage (a pushed screen).

Two modes:

* ``FileBrowser.pick_file``: choose a file (archives only unless "Show all files").
* ``FileBrowser.pick_folder``: choose where to put something, like a "Save as" dialog:
  optional new folder name, location chips, folder list, New folder, and a fixed
  "Will extract to:" bar with Cancel / Use this.

It reads real paths, so it needs file access (``file_access``); without it the list says
so instead of failing.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import flet as ft

from taskdeck.shared import files

from .common import icon_for

if TYPE_CHECKING:
    from .app import TaskDeckApp

FILE, FOLDER = "file", "folder"


class FileBrowser:
    def __init__(self, app: "TaskDeckApp", mode: str, title: str, start: Path | None,
                 on_done: Callable[..., None], new_folder_name: str | None = None, action_label: str = "Use this"):
        self.app = app
        self.mode = mode
        self.on_done = on_done
        self.locations = files.locations()
        self.folder = self._start_folder(start)
        self.show_all = ft.Switch(label="Show all files", value=False, on_change=lambda e: self.refresh())
        self.chips = ft.Row(wrap=True, spacing=8)
        self.crumbs = ft.Row(wrap=True, spacing=0)
        self.list = ft.ListView(expand=True, spacing=0)
        controls: list[ft.Control] = []

        self.new_folder = None
        self.into_new = None
        self.target = ft.Text(selectable=True, weight=ft.FontWeight.W_500)
        if mode == FOLDER:
            self.into_new = ft.Checkbox(label="Into a new folder named", value=new_folder_name is not None,
                                        on_change=lambda e: self._update_target())
            self.new_folder = ft.TextField(value=new_folder_name or "", dense=True, text_size=16,
                                           on_change=lambda e: self._update_target())
            self.name_note = ft.Text(size=13, color=ft.Colors.ON_SURFACE_VARIANT)
            controls += [self.into_new, self.new_folder, self.name_note]
        controls += [self.chips, self.crumbs]
        bottom: list[ft.Control] = []
        if mode == FILE:
            bottom = [self.show_all]
        else:
            self.use_button = ft.FilledButton(action_label, icon=ft.Icons.CHECK, on_click=self._use, height=48)
            bottom = [
                ft.Text("Will extract to:", size=13, color=ft.Colors.ON_SURFACE_VARIANT),
                self.target,
                ft.Row([ft.TextButton("Cancel", on_click=lambda e: self.app.back(), height=48), self.use_button],
                       alignment=ft.MainAxisAlignment.END),
            ]
        actions = []
        if mode == FOLDER:
            actions.append(ft.IconButton(ft.Icons.CREATE_NEW_FOLDER_OUTLINED, tooltip="New folder",
                                         on_click=lambda e: self._ask_new_folder()))
        body = ft.Column(
            [*controls, ft.Divider(height=1), self.list, ft.Divider(height=1), *bottom],
            expand=True, spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        self.view = ft.View(
            route=f"/browse/{mode}",
            appbar=ft.AppBar(title=ft.Text(title), actions=actions),
            padding=0,
            controls=[ft.SafeArea(ft.Container(body, padding=ft.Padding.only(left=16, top=8, right=16, bottom=8)),
                                  expand=True)],
        )
        self.refresh(update=False)

    # ---- opening ----------------------------------------------------------------------

    @classmethod
    def pick_file(cls, app: "TaskDeckApp", on_pick: Callable[[Path], None], start: Path | None = None) -> None:
        app.push(cls(app, FILE, "Choose an archive", start, on_pick))

    @classmethod
    def show(cls, app: "TaskDeckApp", folder: Path, on_pick: Callable[[Path], None]) -> None:
        """Look inside a folder (e.g. after extracting); tapping an archive opens it."""
        browser = cls(app, FILE, folder.name or "Files", folder, on_pick)
        browser.show_all.value = True
        browser.refresh(update=False)
        app.push(browser)

    @classmethod
    def pick_folder(cls, app: "TaskDeckApp", on_pick: Callable[[Path, str | None], None], start: Path | None,
                    new_folder_name: str | None) -> None:
        """on_pick(parent folder, new folder name or None)."""
        app.push(cls(app, FOLDER, "Extract to", start, on_pick, new_folder_name))

    def _start_folder(self, start: Path | None) -> Path:
        if start and start.is_dir():
            return start
        return self.locations[0].path if self.locations else Path.home()

    # ---- rendering ----------------------------------------------------------------------

    def refresh(self, update: bool = True) -> None:
        self.chips.controls = [
            ft.Chip(label=ft.Text(loc.label), leading=ft.Icon(icon_for(loc.icon), size=18),
                    selected=self._within(loc.path), on_click=lambda e, p=loc.path: self.go(p))
            for loc in self.locations
        ]
        self.crumbs.controls = self._breadcrumb()
        self.list.controls = self._items()
        if self.mode == FOLDER:
            self._update_target(update=False)
        if update:
            self.app.page.update()

    def _within(self, root: Path) -> bool:
        best = max((loc.path for loc in self.locations if self._is_under(self.folder, loc.path)),
                   key=lambda p: len(str(p)), default=None)
        return best == root

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _breadcrumb(self) -> list[ft.Control]:
        root = max((loc for loc in self.locations if self._is_under(self.folder, loc.path)),
                   key=lambda loc: len(str(loc.path)), default=None)
        if root is None:
            return [ft.Text(str(self.folder), size=13)]
        parts = [(root.label, root.path)]
        p = root.path
        for name in self.folder.relative_to(root.path).parts:
            p = p / name
            parts.append((name, p))
        out: list[ft.Control] = []
        for i, (label, path) in enumerate(parts):
            if i:
                out.append(ft.Text("›", color=ft.Colors.ON_SURFACE_VARIANT))
            last = i == len(parts) - 1
            out.append(ft.TextButton(label, disabled=last, on_click=None if last else lambda e, p=path: self.go(p),
                                     style=ft.ButtonStyle(padding=ft.Padding.symmetric(horizontal=6))))
        return out

    def _items(self) -> list[ft.Control]:
        try:
            items = files.list_folder(self.folder, archives_only=self.mode == FILE and not self.show_all.value)
        except PermissionError:
            return [self._message(ft.Icons.LOCK_OUTLINE, "TaskDeck can't see this folder.",
                                  "Allow file access when TaskDeck asks for it.")]
        except OSError as exc:
            return [self._message(ft.Icons.ERROR_OUTLINE, "Can't open this folder.", str(exc))]
        if self.mode == FOLDER:
            items = [i for i in items if i.is_dir]
        rows = [self._row(i) for i in items]
        if not rows:
            hint = "No archives here. Turn on “Show all files” to see everything." if self.mode == FILE else "No folders here."
            rows = [self._message(ft.Icons.FOLDER_OPEN, "Empty", hint)]
        return rows

    def _row(self, item: files.Item) -> ft.Control:
        if item.is_dir:
            return ft.ListTile(leading=ft.Icon(ft.Icons.FOLDER), title=ft.Text(item.name, max_lines=2),
                               trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT), on_click=lambda e: self.go(item.path))
        archive = files.is_archive(item.name)
        t = time.localtime(item.modified)
        when = f"{time.strftime('%b', t)} {t.tm_mday}" + ("" if t.tm_year == time.localtime().tm_year else f", {t.tm_year}")
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.FOLDER_ZIP_OUTLINED if archive else ft.Icons.INSERT_DRIVE_FILE_OUTLINED),
            title=ft.Text(item.name, max_lines=2),
            subtitle=ft.Text(f"{files.human_size(item.size)} · {when}"),
            disabled=not archive,
            on_click=(lambda e: self._pick(item.path)) if archive else None,
        )

    @staticmethod
    def _message(icon, title: str, hint: str) -> ft.Control:
        return ft.Container(ft.Column([ft.Icon(icon, size=36, color=ft.Colors.OUTLINE), ft.Text(title, weight=ft.FontWeight.W_600),
                                       ft.Text(hint, color=ft.Colors.ON_SURFACE_VARIANT, text_align=ft.TextAlign.CENTER)],
                                      horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                            padding=24, alignment=ft.Alignment.CENTER)

    # ---- actions ------------------------------------------------------------------------

    def go(self, folder: Path) -> None:
        self.folder = folder
        self.refresh()

    def _pick(self, path: Path) -> None:
        self.app.back()
        self.on_done(path)

    def _new_name(self) -> str | None:
        return self.new_folder.value.strip() if self.into_new.value else None

    def _update_target(self, update: bool = True) -> None:
        name = self._new_name()
        self.new_folder.disabled = not self.into_new.value
        problem = files.valid_folder_name(name) if name is not None else None
        note = ""
        if problem:
            note = problem
        elif name and (self.folder / name).exists():
            note = f"“{name}” already exists here: files go into it and replace any with the same name."
        self.name_note.value = note
        self.name_note.color = ft.Colors.ERROR if problem else ft.Colors.ON_SURFACE_VARIANT
        self.use_button.disabled = bool(problem)
        target = self.folder / name if name else self.folder
        self.target.value = files.display_path(target) + ("/" if name else "")
        if update:
            self.app.page.update()

    def _use(self, e=None) -> None:
        name = self._new_name()
        if name is not None and files.valid_folder_name(name):
            return
        self.app.back()
        self.on_done(self.folder, name)

    def _ask_new_folder(self) -> None:
        field = ft.TextField(label="Folder name", autofocus=True, text_size=16)
        error = ft.Text("", color=ft.Colors.ERROR, size=13)

        def create(e=None):
            name = (field.value or "").strip()
            problem = files.valid_folder_name(name)
            if not problem and (self.folder / name).exists():
                problem = "That folder already exists."
            if problem:
                error.value = problem
                self.app.page.update()
                return
            try:
                (self.folder / name).mkdir()
            except OSError as exc:
                error.value = f"Couldn't create it: {exc}"
                self.app.page.update()
                return
            self.app.page.pop_dialog()
            self.go(self.folder / name)

        field.on_submit = create
        self.app.page.show_dialog(ft.AlertDialog(
            title=ft.Text("New folder"), content=ft.Column([field, error], tight=True),
            actions=[ft.TextButton("Cancel", on_click=lambda e: self.app.page.pop_dialog()),
                     ft.FilledButton("Create", on_click=create)]))

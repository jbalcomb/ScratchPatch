"""Application shell: bottom navigation, Home (pinned + recent), All jobs,
History, Settings, the command palette and keyboard shortcuts."""

from __future__ import annotations

import time
from typing import Any

try:  # the fixed loader that ships in the APK (src/taskdeck_boot.py)
    import taskdeck_boot as boot
except ImportError:  # e.g. imported as a library without the app shell
    boot = None

import flet as ft

from taskdeck.core import (
    Command, CommandKind, CommandRegistry, Drafts, Favorites, History, JobPlugin, JsonStore,
    PluginRegistry, SecretBackend, Task, TaskManager, Vault, VaultError, default_data_dir,
)
from taskdeck.core.tasks import DONE
from taskdeck.core.trace import TraceStore
from taskdeck.shared import http

from .common import empty_state, plugin_icon, section_title
from .job_view import JobScreen
from .settings_view import JobSettingsScreen
from . import accounts
from .background import BackgroundKeeper
from .diagnostics import DiagnosticsSync
from .network import NetworkMonitor
from .palette import CommandPalette
from .vault_backend import make_backend

TABS = [
    ("home", "Home", ft.Icons.HOME_OUTLINED, ft.Icons.HOME),
    ("jobs", "All jobs", ft.Icons.APPS_OUTLINED, ft.Icons.APPS),
    ("history", "History", ft.Icons.HISTORY_OUTLINED, ft.Icons.HISTORY),
    ("settings", "Settings", ft.Icons.SETTINGS_OUTLINED, ft.Icons.SETTINGS),
]
THEMES = {"system": ft.ThemeMode.SYSTEM, "light": ft.ThemeMode.LIGHT, "dark": ft.ThemeMode.DARK}
TILE_WIDTH = 96
LOCK_AFTER = 5 * 60  # seconds in the background before credentials lock again
_AUTO = object()


class TaskDeckApp:
    def __init__(self, page: ft.Page, registry: PluginRegistry | None = None, store: JsonStore | None = None,
                 vault_backend: SecretBackend | None | object = _AUTO):
        self.page = page
        self.registry = registry or PluginRegistry().discover()
        self.store = store or JsonStore(default_data_dir() / "state.json")
        secret_fields: dict[str, set[str]] = {}
        for p in self.registry:
            secret_fields.setdefault(p.settings_id, set()).update(f.name for f in p.settings if f.secret)
        secret_fields.update(accounts.SECRET_FIELDS)  # connected accounts' secrets are credentials too
        self.vault = Vault(make_backend(page) if vault_backend is _AUTO else vault_backend, self.store, secret_fields)
        self._hidden_at: float | None = None
        self.favorites = Favorites(self.store)
        self.history = History(self.store)
        self.drafts = Drafts(self.store)
        run_thread = getattr(page, "run_thread", None)
        traces = TraceStore(self.store.path.parent / "diagnostics")  # one trace file per task, for analysis
        self.tasks = TaskManager(start_thread=run_thread, traces=traces,
                                 around=lambda task: http.scope(trace=task.trace))  # every request is recorded
        self.tasks.listeners.append(self.on_task)
        self.background = BackgroundKeeper(self)  # foreground service while tasks run (APK with the extension)
        self.tasks.listeners.append(self.background.on_task)
        self.diagnostics = DiagnosticsSync(self, traces)  # traces are copied to Google Drive as they grow
        self.tasks.listeners.append(self.diagnostics.on_task)
        self.network = NetworkMonitor(self)  # Wi-Fi or mobile data, for the jobs' Wi-Fi-only setting
        self.google = accounts.GoogleAccount(self)
        self._housekeeping()
        self.favorites.prune({p.id for p in self.registry})
        if not self.store.get("initialized"):
            for pid in ("unit_convert", "text_stats", "json_format"):  # sensible first-run pins
                if self.registry.get(pid):
                    self.favorites.pin(pid)
            self.store.set("initialized", True)

        self.commands = CommandRegistry()
        self.palette = CommandPalette(self)
        self.tab = "home"
        self.stack: list[JobScreen | JobSettingsScreen] = []  # pushed views above the root

        page.title = "TaskDeck"
        page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
        page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
        page.theme_mode = THEMES.get(self.store.get("theme", "system"), ft.ThemeMode.SYSTEM)
        page.on_keyboard_event = self.on_key
        page.on_view_pop = self.on_view_pop
        page.on_app_lifecycle_state_change = self.on_lifecycle
        if getattr(page, "run_task", None):
            page.run_task(self.network.start)
        self.diagnostics.sync_soon()  # anything a closed or killed app didn't get to upload

        self.body = ft.Container(expand=True, padding=ft.Padding.symmetric(horizontal=16))
        self.nav = ft.NavigationBar(
            destinations=[ft.NavigationBarDestination(icon=i, selected_icon=s, label=l) for _, l, i, s in TABS],
            on_change=lambda e: self.go(TABS[e.control.selected_index][0]),
        )
        self.root = ft.View(
            route="/",
            controls=[ft.SafeArea(self.body, expand=True)],
            navigation_bar=self.nav,
            padding=0,
        )
        page.views.clear()
        page.views.append(self.root)
        self.register_commands()
        self.render()
        self.update_status = ""
        if boot:
            boot.listeners.clear()  # drop listeners from a previous (reloaded) app instance
            boot.listeners.append(self.on_update_ready)

    # ---- navigation ---------------------------------------------------------------

    def go(self, tab: str) -> None:
        while len(self.page.views) > 1:  # leave any open job or settings screen
            self.page.views.pop()
            self.stack.pop()
        self.tab = tab
        self.nav.selected_index = [t[0] for t in TABS].index(tab)
        self.render()
        self.page.update()

    def open_job(self, job_id: str, preset: dict[str, Any] | None = None) -> None:
        plugin = self.registry.get(job_id)
        if plugin is None:
            self.toast(f"Job {job_id!r} is not installed")
            return
        make_screen = getattr(plugin, "make_screen", None)
        screen = make_screen(self) if make_screen else JobScreen(self, plugin, preset)
        self.push(screen)
        screen.check_ready()
        self.page.update()
        if self.vault.needs_unlock(plugin.settings_id):
            self.page.run_task(self.unlock, screen.recheck)

    def open_settings(self, job_id: str) -> None:
        plugin = self.registry.get(job_id)
        if not (plugin and plugin.settings):
            return
        if self.vault.needs_unlock(plugin.settings_id):  # never show (or save over) locked secrets
            self.page.run_task(self.unlock, lambda: self.push(JobSettingsScreen(self, plugin)))
        else:
            self.push(JobSettingsScreen(self, plugin))

    def push(self, screen) -> None:
        self.stack.append(screen)
        self.page.views.append(screen.view)
        self.page.update()

    def on_view_pop(self, e) -> None:
        if len(self.page.views) > 1:
            self.page.views.pop()
            left = self.stack.pop()
            if isinstance(left, JobSettingsScreen) and self.top_job_screen:
                self.top_job_screen.check_ready()  # settings may have fixed (or broken) something
            self.render()
            self.page.update()

    def back(self) -> None:
        if self.palette.open:
            self.palette.close()
        elif len(self.page.views) > 1:
            self.on_view_pop(None)

    @property
    def job_screens(self) -> list[JobScreen]:
        return [s for s in self.stack if isinstance(s, JobScreen)]

    @property
    def top_job_screen(self) -> JobScreen | None:
        return self.stack[-1] if self.stack and isinstance(self.stack[-1], JobScreen) else None

    @property
    def current_job(self) -> JobPlugin | None:
        return self.stack[-1].plugin if self.stack else None

    # ---- job settings -------------------------------------------------------------

    def _settings_id(self, job_id: str) -> str:
        plugin = self.registry.get(job_id)
        return plugin.settings_id if plugin else job_id

    def job_settings(self, job_id: str) -> dict[str, Any]:
        """Stored settings plus, when unlocked, the job's credentials from the vault."""
        sid = self._settings_id(job_id)
        values = dict(self.store.get(f"settings:{sid}", {}))
        if self.vault.protects(sid):
            for name in self.vault.secret_fields[sid]:
                values.pop(name, None)  # a copy from before the vault is moved into it on unlock
            values.update(self.vault.secrets_for(sid))
        return values

    def save_job_settings(self, job_id: str, values: dict[str, Any]) -> None:
        sid = self._settings_id(job_id)
        plain, secret = self.vault.split(sid, values)
        self.store.set(f"settings:{sid}", plain)
        ops = self.vault.stage(sid, secret)
        if ops:
            self.page.run_task(self._persist_secrets, ops)

    async def _persist_secrets(self, ops) -> None:
        try:
            await self.vault.persist(ops)
        except Exception as exc:
            self.toast(f"Couldn't save your credentials securely: {exc}")

    # ---- credentials vault ------------------------------------------------------------

    async def unlock(self, then=None) -> bool:
        """Ask for the fingerprint; on success run ``then`` (e.g. re-check the open job)."""
        try:
            await self.vault.unlock()
        except VaultError as exc:
            self.toast(str(exc))
            return False
        if self.vault.lost:
            self.vault.lost = []
            self.toast("Android erased your saved credentials (the phone's fingerprints changed). "
                       "Enter them again in the job's settings.")
        if then:
            then()
        self.render()
        self.page.update()
        return True

    def lock_credentials(self) -> None:
        if not self.vault.available or not self.vault.unlocked:
            return
        self.vault.lock()
        while self.stack and isinstance(self.stack[-1], JobSettingsScreen):  # don't leave secrets on screen
            self.page.views.pop()
            self.stack.pop()
        top = self.top_job_screen
        if top:
            top.check_ready()
        self.render()
        self.page.update()
        self.toast("Credentials locked")

    @property
    def hidden(self) -> bool:
        """TaskDeck isn't on screen (another app, or the phone is locked)."""
        return self._hidden_at is not None

    def on_lifecycle(self, e) -> None:
        self.tasks.log_running("app", state=getattr(e.state, "value", str(e.state)))
        if e.state in (ft.AppLifecycleState.HIDE, ft.AppLifecycleState.PAUSE):
            self._hidden_at = self._hidden_at or time.time()
        elif e.state in (ft.AppLifecycleState.SHOW, ft.AppLifecycleState.RESUME):
            away = time.time() - self._hidden_at if self._hidden_at else 0
            self._hidden_at = None
            if away > LOCK_AFTER and self.vault.available and self.vault.unlocked:
                self.lock_credentials()
                top = self.top_job_screen
                if top and self.vault.needs_unlock(top.plugin.settings_id):
                    self.page.run_task(self.unlock, top.recheck)

    def credentials_section(self) -> list[ft.Control]:
        if not any(f.secret for p in self.registry for f in p.settings):
            return []
        v = self.vault
        switch = ft.Switch(label="Fingerprint lock", value=v.available, disabled=not v.supported,
                           on_change=lambda e: self.page.run_task(self.set_fingerprint_lock, e.control.value))
        if not v.supported:
            note = "Install the latest APK to protect your API keys and Google key with your fingerprint."
        elif not v.available:
            note = "Off. API keys and the Google key are kept in the app's own settings file."
        else:
            note = (f"{'Unlocked' if v.unlocked else 'Locked'}. API keys and the Google key are encrypted by Android "
                    f"and need your fingerprint (or phone PIN) after each app start, and after "
                    f"{LOCK_AFTER // 60} minutes away.")
        controls = [section_title("Credentials"), switch, ft.Text(note, color=ft.Colors.ON_SURFACE_VARIANT)]
        if v.available:
            controls.append(ft.Row([
                ft.OutlinedButton("Lock now", icon=ft.Icons.LOCK, on_click=lambda e: self.lock_credentials())
                if v.unlocked else
                ft.FilledTonalButton("Unlock", icon=ft.Icons.FINGERPRINT, on_click=lambda e: self.page.run_task(self.unlock))]))
        return controls

    async def set_fingerprint_lock(self, on: bool) -> None:
        try:
            await (self.vault.enable() if on else self.vault.disable())
        except VaultError as exc:
            self.toast(str(exc))
        else:
            self.toast("Fingerprint lock on" if on else "Fingerprint lock off")
        top = self.top_job_screen
        if top:
            top.check_ready()
        self.render()  # the switch shows what actually happened
        self.page.update()

    # ---- rendering ----------------------------------------------------------------

    def render(self) -> None:
        builder = {"home": self.home, "jobs": self.all_jobs, "history": self.history_tab, "settings": self.settings_tab}
        self.body.content = builder[self.tab]()

    def search_pill(self) -> ft.Control:
        """Looks like a search field; opens the palette. The primary way to get anywhere."""
        return ft.Container(
            ft.Row(
                [
                    ft.Icon(ft.Icons.SEARCH, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text("Search jobs and actions", color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                    ft.Text("Ctrl K", size=11, color=ft.Colors.OUTLINE),
                ],
                spacing=12,
            ),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            border_radius=28,
            padding=ft.Padding.symmetric(horizontal=16, vertical=14),
            on_click=lambda e: self.palette.show(),
            ink=True,
        )

    def home(self) -> ft.Control:
        pinned = [p for p in (self.registry.get(i) for i in self.favorites.ids) if p]
        tiles: ft.Control
        if pinned:
            tiles = ft.Row([self.tile(p) for p in pinned], wrap=True, spacing=8, run_spacing=8)
        else:
            tiles = empty_state(ft.Icons.PUSH_PIN_OUTLINED, "Nothing pinned yet",
                                "Open any job and tap the pin, or long-press a job in All jobs.")
        recent_ids = [i for i in self.history.recent_job_ids(5) if self.registry.get(i)]
        recent = [self.job_tile(self.registry.get(i), subtitle=self._last_run_text(i)) for i in recent_ids]
        return ft.Column(
            [
                ft.Container(height=8),
                self.search_pill(),
                *self.running_strip(),
                section_title("Pinned", ft.TextButton("All jobs  ›", on_click=lambda e: self.go("jobs"))),
                tiles,
                section_title("Recent"),
                *(recent or [ft.Text("Jobs you run show up here.", color=ft.Colors.ON_SURFACE_VARIANT)]),
            ],
            scroll=ft.ScrollMode.AUTO,
            spacing=14,
            expand=True,
        )

    def running_strip(self) -> list[ft.Control]:
        """One line per background task while it runs; tap to open its job."""
        out = []
        for t in self.tasks.running():
            pct = f" · {t.fraction * 100:.0f}%" if t.fraction is not None else ""
            out.append(ft.Container(
                ft.Row([ft.ProgressRing(width=18, height=18, stroke_width=2, value=t.fraction),
                        ft.Text(f"{t.title}{pct}", expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Icon(ft.Icons.CHEVRON_RIGHT)]),
                bgcolor=ft.Colors.SECONDARY_CONTAINER, border_radius=12, padding=12,
                on_click=lambda e, j=t.job_id: self.open_job(j)))
        return out

    def on_task(self, task: Task) -> None:
        """Background task changed (worker thread): refresh Home, announce the end."""
        if not self.stack and self.tab == "home":
            self.render()
        if not task.running:
            top = self.stack[-1] if self.stack else None
            if getattr(getattr(top, "plugin", None), "id", None) != task.job_id:  # its screen isn't showing it
                self.toast(task.message if task.status == DONE else f"{task.title}: {task.message}")
        try:
            self.page.update()
        except Exception:
            pass

    def _housekeeping(self) -> None:
        """One-time clean-ups of things older versions left behind (in the background)."""
        import shutil
        import threading

        for leftover in (self.store.path.parent / "extract-test",  # the old 7-Zip engine check's test extractions
                         self.temp_dir):  # scratch space from an interrupted run
            if leftover.exists():
                threading.Thread(target=shutil.rmtree, args=(leftover,), kwargs={"ignore_errors": True},
                                 daemon=True).start()

    def tile(self, plugin: JobPlugin) -> ft.Control:
        """A Windows-11-style pinned tile: icon over label; long-press for options."""
        return ft.Container(
            ft.Column(
                [
                    ft.Icon(plugin_icon(plugin), size=30, color=ft.Colors.PRIMARY),
                    ft.Text(plugin.title, size=12, text_align=ft.TextAlign.CENTER, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=6,
            ),
            width=TILE_WIDTH,
            height=TILE_WIDTH,
            padding=8,
            border_radius=16,
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            ink=True,
            tooltip=plugin.description,
            on_click=lambda e, pid=plugin.id: self.open_job(pid),
            on_long_press=lambda e, pid=plugin.id: self.tile_menu(pid),
        )

    def tile_menu(self, job_id: str) -> None:
        plugin = self.registry.get(job_id)
        pinned = self.favorites.is_pinned(job_id)
        idx = self.favorites.ids.index(job_id) if pinned else -1

        sheet = ft.BottomSheet(ft.Container(), show_drag_handle=True)

        def close_sheet():
            sheet.open = False
            sheet.update()

        def act(fn):
            def handler(e):
                close_sheet()
                fn()
                self.on_favorites_changed()
            return handler

        def open_it(e):
            close_sheet()
            self.open_job(job_id)

        items = [ft.ListTile(leading=ft.Icon(ft.Icons.OPEN_IN_NEW), title=ft.Text("Open"), on_click=open_it)]
        if pinned:
            if idx > 0:
                items.append(ft.ListTile(leading=ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_LEFT), title=ft.Text("Move to front"),
                                         on_click=act(lambda: self.favorites.move(job_id, 0))))
                items.append(ft.ListTile(leading=ft.Icon(ft.Icons.ARROW_BACK), title=ft.Text("Move earlier"),
                                         on_click=act(lambda: self.favorites.shift(job_id, -1))))
            if idx < len(self.favorites.ids) - 1:
                items.append(ft.ListTile(leading=ft.Icon(ft.Icons.ARROW_FORWARD), title=ft.Text("Move later"),
                                         on_click=act(lambda: self.favorites.shift(job_id, 1))))
            items.append(ft.ListTile(leading=ft.Icon(ft.Icons.PUSH_PIN_OUTLINED), title=ft.Text("Unpin"),
                                     on_click=act(lambda: self.favorites.unpin(job_id))))
        else:
            items.append(ft.ListTile(leading=ft.Icon(ft.Icons.PUSH_PIN), title=ft.Text("Pin to Home"),
                                     on_click=act(lambda: self.favorites.pin(job_id))))
        sheet.content = ft.Container(
            ft.Column([ft.Text(plugin.title, weight=ft.FontWeight.W_600), *items], tight=True), padding=16
        )
        self.page.show_dialog(sheet)

    def job_tile(self, plugin: JobPlugin, subtitle: str | None = None) -> ft.Control:
        pinned = self.favorites.is_pinned(plugin.id)
        return ft.ListTile(
            leading=ft.Icon(plugin_icon(plugin)),
            title=ft.Text(plugin.title),
            subtitle=ft.Text(subtitle or plugin.description, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            trailing=ft.IconButton(
                icon=ft.Icons.PUSH_PIN if pinned else ft.Icons.PUSH_PIN_OUTLINED,
                icon_color=ft.Colors.PRIMARY if pinned else ft.Colors.OUTLINE,
                tooltip="Unpin" if pinned else "Pin to Home",
                on_click=lambda e, pid=plugin.id: self.toggle_pin(pid),
            ),
            shape=ft.RoundedRectangleBorder(radius=12),
            on_click=lambda e, pid=plugin.id: self.open_job(pid),
            on_long_press=lambda e, pid=plugin.id: self.tile_menu(pid),
        )

    def all_jobs(self) -> ft.Control:
        sections: list[ft.Control] = [ft.Container(height=8), self.search_pill()]
        for category, plugins in self.registry.by_category().items():
            sections.append(section_title(category))
            sections.extend(self.job_tile(p) for p in plugins)
        if self.registry.errors:
            sections.append(section_title("Failed to load"))
            sections.extend(ft.ListTile(leading=ft.Icon(ft.Icons.ERROR_OUTLINE, color=ft.Colors.ERROR),
                                        title=ft.Text(n), subtitle=ft.Text(err)) for n, err in self.registry.errors.items())
        return ft.Column(sections, scroll=ft.ScrollMode.AUTO, spacing=6, expand=True)

    def history_tab(self) -> ft.Control:
        entries = self.history.entries()
        header = section_title("History", ft.TextButton("Clear", icon=ft.Icons.DELETE_SWEEP_OUTLINED,
                                                        on_click=lambda e: self.clear_history(), disabled=not entries))
        if not entries:
            return ft.Column([ft.Container(height=8), self.search_pill(), header,
                              empty_state(ft.Icons.HISTORY, "No runs yet", "Every job you run is listed here so you can re-run it.")])
        rows = []
        for entry in entries:
            plugin = self.registry.get(entry.job_id)
            if not plugin:
                continue
            rows.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE if entry.ok else ft.Icons.ERROR_OUTLINE,
                                color=ft.Colors.GREEN if entry.ok else ft.Colors.ERROR),
                title=ft.Text(plugin.title),
                subtitle=ft.Text(f"{_ago(entry.at)} · {entry.summary}", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                trailing=ft.Icon(ft.Icons.REPLAY, size=18),
                tooltip="Open with these inputs",
                on_click=lambda e, en=entry: self.open_job(en.job_id, en.inputs),
            ))
        return ft.Column([ft.Container(height=8), self.search_pill(), header, *rows], scroll=ft.ScrollMode.AUTO,
                         spacing=2, expand=True)

    def settings_tab(self) -> ft.Control:
        theme = ft.SegmentedButton(
            segments=[ft.Segment(value=k, label=ft.Text(k.title())) for k in THEMES],
            selected=[self.store.get("theme", "system")],
            on_change=lambda e: self.set_theme(list(e.control.selected)[0]),
        )
        configurable = [p for p in self.registry if p.settings]
        shortcuts = [("Ctrl+K / Ctrl+Shift+P", "Command palette"), ("Ctrl+1…4", "Switch tab"),
                     ("Ctrl+D", "Pin / unpin the open job"), ("Esc", "Close palette or go back"),
                     ("> / @ in palette", "Only actions / only jobs")]
        return ft.Column(
            [
                ft.Container(height=8),
                self.search_pill(),
                section_title("Appearance"),
                theme,
                *([section_title("Job settings")] if configurable else []),
                *[ft.ListTile(leading=ft.Icon(plugin_icon(p)), title=ft.Text(p.title), trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT),
                              on_click=lambda e, pid=p.id: self.open_settings(pid)) for p in configurable],
                section_title("Accounts"),
                *self.google.settings_rows(),
                *self.credentials_section(),
                *([section_title("Background work"), *self.background.settings_rows()] if self.background.enabled else []),
                section_title("Keyboard shortcuts"),
                *[ft.Row([ft.Text(k, width=170, font_family="monospace", size=12), ft.Text(v)]) for k, v in shortcuts],
                *self.updates_section(),
                section_title("About"),
                ft.Text(f"{len(self.registry)} jobs installed · {len(self.commands)} palette commands"),
            ],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    # ---- live code updates -------------------------------------------------------------

    def updates_section(self) -> list[ft.Control]:
        if not boot:
            return []
        st = boot.status()
        running = st["active"]
        ready = st["staged"] and st["staged"] != running["version"]
        lines = [f"Running: {running['version']} ({running['source']})"]
        if ready:
            lines.append(f"Downloaded: {st['staged']}, reload to use it")
        if st.get("last_error"):
            lines.append(f"Last rejected update: {st['last_error'].splitlines()[0][:120]}")
        if self.update_status:
            lines.append(self.update_status)
        buttons = [ft.OutlinedButton("Check for updates", icon=ft.Icons.SYSTEM_UPDATE, on_click=lambda e: self.check_updates())]
        if ready:
            buttons.append(ft.FilledButton("Reload now", icon=ft.Icons.REFRESH, on_click=lambda e: self.reload_app()))
        return [
            section_title("App updates"),
            ft.Text("\n".join(lines), color=ft.Colors.ON_SURFACE_VARIANT, selectable=True),
            ft.Row(buttons, wrap=True),
        ]

    def check_updates(self) -> None:
        self.update_status = "Checking…"
        self.render()
        self.page.update()

        def work():
            result = boot.check_for_update()
            self.update_status = result["message"]
            self.render()
            self.page.update()

        self.page.run_thread(work)

    def on_update_ready(self, result: dict) -> None:
        """Called from the updater's background thread when a new version is downloaded."""
        self.update_status = result["message"]
        self.render()
        self.page.show_dialog(ft.SnackBar(
            ft.Text(f"Update {result['version']} downloaded"),
            action="Reload",
            on_action=lambda e: self.reload_app(),
            duration=ft.Duration(seconds=10),
        ))

    def reload_app(self) -> None:
        boot.reload(self.page)

    # ---- state changes ------------------------------------------------------------

    def toggle_pin(self, job_id: str) -> None:
        pinned = self.favorites.toggle(job_id)
        self.on_favorites_changed()
        self.toast("Pinned to Home" if pinned else "Unpinned")

    def on_favorites_changed(self) -> None:
        for screen in self.job_screens:
            screen.pin_button.selected = self.favorites.is_pinned(screen.plugin.id)
        self.render()
        self.page.update()

    def on_history_changed(self) -> None:
        self.render()

    def clear_history(self) -> None:
        self.history.clear()
        self.render()
        self.page.update()

    def set_theme(self, key: str) -> None:
        self.store.set("theme", key)
        self.page.theme_mode = THEMES[key]
        self.page.update()

    def toast(self, message: str) -> None:
        self.page.show_dialog(ft.SnackBar(ft.Text(message), duration=ft.Duration(milliseconds=1800)))

    async def copy_async(self, text: str) -> None:
        await ft.Clipboard().set(text)
        self.toast("Copied")

    def copy(self, text: str) -> None:
        self.page.run_task(self.copy_async, text)

    def open_url(self, url: str) -> None:
        self.page.run_task(ft.UrlLauncher().launch_url, url)

    @property
    def temp_dir(self):
        """Scratch space inside TaskDeck's storage (e.g. unpacking before an upload); emptied at start."""
        return self.store.path.parent / "tmp"

    # ---- commands -----------------------------------------------------------------

    def command_usage(self) -> dict[str, int]:
        usage = dict(self.store.get("command_usage", {}))
        for job_id, n in self.history.usage_counts().items():
            usage[f"job:{job_id}"] = usage.get(f"job:{job_id}", 0) + n
        return usage

    def record_command(self, command_id: str) -> None:
        usage = dict(self.store.get("command_usage", {}))
        usage[command_id] = usage.get(command_id, 0) + 1
        self.store.set("command_usage", usage)

    def register_commands(self) -> None:
        c = self.commands
        for p in self.registry:
            c.add(Command(f"job:{p.id}", p.title, lambda pid=p.id: self.open_job(pid), CommandKind.JOB,
                          subtitle=f"{p.category} · {p.description}", icon=p.icon, keywords=p.tags))
            if p.settings:
                c.add(Command(f"settings:{p.id}", f"Settings: {p.title}", lambda pid=p.id: self.open_settings(pid),
                              icon="TUNE", subtitle="Job settings", keywords=("configure", *p.tags)))
            for qa in p.quick_actions:
                c.add(Command(f"quick:{p.id}:{qa.title}", qa.title,
                              lambda pid=p.id, inputs=qa.inputs: self.open_job(pid, inputs),
                              CommandKind.QUICK, subtitle=p.title, icon=p.icon, keywords=p.tags))
        nav = [("home", "Go to Home", "HOME", "Ctrl+1"), ("jobs", "Go to All jobs", "APPS", "Ctrl+2"),
               ("history", "Go to History", "HISTORY", "Ctrl+3"), ("settings", "Go to Settings", "SETTINGS", "Ctrl+4")]
        for tab, title, icon, key in nav:
            c.add(Command(f"nav:{tab}", title, lambda t=tab: self.go(t), icon=icon, shortcut=key, keywords=("navigate", "open")))
        c.add(Command("job:pin", "Pin current job to Home", lambda: self.toggle_pin(self.current_job.id), icon="PUSH_PIN",
                      shortcut="Ctrl+D", keywords=("favorite", "star"),
                      when=lambda: bool(self.current_job) and not self.favorites.is_pinned(self.current_job.id)))
        c.add(Command("job:unpin", "Unpin current job", lambda: self.toggle_pin(self.current_job.id), icon="PUSH_PIN_OUTLINED",
                      shortcut="Ctrl+D", keywords=("favorite", "remove"),
                      when=lambda: bool(self.current_job) and self.favorites.is_pinned(self.current_job.id)))
        c.add(Command("job:run", "Run current job", lambda: self.page.run_task(self.top_job_screen.run),
                      icon="PLAY_ARROW", keywords=("execute", "start"), when=lambda: bool(self.top_job_screen)))
        c.add(Command("job:settings", "Settings for current job", lambda: self.open_settings(self.current_job.id),
                      icon="TUNE", keywords=("configure", "options"),
                      when=lambda: bool(self.top_job_screen) and bool(self.current_job.settings)))
        c.add(Command("job:back", "Close current job", self.back, icon="ARROW_BACK", shortcut="Esc",
                      when=lambda: bool(self.stack)))
        for key in THEMES:
            c.add(Command(f"theme:{key}", f"Theme: {key.title()}", lambda k=key: self.set_theme(k),
                          icon="DARK_MODE" if key == "dark" else "LIGHT_MODE" if key == "light" else "BRIGHTNESS_AUTO",
                          keywords=("appearance", "color", "mode")))
        if boot:
            c.add(Command("app:update", "Check for app updates", self.check_updates, icon="SYSTEM_UPDATE",
                          keywords=("upgrade", "version", "ota", "reload")))
        c.add(Command("history:clear", "Clear run history", self.clear_history, icon="DELETE_SWEEP", keywords=("reset",)))
        c.add(Command("vault:lock", "Lock credentials", self.lock_credentials, icon="LOCK",
                      keywords=("fingerprint", "security", "keys", "secrets"),
                      when=lambda: self.vault.available and self.vault.unlocked))
        c.add(Command("vault:unlock", "Unlock credentials", lambda: self.page.run_task(self.unlock), icon="FINGERPRINT",
                      keywords=("fingerprint", "security", "keys", "secrets"),
                      when=lambda: self.vault.available and not self.vault.unlocked))

    # ---- keyboard -----------------------------------------------------------------

    def on_key(self, e: ft.KeyboardEvent) -> None:
        key = e.key
        if self.palette.open:
            if key == "Arrow Down":
                self.palette.move(1)
            elif key == "Arrow Up":
                self.palette.move(-1)
            return  # Escape is handled by the dialog itself (fires on_dismiss)
        if (e.ctrl or e.meta) and (key == "K" or (e.shift and key == "P")):
            self.palette.show()
        elif (e.ctrl or e.meta) and key in "1234" and len(key) == 1:
            self.go(TABS[int(key) - 1][0])
        elif (e.ctrl or e.meta) and key == "D" and self.current_job:
            self.toggle_pin(self.current_job.id)
        elif key == "Escape":
            self.back()

    def _last_run_text(self, job_id: str) -> str:
        for entry in self.history.entries():
            if entry.job_id == job_id:
                return f"{_ago(entry.at)} · {entry.summary}"
        return ""


def _ago(ts: float) -> str:
    secs = max(0, time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


def main(page: ft.Page) -> None:
    TaskDeckApp(page)

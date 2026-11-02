"""The Voice notes screen, the recorder, and the background sync that outlives the screen."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

import flet as ft

from taskdeck.shared.files import human_size, is_android
from taskdeck.ui.common import empty_state, plugin_icon, screen_view, section_title

from .client import Client
from .library import (
    ARCHIVE, ARCHIVED, ARCHIVING, QUEUED, RECORDED, STAGE_LABEL, STAGES, TRANSCRIBE, TRANSCRIBED, Library,
    Recording, clock, local_time, now_iso,
)
from .sync import SyncResult, recover_orphans, sync_once

if TYPE_CHECKING:
    from taskdeck.ui.app import TaskDeckApp

log = logging.getLogger(__name__)

POLL_DELAYS = (5, 10, 20, 30, 60, 120)  # seconds between checks while the workstation is busy with a note
BREADCRUMB = "voice_rec:native_step"
REOPEN_WITHIN = 300  # seconds: after an unexpected close this soon, the app opens Voice notes again
MIC_JUST_ALLOWED = "Microphone allowed. Tap the button again to record."
MIC_BLOCKED = ("The microphone is blocked for TaskDeck. Open TaskDeck's app info → Permissions → Microphone → "
               "“Allow only while using the app”.")
NEEDS_APK = "Recording needs the newest TaskDeck APK (the microphone plugin is built into the app). " \
            "Install it from Settings → App updates, or the taskdeck-latest release."
STATUS_TEXT = {
    RECORDED: "Waiting to send",
    QUEUED: "Transcribing on the workstation…",
    TRANSCRIBED: "Transcribed · waiting to archive",
    ARCHIVING: "Archiving on the workstation…",
    ARCHIVED: "Archived",
}


def recorder_supported() -> bool:
    """False on an APK built before the microphone plugin was added (a live update can't add it)."""
    return importlib.util.find_spec("flet_audio_recorder") is not None


# ---- recorder ----------------------------------------------------------------------------------


class Recorder:
    """Wraps flet-audio-recorder. Kept by VoiceSync, so a recording carries on when the screen closes."""

    def __init__(self, lib: Library, store: Any = None):
        self.lib = lib
        self.store = store  # app.store: breadcrumbs survive a crash (see mark / last_crash)
        self.service: Any = None
        self.rec_id = ""
        self.path = None
        self.started_at = ""  # ISO time the recording started
        self._t0 = 0.0
        self._paused_total = 0.0
        self._paused_at: float | None = None

    @property
    def active(self) -> bool:
        return bool(self.rec_id)

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def elapsed(self) -> float:
        if not self.active:
            return 0.0
        end = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0.0, end - self._t0 - self._paused_total)

    def _ensure(self) -> Any:
        if self.service is None:
            import flet_audio_recorder as far

            config = far.AudioRecorderConfiguration(
                encoder=far.AudioEncoder.AACLC,  # .m4a, about 0.35 MB a minute at these settings
                channels=1, sample_rate=16000, bit_rate=48000,  # speech; Whisper works at 16 kHz mono anyway
                auto_gain=True,
            )
            self.service = far.AudioRecorder(configuration=config)
        return self.service

    # ---- breadcrumbs: which native step was running if the app closed unexpectedly ----

    def mark(self, step: str) -> None:
        """Note the step about to run ("" = finished). JsonStore writes atomically, so it survives a crash."""
        if self.store is not None:
            self.store.set(BREADCRUMB, {"step": step, "at": time.time()} if step else {})

    def last_crash(self) -> dict | None:
        """The step that was running when TaskDeck last closed unexpectedly (and forget it)."""
        if self.store is None:
            return None
        crumb = self.store.get(BREADCRUMB) or {}
        if crumb.get("step") and not self.active:
            self.store.set(BREADCRUMB, {})
            return crumb
        return None

    async def mic_permission(self) -> str:
        """"" when allowed; otherwise what to tell the user. Asks through flet-permission-handler (the plugin
        TaskDeck already uses for notifications and file access), not the recorder's own permission call:
        the app closed when the recorder plugin asked for it."""
        if not is_android():
            return ""
        try:
            from flet_permission_handler import Permission, PermissionHandler, PermissionStatus
        except ImportError:
            return NEEDS_APK
        handler = PermissionHandler()
        if await handler.get_status(Permission.MICROPHONE) == PermissionStatus.GRANTED:
            return ""
        self.mark("asking for microphone permission")
        status = await handler.request(Permission.MICROPHONE)
        self.mark("")
        if status == PermissionStatus.GRANTED:
            # Don't start in the same tap: let Android finish returning to TaskDeck first.
            return MIC_JUST_ALLOWED
        if status == PermissionStatus.PERMANENTLY_DENIED:
            return MIC_BLOCKED
        return "TaskDeck needs the microphone to record. Tap the button again and choose “While using the app”."

    async def start(self) -> str:
        """Start recording; returns "" or a problem to show."""
        if not recorder_supported():
            return NEEDS_APK
        problem = await self.mic_permission()
        if problem:
            return problem
        self.mark("setting up the recorder")
        svc = self._ensure()
        rec_id, path = self.lib.new_path()
        self.mark("starting the recording")
        ok = await svc.start_recording(output_path=str(path))
        self.mark("")
        if not ok:
            return "The microphone didn't start (another app may be using it)."
        self.rec_id, self.path, self.started_at = rec_id, path, now_iso()
        self._t0, self._paused_total, self._paused_at = time.monotonic(), 0.0, None
        return ""

    async def pause(self) -> None:
        if self.active and not self.paused:
            await self.service.pause_recording()
            self._paused_at = time.monotonic()

    async def resume(self) -> None:
        if self.active and self.paused:
            await self.service.resume_recording()
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None

    async def stop(self, mobile_data: dict[str, bool]) -> Recording | None:
        if not self.active:
            return None
        duration = self.elapsed
        self.mark("stopping the recording")
        saved = await self.service.stop_recording()
        self.mark("")
        path = self.path
        if saved and not str(saved).startswith("blob:"):
            from pathlib import Path

            path = Path(saved)
        rec = None
        if path and path.exists() and path.stat().st_size > 0:
            if path.parent != self.lib.audio_dir:  # some platforms ignore output_path
                target = self.lib.audio_dir / f"{self.rec_id}{path.suffix or '.m4a'}"
                path.replace(target)
                path = target
            rec = self.lib.add(self.rec_id, path, self.started_at, duration, mobile_data)
        self._reset()
        return rec

    async def discard(self) -> None:
        if not self.active:
            return
        try:
            await self.service.cancel_recording()
        finally:
            if self.path:
                self.path.unlink(missing_ok=True)
            self._reset()

    def _reset(self) -> None:
        self.rec_id, self.path, self.started_at = "", None, ""
        self._paused_at = None


# ---- background sync ---------------------------------------------------------------------------


class VoiceSync:
    """One per app. Runs the sync on its own thread when asked (kick), and again while the workstation
    is busy with a note. Screens subscribe through ``listeners``."""

    def __init__(self, app: "TaskDeckApp", plugin):
        self.app = app
        self.plugin = plugin
        self.lib = Library(app.store.path.parent / "voice_rec")
        self.recorder = Recorder(self.lib, app.store)
        self.listeners: list[Callable[[], None]] = []
        self.last: SyncResult | None = None
        self.last_at = 0.0
        self.syncing = False
        self._lock = threading.Lock()
        self._again = False
        self._archive_now: set[str] = set()
        self._timer: threading.Timer | None = None
        self._busy_polls = 0
        self._recovered = False

    @classmethod
    def for_app(cls, app: "TaskDeckApp", plugin) -> "VoiceSync":
        sync = getattr(app, "_voice_sync", None)
        if sync is None:
            sync = cls(app, plugin)
            app._voice_sync = sync
            listeners = getattr(app.network, "listeners", None)
            if listeners is not None:
                listeners.append(sync.on_network)
        return sync

    # ---- settings ----------------------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        return self.plugin.resolve_settings(self.app.job_settings(self.plugin.id))

    def default_mobile_data(self) -> dict[str, bool]:
        s = self.settings()
        return {TRANSCRIBE: not s.get("transcribe_wifi_only", True), ARCHIVE: not s.get("archive_wifi_only", True)}

    @property
    def on_wifi(self) -> bool:
        return self.app.network.on_wifi

    # ---- running -----------------------------------------------------------------------------

    def on_network(self, types=None) -> None:
        if any(r.allowed(self.on_wifi) for r in self.lib.pending()):
            self.kick()

    def archive(self, rec_id: str) -> None:
        """"Archive now" for one note (when automatic archiving is off)."""
        self._archive_now.add(rec_id)
        self.kick()

    def kick(self) -> None:
        with self._lock:
            if self.syncing:
                self._again = True
                return
            self.syncing = True
        if self._timer:
            self._timer.cancel()
            self._timer = None
        threading.Thread(target=self._run, daemon=True, name="voice-sync").start()
        self._notify()

    def _run(self) -> None:
        result = SyncResult()
        try:
            if not self._recovered and not self.recorder.active:
                self._recovered = True
                recover_orphans(self.lib)
            while True:
                s = self.settings()
                client = Client(s.get("server_url") or "", s.get("token") or "")
                if self.lib.pending():
                    result = sync_once(self.lib, client, self.on_wifi, auto_archive=bool(s.get("auto_archive", True)),
                                       keep_audio=bool(s.get("keep_audio", True)), archive_now=set(self._archive_now))
                    self._archive_now -= {r.id for r in self.lib.all() if r.status in (ARCHIVING, ARCHIVED)}
                else:
                    result = SyncResult()
                self.last, self.last_at = result, time.time()
                self._announce(result)
                with self._lock:
                    if not self._again:
                        self.syncing = False
                        break
                    self._again = False
        except Exception:  # never let the sync thread die silently
            log.exception("voice sync failed")
            with self._lock:
                self.syncing = False
                self._again = False
        self._notify()
        self._schedule(result)

    def _schedule(self, result: SyncResult) -> None:
        """While the workstation is transcribing or archiving, look again in a little while."""
        if result.busy and not result.unreachable:
            self._busy_polls = 0 if result.changed else self._busy_polls + 1
            delay = POLL_DELAYS[min(self._busy_polls, len(POLL_DELAYS) - 1)]
            self._timer = threading.Timer(delay, self.kick)
            self._timer.daemon = True
            self._timer.start()
        else:
            self._busy_polls = 0

    def _announce(self, result: SyncResult) -> None:
        text = result.summary()
        top = self.app.stack[-1] if self.app.stack else None
        if text and not isinstance(top, VoiceRecScreen):
            try:
                self.app.toast(text)
            except Exception:
                pass

    def _notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                pass


# ---- the screen --------------------------------------------------------------------------------


class VoiceRecScreen:
    def __init__(self, app: "TaskDeckApp", plugin):
        self.app = app
        self.plugin = plugin
        self.sync = VoiceSync.for_app(app, plugin)
        self.recorder = self.sync.recorder
        self.use_mobile = ft.Switch(label="Use mobile data for this note", value=False,
                                    on_change=lambda e: self._update())
        self.timer = ft.Text("0:00", size=40, weight=ft.FontWeight.W_300, text_align=ft.TextAlign.CENTER)
        self.record_box = ft.Column(spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        self.status_box = ft.Column(spacing=6)
        self.history_box = ft.Column(spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.pin_button = ft.IconButton(icon=ft.Icons.PUSH_PIN_OUTLINED, selected_icon=ft.Icons.PUSH_PIN,
                                        selected=app.favorites.is_pinned(plugin.id), tooltip="Pin to Home",
                                        on_click=self.toggle_pin)
        self.view = screen_view(
            route=f"/job/{plugin.id}",
            appbar=ft.AppBar(title=ft.Text(plugin.title), actions=[
                self.pin_button,
                ft.IconButton(ft.Icons.TUNE, tooltip="Settings", on_click=lambda e: app.open_settings(plugin.id)),
                ft.IconButton(ft.Icons.SEARCH, tooltip="Command palette (Ctrl+K)", on_click=lambda e: app.palette.show()),
            ]),
            controls=[
                ft.Row([ft.Icon(plugin_icon(plugin), color=ft.Colors.PRIMARY),
                        ft.Text(plugin.description, color=ft.Colors.ON_SURFACE_VARIANT, expand=True)]),
                ft.Card(ft.Container(self.record_box, padding=16), variant=ft.CardVariant.OUTLINED),
                self.status_box,
                section_title("History (oldest first)"),
                self.history_box,
            ],
        )
        self.notice = ""  # last problem or hint from the record button
        self.crash = self.recorder.last_crash()  # set when TaskDeck closed during a native step
        self.render(update=False)

    # ---- lifecycle (called by the app) -------------------------------------------------------

    def check_ready(self) -> None:
        """Called by the app once the screen is showing (so _on_sync can tell when it is closed)."""
        if self._on_sync not in self.sync.listeners:
            self.sync.listeners.append(self._on_sync)
        self.sync.kick()
        if self.recorder.active:
            self.app.page.run_task(self._tick)

    def recheck(self) -> None:
        self.render()

    def toggle_pin(self, e=None) -> None:
        self.app.toggle_pin(self.plugin.id)
        self.pin_button.selected = self.app.favorites.is_pinned(self.plugin.id)
        self._update()

    def _on_sync(self) -> None:
        if self.view not in self.app.page.views:  # this screen was closed
            if self._on_sync in self.sync.listeners:
                self.sync.listeners.remove(self._on_sync)
            return
        self.render()

    def _update(self) -> None:
        try:
            self.view.update()
        except Exception:  # not on screen (yet / any more)
            pass

    # ---- recording ---------------------------------------------------------------------------

    async def start(self, e=None) -> None:
        self.crash = None
        problem = await self.recorder.start()
        if problem:
            self.notice = problem
            self.render()
            return
        self.notice = ""
        self.render()
        await self._tick()

    async def _tick(self) -> None:
        while self.recorder.active and self.view in self.app.page.views:  # stops when the screen closes
            self.timer.value = clock(self.recorder.elapsed)
            self._update()
            await asyncio.sleep(0.5)

    async def toggle_pause(self, e=None) -> None:
        await (self.recorder.resume() if self.recorder.paused else self.recorder.pause())
        self.render()

    async def stop(self, e=None) -> None:
        mobile = self.sync.default_mobile_data()
        if self.use_mobile.value:
            mobile = {s: True for s in STAGES}
        rec = await self.recorder.stop(mobile)
        self.use_mobile.value = False
        if rec is None:
            self.app.toast("Nothing was recorded.")
        self.render()
        self.sync.kick()

    async def discard(self, e=None) -> None:
        await self.recorder.discard()
        self.render()

    # ---- rendering ---------------------------------------------------------------------------

    def render(self, update: bool = True) -> None:
        self._render_record()
        self._render_status()
        self._render_history()
        if update:
            self._update()

    def _render_record(self) -> None:
        r = self.recorder
        if not recorder_supported() and is_android():
            self.record_box.controls = [ft.Text(NEEDS_APK, color=ft.Colors.ERROR)]
            return
        if not r.active:
            self.timer.value = "0:00"
            self.record_box.controls = [
                ft.IconButton(ft.Icons.MIC, icon_size=56, tooltip="Record", on_click=self.start,
                              style=ft.ButtonStyle(bgcolor=ft.Colors.PRIMARY_CONTAINER, padding=24)),
                ft.Text("Tap to record", color=ft.Colors.ON_SURFACE_VARIANT),
                *self._notices(),
                self.use_mobile,
            ]
            return
        self.timer.value = clock(r.elapsed)
        self.record_box.controls = [
            ft.Row([ft.Icon(ft.Icons.FIBER_MANUAL_RECORD, color=ft.Colors.ERROR if not r.paused else ft.Colors.OUTLINE),
                    ft.Text("Paused" if r.paused else "Recording", weight=ft.FontWeight.W_600)],
                   alignment=ft.MainAxisAlignment.CENTER),
            self.timer,
            ft.Row([
                ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Discard", on_click=self.discard),
                ft.IconButton(ft.Icons.PLAY_ARROW if r.paused else ft.Icons.PAUSE, icon_size=36,
                              tooltip="Resume" if r.paused else "Pause", on_click=self.toggle_pause),
                ft.IconButton(ft.Icons.STOP, icon_size=56, tooltip="Stop and save", on_click=self.stop,
                              style=ft.ButtonStyle(bgcolor=ft.Colors.ERROR_CONTAINER, padding=16)),
            ], alignment=ft.MainAxisAlignment.CENTER, spacing=16),
            self.use_mobile,
        ]

    def _notices(self) -> list[ft.Control]:
        out: list[ft.Control] = []
        if self.crash:
            out.append(_note(ft.Icons.WARNING_AMBER, f"TaskDeck closed unexpectedly last time, while "
                                                     f"{self.crash['step']}. If it keeps happening, tell Claude "
                                                     "this step's name.", error=True))
        if self.notice:
            ok = self.notice == MIC_JUST_ALLOWED
            out.append(_note(ft.Icons.CHECK_CIRCLE_OUTLINE if ok else ft.Icons.MIC_OFF, self.notice, error=not ok))
            if self.notice == MIC_BLOCKED:
                out.append(ft.TextButton("Open app settings", icon=ft.Icons.SETTINGS,
                                         on_click=lambda e: self.app.page.run_task(_open_app_settings)))
        return out

    def _render_status(self) -> None:
        pending = self.sync.lib.pending()
        last = self.sync.last
        lines: list[ft.Control] = []
        waiting = [p for p in pending if not p.allowed(self.sync.on_wifi)]
        if waiting:
            lines.append(_note(ft.Icons.WIFI, f"{len(waiting)} waiting for Wi-Fi (mobile data is off for "
                                              f"{'it' if len(waiting) == 1 else 'them'})"))
        if last and last.unreachable and pending:
            lines.append(_note(ft.Icons.CLOUD_OFF, last.unreachable, error=True))
        for err in (last.errors if last else [])[:3]:
            lines.append(_note(ft.Icons.ERROR_OUTLINE, err, error=True))
        row = ft.Row([
            ft.Text(f"{len(pending)} in progress" if pending else "All caught up",
                    color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
            ft.ProgressRing(width=16, height=16, stroke_width=2) if self.sync.syncing else
            ft.TextButton("Sync now", icon=ft.Icons.SYNC, on_click=lambda e: self.sync.kick()),
        ])
        self.status_box.controls = [row, *lines]

    def _render_history(self) -> None:
        recs = self.sync.lib.all()  # ascending by recorded time
        if not recs:
            self.history_box.controls = [empty_state(ft.Icons.MIC_NONE, "No recordings yet",
                                                     "Tap the microphone to record your first note.")]
            return
        self.history_box.controls = [self._item(r) for r in recs]

    def _item(self, r: Recording) -> ft.Control:
        stamps = [ft.Text(f"Recorded {local_time(r.recorded_at)} · {clock(r.duration)}", size=13)]
        if r.transcribed_at:
            stamps.append(ft.Text(f"Transcribed {local_time(r.transcribed_at)}", size=13))
        status = STATUS_TEXT.get(r.status, r.status)
        if r.stage and not r.allowed(self.sync.on_wifi):
            status = f"Waiting for Wi-Fi to {STAGE_LABEL[r.stage].lower()}"
        trailing = ft.Icon(ft.Icons.SIGNAL_CELLULAR_ALT, size=18, color=ft.Colors.PRIMARY,
                           tooltip="May use mobile data") if r.stage and r.may_use_mobile_data(r.stage) else None
        body = [
            ft.Row([ft.Text(r.display_title, weight=ft.FontWeight.W_600, expand=True, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS), *([trailing] if trailing else [])]),
            *stamps,
            ft.Text(status + (f" · {r.category}" if r.category else ""), size=13,
                    color=ft.Colors.PRIMARY if r.status == ARCHIVED else ft.Colors.ON_SURFACE_VARIANT),
        ]
        if r.error:
            body.append(ft.Text(r.error, size=13, color=ft.Colors.ERROR, max_lines=3))
        return ft.Card(ft.Container(ft.Column(body, spacing=2), padding=12, on_click=lambda e, i=r.id: self.details(i)),
                       variant=ft.CardVariant.OUTLINED)

    # ---- one recording ------------------------------------------------------------------------

    def details(self, rec_id: str) -> None:
        r = self.sync.lib.get(rec_id)
        if r is None:
            return
        lib, page = self.sync.lib, self.app.page

        def close(e=None):
            page.pop_dialog()

        def set_stage(stage: str, value: bool) -> None:
            lib.set_mobile_data(rec_id, stage, value)
            self.render()
            if value:
                self.sync.kick()

        def delete(e=None):
            close()

            def really(e=None):
                page.pop_dialog()
                lib.delete(rec_id)
                self.render()

            page.show_dialog(ft.AlertDialog(
                title=ft.Text("Delete this note from the phone?"),
                content=ft.Text("A copy already sent to the workstation stays there." if r.status != RECORDED
                                else "It hasn't been sent to the workstation, so it will be gone for good."),
                actions=[ft.TextButton("Keep", on_click=lambda e: page.pop_dialog()),
                         ft.FilledButton("Delete", on_click=really)]))

        rows: list[ft.Control] = [
            ft.Text(f"Recorded {local_time(r.recorded_at)} · {clock(r.duration)}"
                    + (f" · {human_size(r.size)}" if r.size else "")),
            ft.Text(f"Transcribed {local_time(r.transcribed_at)}" if r.transcribed_at else "Not transcribed yet"),
        ]
        if r.archived_at:
            rows.append(ft.Text(f"Archived {local_time(r.archived_at)}" + (f" · {r.category}" if r.category else "")))
        if r.tags:
            rows.append(ft.Text(" ".join(f"#{t}" for t in r.tags), color=ft.Colors.PRIMARY))
        if r.summary:
            rows.append(ft.Text(r.summary, italic=True))
        if r.error:
            rows.append(ft.Text(r.error, color=ft.Colors.ERROR))
        if r.status != ARCHIVED:
            rows.append(section_title("Mobile data for this note"))
            for stage in STAGES:
                done = stage == TRANSCRIBE and r.status not in (RECORDED, QUEUED)
                rows.append(ft.Switch(label=f"{STAGE_LABEL[stage]} over mobile data",
                                      value=r.may_use_mobile_data(stage), disabled=done,
                                      on_change=lambda e, s=stage: set_stage(s, bool(e.control.value))))
        rows.append(section_title("Transcript"))
        rows.append(ft.Text(r.transcript or "—", selectable=True))
        actions: list[ft.Control] = [ft.TextButton("Delete", icon=ft.Icons.DELETE_OUTLINE, on_click=delete)]
        if r.transcript:
            actions.append(ft.TextButton("Copy", icon=ft.Icons.COPY, on_click=lambda e: self.app.copy(r.transcript)))
        if r.status == TRANSCRIBED and not self.sync.settings().get("auto_archive", True):
            actions.append(ft.TextButton("Archive now", icon=ft.Icons.ARCHIVE_OUTLINED,
                                         on_click=lambda e: (close(), self.sync.archive(rec_id))))
        actions.append(ft.TextButton("Close", on_click=close))
        page.show_dialog(ft.AlertDialog(
            title=ft.Text(r.display_title),
            content=ft.Column(rows, tight=True, scroll=ft.ScrollMode.AUTO, spacing=8, width=480),
            actions=actions, scrollable=True))


async def _open_app_settings() -> None:
    try:
        from flet_permission_handler import PermissionHandler

        await PermissionHandler().open_app_settings()
    except Exception:
        pass


def _note(icon, text: str, error: bool = False) -> ft.Control:
    color = ft.Colors.ERROR if error else ft.Colors.ON_SURFACE_VARIANT
    return ft.Row([ft.Icon(icon, size=18, color=color), ft.Text(text, size=13, color=color, expand=True)],
                  vertical_alignment=ft.CrossAxisAlignment.START)

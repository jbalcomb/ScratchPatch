"""Keeps TaskDeck running while a background task runs and you use other apps.

Android freezes apps in the background and cuts their network, which stopped Google Drive uploads
as soon as the user switched apps. While any task runs, TaskDeck starts a foreground service
(the taskdeck-background extension in native/background, built into the APK): Android then shows
an ongoing notification with the task's progress and leaves TaskDeck alone. When the last task ends
the service stops; if TaskDeck isn't on screen, a "finished" notification says how it went.

The first time, TaskDeck also asks for notifications (Android 13+) and "unrestricted" battery use.
On an APK built without the extension (or on a desktop) all of this does nothing.
"""

from __future__ import annotations

import importlib.util
import time
from typing import TYPE_CHECKING, Any, Callable

import flet as ft

from taskdeck.core.tasks import CANCELLED, DONE, FAILED, Task
from taskdeck.shared.files import is_android

if TYPE_CHECKING:
    from .app import TaskDeckApp

ASKED = "background:asked"  # store key: permissions asked once
UPDATE_EVERY = 1.0  # seconds between notification updates
ENDED = {DONE: "Finished", FAILED: "Failed", CANCELLED: "Stopped"}


def supported() -> bool:
    return is_android() and importlib.util.find_spec("taskdeck_background") is not None


def describe(task: Task, others: int = 0) -> tuple[str, str, int | None]:
    """(title, text, percent) for the ongoing notification."""
    phase = task.current_phase
    fraction = phase.fraction if phase and phase.started is not None else task.fraction
    pct = None if fraction is None else int(fraction * 100)
    if task.note:
        text = task.note
    elif phase:
        text = phase.label + (f" · {pct}%" if pct is not None else "")
    else:
        text = f"{pct}%" if pct is not None else "Working…"
    if others:
        text += f" · {others} more running"
    return task.title, text, pct


class BackgroundKeeper:
    def __init__(self, app: "TaskDeckApp", service: Callable[[], Any] | None = None,
                 run: Callable[..., Any] | None = None, enabled: bool | None = None):
        self.app = app
        self._make_service = service or _make_service
        self._run = run or getattr(app.page, "run_task", None)
        self.enabled = supported() if enabled is None else enabled
        self.active = False
        self._service: Any = None
        self._shown: tuple[str, str, int | None] | None = None
        self._last = 0.0
        self._note = ""  # a pause shows at once, not after UPDATE_EVERY

    def on_task(self, task: Task) -> None:
        """Task listener (worker threads): start, update or stop the service."""
        if not self.enabled:
            return
        running = self.app.tasks.running()
        if running:
            shown = describe(running[0], len(running) - 1)
            now = time.time()
            if not self.active:
                self.active, self._shown, self._last, self._note = True, shown, now, running[0].note
                self._run(self._start, *shown)
            elif shown != self._shown and (now - self._last >= UPDATE_EVERY or running[0].note != self._note):
                self._shown, self._last, self._note = shown, now, running[0].note
                self._run(self._call, "update", *shown)
        elif self.active:
            self.active, self._shown = False, None
            away = self.app.hidden
            title = f"{ENDED.get(task.status, 'Finished')}: {task.title}" if away else ""
            self._run(self._call, "stop", title, task.message if away else "")

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = self._make_service()
        return self._service

    async def _start(self, title: str, text: str, pct: int | None) -> None:
        started = await self._call("start", title, text, pct)
        self.app.tasks.log_running("service", started=bool(started))
        await self.ask_once()

    async def _call(self, method: str, *args: Any) -> Any:
        try:
            return await getattr(self.service, method)(*args)
        except Exception as exc:  # a notification problem must never stop the task
            self.app.tasks.log_running("service", method=method, error=f"{type(exc).__name__}: {exc}")
            return None

    # ---- permissions ------------------------------------------------------------------------------

    async def ask_once(self) -> None:
        if self.app.store.get(ASKED):
            return
        self.app.store.set(ASKED, True)
        self.app.toast("TaskDeck shows a notification while it works in the background.")
        await self.allow_notifications()
        await self.allow_battery()

    async def allow_notifications(self) -> bool:
        return await _request("NOTIFICATION")

    async def allow_battery(self) -> bool:
        return await _request("IGNORE_BATTERY_OPTIMIZATIONS")

    def settings_rows(self) -> list[ft.Control]:
        if not self.enabled:
            return []

        async def ask(fn: Callable, done: str) -> None:
            self.app.toast(done if await fn() else "Not allowed. You can change it in Android's settings for TaskDeck.")

        return [
            ft.Text("While a task runs (e.g. an extraction), TaskDeck keeps going in the background and shows its "
                    "progress in a notification. For that to work well, allow both:",
                    color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Row([
                ft.OutlinedButton("Notifications", icon=ft.Icons.NOTIFICATIONS,
                                  on_click=lambda e: self.app.page.run_task(ask, self.allow_notifications,
                                                                            "Notifications allowed")),
                ft.OutlinedButton("Unrestricted battery", icon=ft.Icons.BATTERY_CHARGING_FULL,
                                  on_click=lambda e: self.app.page.run_task(ask, self.allow_battery,
                                                                            "Battery use unrestricted")),
            ], wrap=True),
        ]


def _make_service() -> Any:
    from taskdeck_background import BackgroundWork

    return BackgroundWork()


async def _request(name: str) -> bool:
    try:
        from flet_permission_handler import Permission, PermissionHandler, PermissionStatus

        status = await PermissionHandler().request(getattr(Permission, name))
        return status == PermissionStatus.GRANTED
    except Exception:
        return False

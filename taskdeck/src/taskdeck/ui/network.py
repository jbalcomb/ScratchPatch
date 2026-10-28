"""Which network the phone is on (Flet's Connectivity service), and the per-job "Wi-Fi only" setting.

A job with Wi-Fi only on runs its work inside :meth:`NetworkMonitor.scope`: every request it makes
first waits until the phone is on Wi-Fi (or Ethernet), showing "Waiting for Wi-Fi…", and goes on
by itself when Wi-Fi connects. Cancel still works while it waits. When the network type isn't
known (a desktop, or the service didn't answer) nothing is held back.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterator

from taskdeck.core import Cancelled
from taskdeck.shared import http
from taskdeck.shared.files import is_android

if TYPE_CHECKING:
    from .app import TaskDeckApp

WAITING = "Waiting for Wi-Fi… (this job is set to Wi-Fi only)"
UNMETERED = {"wifi", "ethernet"}
CHECK_EVERY = 2.0  # seconds between looks at the cancel flag while waiting


class NetworkMonitor:
    def __init__(self, app: "TaskDeckApp"):
        self.app = app
        self.types: list[str] | None = None  # e.g. ["wifi"], ["mobile", "vpn"]; None = unknown
        self._changed = threading.Condition()
        self._service = None

    async def start(self) -> None:
        """Ask the phone once, then follow its change events."""
        if not is_android():
            return
        try:
            import flet as ft

            self._service = ft.Connectivity(on_change=lambda e: self.set_types([t.value for t in e.connectivity]))
            self.set_types([t.value for t in await self._service.get_connectivity()])
        except Exception:  # unknown network: nothing is held back
            self._service = None

    def set_types(self, types: list[str]) -> None:
        with self._changed:
            changed = types != self.types
            switched = changed and self.types is not None
            self.types = list(types)
            self._changed.notify_all()
        if changed:
            cut = http.abort_connections() if switched else 0  # don't wait for the old network to time out
            self.app.tasks.log_running("network", types=self.types, connections_cut=cut)

    @property
    def on_wifi(self) -> bool:
        return self.types is None or bool(UNMETERED & set(self.types))

    def wifi_only(self, settings_id: str) -> bool:
        return bool(self.app.job_settings(settings_id).get("wifi_only"))

    def wait_for_wifi(self, cancelled: Callable[[], bool], on_wait: Callable[[str], None]) -> None:
        if self.on_wifi:
            return
        on_wait(WAITING)
        with self._changed:
            while not self.on_wifi:
                if cancelled():
                    raise Cancelled("Stopped while waiting for Wi-Fi.")
                self._changed.wait(CHECK_EVERY)
        on_wait("")

    @contextmanager
    def scope(self, settings_id: str, cancelled: Callable[[], bool],
              on_wait: Callable[[str], None]) -> Iterator[None]:
        """Run a job's network work under its Wi-Fi-only setting."""
        gate = None
        if self.wifi_only(settings_id):
            def gate(url: str) -> None:
                self.wait_for_wifi(cancelled, on_wait)
        with http.scope(gate=gate):
            yield

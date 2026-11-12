"""Background work: the foreground service follows the task manager (against a fake service)."""

import asyncio

from taskdeck.core import JsonStore, TaskManager
from taskdeck.ui import background
from taskdeck.ui.background import BackgroundKeeper, describe


class FakeService:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def call(*args):
            self.calls.append((name, *args))
        return call


class FakeApp:
    def __init__(self, tmp_path):
        self.tasks = TaskManager(start_thread=lambda fn: None)  # tasks stay running until finished by hand
        self.store = JsonStore(tmp_path / "state.json")
        self.hidden = False
        self.toasts = []
        self.toast = self.toasts.append


def keeper(tmp_path, monkeypatch):
    monkeypatch.setattr("taskdeck.core.tasks.NOTIFY_EVERY", 0)  # every report reaches the keeper
    app = FakeApp(tmp_path)
    svc = FakeService()
    asked = []

    async def request(name):
        asked.append(name)
        return True
    monkeypatch.setattr(background, "_request", request)
    k = BackgroundKeeper(app, service=lambda: svc, run=lambda fn, *a: asyncio.run(fn(*a)), enabled=True)
    app.tasks.listeners.append(k.on_task)
    return app, k, svc, asked


def finish(app, task, status, message):
    task.status, task.message = status, message
    app.tasks._notify(task)


def test_service_follows_tasks(tmp_path, monkeypatch):
    app, k, svc, asked = keeper(tmp_path, monkeypatch)
    t = app.tasks.start("Extracting a.7z", "extract_archive", lambda t: "")
    assert svc.calls == [("start", "Extracting a.7z", "Working…", None)]
    assert asked == ["NOTIFICATION", "IGNORE_BATTERY_OPTIMIZATIONS"] and app.toasts

    k._last = 0
    t.set_phases(("Unpacking", 100), ("Uploading to Google Drive", 100))
    t.advance(0, 40)
    k._last = 0
    t.advance(0, 50)
    assert svc.calls[-1] == ("update", "Extracting a.7z", "Unpacking · 50%", 50)

    t.set_note("Paused: no connection.")  # shown at once, not throttled
    assert svc.calls[-1] == ("update", "Extracting a.7z", "Paused: no connection.", 50)

    finish(app, t, "done", "Uploaded 3 files")
    assert svc.calls[-1] == ("stop", "", "")  # on screen: no "finished" notification

    t2 = app.tasks.start("Extracting b.7z", "extract_archive", lambda t: "")
    assert svc.calls[-1][0] == "start" and asked == ["NOTIFICATION", "IGNORE_BATTERY_OPTIMIZATIONS"]  # asked once
    app.hidden = True
    finish(app, t2, "failed", "Drive is full")
    assert svc.calls[-1] == ("stop", "Failed: Extracting b.7z", "Drive is full")


def test_throttled_and_two_tasks(tmp_path, monkeypatch):
    app, k, svc, _ = keeper(tmp_path, monkeypatch)
    a = app.tasks.start("A", "j", lambda t: "")
    a.report(1, 10)
    a.report(2, 10)
    assert [c[0] for c in svc.calls] == ["start"]  # within a second: no updates
    b = app.tasks.start("B", "j", lambda t: "")
    k._last = 0
    a.report(5, 10)
    assert svc.calls[-1] == ("update", "A", "50% · 1 more running", 50)
    finish(app, a, "done", "ok")
    assert svc.calls[-1][0] == "update"  # B still runs
    finish(app, b, "cancelled", "Stopped.")
    assert svc.calls[-1][0] == "stop"


def test_disabled_without_the_extension(tmp_path):
    app = FakeApp(tmp_path)
    k = BackgroundKeeper(app, service=lambda: 1 / 0, run=lambda *a: 1 / 0)
    assert not k.enabled and k.settings_rows() == []  # not Android / APK without the extension
    app.tasks.listeners.append(k.on_task)
    app.tasks.start("A", "j", lambda t: "")


def test_describe():
    t = TaskManager(start_thread=lambda fn: None).start("X", "j", lambda t: "")
    assert describe(t) == ("X", "Working…", None)
    t.report(3, 4)
    assert describe(t, 2) == ("X", "75% · 2 more running", 75)

"""Extract Archive: background tasks, phone-storage helpers, the job and its screen."""

import asyncio
import inspect
import os
import time
from pathlib import Path

import pytest

from taskdeck.core import Cancelled, JsonStore, TaskManager
from taskdeck.core.tasks import CANCELLED, DONE, FAILED
from taskdeck.jobs import extract_archive
from taskdeck.jobs.extract_archive import logic
from taskdeck.shared import archive, files

FIX = Path(__file__).parent / "fixtures" / "archives"
CONTENT = {"docs/readme.txt": b"hello archive\n", "docs/notes/a.txt": b"note a\n", "big.txt": b"x" * 3000}
needs_engine = pytest.mark.skipif(not archive.available(), reason="7-Zip engine not built (set TASKDECK_7Z_DIR)")


def files_under(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def inline():
    """A TaskManager that runs work immediately (no thread)."""
    return TaskManager(start_thread=lambda fn: fn())


# ---- background tasks ------------------------------------------------------------------

def test_task_reports_and_finishes():
    seen = []
    tm = inline()
    tm.listeners.append(lambda t: seen.append((t.status, t.done)))

    def work(task):
        task.report(5, 10)
        return "all good"

    t = tm.start("Doing", "job", work)
    assert t.status == DONE and t.message == "all good" and t.finished
    assert seen[0] == ("running", 0) and seen[-1] == (DONE, 5)
    assert tm.latest("job") is t and tm.running() == []


def test_task_failure_and_cancel():
    tm = inline()
    assert tm.start("x", "j", lambda t: (_ for _ in ()).throw(RuntimeError("Disk full"))).status == FAILED
    assert tm.latest("j").message == "Disk full"

    def cancellable(task):
        tm.cancel(task)
        if task.cancel_requested:
            raise Cancelled("Stopped.")
        return "no"

    t = tm.start("y", "j", cancellable)
    assert t.status == CANCELLED and t.message == "Stopped."
    tm.forget(t)
    assert t not in tm.tasks


def test_task_eta():
    tm = TaskManager(start_thread=lambda fn: None)  # never runs: we drive it by hand
    t = tm.start("x", "j", lambda t: "")
    t.started = 100.0
    t.report(25, 100)
    assert t.fraction == 0.25 and t.eta(now=110.0) == pytest.approx(30.0)


# ---- phone storage helpers ---------------------------------------------------------------

@pytest.mark.parametrize("name,stem", [
    ("CoM2ModWarlord1.5.12.6.2.7z", "CoM2ModWarlord1.5.12.6.2"),  # the folder name the test job cut short
    ("site.tar.gz", "site"), ("backup.tgz", "backup"), ("photos.ZIP", "photos"), ("a.rar", "a"),
    ("notes.txt", "notes"),
])
def test_archive_stem(name, stem):
    assert files.archive_stem(name) == stem


def test_list_folder_order_and_filter(tmp_path):
    (tmp_path / "b-folder").mkdir()
    (tmp_path / "A-folder").mkdir()
    (tmp_path / ".hidden").mkdir()
    old, new = tmp_path / "old.zip", tmp_path / "new.7z"
    old.write_bytes(b"1")
    new.write_bytes(b"22")
    os.utime(old, (1000, 1000))
    (tmp_path / "notes.txt").write_text("x")
    names = [i.name for i in files.list_folder(tmp_path, archives_only=True)]
    assert names == ["A-folder", "b-folder", "new.7z", "old.zip"]
    assert "notes.txt" in [i.name for i in files.list_folder(tmp_path)]


def test_names_and_sizes(tmp_path):
    (tmp_path / "photos").mkdir()
    assert files.unique_name(tmp_path, "photos") == "photos (2)" and files.unique_name(tmp_path, "new") == "new"
    assert files.valid_folder_name("a/b") and files.valid_folder_name(" ") and files.valid_folder_name("ok") is None
    assert files.human_size(2.5 * 1024 ** 3) == "2.5 GB" and files.human_size(12) == "12 bytes"
    assert files.human_duration(130) == "about 2 min" and files.human_duration(5) == "a few seconds"
    assert files.free_space(tmp_path / "not" / "yet") > 0


# ---- the job ----------------------------------------------------------------------------------

@needs_engine
def test_scan(tmp_path):
    s = logic.scan(FIX / "plain.7z")
    assert (s.format, s.files, s.folders, s.unpacked, s.encrypted) == ("7z", 3, 2, 3021, False)
    hidden = logic.scan(FIX / "headers-encrypted.7z")
    assert hidden.names_hidden and hidden.encrypted and hidden.error is None
    assert logic.scan(FIX / "headers-encrypted.7z", "nope").error == "Wrong password."
    assert logic.scan(FIX / "headers-encrypted.7z", "Secret1").files == 3
    assert logic.scan(FIX / "aes.zip").encrypted and not logic.scan(FIX / "aes.zip").names_hidden
    assert "outside the folder" in logic.scan(FIX / "traversal.zip").error


@needs_engine
def test_problems(tmp_path, monkeypatch):
    s = logic.scan(FIX / "aes.zip")
    assert logic.problems(s, tmp_path, "aes", "") == ["Enter the archive's password."]
    assert logic.problems(s, tmp_path, "aes", "Secret1") == []
    assert "doesn't exist" in logic.problems(s, tmp_path / "gone", "aes", "Secret1")[0]
    monkeypatch.setattr(files, "free_space", lambda p: 100)
    assert logic.problems(s, tmp_path, "aes", "Secret1") == ["Not enough space: needs 3.0 KB, 100 bytes free."]


@needs_engine
def test_extract_task_and_overwrite(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "big.txt").write_bytes(b"old")
    tm = inline()
    t = tm.start("Extracting", "extract_archive", lambda t: logic.extract(t, FIX / "data-encrypted.7z", dest, "Secret1"))
    assert t.status == DONE, t.message
    assert t.message.startswith("Extracted 3 files (3.0 KB) to ")
    assert files_under(dest) == CONTENT  # big.txt replaced


@needs_engine
def test_extract_task_wrong_password_and_cancel(tmp_path):
    tm = inline()
    bad = tm.start("x", "j", lambda t: logic.extract(t, FIX / "data-encrypted.7z", tmp_path / "a", "nope"))
    assert bad.status == FAILED and bad.message.startswith("Wrong password.")

    def cancelled(task):
        task.cancel_requested = True
        return logic.extract(task, FIX / "plain.7z", tmp_path / "b", None)

    c = tm.start("y", "j", cancelled)
    assert c.status == CANCELLED and "stay in" in c.message


@needs_engine
def test_job_pipeline_defaults_to_a_folder_next_to_the_archive(tmp_path):
    src = tmp_path / "Mod.1.2.7z"
    src.write_bytes((FIX / "plain.7z").read_bytes())
    report = extract_archive.PLUGIN.run({"archive": str(src)})
    assert report.ok, report.error
    assert files_under(tmp_path / "Mod.1.2") == CONTENT


# ---- the app: cleanup, screen, Home strip ----------------------------------------------------------

class FakePage:
    def __init__(self):
        self.views, self.web, self.platform, self.dialogs = [], False, None, []

    def update(self, *a):
        pass

    def schedule_update(self):
        pass

    def run_task(self, fn, *args):
        result = fn(*args)
        if inspect.isawaitable(result):
            asyncio.run(result)

    def run_thread(self, fn, *args):
        fn(*args)

    def show_dialog(self, d):
        self.dialogs.append(d)

    def pop_dialog(self):
        self.dialogs.pop()


def make_app(tmp_path):
    from taskdeck.ui.app import TaskDeckApp

    app = TaskDeckApp(FakePage(), store=JsonStore(tmp_path / "state.json"), vault_backend=None)
    app.tasks.notify_in_background = False  # listeners run at once, so tests can check right after
    app.toasts = []
    app.toast = app.toasts.append
    return app


def test_removes_the_old_test_extraction(tmp_path):
    old = tmp_path / "extract-test" / "CoM2ModWarlord1" / "a.txt"
    old.parent.mkdir(parents=True)
    old.write_text("x")
    make_app(tmp_path)
    for _ in range(50):  # deleted in the background
        if not old.parent.parent.exists():
            break
        time.sleep(0.02)
    assert not (tmp_path / "extract-test").exists()


@needs_engine
def test_screen_flow(tmp_path):
    app = make_app(tmp_path)
    app.tasks._start = lambda fn: fn()  # run the extraction inline
    src = tmp_path / "Pics 2026.7z"
    src.write_bytes((FIX / "plain.7z").read_bytes())
    app.open_job("extract_archive")
    screen = app.stack[-1]
    assert type(screen).__name__ == "ExtractScreen" and screen.access_ok  # desktop: no permission needed
    screen.set_archive(src)
    assert screen.scan.files == 3 and screen.name == "Pics 2026" and screen.parent == tmp_path
    assert not screen.extract_button.disabled
    screen.set_destination(tmp_path, "Holiday")
    screen.extract()
    assert screen.task.status == DONE and files_under(tmp_path / "Holiday") == CONTENT
    assert app.store.get("extract:recent") == [str(src)]
    screen.dismiss()
    assert screen.task is None


@needs_engine
def test_screen_password_and_blocking(tmp_path):
    app = make_app(tmp_path)
    app.open_job("extract_archive")
    screen = app.stack[-1]
    screen.set_archive(FIX / "headers-encrypted.7z")
    assert screen.scan.names_hidden and screen.password.visible and screen.extract_button.disabled
    screen.password.value = "nope"
    screen.rescan()
    assert screen.scan.error == "Wrong password." and screen.extract_button.disabled
    screen.password.value = "Secret1"
    screen.rescan()
    assert screen.scan.files == 3


def test_home_strip_shows_running_tasks(tmp_path):
    app = make_app(tmp_path)
    app.tasks._start = lambda fn: None  # stays running
    t = app.tasks.start("Extracting big.7z", "extract_archive", lambda t: "")
    t.report(50, 100)
    strip = app.running_strip()
    assert len(strip) == 1 and "Extracting big.7z · 50%" in strip[0].content.controls[1].value


def test_task_phases():
    tm = TaskManager(start_thread=lambda fn: None)
    t = tm.start("x", "j", lambda t: "")
    t.set_phases(("Unpacking", 100), ("Uploading", 100))
    assert t.total == 200 and t.current_phase.label == "Unpacking"
    t.advance(0, 50)
    assert t.done == 50 and t.phases[0].fraction == 0.5 and t.phases[1].started is None
    t.advance(1, 20)  # unpacking counts as finished once uploading starts
    assert t.phases[0].complete and t.done == 120 and t.current_phase.label == "Uploading"
    t.phases[1].started -= 10
    assert t.phases[1].eta() == pytest.approx(40.0, abs=1)


def test_human_speed():
    assert [files.human_speed(n) for n in (0, 512, 1536, 3.4 * 2**20, 1.1 * 2**30, 5 * 2**40, -3)] == [
        "0 B/s", "512 B/s", "1.5 KB/s", "3.4 MB/s", "1.1 GB/s", "5.0 TB/s", "0 B/s"]


def test_current_speed():
    from taskdeck.core.tasks import SPEED_WINDOW, Speedometer

    m = Speedometer()
    assert m.speed(0) is None
    m.add(0, 100.0)
    m.add(1000, 100.5)
    assert m.speed(100.5) is None  # under a second of data
    for i in range(2, 31):  # 1000 bytes every half second for 15 s = 2000 B/s
        m.add(i * 1000, 100 + i / 2)
    assert m.speed(115.0) == pytest.approx(2000, rel=0.05)
    assert m.samples[0][0] <= 115.0 - SPEED_WINDOW  # older samples dropped, one kept as the base
    assert m.speed(135.0) < 1000  # stalled: falls towards 0
    m.add(10, 136.0)  # went backwards (upload restarted): starts over
    assert m.speed(140.0) is None


def test_screen_shows_a_bar_per_phase(tmp_path):
    import flet as ft

    from taskdeck.shared.gdrive import DriveLocation

    app = make_app(tmp_path)
    app.tasks._start = lambda fn: None
    app.open_job("extract_archive")
    screen = app.stack[-1]
    t = app.tasks.start("Extracting x.7z to Google Drive", "extract_archive", lambda t: "", result=DriveLocation())
    t.set_phases(("Unpacking", 1000), ("Uploading to Google Drive", 1000))
    t.advance(1, 250)
    screen.task = t
    screen.render(update=False)
    bars = [c for c in screen.progress_box.controls if isinstance(c, ft.ProgressBar)]
    assert [b.value for b in bars] == [1.0, 0.25]
    texts = [c.controls[1].value for c in screen.progress_box.controls if isinstance(c, ft.Row) and len(c.controls) == 2
             and isinstance(c.controls[1], ft.Text)]
    assert texts[:2] == ["✓ Done", "25%"]

    t.phases[1].meter.samples = [(time.time() - 10, 0), (time.time(), 250)]  # 25 B/s lately
    screen.render(update=False)
    lines = [c.value for c in screen.progress_box.controls if isinstance(c, ft.Text)]
    assert any(str(v).startswith("250 bytes of 1,000 bytes · 2") and " B/s" in str(v) for v in lines), lines

    # uploading counts files: "N of M files · rate · time left" at the current rate
    t.set_phase(1, label="Uploading to Google Drive (9,779 already there)", total=1000)
    t.phases[1].unit = "files"
    t.advance(1, 400)
    t.phases[1].meter.samples = [(time.time() - 10, 380), (time.time(), 400)]  # 2 files/s lately
    screen.render(update=False)
    lines = [str(c.value) for c in screen.progress_box.controls if isinstance(c, ft.Text)]
    assert any(v.startswith("400 of 1,000 files · 2.0 files/s · about 5 min left") for v in lines), lines
    assert t.fraction == 0.4  # the task's fraction is the current phase's, not bytes and files mixed


def test_reload_waits_for_running_tasks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from taskdeck.ui import app as app_module

    reloads = []
    app = make_app(tmp_path)
    monkeypatch.setattr(app_module, "boot", SimpleNamespace(reload=lambda page: reloads.append(page)))
    monkeypatch.setattr(app_module.time, "sleep", lambda s: None)
    app.tasks._start = lambda fn: None  # stays running until finished by hand
    t = app.tasks.start("Extracting x.7z to Google Drive", "extract_archive", lambda t: "")

    app.reload_app()
    dialog = app.page.dialogs[-1]
    assert reloads == [] and "Extracting x.7z" in dialog.content.value
    cancel, now, later = dialog.actions
    later.on_click(None)
    assert app.reload_pending and not app.page.dialogs and reloads == []

    t.status = DONE  # the task ends: the waited-for reload happens
    app.tasks._notify(t)
    assert reloads == [app.page] and not app.reload_pending

    t2 = app.tasks.start("Another", "extract_archive", lambda t: "")
    app.reload_app()
    app.page.dialogs[-1].actions[1].on_click(None)  # Reload now
    assert len(reloads) == 2 and t2.running
    app.tasks.forget(t)


@needs_engine
def test_drive_rerun_skips_files_already_uploaded(tmp_path):
    from test_gdrive import FakeDrive, client

    from taskdeck.shared.gdrive import DriveLocation

    fake = FakeDrive()
    tm = TaskManager(start_thread=lambda fn: fn())
    run = lambda: tm.start("x", "extract_archive", lambda t: logic.extract_to_drive(
        t, FIX / "plain.7z", None, client(fake), DriveLocation(), "P", tmp_path / "tmp"))
    first = run()
    assert first.status == DONE and "Skipped" not in first.message
    root = next(i for i, it in fake.items.items() if it["name"] == "P")
    by_name = {it["name"]: i for i, it in fake.items.items() if not it["folder"]}
    fake.items[by_name["big.txt"]]["data"] = b"partial"  # an upload cut short: different size
    del fake.items[by_name["readme.txt"]]  # never got there
    single = fake.single

    old_big = by_name["big.txt"]
    second = run()
    assert second.status == DONE, second.message
    assert second.message.startswith("Uploaded 2 files") and "Skipped 1 already there" in second.message
    assert fake.tree(root) == CONTENT and fake.single == single + 2
    assert fake.trashed == [old_big]  # the broken leftover: a fresh copy, then the leftover to the trash

    # same size, different bytes (a corrupt copy) and a duplicate of a good one
    by_name = {it["name"]: i for i, it in fake.items.items() if not it["folder"] and not it.get("trashed")}
    fake.items[by_name["a.txt"]]["data"] = b"note b\n"  # same length as "note a\n"
    readme = fake.items[by_name["readme.txt"]]
    fake.items["dup"] = dict(readme)  # a second "readme.txt" next to it
    third = run()
    assert third.status == DONE, third.message
    assert third.message.startswith("Uploaded 1 file ")
    assert fake.tree(root) == CONTENT  # exactly one of each, the right content
    assert by_name["a.txt"] in fake.trashed and len(fake.trashed) == 3
    assert second.phases[1].complete


def test_startup_keeps_this_process_temp_folders(tmp_path):
    mine = tmp_path / "tmp" / f"task-1-{os.getpid()}"
    stale = tmp_path / "tmp" / "task-1-1"
    for d in (mine, stale):
        d.mkdir(parents=True)
        (d / "a.png").write_text("x")
    make_app(tmp_path)  # a reload in the same process: a task of the old instance may still use `mine`
    for _ in range(50):
        if not stale.exists():
            break
        time.sleep(0.02)
    assert not stale.exists() and (mine / "a.png").exists()


def test_check_for_updates_never_stays_on_checking(tmp_path, monkeypatch):
    import threading as th
    from types import SimpleNamespace

    from taskdeck.ui import app as app_module

    app = make_app(tmp_path)
    hang = th.Event()
    monkeypatch.setattr(app_module, "boot", SimpleNamespace(check_for_update=lambda: hang.wait(5) and {"message": "Up to date."}))
    monkeypatch.setattr(app_module, "CHECK_LIMIT", 0.1)
    app.check_updates()
    assert app.update_status == "Checking…"
    for _ in range(100):
        if app.update_status != "Checking…":
            break
        time.sleep(0.02)
    hang.set()
    assert app.update_status.startswith("No answer from GitHub within")


def test_background_jobs_are_in_history(tmp_path):
    app = make_app(tmp_path)
    app.tasks._start = lambda fn: fn()  # run inline
    app.tasks.start("Extracting a.7z", "extract_archive", lambda t: "Extracted 3 files", inputs={"archive": "/x/a.7z"})

    def boom(t):
        raise RuntimeError("Drive is full")
    app.tasks.start("Extracting b.7z", "extract_archive", boom)
    entries = app.history.entries()
    assert [(e.job_id, e.ok, e.summary) for e in entries] == [
        ("extract_archive", False, "Drive is full"), ("extract_archive", True, "Extracted 3 files")]
    assert entries[1].inputs == {"archive": "/x/a.7z"}
    t = app.tasks.latest("extract_archive")
    app.tasks._notify(t)  # later notifications of a finished task don't add it again
    assert len(app.history.entries()) == 2


@needs_engine
def test_open_from_history_restores_archive_and_destination(tmp_path):
    from taskdeck.shared.gdrive import DriveLocation

    app = make_app(tmp_path)
    archive = tmp_path / "plain.7z"
    archive.write_bytes((FIX / "plain.7z").read_bytes())
    app.open_job("extract_archive", {"archive": str(archive), "name": "Pics", "to": "drive",
                                     "folder_id": "f1", "folder_names": "My Drive/Backups"})
    screen = app.stack[-1]
    assert screen.archive == archive and screen.name == "Pics"
    assert screen.parent == DriveLocation("f1", ("My Drive", "Backups"))
    assert screen.inputs()["folder_id"] == "f1"

    app.back()
    app.open_job("extract_archive", {"archive": str(archive), "name": "out", "to": "phone", "parent": str(tmp_path)})
    screen = app.stack[-1]
    assert screen.parent == tmp_path and screen.destination() == tmp_path / "out"

    app.back()
    app.open_job("extract_archive", {"archive": str(tmp_path / "gone.7z")})  # deleted since: opens empty
    assert app.stack[-1].archive is None

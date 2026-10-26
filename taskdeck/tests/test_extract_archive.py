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

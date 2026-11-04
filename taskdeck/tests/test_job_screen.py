"""The job screen keeps a draft of its inputs (no running Flet page needed)."""

from types import SimpleNamespace

from taskdeck.core import Drafts, JsonStore, RunReport
from taskdeck.jobs import daily_log
from taskdeck.ui.job_view import JobScreen

LOG = "Friday October 16th\nStart time 10:38\n13 orders for $187.82"


def fake_app(tmp_path):
    store = JsonStore(tmp_path / "state.json")
    return SimpleNamespace(
        store=store, drafts=Drafts(store),
        favorites=SimpleNamespace(is_pinned=lambda job_id: False),
        palette=None, open_settings=lambda job_id: None, services=dict,
    )


def test_typed_log_is_kept_for_next_visit(tmp_path):
    app = fake_app(tmp_path)
    screen = JobScreen(app, daily_log.PLUGIN)
    screen.fields["log"].input.value = LOG
    screen.invalidate()  # what typing / Paste triggers

    again = JobScreen(fake_app(tmp_path), daily_log.PLUGIN)  # new screen, store re-read from disk
    assert again.fields["log"].value == LOG
    assert again.status.value == "Restored what you entered last time."


def test_preset_wins_over_draft(tmp_path):
    app = fake_app(tmp_path)
    app.drafts.save("daily_log", {"log": LOG})
    screen = JobScreen(app, daily_log.PLUGIN, preset={"log": "from history"})
    assert screen.fields["log"].value == "from history"


def test_clear_forgets_the_draft(tmp_path):
    app = fake_app(tmp_path)
    app.drafts.save("daily_log", {"log": LOG})
    screen = JobScreen(app, daily_log.PLUGIN)
    screen.view.update = lambda: None  # no page in tests
    screen.clear()
    assert app.drafts.get("daily_log") == {}
    assert JobScreen(app, daily_log.PLUGIN).fields["log"].value == ""


def run_screen(screen, report):
    """Drive JobScreen.run with a canned report and no Flet page."""
    import asyncio

    screen.plugin = SimpleNamespace(**{k: getattr(daily_log.PLUGIN, k) for k in ("id", "has_commit", "commit_label")},
                                    run=lambda *a, **k: report)
    screen.view.update = lambda: None
    screen._show_report = lambda r: None
    screen.app.job_settings = lambda job_id: {}
    screen.app.page = SimpleNamespace(update=lambda: None)
    screen.app.history = SimpleNamespace(add=lambda *a: None)
    screen.app.on_history_changed = lambda: None
    asyncio.run(screen.run())


def commit_screen(screen, ok):
    import asyncio

    import flet as ft

    from taskdeck.core import CommitOutcome

    screen.plugin.attempt_commit = lambda payload, settings: CommitOutcome(ok, "Saved row" if ok else "Sheet error")
    screen.plugin.committed_label = "Saved"
    screen.app.toast = lambda message: None
    screen.payload = {"date": "10/16/2026"}
    screen.commit_button, screen.commit_error = ft.FilledButton("Save"), ft.Column()
    asyncio.run(screen.commit())


def test_draft_kept_until_the_row_is_saved(tmp_path):
    from taskdeck.core import RunReport

    app = fake_app(tmp_path)
    screen = JobScreen(app, daily_log.PLUGIN)
    screen.fields["log"].input.value = LOG
    run_screen(screen, RunReport(False, None, [], "Gemini: didn't answer"))
    assert app.drafts.get("daily_log") == {"log": LOG}  # parse error: still there
    run_screen(screen, RunReport(True, None, []))
    assert app.drafts.get("daily_log") == {"log": LOG}  # parsed but not saved: still there
    commit_screen(screen, ok=False)
    assert app.drafts.get("daily_log") == {"log": LOG}  # save failed: still there
    commit_screen(screen, ok=True)
    assert app.drafts.get("daily_log") == {}  # saved: dropped
    assert screen.fields["log"].value == LOG  # the text box itself is untouched


def test_run_is_blocked_until_the_job_is_ready(tmp_path):
    app = fake_app(tmp_path)
    app.job_settings = lambda job_id: {}  # nothing set up
    screen = JobScreen(app, daily_log.PLUGIN)
    screen.check_ready()
    assert screen.blocked and screen.run_button.disabled
    text = " ".join(c.value for c in screen.readiness.controls[0].content.controls if hasattr(c, "value") and c.value)
    assert "Before you can run this job" in text and "Gemini API key" in text


def test_online_check_runs_after_offline_checks_pass(tmp_path, monkeypatch):
    from taskdeck.core import Problem

    app = fake_app(tmp_path)
    app.job_settings = lambda job_id: {}
    app.page = SimpleNamespace(run_thread=lambda fn: fn())  # run the background check inline
    monkeypatch.setattr(type(daily_log.PLUGIN), "check_ready", lambda self, s: [])
    monkeypatch.setattr(type(daily_log.PLUGIN), "check_ready_online",
                        lambda self, s: [Problem("Sheet headers don’t match: M1 …", fix="recheck")])
    screen = JobScreen(app, daily_log.PLUGIN)
    screen.check_ready()
    assert not screen.checking and screen.blocked


def test_settings_the_online_check_fixed_are_saved(tmp_path, monkeypatch):
    from taskdeck.core import Problem
    from taskdeck.jobs import budget

    app = fake_app(tmp_path)
    saved = {"budget_url": "http://100.83.88.59:8092", "sheet_id": "s"}
    app.job_settings = lambda job_id: dict(saved)
    app.save_job_settings = lambda job_id, values: saved.update(values)
    app.page = SimpleNamespace(run_thread=lambda fn: fn())
    monkeypatch.setattr(type(budget.PLUGIN), "check_ready", lambda self, s: [])
    monkeypatch.setattr(type(budget.PLUGIN), "check_ready_online", lambda self, s: [Problem(
        "Budget service address updated to https://ws.ts.net", blocking=False, fix="recheck",
        settings={"budget_url": "https://ws.ts.net"})])
    screen = JobScreen(app, budget.PLUGIN)
    screen.check_ready()
    assert saved == {"budget_url": "https://ws.ts.net", "sheet_id": "s"}
    assert not screen.blocked


def settings_screen(monkeypatch, check):
    """A budget settings screen with no page, its checks replaced by ``check``; returns (screen, saved)."""
    from taskdeck.jobs import budget
    from taskdeck.ui import settings_view

    saved = {}
    app = SimpleNamespace(job_settings=lambda job_id: {"budget_url": "https://old.ts.net"},
                          save_job_settings=lambda job_id, v: saved.update(v), toast=lambda m: None,
                          back=lambda: None, page=SimpleNamespace(run_thread=lambda fn: fn()))
    monkeypatch.setattr(type(budget.PLUGIN), "check_ready", lambda self, s: [])
    monkeypatch.setattr(type(budget.PLUGIN), "check_ready_online", lambda self, s: check(s))
    screen = settings_view.JobSettingsScreen(app, budget.PLUGIN)
    screen.view.update = lambda: None
    return screen, saved


def report_text(screen):
    import flet as ft

    return " ".join(c.value for c in screen.report.controls if isinstance(c, ft.Text))


def test_save_checks_first_and_keeps_working_settings(monkeypatch):
    from taskdeck.core import Problem

    def check(s):
        if s["budget_url"] == "https://good.ts.net":
            return [Problem.passed("Budget service at https://good.ts.net: 3 accounts")]
        return [Problem("The budget service didn't answer at https://bad.ts.net", fix="recheck")]

    screen, saved = settings_screen(monkeypatch, check)
    screen.fields["budget_url"].value = "bad.ts.net/upload"
    screen.save()
    assert saved == {}  # not saved: the old, working address stays
    assert "don't work yet" in report_text(screen) and "didn't answer" in report_text(screen)
    screen.save_anyway()
    assert saved["budget_url"] == "https://bad.ts.net"  # tidied, saved on request

    saved.clear()
    screen.fields["budget_url"].value = "good.ts.net"
    screen.test()
    assert saved == {} and "✓ Budget service at https://good.ts.net" in report_text(screen)
    screen.save()
    assert saved["budget_url"] == "https://good.ts.net"


def test_test_settings_fills_in_what_it_fixed(monkeypatch):
    from taskdeck.core import Problem

    screen, saved = settings_screen(monkeypatch, lambda s: [Problem(
        "Budget service address updated to https://ws.ts.net", blocking=False, fix="recheck",
        settings={"budget_url": "https://ws.ts.net"})])
    screen.fields["budget_url"].value = "http://100.83.88.59:8092"
    screen.test()
    assert screen.fields["budget_url"].value == "https://ws.ts.net" and saved == {}
    assert "Tap Save to keep these settings" in report_text(screen)


def test_job_without_inputs_and_shared_settings(tmp_path):
    from taskdeck.jobs.daily_log import reconcile_job

    screen = JobScreen(fake_app(tmp_path), reconcile_job.PLUGIN)
    assert screen.fields == {}
    assert reconcile_job.PLUGIN.settings_id == "daily_log"


def test_background_task_shows_its_progress_and_outcome(tmp_path):
    """A job that hands long work to a background task (Parse executable's Drive upload) gets Extract
    Archive's progress: a bar per phase while it runs (Run disabled), then the outcome with Open in Drive."""
    import flet as ft

    from taskdeck.core import TaskManager
    from taskdeck.jobs import exe_parse
    from taskdeck.shared.gdrive import DriveLocation, Folder

    app = fake_app(tmp_path)
    app.tasks = TaskManager(start_thread=lambda fn: fn())
    seen = {}

    def work(t):
        t.set_phases(("Checking what's already in Google Drive", 3, "files"), ("Uploading to Google Drive", 3, "files"))
        t.advance(1, 1)
        t.result = DriveLocation().child(Folder("f1", "GAME.EXE"))
        seen["running"] = [c.value for c in screen.task_box.controls if isinstance(c, ft.Text)]
        seen["run_disabled"] = screen.blocked
        return "Uploaded 3 files to Google Drive › My Drive › GAME.EXE."

    screen = JobScreen(app, exe_parse.PLUGIN)
    screen.view.update = lambda: None
    task = screen._start_task("Uploading GAME.EXE to Google Drive", work)
    assert task.status == "done"
    assert "Uploading GAME.EXE to Google Drive" in seen["running"] and seen["run_disabled"]
    card = screen.task_box.controls[0].content.controls
    assert card[0].controls[1].value == task.message
    assert [b.content for b in card[1].controls] == ["Open in Drive", "Close"]
    assert not screen.blocked
    screen.dismiss_task()
    assert screen.task_box.controls == []


def run_unlocking(tmp_path, plugin, raw, locked: bool, fingerprint_ok: bool):
    """JobScreen.run up to the account unlock; returns (unlock asked, the job ran)."""
    import asyncio

    asked, ran = [], []
    app = fake_app(tmp_path)
    app.vault = SimpleNamespace(needs_unlock=lambda sid: locked and sid == "account:google")

    async def unlock(then=None):
        asked.append(True)
        return fingerprint_ok

    app.unlock = unlock
    screen = JobScreen(app, plugin)
    screen.view.update = lambda: None
    screen.raw_inputs = lambda: raw
    screen._set_running = lambda running: ran.append(running)  # the run gets this far only after unlocking
    screen.app.page = SimpleNamespace(update=lambda: None)
    screen.app.job_settings = lambda job_id: {}
    screen.plugin = SimpleNamespace(**{k: getattr(plugin, k) for k in ("id", "settings_id", "accounts_needed")},
                                    run=lambda *a, **k: RunReport(False, None, [], "stop"), has_commit=False)
    screen.app.history = SimpleNamespace(add=lambda *a: None)
    screen.app.on_history_changed = lambda: None
    asyncio.run(screen.run())
    return bool(asked), bool(ran)


def test_run_asks_for_the_fingerprint_when_it_signs_in_with_a_locked_account(tmp_path):
    from taskdeck.jobs import exe_parse

    drive_on = {"exe": "x.exe", "drive": True}
    assert run_unlocking(tmp_path, exe_parse.PLUGIN, drive_on, locked=True, fingerprint_ok=True) == (True, True)
    assert run_unlocking(tmp_path, exe_parse.PLUGIN, drive_on, locked=True, fingerprint_ok=False) == (True, False)
    assert run_unlocking(tmp_path, exe_parse.PLUGIN, drive_on, locked=False, fingerprint_ok=True) == (False, True)
    off = {"exe": "x.exe", "drive": False}  # no Drive, no Google account: never asks
    assert run_unlocking(tmp_path, exe_parse.PLUGIN, off, locked=True, fingerprint_ok=False) == (False, True)


def test_file_fields_browse_with_taskdecks_own_browser_and_notes_follow_the_inputs(tmp_path, monkeypatch):
    """Browse opens TaskDeck's file browser (real paths, only the field's file types); picking a file updates
    the notes under the fields (Parse executable: the Drive path under its switch)."""
    from taskdeck.jobs import exe_parse
    from taskdeck.ui import job_view

    opened = {}
    monkeypatch.setattr(job_view.FileBrowser, "pick_file",
                        classmethod(lambda cls, app, on_pick, **kw: opened.update(kw, on_pick=on_pick)))
    app = fake_app(tmp_path)
    app.job_settings = lambda job_id: {"drive_folder": "RE"}
    screen = JobScreen(app, exe_parse.PLUGIN)
    screen.view.update = lambda: None
    assert screen.notes["drive"].value == "Would go to Google Drive › My Drive › RE › _EXECUTABLE"

    fc = screen.fields["exe"]
    fc.input.update = lambda: None
    screen._browse_file(fc)
    assert opened["title"] == "Choose Executable"
    assert opened["accept"]("WIZARDS.EXE") and opened["accept"]("x.ovl") and not opened["accept"]("x.dll")
    opened["on_pick"](tmp_path / "MOM" / "WIZARDS.EXE")
    assert fc.value == str(tmp_path / "MOM" / "WIZARDS.EXE")
    assert screen.notes["drive"].value == "Would go to Google Drive › My Drive › RE › _WIZARDS"
    screen.fields["drive"].input.value = True
    screen.invalidate()
    assert screen.notes["drive"].value == "Goes to Google Drive › My Drive › RE › _WIZARDS"
    assert screen.notes["drive"].visible and not screen.notes["exe"].visible
    assert screen.notes["output"].value == f"Saves into {tmp_path / 'MOM' / '_WIZARDS'}"

"""The job screen keeps a draft of its inputs (no running Flet page needed)."""

from types import SimpleNamespace

from taskdeck.core import Drafts, JsonStore
from taskdeck.jobs import daily_log
from taskdeck.ui.job_view import JobScreen

LOG = "Friday October 16th\nStart time 10:38\n13 orders for $187.82"


def fake_app(tmp_path):
    store = JsonStore(tmp_path / "state.json")
    return SimpleNamespace(
        store=store, drafts=Drafts(store),
        favorites=SimpleNamespace(is_pinned=lambda job_id: False),
        palette=None, open_settings=lambda job_id: None,
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
    screen.app.page = SimpleNamespace(schedule_update=lambda: None)
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


def test_job_without_inputs_and_shared_settings(tmp_path):
    from taskdeck.jobs.daily_log import reconcile_job

    screen = JobScreen(fake_app(tmp_path), reconcile_job.PLUGIN)
    assert screen.fields == {}
    assert reconcile_job.PLUGIN.settings_id == "daily_log"

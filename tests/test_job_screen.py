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


def test_draft_kept_when_parse_fails_and_dropped_when_it_succeeds(tmp_path):
    from taskdeck.core import RunReport

    app = fake_app(tmp_path)
    screen = JobScreen(app, daily_log.PLUGIN)
    screen.fields["log"].input.value = LOG
    run_screen(screen, RunReport(False, None, [], "Gemini: didn't answer"))
    assert app.drafts.get("daily_log") == {"log": LOG}  # error: still there
    run_screen(screen, RunReport(True, None, []))
    assert app.drafts.get("daily_log") == {}  # parsed: dropped
    assert screen.fields["log"].value == LOG  # the text box itself is untouched

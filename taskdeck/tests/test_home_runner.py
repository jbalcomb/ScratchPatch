"""Home Jobs and Discord: Capture Channel, against a fake Home Runner."""

from datetime import datetime, timezone

import pytest

from taskdeck.core import StepError
from taskdeck.jobs.home_runner import client, discord_job, status_job
from taskdeck.shared.http import HttpError

SETTINGS = {"runner_url": "https://ws.tail.ts.net:8765", "runner_key": "k"}
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

CHANNELS = [
    {"id": "100000000000000001", "name": "general", "type": "text", "guild_id": "9", "guild": "Home",
     "messages": 1234, "newest_at": "2026-10-24T11:00:00+00:00", "backfill": "partial", "error": None},
    {"id": "100000000000000002", "name": "general", "type": "text", "guild_id": "8", "guild": "Work",
     "messages": 0, "newest_at": None, "backfill": "not started", "error": None},
    {"id": "100000000000000003", "name": "dev-log", "type": "text", "guild_id": "8", "guild": "Work",
     "messages": 50, "newest_at": None, "backfill": "done", "error": None},
]


@pytest.fixture
def runner(monkeypatch):
    """Records calls; answers like Home Runner would."""
    calls = []

    def fake_request(method, url, json_body=None, headers=None, timeout=30):
        calls.append((method, url, json_body, headers))
        path = url.removeprefix(SETTINGS["runner_url"])
        if path == "/jobs":
            return [{"name": "discord-backfill", "title": "Discord: capture a channel's full history"}]
        if path == "/runs" and method == "POST" and json_body["job"] == "discord-channels":
            return {"id": 1, "status": "succeeded", "result": CHANNELS}
        if path == "/runs" and method == "POST":
            return {"id": 7, "job": json_body["job"], "status": "queued", "ahead": 1}
        if path.startswith("/runs?"):
            return [{"id": 7, "job": "discord-backfill", "status": "running", "params": {},
                     "started_at": "2026-10-24T11:50:00+00:00", "last_line": "channel 1: saved 100 messages"}]
        if path.startswith("/runs/7?"):
            return {"id": 7, "job": "discord-backfill", "status": "running", "params": {"channel": "1"},
                    "created_at": "2026-10-24T11:49:00+00:00", "started_at": "2026-10-24T11:50:00+00:00",
                    "finished_at": None, "exit_code": None, "log": ["a", "b"]}
        if path == "/runs/7/cancel":
            return {"id": 7, "status": "running"}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)
    return calls


@pytest.mark.parametrize("query,expected", [
    ("#dev-log", "100000000000000003"),
    ("  DEV-LOG ", "100000000000000003"),
    ("Home / #general", "100000000000000001"),
    ("work/general", "100000000000000002"),
    ("100000000000000002", "100000000000000002"),
])
def test_match_channel(query, expected):
    assert discord_job.match_channel(query, CHANNELS)["id"] == expected


@pytest.mark.parametrize("query,message", [
    ("general", "More than one channel"),
    ("dev-logs", "Did you mean: #dev-log"),
    ("199999999999999999", "No channel with that ID"),
])
def test_match_channel_explains_misses(query, message):
    with pytest.raises(StepError, match=message):
        discord_job.match_channel(query, CHANNELS)


def test_match_channel_with_nothing_discovered():
    with pytest.raises(StepError, match="hasn't recorded any channels"):
        discord_job.match_channel("general", [])


def test_preview_then_start_capture(runner):
    job = discord_job.PLUGIN
    report = job.run({"channel_query": "Home/general", "mode": discord_job.FULL}, settings=SETTINGS)
    assert report.ok, report.error
    rows = dict(report.result.rows)
    assert rows["Captured so far"] == "1,234 messages" and rows["History"] == "partial"
    assert report.result.payload == {"job": "discord-backfill", "channel": "100000000000000001", "name": "general"}
    assert runner[0][3] == {"Authorization": "Bearer k"}

    ok, message = job.try_commit(report.result.payload, SETTINGS)
    assert ok and message.startswith("Run #7 queued, 1 run(s) ahead")
    assert runner[-1][2] == {"job": "discord-backfill", "params": {"channel": "100000000000000001"}}


def test_new_only_on_unfinished_history_warns(runner):
    report = discord_job.PLUGIN.run({"channel_query": "Work / general", "mode": discord_job.NEW}, settings=SETTINGS)
    assert report.result.payload["job"] == "discord-sync"
    assert "isn't complete" in report.result.warning


def test_status_list_and_detail(runner):
    report = status_job.PLUGIN.run({"run": ""}, settings=SETTINGS)
    assert report.ok, report.error
    [(label, value)] = report.result.rows
    assert label == "#7 Discord: capture a channel's full history" and "saved 100 messages" in value
    assert report.result.payload is None  # no Cancel button on the list

    report = status_job.PLUGIN.run({"run": "#7"}, settings=SETTINGS)
    assert report.result.text == "a\nb" and report.result.payload == 7
    assert status_job.PLUGIN.try_commit(7, SETTINGS) == (True, "Stopping run #7.")


def test_status_line():
    run = {"status": "running", "started_at": "2026-10-24T09:30:00+00:00"}
    assert status_job.status_line(run, NOW) == "running for 2 h 30 min"
    assert status_job.status_line({"status": "queued", "ahead": 2}, NOW) == "queued, 2 ahead"
    assert client.ago("2026-10-20T10:00:00+00:00", NOW) == "4 days ago"
    assert status_job.status_line({"status": "failed", "finished_at": "2026-10-24T11:55:00+00:00"}, NOW) == "failed 5 min ago"


def test_wrong_key_and_unreachable_runner_explain_themselves(monkeypatch):
    def refuse(*a, **kw):
        raise HttpError("HTTP 401: missing or wrong key", 401)

    monkeypatch.setattr(client, "request", refuse)
    report = status_job.PLUGIN.run({}, settings=SETTINGS)
    assert not report.ok and "refused the runner key" in report.error

    def offline(*a, **kw):
        raise HttpError("Can't reach ws.tail.ts.net:8765: timed out")

    monkeypatch.setattr(client, "request", offline)
    assert "Tailscale connected" in client.test_connection(SETTINGS)


def test_settings_are_shared_and_required():
    assert discord_job.PLUGIN.settings_id == status_job.PLUGIN.id
    problems = status_job.PLUGIN.check_ready({"runner_url": "http://localhost:8765"})
    assert any("Runner key" in p.message for p in problems)
    assert any("phone itself" in p.message for p in problems)

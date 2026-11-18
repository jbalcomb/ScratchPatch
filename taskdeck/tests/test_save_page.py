"""Save web page: queue on the storage machine's chromers daemon, follow it, show the files (fake daemon)."""

import json

import pytest

from taskdeck.jobs import save_page
from taskdeck.jobs.save_page import PLUGIN
from taskdeck.shared import http

SETTINGS = {"daemon_url": "http://100.83.88.59:7700", "token": "t0k", "root": "web"}


class FakeDaemon:
    def __init__(self, polls_before_done=1, fail=None):
        self.calls, self.polls, self.fail = [], polls_before_done, fail

    def __call__(self, method, url, json_body=None, timeout=30, headers=None, **kw):
        command = url.rsplit("/", 1)[1]
        self.calls.append((command, json_body, headers))
        if headers.get("Authorization") != "Bearer t0k":
            raise http.HttpError("HTTP 401", 401, json.dumps({"ok": False, "error": "unauthorized"}))
        if command == "save.roots":
            return {"ok": True, "result": [{"name": "web", "folder": "/home/me/Archive/web"}]}
        if command == "save.queue":
            if ".." in json_body["folder"]:
                raise http.HttpError("HTTP 400", 400, json.dumps({"ok": False, "error": "folder must stay inside the root"}))
            return {"ok": True, "result": {"id": "j1", "status": "queued", "url": json_body["url"],
                                           "folder": json_body["folder"], "files": []}}
        if command == "save.status":
            self.polls -= 1
            if self.polls > 0:
                return {"ok": True, "result": {"id": "j1", "status": "running", "folder": "recipes", "files": []}}
            if self.fail:
                return {"ok": True, "result": {"id": "j1", "status": "failed", "error": self.fail, "files": []}}
            return {"ok": True, "result": {"id": "j1", "status": "done", "title": "Tomato Soup", "folder": "recipes",
                                           "files": [{"path": "/home/me/Archive/web/recipes/Tomato Soup 2026-11-18.mhtml",
                                                      "bytes": 20480}]}}
        raise AssertionError(command)


@pytest.fixture
def daemon(monkeypatch):
    d = FakeDaemon()
    monkeypatch.setattr(http, "request", d)
    monkeypatch.setattr(save_page.time, "sleep", lambda s: None)
    return d


def test_saves_and_shows_the_files(daemon):
    daemon.polls = 3
    report = PLUGIN.run({"url": "example.com/soup", "folder": "/recipes/", "mhtml": True, "pdf": True}, settings=SETTINGS)
    assert report.ok, report.error
    queued = [b for c, b, _ in daemon.calls if c == "save.queue"][0]
    assert queued == {"url": "example.com/soup", "root": "web", "folder": "recipes", "formats": ["mhtml", "pdf"]}
    assert report.result.title == "Saved"
    assert ("Saved in", "web/recipes") in report.result.rows
    assert any(k == "MHTML" and "20 KB" in v for k, v in report.result.rows)


def test_errors_are_readable(daemon):
    r = PLUGIN.run({"url": "x.com", "folder": "../etc", "mhtml": True}, settings=SETTINGS)
    assert not r.ok and "folder must stay inside the root" in r.error
    r = PLUGIN.run({"url": "x.com", "mhtml": False, "pdf": False}, settings=SETTINGS)
    assert not r.ok and "Pick at least one format" in r.error
    daemon.fail = "net::ERR_NAME_NOT_RESOLVED at https://nope.invalid"
    r = PLUGIN.run({"url": "nope.invalid", "mhtml": True}, settings=SETTINGS)
    assert not r.ok and "couldn't save the page: net::ERR_NAME_NOT_RESOLVED" in r.error


def test_still_running_is_left_to_the_machine(daemon, monkeypatch):
    daemon.polls = 10**6
    monkeypatch.setattr(save_page, "WAIT_SECONDS", 0)
    r = PLUGIN.run({"url": "x.com", "mhtml": True}, settings=SETTINGS)
    assert r.ok and r.result.title == "Queued on the storage machine" and ("Job", "j1") in r.result.rows


def test_readiness(daemon, monkeypatch):
    assert PLUGIN.check_ready_online(SETTINGS) == []
    assert "refused the token" in PLUGIN.check_ready_online({**SETTINGS, "token": "x"})[0].message
    assert "no save root “music”" in PLUGIN.check_ready_online({**SETTINGS, "root": "music"})[0].message
    assert "must start with http://" in PLUGIN.check_ready({**SETTINGS, "daemon_url": "100.83.88.59:7700"})[0].message
    assert [p.message for p in PLUGIN.check_ready({**SETTINGS, "token": ""})]  # required

    def offline(*a, **k):
        raise http.HttpError("Can't reach 100.83.88.59:7700: timed out")
    monkeypatch.setattr(http, "request", offline)
    monkeypatch.setattr(save_page.tailscale, "why_unreachable", lambda url: "this phone isn't connected to Tailscale")
    assert "Can't reach the storage machine: this phone isn't connected to Tailscale" in \
        PLUGIN.check_ready_online(SETTINGS)[0].message


def test_old_chromers_without_save_commands(monkeypatch):
    def old(*a, **k):  # what a chromers daemon without the save.* commands answers
        raise http.HttpError("HTTP 400", 400, json.dumps({"ok": False, "error": 'unknown command "save.roots"'}))
    monkeypatch.setattr(http, "request", old)
    assert "doesn't know how to save pages yet" in PLUGIN.check_ready_online(SETTINGS)[0].message

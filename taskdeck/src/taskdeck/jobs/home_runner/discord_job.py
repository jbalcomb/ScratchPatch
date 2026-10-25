"""Discord: Capture Channel. Finds the channel in the workstation's capture database,
shows how far its capture has got, then starts a backfill or sync there."""

import difflib
import re

from taskdeck.core import Field, JobPlugin, Pipeline, Result, Stage, StepError, step
from taskdeck.shared import steps

from . import client
from .config import SETTINGS, SETTINGS_ACTIONS, SETTINGS_JOB, url_problems
from .status_job import online_problems

FULL, NEW = "Full history", "New messages only"
SNOWFLAKE = re.compile(r"\d{17,20}")


@step(Stage.EXECUTE, name="find the channel")
def find(ctx):
    """Ask the workstation which channels it knows, and pick the one asked for."""
    known = client.run_quick(ctx.settings, "discord-channels")
    ctx["channel"] = match_channel(ctx["channel_query"], known)


@step(Stage.PRESENT)
def preview(ctx):
    c, mode = ctx["channel"], ctx["mode"]
    job = "discord-backfill" if mode == FULL else "discord-sync"
    warning = None
    if mode == FULL and c["backfill"] == "done":
        warning = "Its history is already complete, so this only fetches new messages."
    elif mode == NEW and c["backfill"] != "done":
        warning = "Its history isn't complete yet. Choose Full history to fetch the older messages too."
    rows = [("Server", c.get("guild") or "?"), ("Channel", f"#{c['name']} ({c['type']})"),
            ("Captured so far", f"{c['messages']:,} message{'' if c['messages'] == 1 else 's'}"), ("History", c["backfill"])]
    if c.get("newest_at"):
        rows.append(("Newest captured", f"{client.ago(c['newest_at'])} ({c['newest_at'][:16].replace('T', ' ')} UTC)"))
    if c.get("error"):
        rows.append(("Last error", c["error"]))
    rows.append(("Will run", f"{mode} (job {job})"))
    ctx.result = Result(title=f"#{c['name']}", rows=rows, warning=warning,
                        payload={"job": job, "channel": c["id"], "name": c["name"]})


def match_channel(query, channels):
    """Find one channel by ID, name or "Server / name". Names ignore case and a leading #."""
    if not channels:
        raise StepError("The workstation hasn't recorded any channels yet. The hourly sync finds them; "
                        "or run `python -m discord_capture channels` there.")
    query = query.strip()
    if SNOWFLAKE.fullmatch(query):
        hits = [c for c in channels if c["id"] == query]
        if not hits:
            raise StepError("No channel with that ID. The bot may not see it, or it hasn't been discovered yet.")
        return hits[0]
    server, _, name = query.rpartition("/")
    name = _norm(name)
    hits = [c for c in channels if _norm(c["name"]) == name
            and (not server.strip() or _norm(c.get("guild")) == _norm(server))]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise StepError("More than one channel is called that. Use “Server / #name” or the ID: "
                        + "; ".join(f"{c.get('guild')} / #{c['name']} ({c['id']})" for c in hits))
    close = difflib.get_close_matches(name, sorted({_norm(c["name"]) for c in channels}), n=5, cutoff=0.5)
    raise StepError("No channel called that." + (f" Did you mean: {', '.join('#' + n for n in close)}?" if close else ""))


def _norm(text):
    return (text or "").strip().lstrip("#").strip().casefold()


class DiscordCapture(JobPlugin):
    id = "discord_capture"
    title = "Discord: Capture Channel"
    description = "Have the workstation save a Discord channel's messages: its full history, or just what's new."
    category = "Home"
    icon = "FORUM_OUTLINED"
    tags = ("discord", "channel", "messages", "archive", "backfill", "sync", "capture", "workstation")
    fields = (
        Field.text("channel_query", "Channel", required=True,
                   help="Name (#general), “Server / #general” if names repeat, or the channel ID."),
        Field.choice("mode", "Fetch", (FULL, NEW)),
    )
    settings = SETTINGS
    settings_from = SETTINGS_JOB
    settings_actions = SETTINGS_ACTIONS
    run_label = "Find channel"
    commit_label = "Start capture"
    committed_label = "Started"

    def check_ready(self, settings):
        return super().check_ready(settings) + url_problems(settings)

    def check_ready_online(self, settings):
        return online_problems(settings)

    def build_pipeline(self):
        return Pipeline([steps.require("channel_query"), find, preview])

    def commit(self, payload, settings):
        run = client.start(settings, payload["job"], {"channel": payload["channel"]})
        where = f", {run['ahead']} run(s) ahead of it" if run.get("ahead") else ""
        return f"Run #{run['id']} {run['status']}{where}. Follow it in Home Jobs (run {run['id']})."


PLUGIN = DiscordCapture()

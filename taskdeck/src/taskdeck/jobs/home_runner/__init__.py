"""Jobs that run on the workstation through Home Runner (home-runner/ in this repo):
Home Jobs (status, logs, cancel) and Discord: Capture Channel."""

from . import discord_job, status_job

PLUGIN = status_job.PLUGIN
PLUGINS = (status_job.PLUGIN, discord_job.PLUGIN)

"""Settings shared by the Home Runner jobs (stored once, under Home Jobs)."""

from taskdeck.core import Field, Problem, SettingsAction

from . import client

SETTINGS_JOB = "home_jobs"

SETTINGS = (
    Field.text("runner_url", "Runner URL", required=True,
               help="The workstation's Home Runner, e.g. https://workstation.your-tailnet.ts.net:8765 "
                    "(the install script prints it)."),
    Field.text("runner_key", "Runner key", secret=True, required=True,
               help="Printed by the install script; on the workstation, `python -m home_runner key` shows it again."),
)
SETTINGS_ACTIONS = (SettingsAction("Test connection", client.test_connection, icon="NETWORK_CHECK"),)


def url_problems(settings):
    url = (settings.get("runner_url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return [Problem("The Runner URL must start with https:// (or http://).")]
    if "localhost" in url or "127.0.0.1" in url:
        return [Problem("The Runner URL points at the phone itself. Use the workstation's Tailscale name or address.")]
    return []

"""Send to Devices: put text on your other devices through Courier (courier/ in this repo).

Each device's own policy decides what happens: the clip channel copies to the clipboard, others
notify or land in an inbox. The message is signed on the phone and screened by the gateway; it is
never kept in TaskDeck's run history.
"""

from taskdeck.core import Field, JobPlugin, Pipeline, Problem, QuickAction, Result, SettingsAction, Stage, StepError, step
from taskdeck.shared.http import reachable

from . import client

LABELS = {
    "trailing_newline_removed": "trailing line break removed",
    "multiline": "more than one line",
    "shell_chars": "shell characters",
    "secret": "looks like a secret",
    "has_url": "has a link",
}


@step(Stage.VALIDATE)
def check_inputs(ctx):
    channel = (ctx.get("channel") or "").strip().lower()
    if not client.CHANNEL.match(channel):
        raise StepError("Channel names are lowercase letters, digits and dashes, like clip or agent-inbox.")
    if not (ctx.get("message") or "").strip():
        raise StepError("Type or paste the message to send.")
    ctx["channel_name"] = channel
    ctx["creds"] = client.parse_credentials(ctx.settings.get("credentials") or "")


@step(Stage.PREPARE, name="sign")
def sign(ctx):
    ctx["submission"] = client.build_submission(ctx["creds"], ctx["channel_name"], ctx["message"])


@step(Stage.EXECUTE, name="send to the gateway")
def send(ctx):
    ctx["reply"] = client.post(ctx.settings, ctx["creds"], ctx["submission"])


@step(Stage.PRESENT)
def present(ctx):
    reply = ctx["reply"]
    labels = [LABELS.get(l, l) for l in reply.get("labels", []) if not l.startswith("origin_")]
    ctx.result = Result(
        title=f"Sent to {reply['channel']}" + (" (already sent)" if reply.get("duplicate") else ""),
        rows=[("Channel", reply["channel"]),
              ("Screening", ", ".join(labels) if labels else "nothing flagged"),
              ("Message id", reply["id"])],
        text="Each device does what its own policy allows for this channel; a clipboard device "
             "refuses multi-line text and secrets unless told otherwise.",
    )


class SendToDevices(JobPlugin):
    id = "courier_send"
    title = "Send to Devices"
    description = "Send text to your other devices through Courier: their clipboard, a notification or an inbox."
    category = "Home"
    icon = "SEND_TO_MOBILE"
    tags = ("courier", "clipboard", "copy", "paste", "share", "push", "notify", "devices", "ntfy", "tailscale")
    fields = (
        Field.text("channel", "Channel", default="clip",
                   help="clip copies to the clipboard on devices that allow it; agent-inbox notifies."),
        Field.multiline("message", "Message", required=True, lines=4, private=True),
    )
    quick_actions = (
        QuickAction("Send to clipboards", {"channel": "clip"}),
        QuickAction("Send to agent inbox", {"channel": "agent-inbox"}),
    )
    settings = (
        Field.text("credentials", "Sender credentials", secret=True, required=True,
                   help="On the gateway machine: python -m courier add-sender phone --kind human "
                        "--channels clip,agent-inbox --paste-string. Paste the line it prints."),
        Field.text("gateway_url", "Gateway URL",
                   help="e.g. https://workstation.your-tailnet.ts.net:8766. Blank uses the one in the credentials."),
    )
    settings_actions = (SettingsAction("Test connection", client.test_connection, icon="NETWORK_CHECK"),)
    run_label = "Send"

    def check_ready(self, settings):
        problems = super().check_ready(settings)
        if problems:
            return problems
        try:
            creds = client.parse_credentials(settings.get("credentials") or "")
            url = client.gateway_url(settings, creds)
        except StepError as exc:
            return [Problem(str(exc))]
        if not url.startswith(("https://", "http://")):
            return [Problem("The Gateway URL must start with https://.")]
        if "localhost" in url or "127.0.0.1" in url:
            return [Problem("The Gateway URL points at the phone itself. Use the gateway machine's Tailscale name.")]
        return []

    def check_ready_online(self, settings):
        try:
            url = client.gateway_url(settings, client.parse_credentials(settings.get("credentials") or ""))
        except StepError:
            return []
        if not reachable(url, 3):
            return [Problem("The gateway isn't answering. Is its machine on, and Tailscale connected on this phone?",
                            fix="recheck")]
        return []

    def build_pipeline(self):
        return Pipeline([check_inputs, sign, send, present])


PLUGIN = SendToDevices()

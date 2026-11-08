"""`python -m courier <command>`.

  serve           start the gateway (default 127.0.0.1:8766; put `tailscale serve` in front of it)
  gateway-key     print the gateway's public key, creating the key first if needed
  ntfy-setup      write ntfy's server.yml and the gateway (write) and device (read) tokens
  ntfy-run        run ntfy with that config, without a console window, appending to a log
  add-sender      create a sender and write its credentials file (shown once, not kept here)
  remove-sender   revoke a sender
  senders         list senders, their kind and channels
  send            send a message with a credentials file: send CHANNEL TEXT (or - for stdin)
  device-init     write <config>/device.toml for this device's agent
  device          run the device agent (subscribe, verify, re-screen, act per device.toml)
  inbox           show the latest inbox entries
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from courier.desktop import Clipboard, Notifier
from courier.device import ConfigError, DeviceAgent, DeviceConfig, subscribe
from courier.envelope import make_submission
from courier.gateway import ORIGIN_KINDS
from courier.ntfy import publisher
from courier.ntfy_setup import ntfy_hasher, write_config
from courier.screen import Ruleset
from courier.server import RateLimit, Service, make_server
from courier.store import CONFIG_DIR, Senders, load_gateway_key, private_key_from, public_key_text, write_private

PROJECT = Path(__file__).resolve().parents[2]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="courier")
    parser.add_argument("--config", type=Path, default=CONFIG_DIR, help="key and senders folder (default %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the gateway")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--ntfy", default="http://127.0.0.1:2586", help="ntfy base URL (default %(default)s)")
    serve.add_argument("--ntfy-token-file", type=Path, help="file holding an ntfy access token, if ntfy needs one")
    serve.add_argument("--data", type=Path, default=PROJECT / "data", help="audit log folder")
    serve.add_argument("--max-envelope", type=int, default=4096, help="ntfy's message-size-limit (default 4096)")
    serve.add_argument("--rate", type=int, default=30, help="messages per sender per minute (default 30)")
    serve.add_argument("--log-file", help="append the log here instead of printing it")

    sub.add_parser("gateway-key", help="print the gateway's public key")

    setup = sub.add_parser("ntfy-setup", help="write ntfy's server.yml and the gateway and device tokens")
    setup.add_argument("--ntfy-exe", required=True, help="the ntfy binary, used to bcrypt the users' passwords")
    setup.add_argument("--listen", default="127.0.0.1:2586")

    run = sub.add_parser("ntfy-run", help="run ntfy with <config>/ntfy/server.yml, no console window, output to a log")
    run.add_argument("--ntfy-exe", required=True)
    run.add_argument("--log-file", type=Path, required=True)

    add = sub.add_parser("add-sender", help="create a sender")
    add.add_argument("name")
    add.add_argument("--kind", required=True, choices=sorted(ORIGIN_KINDS))
    add.add_argument("--channels", required=True, help="comma-separated channels it may post to")
    add.add_argument("--gateway", default="", help="gateway URL to record in the credentials file")
    add.add_argument("--out", type=Path, help="credentials file (default <config>/<name>.sender.json)")
    add.add_argument("--paste-string", action="store_true",
                     help="also print the credentials as one line to paste into TaskDeck's Send to Devices")

    remove = sub.add_parser("remove-sender", help="revoke a sender")
    remove.add_argument("name")

    sub.add_parser("senders", help="list senders")

    init = sub.add_parser("device-init", help="write <config>/device.toml for this device's agent")
    init.add_argument("--ntfy", required=True, help="ntfy URL, e.g. https://<machine>.<tailnet>.ts.net:2586")
    init.add_argument("--gateway-key", help="the gateway's public key (default: this machine's gateway)")
    init.add_argument("--token-file", default="ntfy-device.token", help="device token, relative to the config folder")
    init.add_argument("--force", action="store_true", help="replace an existing device.toml")

    device = sub.add_parser("device", help="run the device agent")
    device.add_argument("--data", type=Path, default=PROJECT / "data", help="inbox and device log folder")
    device.add_argument("--log-file", help="append the log here instead of printing it")

    inbox = sub.add_parser("inbox", help="show the latest inbox entries")
    inbox.add_argument("-n", type=int, default=10)
    inbox.add_argument("--data", type=Path, default=PROJECT / "data")

    send = sub.add_parser("send", help="send a message")
    send.add_argument("channel")
    send.add_argument("text", help="the message, or - to read stdin")
    send.add_argument("--creds", type=Path, required=True, help="a credentials file from add-sender")
    send.add_argument("--gateway", help="gateway URL (default: the one in the credentials file)")
    send.add_argument("--mode", default="fanout", choices=["fanout", "queue"])
    send.add_argument("--ttl", type=int, default=600, help="seconds until it expires (default 600)")
    send.add_argument("--content-type", default="text/plain")

    args = parser.parse_args(argv)
    senders = Senders(args.config / "senders.json")

    if args.command == "gateway-key":
        print(public_key_text(load_gateway_key(args.config / "gateway.key")))
        return 0

    if args.command == "ntfy-setup":
        path = write_config(args.config, ntfy_hasher(args.ntfy_exe), args.listen)
        print(path)
        return 0

    if args.command == "ntfy-run":
        # Under pythonw on Windows this keeps ntfy's console from popping up at logon. It exits
        # when ntfy does, so the scheduled task's watchdog (or systemd) brings both back.
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with args.log_file.open("a", encoding="utf-8") as log:
            command = [args.ntfy_exe, "serve", "--config", str(args.config / "ntfy" / "server.yml")]
            return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, creationflags=flags).returncode

    if args.command == "add-sender":
        channels = [c.strip() for c in args.channels.split(",") if c.strip()]
        try:
            creds = senders.add(args.name, args.kind, channels)
        except ValueError as error:
            print(f"courier: {error}", file=sys.stderr)
            return 2
        creds["gateway"] = args.gateway
        out = args.out or args.config / f"{args.name}.sender.json"
        write_private(out, json.dumps(creds, indent=2) + "\n")
        print(f"sender {args.name!r} ({args.kind}) may post to: {', '.join(sorted(set(channels)))}")
        print(f"credentials: {out}  (give this file to the sender; the gateway keeps only its public half)")
        if args.paste_string:
            line = base64.urlsafe_b64encode(json.dumps(creds, separators=(",", ":")).encode()).decode().rstrip("=")
            print("paste into TaskDeck (Send to Devices > settings > Sender credentials); it is a secret:")
            print(f"courier-sender:{line}")
        return 0

    if args.command == "remove-sender":
        if not senders.remove(args.name):
            print(f"courier: no sender named {args.name!r}", file=sys.stderr)
            return 1
        print(f"removed {args.name!r}; its secret stops working at once")
        return 0

    if args.command == "senders":
        for name, r in sorted(senders.records().items()):
            print(f"{name:24} {r['kind']:7} {', '.join(r['channels'])}")
        return 0

    if args.command == "send":
        return send_message(args)

    if args.command == "device-init":
        return device_init(args)

    if args.command == "inbox":
        path = args.data / "inbox.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()[-args.n:] if path.is_file() else []
        for line in lines:
            e = json.loads(line)
            body = e["body"] if e["body"] is not None else "(not stored: it looks like a secret)"
            print(f"{e['at']}  {e['channel']}  {e['sender']}  {','.join(e['labels'])}\n    {body}")
        return 0

    if args.command == "device":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            filename=args.log_file, encoding="utf-8")
        try:
            config = DeviceConfig.load(args.config / "device.toml")
        except ConfigError as error:
            logging.error("%s", error)
            return 2
        agent = DeviceAgent(config, Ruleset.load(), args.data, Clipboard(), Notifier())
        logging.info("courier device agent: %s -> %s", ", ".join(sorted(config.subscriptions)), config.ntfy)
        try:
            subscribe(config, agent, args.data / "device-state.json")
        except KeyboardInterrupt:
            pass
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        filename=args.log_file, encoding="utf-8")
    token = args.ntfy_token_file.read_text(encoding="utf-8").strip() if args.ntfy_token_file else None
    service = Service(
        Ruleset.load(), load_gateway_key(args.config / "gateway.key"), senders, publisher(args.ntfy, token),
        args.data, rate=RateLimit(args.rate, 60), max_envelope=args.max_envelope,
    )
    server = make_server(service, args.host, args.port)
    info = service.describe()
    logging.info("courier gateway on http://%s:%d -> %s; key %s; ruleset %s",
                 args.host, args.port, args.ntfy, info["public_key"], info["ruleset"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


DEVICE_TOML = """\
# Courier device agent. Each subscription sets the most this device will do with a channel's
# messages; nothing in a message can raise it. Restart the agent to apply changes.
ntfy = {ntfy}
token_file = {token_file}
gateway_keys = [{gateway_key}]
accept_rulesets = []  # pin screening ruleset hashes; empty accepts any

[[subscriptions]]
channel = "clip"
action = "clipboard"       # inbox | notify | clipboard | open-url
clear_after = 90           # seconds until the clipboard is cleared, if it still holds this; 0 keeps it
allow_multiline = false    # pasting a line break into a terminal runs the line
allow_shell_chars = true   # ; | & $ ` < > on a single line: nothing runs until you press Enter
toppings = [
  {{ filter = "drop-labels", labels = ["secret", "ai_injection", "ai_suspicious"] }},
]

[[subscriptions]]
channel = "agent-inbox"
action = "notify"
"""


def device_init(args) -> int:
    path = args.config / "device.toml"
    if path.exists() and not args.force:
        print(f"courier: {path} exists; pass --force to replace it", file=sys.stderr)
        return 1
    key = args.gateway_key
    if not key:
        try:
            key = public_key_text(load_gateway_key(args.config / "gateway.key", create=False))
        except FileNotFoundError:
            print("courier: no gateway on this machine; pass --gateway-key", file=sys.stderr)
            return 2
    if not (args.config / args.token_file).is_file():
        print(f"courier: warning: {args.config / args.token_file} doesn't exist yet; copy the device token there",
              file=sys.stderr)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEVICE_TOML.format(ntfy=json.dumps(args.ntfy.rstrip("/")), token_file=json.dumps(args.token_file),
                                       gateway_key=json.dumps(key)), encoding="utf-8", newline="\n")
    print(path)
    return 0


def send_message(args) -> int:
    creds = json.loads(args.creds.read_text(encoding="utf-8"))
    gateway = (args.gateway or creds.get("gateway") or "").rstrip("/")
    if not gateway:
        print("courier: no gateway URL; pass --gateway", file=sys.stderr)
        return 2
    text = sys.stdin.read() if args.text == "-" else args.text
    submission = make_submission(private_key_from(creds["private_key"]), args.channel, text,
                                 content_type=args.content_type, mode=args.mode, ttl_s=args.ttl)
    request = urllib.request.Request(f"{gateway}/messages", data=json.dumps(submission).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {creds['secret']}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.read().decode("utf-8"))
            return 0
    except urllib.error.HTTPError as error:
        print(f"courier: {error.code} {error.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"courier: cannot reach {gateway}: {error.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

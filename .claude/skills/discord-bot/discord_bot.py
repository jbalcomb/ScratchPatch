"""Helper for setting up a Discord bot. Standard library only.

  save-token   read a token from the clipboard (or stdin) into a 0600 file, without printing it
  check        who the bot is, which intents are on, which servers it's in
  intents      turn on the "limited" privileged intents (bots in fewer than 100 servers)
  invite-url   build the OAuth2 link a server admin clicks to add the bot

The token is never printed and never taken on the command line (it would land in
shell history and process listings). It comes from DISCORD_BOT_TOKEN or a file.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/jbalcomb/ScratchPatch, 0.1)"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "discord-capture" / "token"

# https://discord.com/developers/docs/resources/application#application-object-application-flags
INTENT_FLAGS = {
    "presence": (1 << 12, 1 << 13),         # (verified, limited)
    "guild-members": (1 << 14, 1 << 15),
    "message-content": (1 << 18, 1 << 19),
}
LIMITED_MASK = sum(limited for _, limited in INTENT_FLAGS.values())

# https://discord.com/developers/docs/topics/permissions#permissions-bitwise-permission-flags
PERMISSIONS = {
    "view-channel": 1 << 10,
    "read-message-history": 1 << 16,
    "send-messages": 1 << 11,
    "send-messages-in-threads": 1 << 38,
    "add-reactions": 1 << 6,
    "embed-links": 1 << 14,
    "attach-files": 1 << 15,
    "manage-threads": 1 << 34,               # needed to list private archived threads
}
PRESETS = {
    "read": ["view-channel", "read-message-history"],
    "read-private-threads": ["view-channel", "read-message-history", "manage-threads"],
    "chat": ["view-channel", "read-message-history", "send-messages", "send-messages-in-threads",
             "add-reactions", "embed-links", "attach-files"],
}


def request(token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    if data:
        headers["Content-Type"] = "application/json"
    for attempt in range(4):
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                time.sleep(1)  # this tool makes a handful of calls; no need to hurry them
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read() or b"{}")
            if error.code == 429 and attempt < 3:
                time.sleep(float(payload.get("retry_after", 5)) * 1.5 + 1)
                continue
            raise SystemExit(f"Discord said {error.code} for {method} {path}: {payload.get('message', payload)}")
    raise SystemExit("still rate limited; try again later")


def load_token(path):
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token and path.is_file():
        token = path.read_text(encoding="utf-8-sig")
    token = (token or "").strip()
    if not token:
        raise SystemExit(f"No token. Run `save-token` first, or set DISCORD_BOT_TOKEN. (looked in {path})")
    return token


def read_clipboard():
    if os.name == "nt":
        cmd = ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]
    elif sys.platform == "darwin":
        cmd = ["pbpaste"]
    elif os.environ.get("WAYLAND_DISPLAY"):
        cmd = ["wl-paste", "--no-newline"]
    else:
        cmd = ["xclip", "-o", "-selection", "clipboard"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def save_token(args):
    token = (sys.stdin.read() if args.stdin else read_clipboard()).strip()
    # A bot token is three base64url parts separated by dots; refuse anything else
    # rather than saving whatever happened to be on the clipboard.
    parts = token.split(".")
    if len(parts) != 3 or not all(parts) or any(c.isspace() for c in token):
        raise SystemExit("That doesn't look like a bot token (expected three dot-separated parts). Nothing saved.")
    path = args.token_file
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F", "SYSTEM:F"],
                       capture_output=True, check=True)
    me = request(token, "GET", "/users/@me")
    print(f"Saved a token for bot {me['username']} (id {me['id']}) to {path}")


def check(args):
    token = load_token(args.token_file)
    me = request(token, "GET", "/users/@me")
    app = request(token, "GET", "/applications/@me")
    guilds = request(token, "GET", "/users/@me/guilds")
    flags = app.get("flags") or 0
    print(f"Bot:          {me['username']} (user id {me['id']})")
    print(f"Application:  {app['name']} (client id {app['id']}), public bot: {app.get('bot_public')}")
    for name, (verified, limited) in INTENT_FLAGS.items():
        state = "on" if flags & (verified | limited) else "off"
        print(f"Intent {name + ':':<16} {state}")
    print(f"Servers ({len(guilds)}):")
    for g in guilds:
        print(f"  {g['id']:>20}  {g['name']}")
    if not guilds:
        print("  none yet: run `invite-url` and have a server admin open the link")


def intents(args):
    token = load_token(args.token_file)
    app = request(token, "GET", "/applications/@me")
    current = app.get("flags") or 0
    wanted = (current & LIMITED_MASK) | sum(INTENT_FLAGS[n][1] for n in args.names)
    # Only the limited intent bits may be sent back; Discord rejects the rest.
    updated = request(token, "PATCH", "/applications/@me", {"flags": wanted})
    flags = updated.get("flags") or 0
    for name in args.names:
        verified, limited = INTENT_FLAGS[name]
        print(f"{name}: {'on' if flags & (verified | limited) else 'STILL OFF'}")
    if any(not flags & sum(INTENT_FLAGS[n]) for n in args.names):
        print("Turn it on by hand: Developer Portal > your app > Bot > Privileged Gateway Intents.")


def invite_url(args):
    client_id = args.client_id or request(load_token(args.token_file), "GET", "/applications/@me")["id"]
    names = PRESETS[args.preset] + (args.add or [])
    query = {"client_id": client_id, "scope": "bot", "permissions": sum(PERMISSIONS[n] for n in set(names))}
    if args.guild:
        query |= {"guild_id": args.guild, "disable_guild_select": "true"}
    print("https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(query))
    print(f"Grants: {', '.join(sorted(set(names)))}")


def main():
    parser = argparse.ArgumentParser(prog="discord_bot")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("save-token")
    p.add_argument("--stdin", action="store_true", help="read from stdin instead of the clipboard")
    p.set_defaults(run=save_token)
    sub.add_parser("check").set_defaults(run=check)
    p = sub.add_parser("intents")
    p.add_argument("names", nargs="+", choices=sorted(INTENT_FLAGS))
    p.set_defaults(run=intents)
    p = sub.add_parser("invite-url")
    p.add_argument("--preset", choices=sorted(PRESETS), default="read")
    p.add_argument("--add", action="append", choices=sorted(PERMISSIONS), help="extra permission")
    p.add_argument("--guild", help="preselect this server in the invite")
    p.add_argument("--client-id", help="skip the token and use this application id")
    p.set_defaults(run=invite_url)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()

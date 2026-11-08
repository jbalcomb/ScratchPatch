# Courier

Carry short messages (text, links, small images) from a web page, a script or an AI agent to every
device you own, where a local agent decides what to do with each one: show it, put it on the
clipboard, open it, or hand it to a handler. **A message is data and never chooses its own action.**

Design doc: <https://claude.ai/artifact/4GTHDgziygR7nYupHYqdcj> (threat model, message format,
components, phase plan).

So far: the signed envelope, the fixed screening stage, the Phase A gateway that admits messages
over HTTP and publishes them to a private ntfy, and the device agent that acts on them (clipboard,
notifications, inbox, links), with install scripts for Windows, Linux and (device agent only) macOS.

```
sender ──make_submission──▶ gateway.admit ──signed envelope──▶ broker ──▶ agent.receive ──▶ toppings ──▶ action tier
            (sender_sig)      token check, screen, gw_sig                   verify, re-screen
```

| Module | What it does |
|---|---|
| `courier/ruleset.toml` | The fixed screening rules. Its SHA-256 is stamped into every envelope; agents can pin the hashes they accept. |
| `courier/screen.py` | `screen()`: reject, or normalize and label a body. Stages only add labels; the optional AI classifier runs last and can't vouch for anything. |
| `courier/envelope.py` | The envelope dataclass, canonical JSON, Ed25519 signing, ULIDs, `make_submission()` for senders. |
| `courier/gateway.py` | `admit()`: token scope, timestamps, sender signature, screening, gateway signature. |
| `courier/agent.py` | `receive()`: gateway signature, ruleset pin, expiry, replay window, re-screen with the local ruleset. `native_text()`: the only place LF becomes CRLF (Windows). |
| `courier/server.py` | The Phase A gateway HTTP API: `POST /messages`, per-sender rate limit, duplicate-ID handling, an audit log with no bodies. |
| `courier/store.py` | Gateway key and `senders.json` (public keys and secret hashes only; re-read when it changes). |
| `courier/ntfy.py` | Publishes admitted envelopes to a self-hosted ntfy topic per channel. |
| `courier/device.py` | The device agent: `device.toml` policy, toppings, action tiers and their guards, inbox, and the ntfy subscriber that resumes after the last message. |
| `courier/desktop.py` | Clipboard and notifications on Windows (Win32, kept out of clipboard history and cloud sync; WinRT toasts), macOS (pbcopy, osascript) and Linux (wl-copy or xclip, notify-send). |
| `courier/ntfy_setup.py` | Writes ntfy's private `server.yml`: no anonymous access, a write-only gateway token, a read-only device token. |
| `courier/__main__.py` | `python -m courier serve · gateway-key · add-sender · remove-sender · senders · send · ntfy-setup · ntfy-run · device-init · device · inbox`. |

Line endings: LF is canonical everywhere inside Courier (stored, signed, hashed, sent), like git's
object store. Agents convert to the host's form only when text leaves Courier.

## Install (Phase A: ntfy + gateway)

```powershell
# Windows: two logon tasks, "Courier ntfy" and "Courier Gateway", restarted within 5 minutes if they die
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```
```bash
# Linux: systemd user services courier-ntfy and courier-gateway (Restart=always)
bash scripts/install-linux.sh
```

The script creates `.venv`, downloads ntfy 2.35.0 into `bin/` (refused unless its SHA-256 matches
the pinned one), writes `~/.config/courier` (readable only by you), starts both services, and runs
`tailscale serve` so devices reach them at `https://<machine>.<tailnet>.ts.net:8766` (gateway) and
`:2586` (ntfy). It prints the gateway's public key and where the device token is. Rerunning it is
safe; keys, tokens and senders are kept. macOS can't host the broker (ntfy's server doesn't run
there), but it can run device agents.

ntfy is private: anonymous access is denied and its web app is off. The gateway holds a
write-only token (`ntfy-gateway.token`) and devices share a read-only one (`ntfy-device.token`),
so nothing reaches a channel except through the gateway. Per-device broker credentials come in
Phase B.

```powershell
cd src
..\.venv\Scripts\python -m courier add-sender laptop-agent --kind agent --channels clip,agent-inbox --gateway https://<machine>.<tailnet>.ts.net:8766
..\.venv\Scripts\python -m courier send clip "git status" --creds $HOME\.config\courier\laptop-agent.sender.json
```

`add-sender` writes the sender's credentials file once (private key + bearer secret); the gateway
keeps only the public key and the secret's SHA-256. `remove-sender` revokes it at once.
`gateway-key` prints the public key device agents verify envelopes with (also at `GET /gateway-key`).

| Status | Meaning |
|---|---|
| 202 | Admitted and published; the reply lists the labels |
| 200 | Same sender and id seen before; not published again |
| 400 / 422 | Invalid envelope or credentials scope / content rejected by screening |
| 401 | Unknown sender secret |
| 413 | Envelope over `--max-envelope` (ntfy's 4 KB message limit by default) |
| 429 | Over `--rate` messages per minute for this sender |
| 502 | ntfy unreachable; safe to retry with the same submission |

## Labels

Screening adds these; device policy acts on them.

| Label | Meaning |
|---|---|
| `trailing_newline_removed` | Body ended in whitespace holding a newline (would run on paste) |
| `ansi_removed`, `control_removed`, `cr_normalized` | Terminal escapes, control characters or a lone CR were removed |
| `bidi_removed`, `invisible_removed` | Direction overrides, zero-width, tag or variation-selector-supplement characters were removed |
| `multiline`, `shell_chars`, `long` | Shape of the text after normalization |
| `has_url`, `url_not_https`, `url_userinfo`, `url_idn`, `url_mixed_script` | Links and lookalike tricks |
| `secret` | Looks like a private key, cloud or API token, or a JWT |
| `origin_agent`, `origin_script`, `origin_web` | Set from the sender's token, never from the request |
| `ai_suspicious`, `ai_injection`, `ai_unavailable` | Optional classifier; `clean` adds nothing |

## Tests

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install "cryptography>=42" "pytest>=8"
.venv\Scripts\python -m pytest
```

## Device agent

Each device runs an agent that subscribes to ntfy with the read-only device token, verifies and
re-screens every envelope, runs the subscription's toppings, and does at most what
`~/.config/courier/device.toml` allows for that channel. Nothing in a message can raise it.

```powershell
# Windows: logon task "Courier Device" (it needs your session for the clipboard)
powershell -ExecutionPolicy Bypass -File scripts\install-device-windows.ps1 -Ntfy https://<broker>.<tailnet>.ts.net:2586 -GatewayKey <key>
```
```bash
# Linux: systemd user service tied to your desktop session; needs wl-copy or xclip, and notify-send
bash scripts/install-device-linux.sh https://<broker>.<tailnet>.ts.net:2586 <gateway key>
```
```bash
# macOS: LaunchAgent net.courier.device in your login session (KeepAlive); uses pbcopy and osascript
bash scripts/install-device-macos.sh https://<broker>.<tailnet>.ts.net:2586 <gateway key>
```

Copy `ntfy-device.token` from the broker machine's `~/.config/courier` first. On the broker
machine itself both arguments can be left out. `python -m courier inbox` shows what arrived.

| Action | What it does | Refused when |
|---|---|---|
| `inbox` | Appends to `data/inbox.jsonl` | Never; a `secret` body is not written to disk |
| `notify` | Desktop notification (first 240 characters) and the inbox | Never; a `secret` body is hidden |
| `clipboard` | Copies with native line endings; clears after `clear_after` s if still unchanged | Not text; `ai_injection`; `secret`, `multiline`, `shell_chars` unless allowed |
| `open-url` | Opens one https link in the browser | Anything but a single https URL; `url_*`, `ai_*` or `secret` labels |

When a clipboard or open-url action is refused, you get a notification saying why (never the
text itself). Toppings: filters `drop-labels`, `allow-origins`, `allow-senders`; mutations
`single-line`, `trim`, `max-length`. Labels about origin and content stay with a message whatever
the toppings do; guards about the body's shape (`multiline`, `shell_chars`, `long`) look at the
body actually being delivered, so `single-line` can make a message copyable. In Phase A a `queue`
message goes to every device like `fanout`; claiming arrives with NATS in Phase B.

## Send from the phone

TaskDeck's **Send to Devices** job signs submissions on the phone and posts them to the gateway.
Create its sender with `--paste-string` and paste the printed line into the job's settings:

```powershell
..\.venv\Scripts\python -m courier add-sender phone --kind human --channels clip,agent-inbox --gateway https://<machine>.<tailnet>.ts.net:8766 --paste-string
```

The line is the credentials file in one line (`courier-sender:` + base64), so treat it as a secret.

## Next

Phase A is complete. Phase B: NATS JetStream in place of ntfy (per-device credentials, queue
claiming), an MCP server so AI agents can send, and a native agent for phones.

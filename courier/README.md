# Courier

Carry short messages (text, links, small images) from a web page, a script or an AI agent to every
device you own, where a local agent decides what to do with each one: show it, put it on the
clipboard, open it, or hand it to a handler. **A message is data and never chooses its own action.**

Design doc: <https://claude.ai/artifact/4GTHDgziygR7nYupHYqdcj> (threat model, message format,
components, phase plan).

So far: the signed envelope, the fixed screening stage, and the Phase A gateway that admits
messages over HTTP and publishes them to ntfy.

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
| `courier/__main__.py` | `python -m courier serve · gateway-key · add-sender · remove-sender · senders · send`. |

Line endings: LF is canonical everywhere inside Courier (stored, signed, hashed, sent), like git's
object store. Agents convert to the host's form only when text leaves Courier.

## Run the gateway (Phase A)

The gateway listens on `127.0.0.1:8766` and publishes to ntfy (default `http://127.0.0.1:2586`);
put `tailscale serve` in front of it, as Home Runner does.

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python -m courier add-sender laptop-agent --kind agent --channels clip,agent-inbox --gateway https://<machine>.<tailnet>.ts.net:8766
.venv\Scripts\python -m courier serve --ntfy http://127.0.0.1:2586
.venv\Scripts\python -m courier send clip "git status" --creds $HOME\.config\courier\laptop-agent.sender.json
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

## Next (Phase A)

Install scripts (logon task / systemd user unit + `tailscale serve`) for the gateway and ntfy, a
desktop agent with a policy file and clipboard writes, and a TaskDeck job that sends.

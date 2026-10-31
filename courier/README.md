# Courier

Carry short messages (text, links, small images) from a web page, a script or an AI agent to every
device you own, where a local agent decides what to do with each one: show it, put it on the
clipboard, open it, or hand it to a handler. **A message is data and never chooses its own action.**

Design doc: <https://claude.ai/artifact/4GTHDgziygR7nYupHYqdcj> (threat model, message format,
components, phase plan).

This folder is the sketch of the two parts everything else depends on: the signed envelope and
the fixed screening stage.

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
| `courier/agent.py` | `receive()`: gateway signature, ruleset pin, expiry, replay window, re-screen with the local ruleset. |

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

Gateway HTTP server behind `tailscale serve`, token file and minting CLI, publish to self-hosted
ntfy, a desktop agent with a policy file and clipboard writes, a sender CLI and a TaskDeck job.

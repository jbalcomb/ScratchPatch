# ScratchPatch

A patch of scratch projects: proofs of concept and MVPs, each in its own folder.

| Project | What it is |
|---|---|
| [taskdeck/](taskdeck/) | TaskDeck: an Android app (Python/Flet) that runs pluggable jobs, with a command palette and pinned tiles. First real job: Daily Log → Google Sheet. |
| [tab-ledger/](tab-ledger/) | Tab Ledger: a Chrome extension that records every window and tab, summarizes each page, tracks scrolling and copy/paste, and writes a script that reopens them. |
| [home-runner/](home-runner/) | Home Runner: a small authenticated HTTP service on the workstation that runs a fixed list of jobs, so TaskDeck can start them over Tailscale. |
| [discord-capture/](discord-capture/) | Discord Capture: pulls Discord server messages into SQLite, with a resumable historic backfill and a scheduled sync that stays far inside the rate limits. |

## Conventions

* One top-level folder per project, with its own README, dependencies and tests. Nothing project-specific at the root.
* GitHub Actions workflows must live in `.github/workflows/` at the root, so each is named after its
  project (`taskdeck-ci.yml`, ...), runs with `working-directory` set to the project folder, and only
  triggers on changes under that folder.

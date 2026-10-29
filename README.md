# ScratchPatch

A patch of scratch projects: proofs of concept and MVPs, each in its own folder.

| Project | What it is |
|---|---|
| [taskdeck/](taskdeck/) | TaskDeck: an Android app (Python/Flet) that runs pluggable jobs, with a command palette and pinned tiles. First real job: Daily Log → Google Sheet. |
| [budget/](budget/) | Budget: upload Bank of America exports from the phone; transactions land in SQLite, categorized by a hand-edited rules file, with balance checks and undo. Runs in Docker on the AI Workstation behind Tailscale. |
| [tab-ledger/](tab-ledger/) | Tab Ledger: a Chrome extension that records every window and tab, summarizes each page, tracks scrolling and copy/paste, and writes a script that reopens them. |
| [asset-forge/](asset-forge/) | Asset Forge: AI-generated replacement art packs for ReMoM. Built so far: an LBX-to-PNG exporter, sprite descriptions and prompts from a local vision model, and SDXL generation fitted to the game palette. Planned: style guides and a generate/judge/refine loop. |

## Conventions

* One top-level folder per project, with its own README, dependencies and tests. Nothing project-specific at the root.
* GitHub Actions workflows must live in `.github/workflows/` at the root, so each is named after its
  project (`taskdeck-ci.yml`, ...), runs with `working-directory` set to the project folder, and only
  triggers on changes under that folder.

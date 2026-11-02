# ScratchPatch

A patch of scratch projects: proofs of concept and MVPs, each in its own folder.

| Project | What it is |
|---|---|
| [taskdeck/](taskdeck/) | TaskDeck: an Android app (Python/Flet) that runs pluggable jobs, with a command palette and pinned tiles. First real job: Daily Log → Google Sheet. |
| [budget/](budget/) | Budget: upload Bank of America exports from the phone; transactions land in SQLite, categorized by a hand-edited rules file, with balance checks and undo. Runs in Docker on the AI Workstation behind Tailscale. |
| [voice-rec/](voice-rec/) | Voice notes: recorded in TaskDeck on the phone, then transcribed (Whisper), categorized (llama.cpp) and archived in Postgres, an Obsidian vault and the knowledge graph on the AI Workstation. |
| [tab-ledger/](tab-ledger/) | Tab Ledger: a Chrome extension that records every window and tab, summarizes each page, tracks scrolling and copy/paste, and writes a script that reopens them. |
| [photo-ocr/](photo-ocr/) | Photo OCR: phone photos of receipts and products read by local models on the AI Workstation, checked and reviewed, then used for a shopping list and for matching receipts to the bank transactions in budget. Immich holds the photos; one Docker service behind Tailscale; TaskDeck jobs on the phone. |

## Conventions

* One top-level folder per project, with its own README, dependencies and tests. Nothing project-specific at the root.
* GitHub Actions workflows must live in `.github/workflows/` at the root, so each is named after its
  project (`taskdeck-ci.yml`, ...), runs with `working-directory` set to the project folder, and only
  triggers on changes under that folder.

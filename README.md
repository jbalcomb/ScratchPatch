# ScratchPatch

A patch of scratch projects: proofs of concept and MVPs, each in its own folder.

| Project | What it is |
|---|---|
| [taskdeck/](taskdeck/) | TaskDeck: an Android app (Python/Flet) that runs pluggable jobs, with a command palette and pinned tiles. First real job: Daily Log → Google Sheet. |
| [taskdeck-desktop/](taskdeck-desktop/) | TaskDeck Desktop: TaskDeck for Windows in C++, first with Dear ImGui, later measured against a WinUI 3 / Windows App SDK build. |
| [budget/](budget/) | Budget: upload Bank of America exports from the phone; transactions land in SQLite, categorized by a hand-edited rules file, with balance checks and undo. Runs in Docker on the AI Workstation behind Tailscale. |
| [voice-rec/](voice-rec/) | Voice notes: recorded in TaskDeck on the phone, then transcribed (Whisper), categorized (llama.cpp) and archived in Postgres, an Obsidian vault and the knowledge graph on the AI Workstation. |
| [tab-ledger/](tab-ledger/) | Tab Ledger: a Chrome extension that records every window and tab, summarizes each page, tracks scrolling and copy/paste, and writes a script that reopens them. |
| [photo-ocr/](photo-ocr/) | Photo OCR: phone photos of receipts and products read by local models on the AI Workstation, checked and reviewed, then used for a shopping list and for matching receipts to the bank transactions in budget. Immich holds the photos; one Docker service behind Tailscale; TaskDeck jobs on the phone. |
| [pdf-parser/](pdf-parser/) | PDF Parser: a PDF from the phone becomes a folder of text (with fonts and positions), tables, images and OCR, plus a manifest for rebuilding the document later. Images get heuristic checks, then the local vision model, with retries. Docker service on the AI Workstation; a TaskDeck job on the phone. |

## Conventions

* One top-level folder per project, with its own README, dependencies and tests. Nothing project-specific at the root.
* GitHub Actions workflows must live in `.github/workflows/` at the root, so each is named after its
  project (`taskdeck-ci.yml`, ...), runs with `working-directory` set to the project folder, and only
  triggers on changes under that folder.
* Services on the AI Workstation are reached from other devices as a **Tailscale Service** with its own name,
  `https://<service>.tailc54221.ts.net` (like `budget`), forwarding to the service's `localhost` port. Don't
  give them a port on the workstation's own Tailscale Serve name (`imustai...:8445`): a name needs no port and
  doesn't clash. Each service listens on `localhost` only, on a port no other service uses.

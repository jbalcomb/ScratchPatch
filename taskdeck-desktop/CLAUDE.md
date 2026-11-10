# TaskDeck Desktop: briefing for Claude Code

You're working on the Windows desktop version of TaskDeck, a job runner whose phone version (Python + Flet,
Android) lives in `../taskdeck` of this repository. Read `README.md` and `docs/PLAN.md` first.

## Start here

* **Ask the user the questions in docs/PLAN.md, "Decisions to make first"**, before building the job model:
  above all how jobs run (reuse the Python jobs, rewrite in C++, or a mix). Don't pick one yourself.
* The app builds and opens a window with the shell (navigation, tiles, job list, history, settings with
  performance numbers, Ctrl+K palette). It was compiled with MinGW from Linux but not run on Windows yet:
  build it with Visual Studio 2022 (README) and check it starts, resizes, sleeps when idle (CPU ~0%), and
  scales on a high-DPI screen.

## The phone app, for reference (`../taskdeck`)

* `src/taskdeck/core/plugin.py`: the job contract (`JobPlugin`: fields, settings, `build_pipeline()`,
  `check_ready`, optional `commit`), `core/pipeline.py` (steps by stage: validate, execute, present),
  `core/tasks.py` (background tasks with phases and progress), `core/commands.py` (palette ranking),
  `core/favorites.py` (pins, history, drafts), `core/copying.py` and `docs/JOB_KINDS.md` (one-time copy vs
  ongoing sync: decides what happens to files already at a destination).
* `src/taskdeck/jobs/`: the built-in jobs; `src/taskdeck/ui/`: the Flet screens (the desktop UI should feel
  like these: Home with pinned tiles, All jobs, History, Settings, command palette).
* `docs/ARCHITECTURE.md`, `docs/DIAGNOSTICS.md` (task traces), `docs/GOOGLE_DRIVE_SETUP.md`,
  `docs/CREDENTIALS.md`.
* Network services the jobs use: the AI Workstation (llama.cpp, over Tailscale), Google Sheets and Drive,
  Gemini as a fallback model.

## Repository conventions

* This repo holds many projects, one folder each; keep everything for this one in `taskdeck-desktop/`.
  GitHub Actions workflows must be at the root in `.github/workflows/`, named `taskdeck-desktop-*.yml`, with
  `working-directory: taskdeck-desktop` and a `paths:` filter for this folder.
* Work on the `taskdeck-desktop` branch. Other sessions push to `main` for other projects: merge
  `origin/main` before you push, and never rewrite shared history.
* Never commit credentials (API keys, the Google service-account key, OAuth client secrets). The repo is
  public.
* Don't change `../taskdeck` from here without the user asking: the phone app is in daily use and publishes
  to the phone on every push to `main`.

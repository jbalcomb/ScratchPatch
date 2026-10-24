# ScratchPatch

A patch of scratch projects: proofs of concept and MVPs, each in its own folder.

| Project | What it is |
|---|---|
| [taskdeck/](taskdeck/) | TaskDeck: an Android app (Python/Flet) that runs pluggable jobs, with a command palette and pinned tiles. First real job: Daily Log → Google Sheet. |
| [tab-ledger/](tab-ledger/) | Tab Ledger: a Chrome extension that records every window and tab, summarizes each page, tracks scrolling and copy/paste, and writes a script that reopens them. |
| [remom-fonts/](remom-fonts/) | ReMoM fonts: converts the Master of Magic bitmap fonts (FONTS.LBX) into layered TrueType fonts, one font per color index, plus SVGs and a specimen page, and ReMoM Font Studio (Dear ImGui) to preview text and export it as game pictures. |

## Conventions

* One top-level folder per project, with its own README, dependencies and tests. Nothing project-specific at the root.
* GitHub Actions workflows must live in `.github/workflows/` at the root, so each is named after its
  project (`taskdeck-ci.yml`, ...), runs with `working-directory` set to the project folder, and only
  triggers on changes under that folder.

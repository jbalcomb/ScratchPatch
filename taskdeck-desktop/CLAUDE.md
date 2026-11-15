# TaskDeck Desktop: briefing for Claude Code

You're working on the Windows desktop version of TaskDeck, a job runner whose phone version (Python + Flet,
Android) lives in `../taskdeck` of this repository. Read `README.md` and `docs/PLAN.md` first.

## Start here

* The decisions in docs/PLAN.md, "Decisions to make first", were made on 2026-11-10. Follow them:
  * a C++ core with Python jobs in a separate process (JSON over stdin/stdout);
  * local state on each device;
  * keys in Credential Manager;
  * first jobs: Extract archive, Parse executable, Send to Devices;
  * a plain exe with an update check.

  Ask before changing any of them.
* The app builds and opens a window with the shell (navigation, tiles, job list, history, settings with
  performance numbers, Ctrl+K palette).
* It was checked on Windows 10 on 2026-11-10, built with Visual Studio 2022 (MSVC 19.44) with no warnings:
  * it starts and resizes;
  * idle CPU is 0.00% (also after resizing);
  * memory is 26 MB working set and 16 MB private;
  * it scales correctly at 150% on a 4K screen;
  * it closes when its window is closed.

  The UI font is Segoe UI from the Windows fonts folder, at 16 px before DPI scaling (`load_ui_font` in
  `main.cpp`).
* Unexplained, seen twice (2026-11-12, and 2026-11-14 on the first start of a freshly unzipped release): the
  window stays blank white after launch. The second time it stayed white for over 7 s, with ~0 CPU, through
  the job host's and the update check's wake-ups, until the mouse moved over it; then it drew at once. It
  didn't happen again in 6 more launches (3 of them freshly copied folders, sampled every second). Every
  start now writes `startup-trace.log` in the data folder (the first 15 s: window shown and foreground, each
  Present's result, idle and wake, occlusion; written at 15 s or at quit). If you see it, read that before
  changing anything. A dark window-class background would only hide it.
* Jobs run in `python/taskdeck_host.py`, a child process (README, "How jobs run"). Change its protocol on both
  sides together (`src/job_host.*`, `src/shell.cpp`) and keep `python/tests` passing.
* Pins, history, drafts and plain settings are in `%LOCALAPPDATA%\TaskDeck\state.json` (`src/store.*`, the
  phone's layout); secret settings in Credential Manager (`src/credentials.*`), never in state.json. When
  testing the app, set `TASKDECK_DATA_DIR` to a scratch folder so the user's own state isn't touched, and
  remove any credential you create (`cmdkey /list | findstr TaskDeck`). `build\Release\core_tests.exe`
  tests these and the palette ranking.
* To see a background task run without Google Drive, copy `../taskdeck/src` to a scratch folder, add a job
  whose step calls `ctx.services["start_task"]` with slow work, and point `TASKDECK_SRC` at the copy (never add
  test jobs to `../taskdeck`). Remove any fake `TaskDeck/settings:account:google:*` credential afterwards.
* To test the tray, close the window while the slow test job's task runs. Windows puts new tray icons in the
  overflow (the ^ arrow), where `Shell_NotifyIconGetRect` (hwnd of class `TaskDeck`, uID 1) finds TaskDeck's
  icon only while the overflow is open. A posted `WM_APP+1` doesn't stand in for a click: the menu won't open
  without the foreground rights a real click gives. Force-killed test instances leave ghost icons there.
* Extract archive is C++ (`src/native_jobs.*`, `src/extract_job.*`, `src/sevenzip.*`); `7z.dll` is built by
  `cmake/build-7z.cmd` (7-Zip's makefile, about 2 minutes, once per build folder). Keep it to the phone's
  behaviour (`../taskdeck/src/taskdeck/jobs/extract_archive/logic.py`, `shared/archive.py`) and run
  `build\Release\archive_tests.exe`. Make large test archives in a scratch folder (Windows'
  `%SystemRoot%\System32\tar.exe`; Git's `tar` reads `C:` as a host name), never in the repo.
* Releases and updates: `src/updater.*`, `tools/package.ps1`, `.github/workflows/taskdeck-desktop-release.yml`
  (README "Install"). To test an update without publishing: configure with `-DTASKDECK_BUILD=1`, build,
  `pwsh tools/package.ps1 -Build 1 -Out <scratch>\rel1`; the same with 2; copy `rel1\TaskDeck` to a scratch
  folder, serve `rel2`'s zip and manifest with `python -m http.server 8765 --bind 127.0.0.1`, and start the
  copy with `TASKDECK_UPDATE_URL=http://127.0.0.1:8765/` (and `TASKDECK_DATA_DIR`). Configure with
  `-DTASKDECK_BUILD=0` again afterwards. A MessageBox's single OK button has control id 2 (`WM_COMMAND` 2
  closes it; 1 doesn't).
* Keep the window idle at 0% CPU: `Shell::draw` returns true only while something animates. Measure after a
  change (a stuck focus request once kept it drawing at 8%).
* When measuring window sizes from a script, make the script per-monitor DPI aware
  (`SetThreadDpiAwarenessContext`). Otherwise Windows scales the sizes it reports.

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

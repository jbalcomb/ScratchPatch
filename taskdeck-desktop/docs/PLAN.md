# TaskDeck Desktop: plan

## Goal

A fast, native Windows version of TaskDeck: the same jobs, pinned tiles, command palette and history as the
phone app ([../../taskdeck](../../taskdeck)), at desktop speed. Built twice and compared:

1. **Dear ImGui** (now): immediate-mode UI on Win32 + Direct3D 11. Quick to build, tiny, full control over
   every frame.
2. **WinUI 3 / Windows App SDK (C++/WinRT)** (later): the native Windows UI framework (Fluent look,
   accessibility, text input and IME, high-DPI and theming handled by the OS).

The same features, measured the same way (below), decide which one TaskDeck Desktop keeps, or what each is
used for.

## Decisions to make first (ask the user)

These shape everything after the window. **Decided 2026-11-10** (the options considered are kept below):

1. **How jobs run: c, the mix.** The UI and core (palette, history, settings, background tasks) are C++. Jobs
   are the phone app's Python jobs, run in a separate Python process that talks JSON over stdin/stdout. Jobs
   where speed matters get C++ versions (Extract archive first, on 7-Zip's C++ library).
2. **Shared state: separate per device.** Pins, history and settings are local files on this PC. Syncing can
   come later.
3. **Credentials: Windows Credential Manager**, protected by the Windows sign-in. No Windows Hello prompt.
4. **First jobs: Extract archive, Parse executable, and Send to Devices** (CrossMess, the old Courier). Send to
   Devices does what the phone job does: sign a message, post it to the gateway, Test connection. It
   doesn't show the inbox or run the device agent. That job (`taskdeck/src/taskdeck/jobs/courier`) and
   CrossMess itself are on the `CrossMess` branch, not on `main` yet.
5. **Distribution: a plain exe with an update check.** GitHub Actions builds it. On start, the app checks for a
   newer release and offers to replace itself. The Python jobs come with each release.

The questions as they were asked:

1. **How jobs run.** The phone app's jobs are Python (`taskdeck/src/taskdeck/jobs`, contract in
   `taskdeck/src/taskdeck/core/plugin.py`: fields, settings, a pipeline of steps, an optional commit).
   Options:
   * **a. Run the existing Python jobs**, unchanged: embed CPython in the exe, or start a small Python
     process and talk to it (JSON over stdin/stdout or a local socket). One set of jobs for phone and
     desktop; the C++ side is only the UI.
   * **b. Rewrite jobs in C++.** Fastest, one language, but every job exists twice.
   * **c. Mix:** the UI and core (palette, history, settings, tasks) in C++, jobs from Python (a), with
     C++ versions of the ones where speed matters (e.g. archives already use 7-Zip's C++ library).
2. **Shared state with the phone.** Separate settings and history per device, or synced (and through what:
   Google Drive, the AI Workstation, a file)?
3. **Credentials.** The phone keeps API keys and sign-ins in a fingerprint-locked vault
   (`taskdeck/docs/CREDENTIALS.md`). On Windows: Credential Manager / DPAPI, optionally with Windows Hello.
4. **Which jobs first.** Probably those that are better on a desktop (Extract Archive with big archives, Parse
   executable, Daily Log) over the phone-only ones (Voice notes).
5. **Distribution and updates.** A plain `.exe` from GitHub Actions, or MSIX; how updates arrive (the phone
   gets live code updates: `taskdeck/docs/UPDATES.md`).

## Steps

1. **Window and shell (done):** Win32 + D3D11 + Dear ImGui; Home (pinned tiles), All jobs, History, Settings,
   command palette (Ctrl+K); performance numbers; idle-friendly frame loop.
2. **Job model (done):** the Python job host (`python/taskdeck_host.py`, JSON lines over stdin/stdout); job
   screens built from a job's fields (text, multiline, number, choice, toggle, file, files), Run, progress,
   Cancel, result, the confirm/commit step. All 20 phone jobs load (Python 3.14). Checked on Windows: JSON
   Formatter and Parse executable (through the file picker) run from the app.
3. **Core features from the phone app (done, 2026-11-14):** pins (move, unpin), history (the last 100
   runs, reopened with their inputs), drafts, settings per job (Save checks first, Test settings, the job's
   settings buttons, Import file for key files, fixes the checks find are saved), secrets in Credential
   Manager, the palette's ranking with usage and quick actions (`core/commands.py`), all in `state.json` in
   the phone's layout. Checked on Windows: a key saved, read back after a restart, and removed; a draft kept
   across a restart; idle CPU still 0.00%. Then background tasks (the phone's own `core/tasks.py` in the job
   host, offered to jobs as `start_task`: progress per phase with speed and time left, Cancel, Retry, Connect
   again, Open in Drive, History, a warning before closing) and the Google account (`account:google`, the
   phone's device sign-in from `shared/google_oauth.py`, offered to jobs as `google_drive`). Checked on
   Windows: Parse executable with Push to Google Drive started its upload task, which failed on a fake sign-in
   with Connect again and Retry upload; a slow test job (in a scratch copy of the jobs) showed the running
   bars, Cancel, and the close warning; CPU 0.5% while a task reports, 0% idle. Not checked: a real Google
   sign-in and upload (needs the user's OAuth client).
   Then the tray (2026-11-14, asked for by the user): closing the window while a job or task runs asks (keep
   running in the tray, stop them and quit, with "Don't show this again", also in Settings); the tray icon's
   menu lists them with Cancel each, Cancel all, Open and Quit; a notification when they've all ended, then
   TaskDeck quits unless something needs the user; one TaskDeck per data folder. Same process, window hidden
   (not a separate service): reopening shows every screen as it was. Checked on Windows: the notice, the
   real tray menu (Cancel, Cancel all with its confirmation, Open), a task finishing and one cancelled in the
   tray (notification, then TaskDeck quit, History written), the job host killed while in the tray (a
   warning, TaskDeck stayed), a second start reopening the window; 0.16% CPU and 2 MB working set in the tray
   while a task reports.
4. **First real jobs** (decision 4).
5. **Measure** (below), then the **WinUI 3 build** of the same features, then measure that.

## Measuring (Dear ImGui vs WinUI 3)

Same machine, Release builds, same window size, 5 runs each, report the median:

| What | How |
|---|---|
| Cold start to first frame | `perf.first_frame_ms` (process creation time to the first Present); for WinUI 3 the same from the first `Rendering` event |
| Memory, idle | working set and private bytes after 30 s idle (Settings tab; Task Manager / `GetProcessMemoryInfo`) |
| CPU, idle | % of one core over 60 s with no input (should be ~0: both should sleep when nothing changes) |
| Frame time | average and worst over 120 frames while scrolling the job list (`--continuous` for ImGui); PresentMon for both |
| Input latency | key press to the command palette's first visible result (Windows Performance Recorder + Analyzer) |
| Size | the exe plus everything it needs to ship (WinUI 3: the Windows App SDK runtime) |

Memory: measure before the file picker has been opened, or after in both builds. Opening it loads about 100
Windows shell DLLs into the process for good (ImGui build: 29 MB working set fresh, 62 MB after a Browse).
The Python job host is a separate process (about 36 MB); report it on its own line.

Tools: PresentMon (frame times), Windows Performance Recorder/Analyzer (startup, CPU, latency), Visual Studio's
profiler (hot spots).

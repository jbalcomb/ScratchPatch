# TaskDeck Desktop

TaskDeck for Windows: a native desktop version of the TaskDeck phone app ([../taskdeck](../taskdeck)).
First built with **Dear ImGui** (Win32 + Direct3D 11, C++20); later measured and optimized against a
**WinUI 3 / Windows App SDK (C++)** build of the same app. See [docs/PLAN.md](docs/PLAN.md).

**Status:** the phone app's four places (Home with pinned tiles, All jobs, History, Settings), the command
palette (Ctrl+K), live performance numbers in Settings, and the phone app's own jobs, run by Python: each job
gets a screen built from its fields (with the Windows file picker for files), Run, progress (with a throbber,
the time so far and what the step says it's doing, e.g. the PDF parser service's page), Cancel, the
result, and the confirm/commit step. Each job's settings have their own screen (Save checks them first, Test
settings, the job's own buttons such as "Test connection"); keys and passwords are kept in Windows Credential
Manager. Pins, history, drafts (what you typed and haven't finished) and the palette's ranking are kept
between runs. Background tasks (an upload to Google Drive after Parse executable) show their progress on the
job's screen and on Home, with Cancel and Retry, and go into History when they end. Closing the window while
a job or task runs can keep TaskDeck running in the notification area (the tray) until it's done. Google Drive is connected in Settings > Accounts, with the phone's sign-in (a code
entered at google.com/device). Extract archive is the first job in C++ (on 7-Zip, below). Releases come as
a zip with Python included, and TaskDeck updates itself (below). Not yet: Send to Devices (docs/PLAN.md).

## Install

Download `TaskDeck-windows-x64.zip` from the
[taskdeck-desktop-latest release](https://github.com/jbalcomb/ScratchPatch/releases/tag/taskdeck-desktop-latest),
unzip it into a folder you can write to (`%LOCALAPPDATA%\Programs\TaskDeck`, say; not Program Files) and run
`TaskDeck.exe`. Nothing else to install: Python comes with it. The exe isn't signed, so Windows SmartScreen may
warn the first time ("More info", "Run anyway").

The zip holds `TaskDeck.exe`, `7z.dll`, the job host (`python\`), the phone app's jobs (`jobs\`), Python 3.14
(`python-runtime\`, python.org's embeddable package, with `rsa` and `pyasn1` for Google service accounts) and
the licences (`LICENSES\`). About 16 MB zipped.

**Updates** (decision 5): a few seconds after start TaskDeck reads the release's manifest
(`taskdeck-desktop-manifest.json`: build number, SHA-256, size). When there's a newer build, a banner offers
it (Update, What's new, Later; also Settings > App updates, where the check at start can be turned off).
Update downloads the zip into `.update\` in TaskDeck's folder, checks its SHA-256 and unpacks it there;
"Restart TaskDeck to update" (once nothing runs) stops the jobs, moves the files it replaces into `.old\`,
moves the new ones in, and starts the new TaskDeck. If a file can't be moved (open in another program), what
was moved is put back, TaskDeck says why and starts again on the old version, still offering the update. The
next start removes `.old\`. Pins, history, settings and keys aren't in that folder, so they stay. Downloads
are HTTPS from github.com; anyone who can push to the `taskdeck-desktop` branch can change what installed
copies run (the same trust as the phone's live updates, `../taskdeck/docs/UPDATES.md`).

The release workflow (`.github/workflows/taskdeck-desktop-release.yml`) runs on every push to the
`taskdeck-desktop` branch that changes this folder or the phone's jobs: tests, a build numbered with the run
number (shown as version `0.1.<build>`), `tools/package.ps1` (which also checks that every job loads in the
bundled Python), then the zip and the manifest are uploaded. A build from source is build 0: it uses Python
from PATH and the source tree's jobs, and never offers updates.

## What's kept, and where

Everything stays on this PC (decision 2 in docs/PLAN.md):

* `%LOCALAPPDATA%\TaskDeck\state.json`: pins, history (the last 100 runs), drafts, how often each palette
  command is used, and job settings, in the phone app's layout (`taskdeck/src/taskdeck/core/storage.py`,
  `favorites.py`). Set `TASKDECK_DATA_DIR` to use another folder (for tests).
* **Credential Manager** (Control Panel > Credential Manager > Windows Credentials): the settings a job marks
  secret (API keys, the Google service-account key), as `TaskDeck/settings:<job>:<setting>`, kept on this PC
  only (not roamed) and readable only by your Windows account while you're signed in. They never go into
  `state.json`. A value over Credential Manager's 2,560-byte limit is kept in parts (`...#2`, `...#3`).
  Passwords typed into a job's screen (an archive's) are not kept in drafts or history.
* The **Google account** (Settings > Accounts) is kept like a job's settings under `account:google`: the
  client ID and the address in `state.json`, the client secret and the sign-in (refresh token) in Credential
  Manager. Disconnect forgets the sign-in and asks Google to revoke it.

## Closing while something runs

Jobs and background tasks run in the job host, which stops when TaskDeck quits. So closing the window while
one runs asks first (Settings > "Closing the window" changes that, as does "Don't show this again"):

* **Keep running in the tray**: the window closes, TaskDeck's icon stays in the notification area (by the
  clock; Windows may put it under the ^ arrow), and the work goes on. Right-click the icon for what's running,
  each with Cancel, and Cancel all, Open TaskDeck and Quit TaskDeck; click it to open the window again, just as
  you left it. When everything has ended, a notification says so and TaskDeck quits, unless something needs
  you (a failed upload to retry, a result waiting for its commit button): then it stays in the tray.
* **Stop them and quit**.

Only one TaskDeck runs at a time: starting it again opens the one already running (from the tray, too). The
phone's "Wi-Fi only" setting has no effect here.

Shortcuts: Ctrl+K the palette (`>` actions only, `@` jobs only; arrows and Enter), Ctrl+1 to Ctrl+4 the four
places, Ctrl+D pin or unpin the open job. Right-click a tile on Home to move or unpin it, or a job in All jobs
to pin it.

## How jobs run

The UI and core are C++. The jobs are the phone app's Python jobs ([../taskdeck/src/taskdeck/jobs](../taskdeck/src/taskdeck/jobs)),
unchanged: at start the app launches `python/taskdeck_host.py` as a child process and talks to it in JSON
lines over stdin/stdout (the protocol is at the top of that file). The host loads the jobs, runs each on its
own thread, and reports progress and results; the app's window still sleeps while nothing happens. Anything
the jobs print goes to `%LOCALAPPDATA%\TaskDeck\job-host.log`.

Jobs where speed matters are C++ (`src/native_jobs.*`). They answer the same messages as the job host, so the
shell runs them the same way, and they take the place of the Python job with the same id. They work without
Python too. So far there is one:

**Extract archive** (`src/extract_job.*`, `src/sevenzip.*`): zip, 7z, RAR (4 and 5), tar, gz/bz2/xz/zst,
cab, iso and the rest of what 7-Zip reads, password-protected ones too (encrypted names included). It is the
phone's job (`jobs/extract_archive/logic.py`) on the same engine: 7-Zip 26.05's library, `7z.dll`, built
from 7-Zip's source with its own makefile and loaded from next to `TaskDeck.exe`, through the phone's C shim
adapted for Windows. As on the phone: it checks the archive, the password and the destination (exists or
can be created, writable, enough free space) before starting; it refuses archives with paths that point
outside the folder; it replaces existing files; and a `.tar.gz` (`.tgz`, `.tar.bz2`, ...) is decompressed and
then extracted, in two phases. It extracts as a background task: a bar per phase with speed and time left,
Cancel, the tray, History, and "Open folder" when done. The destination is a new folder next to the archive
unless one is chosen. 7-Zip is GNU LGPL with the unRAR restriction (the RAR code may not be used to create RAR
archives): https://www.7-zip.org/license.txt.

A release brings its own Python (above). A build from source needs **Python 3.10 or newer** (python.org;
`python.exe` on PATH, or the `py` launcher) and finds the host script and the jobs in the source tree it was
built from. Environment variables override either:

| Variable | Release | Built from source |
|---|---|---|
| `TASKDECK_PYTHON` | `python-runtime\python.exe` | `python.exe` on PATH (not the Microsoft Store stub), else `py -3` |
| `TASKDECK_DESKTOP_DIR` | the exe's folder | this folder (holds `python/taskdeck_host.py`) |
| `TASKDECK_SRC` | `jobs\` | `../taskdeck/src` (holds the `taskdeck` package) |
| `TASKDECK_UPDATE_URL` | the release's download folder | (no updates) |

Tests for the host (they also start it against the real jobs):

```bat
python -m unittest discover -s python/tests -v
```

Tests for the C++ parts without a window (the palette's ranking, the state file, Credential Manager; it writes
and removes a `TaskDeck-tests/<pid>` credential), after building: `build\Release\core_tests.exe`. Extract
archive's tests, on the phone's test archives (`../taskdeck/tests/fixtures/archives`) plus a `.tar.gz` made
with Windows' `tar.exe`: `build\Release\archive_tests.exe`.

## Build (Windows)

Needs Visual Studio 2022 or newer (or its Build Tools) with the **Desktop development with C++** workload,
which includes CMake and the Windows SDK. Dear ImGui, nlohmann/json and 7-Zip's source are downloaded by
CMake (pinned); nothing else to install. The first build also builds `7z.dll` with 7-Zip's makefile (nmake,
about 2 minutes) and copies it next to the exes. Run these in a "Developer Command Prompt" (or any prompt where `cmake` is found); CMake picks
the newest Visual Studio installed.

```bat
cmake -B build -A x64
cmake --build build --config Release
build\Release\TaskDeck.exe
```

`TaskDeck.exe --continuous` draws every frame instead of only after input (for frame-time measurements).

A release folder from a build: `pwsh tools/package.ps1 -Build 1` (writes `dist\TaskDeck\`, the zip and the
manifest; `-DTASKDECK_BUILD=1` at configure time gives the exe the same number). The icon is drawn by
`python tools/make_icon.py` (writes `assets/taskdeck.ico`).

## Build (Linux, cross-compile check)

```bash
sudo apt-get install g++-mingw-w64-x86-64
cmake -B build-mingw -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-w64.cmake -DCMAKE_BUILD_TYPE=Release
cmake --build build-mingw    # -> build-mingw/TaskDeck.exe
```

GitHub Actions builds it with MSVC on every push that changes this folder
(`.github/workflows/taskdeck-desktop-ci.yml`) and keeps `TaskDeck.exe` as a download on the run; the release
workflow (above) publishes from the `taskdeck-desktop` branch.

## Layout

```
CMakeLists.txt        build; fetches Dear ImGui and nlohmann/json; TaskDeck.exe and core_tests.exe
cmake/mingw-w64.cmake toolchain for building on Linux
src/main.cpp          Win32 window, Direct3D 11, the (idle-friendly) frame loop
src/shell.*           the TaskDeck UI: navigation, tiles, job list, job screens, history, settings, palette
src/job_host.*        the Python job host as a child process (pipes, JSON lines, a reader thread)
src/file_dialog.*     the Windows file picker
src/store.*           state.json: pins, history, drafts, settings
src/credentials.*     secret settings in Credential Manager
src/commands.*        the palette's matching and ranking (ported from the phone app's core/commands.py)
tests/core_tests.cpp  tests for those three
python/taskdeck_host.py   runs the phone app's jobs for the app; python/tests/ its tests
src/perf.*            first-frame time, frame time, CPU, memory: the numbers to compare against WinUI 3
src/tray.*            the notification-area icon
src/native_jobs.*, src/extract_job.*, src/sevenzip.*   Extract archive in C++ on 7-Zip (cmake/build-7z.cmd)
src/updater.*         the update check, download and install; where a release keeps its parts
src/taskdeck.rc.in    the icon and version info (assets/taskdeck.ico, drawn by tools/make_icon.py)
tools/package.ps1     the release folder and zip (with Python's embeddable package), and its manifest
python/requirements-release.txt   what the jobs need beyond Python's standard library, for releases
docs/PLAN.md          the plan, the open decisions, and how the two builds will be compared
CLAUDE.md             briefing for Claude Code sessions working here
```

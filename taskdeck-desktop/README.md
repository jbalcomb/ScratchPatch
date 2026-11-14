# TaskDeck Desktop

TaskDeck for Windows: a native desktop version of the TaskDeck phone app ([../taskdeck](../taskdeck)).
First built with **Dear ImGui** (Win32 + Direct3D 11, C++20); later measured and optimized against a
**WinUI 3 / Windows App SDK (C++)** build of the same app. See [docs/PLAN.md](docs/PLAN.md).

**Status:** the phone app's four places (Home with pinned tiles, All jobs, History, Settings), the command
palette (Ctrl+K), live performance numbers in Settings, and the phone app's own jobs, run by Python: each job
gets a screen built from its fields (with the Windows file picker for files), Run, progress, Cancel, the
result, and the confirm/commit step. Each job's settings have their own screen (Save checks them first, Test
settings, the job's own buttons such as "Test connection"); keys and passwords are kept in Windows Credential
Manager. Pins, history, drafts (what you typed and haven't finished) and the palette's ranking are kept
between runs. Background tasks (an upload to Google Drive after Parse executable) show their progress on the
job's screen and on Home, with Cancel and Retry, and go into History when they end. Closing the window while
a job or task runs can keep TaskDeck running in the notification area (the tray) until it's done. Google Drive is connected in Settings > Accounts, with the phone's sign-in (a code
entered at google.com/device). Not yet: the first C++ jobs, releases (docs/PLAN.md).

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
the jobs print goes to `%LOCALAPPDATA%\TaskDeck\job-host.log`. Jobs where speed matters get C++ versions
later (Extract archive first).

This needs **Python 3.10 or newer** (python.org; `python.exe` on PATH, or the `py` launcher). The app finds
the host script and the jobs in the source tree it was built from; override with environment variables:

| Variable | Default |
|---|---|
| `TASKDECK_PYTHON` | `python.exe` on PATH (not the Microsoft Store stub), else `py -3` |
| `TASKDECK_DESKTOP_DIR` | this folder (holds `python/taskdeck_host.py`) |
| `TASKDECK_SRC` | `../taskdeck/src` (holds the `taskdeck` package) |

Tests for the host (they also start it against the real jobs):

```bat
python -m unittest discover -s python/tests -v
```

Tests for the C++ parts without a window (the palette's ranking, the state file, Credential Manager; it writes
and removes a `TaskDeck-tests/<pid>` credential), after building: `build\Release\core_tests.exe`.

## Build (Windows)

Needs Visual Studio 2022 or newer (or its Build Tools) with the **Desktop development with C++** workload,
which includes CMake and the Windows SDK. Dear ImGui is downloaded by CMake (pinned to v1.92.9); nothing else
to install. Run these in a "Developer Command Prompt" (or any prompt where `cmake` is found); CMake picks
the newest Visual Studio installed.

```bat
cmake -B build -A x64
cmake --build build --config Release
build\Release\TaskDeck.exe
```

`TaskDeck.exe --continuous` draws every frame instead of only after input (for frame-time measurements).

## Build (Linux, cross-compile check)

```bash
sudo apt-get install g++-mingw-w64-x86-64
cmake -B build-mingw -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-w64.cmake -DCMAKE_BUILD_TYPE=Release
cmake --build build-mingw    # -> build-mingw/TaskDeck.exe
```

GitHub Actions builds it with MSVC on every push that changes this folder
(`.github/workflows/taskdeck-desktop-ci.yml`) and keeps `TaskDeck.exe` as a download on the run.

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
docs/PLAN.md          the plan, the open decisions, and how the two builds will be compared
CLAUDE.md             briefing for Claude Code sessions working here
```

# TaskDeck Desktop

TaskDeck for Windows: a native desktop version of the TaskDeck phone app ([../taskdeck](../taskdeck)).
First built with **Dear ImGui** (Win32 + Direct3D 11, C++20); later measured and optimized against a
**WinUI 3 / Windows App SDK (C++)** build of the same app. See [docs/PLAN.md](docs/PLAN.md).

**Status:** the phone app's four places (Home with pinned tiles, All jobs, History, Settings), the command
palette (Ctrl+K), live performance numbers in Settings, and the phone app's own jobs, run by Python: each job
gets a screen built from its fields (with the Windows file picker for files), Run, progress, Cancel, the
result, and the confirm/commit step. Not yet: job settings, pins, history and drafts saved to disk
(docs/PLAN.md, step 3).

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
CMakeLists.txt        build; fetches Dear ImGui and nlohmann/json
cmake/mingw-w64.cmake toolchain for building on Linux
src/main.cpp          Win32 window, Direct3D 11, the (idle-friendly) frame loop
src/shell.*           the TaskDeck UI: navigation, tiles, job list, job screens, history, settings, palette
src/job_host.*        the Python job host as a child process (pipes, JSON lines, a reader thread)
src/file_dialog.*     the Windows file picker
python/taskdeck_host.py   runs the phone app's jobs for the app; python/tests/ its tests
src/perf.*            first-frame time, frame time, CPU, memory: the numbers to compare against WinUI 3
docs/PLAN.md          the plan, the open decisions, and how the two builds will be compared
CLAUDE.md             briefing for Claude Code sessions working here
```

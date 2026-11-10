# TaskDeck Desktop

TaskDeck for Windows: a native desktop version of the TaskDeck phone app ([../taskdeck](../taskdeck)).
First built with **Dear ImGui** (Win32 + Direct3D 11, C++20); later measured and optimized against a
**WinUI 3 / Windows App SDK (C++)** build of the same app. See [docs/PLAN.md](docs/PLAN.md).

**Status:** a window with the phone app's four places (Home with pinned tiles, All jobs, History, Settings),
the command palette (Ctrl+K), and live performance numbers in Settings. The jobs are placeholders until the
job model is decided (docs/PLAN.md, "Decisions to make first").

## Build (Windows)

Needs Visual Studio 2022 (or the Build Tools) with the **Desktop development with C++** workload, which
includes CMake and the Windows SDK. Dear ImGui is downloaded by CMake (pinned to v1.92.9); nothing else to
install.

```bat
cmake -B build -G "Visual Studio 17 2022" -A x64
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
CMakeLists.txt        build; fetches Dear ImGui
cmake/mingw-w64.cmake toolchain for building on Linux
src/main.cpp          Win32 window, Direct3D 11, the (idle-friendly) frame loop
src/shell.*           the TaskDeck UI: navigation, tiles, job list, history, settings, command palette
src/perf.*            first-frame time, frame time, CPU, memory: the numbers to compare against WinUI 3
docs/PLAN.md          the plan, the open decisions, and how the two builds will be compared
CLAUDE.md             briefing for Claude Code sessions working here
```

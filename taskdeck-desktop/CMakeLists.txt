cmake_minimum_required(VERSION 3.24)
project(TaskDeckDesktop LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# MSVC: the C runtime linked into the exe (no Visual C++ redistributable needed). Set for every target, so
# Dear ImGui and the app agree (a mismatch fails to link with unresolved __imp_ symbols).
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

# Dear ImGui, pinned (no package manager needed: CMake fetches the sources at configure time).
include(FetchContent)
FetchContent_Declare(imgui
    GIT_REPOSITORY https://github.com/ocornut/imgui.git
    GIT_TAG v1.92.9
    GIT_SHALLOW TRUE)
FetchContent_MakeAvailable(imgui)

# nlohmann/json, pinned: the JSON lines spoken with the Python job host.
FetchContent_Declare(json
    URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(json)

add_library(imgui STATIC
    ${imgui_SOURCE_DIR}/imgui.cpp
    ${imgui_SOURCE_DIR}/imgui_draw.cpp
    ${imgui_SOURCE_DIR}/imgui_tables.cpp
    ${imgui_SOURCE_DIR}/imgui_widgets.cpp
    ${imgui_SOURCE_DIR}/imgui_demo.cpp
    ${imgui_SOURCE_DIR}/misc/cpp/imgui_stdlib.cpp
    ${imgui_SOURCE_DIR}/backends/imgui_impl_win32.cpp
    ${imgui_SOURCE_DIR}/backends/imgui_impl_dx11.cpp)
target_include_directories(imgui PUBLIC ${imgui_SOURCE_DIR} ${imgui_SOURCE_DIR}/backends ${imgui_SOURCE_DIR}/misc/cpp)

add_executable(TaskDeck WIN32
    src/main.cpp
    src/shell.cpp
    src/perf.cpp
    src/job_host.cpp
    src/file_dialog.cpp
    src/store.cpp
    src/credentials.cpp
    src/commands.cpp)
target_link_libraries(TaskDeck PRIVATE imgui nlohmann_json::nlohmann_json d3d11 dxgi d3dcompiler dwmapi psapi ole32 shell32
    uuid advapi32)
target_compile_definitions(TaskDeck PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN
    # Where the job host script (python/) is found when TASKDECK_DESKTOP_DIR isn't set: this source tree.
    TASKDECK_DEV_DIR="${CMAKE_CURRENT_SOURCE_DIR}")

# Tests for the parts without a window (palette ranking, the state file, Credential Manager).
add_executable(core_tests tests/core_tests.cpp src/store.cpp src/credentials.cpp src/commands.cpp)
target_link_libraries(core_tests PRIVATE nlohmann_json::nlohmann_json advapi32)
target_compile_definitions(core_tests PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)

foreach(target TaskDeck core_tests)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /utf-8)
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra)
        target_link_options(${target} PRIVATE -static)
    endif()
endforeach()
if(NOT MSVC)
    target_link_options(TaskDeck PRIVATE -municode)
endif()

cmake_minimum_required(VERSION 3.24)
project(TaskDeckDesktop LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Dear ImGui, pinned (no package manager needed: CMake fetches the sources at configure time).
include(FetchContent)
FetchContent_Declare(imgui
    GIT_REPOSITORY https://github.com/ocornut/imgui.git
    GIT_TAG v1.92.9
    GIT_SHALLOW TRUE)
FetchContent_MakeAvailable(imgui)

add_library(imgui STATIC
    ${imgui_SOURCE_DIR}/imgui.cpp
    ${imgui_SOURCE_DIR}/imgui_draw.cpp
    ${imgui_SOURCE_DIR}/imgui_tables.cpp
    ${imgui_SOURCE_DIR}/imgui_widgets.cpp
    ${imgui_SOURCE_DIR}/imgui_demo.cpp
    ${imgui_SOURCE_DIR}/backends/imgui_impl_win32.cpp
    ${imgui_SOURCE_DIR}/backends/imgui_impl_dx11.cpp)
target_include_directories(imgui PUBLIC ${imgui_SOURCE_DIR} ${imgui_SOURCE_DIR}/backends)

add_executable(TaskDeck WIN32
    src/main.cpp
    src/shell.cpp
    src/perf.cpp)
target_link_libraries(TaskDeck PRIVATE imgui d3d11 dxgi d3dcompiler dwmapi psapi)
target_compile_definitions(TaskDeck PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
if(MSVC)
    target_compile_options(TaskDeck PRIVATE /W4 /utf-8)
    set_property(TARGET TaskDeck PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
else()
    target_compile_options(TaskDeck PRIVATE -Wall -Wextra)
    target_link_options(TaskDeck PRIVATE -static -municode)
endif()

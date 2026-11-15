cmake_minimum_required(VERSION 3.24)
project(TaskDeckDesktop LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# MSVC: the C runtime linked into the exe (no Visual C++ redistributable needed). Set for every target, so
# Dear ImGui and the app agree (a mismatch fails to link with unresolved __imp_ symbols).
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

# The build number: the release workflow's run number (cmake -DTASKDECK_BUILD=42). 0, the default, is a build
# from source, which never offers updates (src/updater.h).
set(TASKDECK_BUILD 0 CACHE STRING "Release build number (0: built from source)")

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

# 7-Zip, for Extract archive: the same release as the phone app's engine
# (../taskdeck/native/sevenzip/7ZIP_VERSION). Two parts, as on the phone:
# * 7z.dll, 7-Zip's archive library (every format, RAR and encryption included), built with 7-Zip's own
#   makefile and copied next to the exes (about 2 minutes, once per build folder);
# * the `sevenzip` library: src/sevenzip.cpp (the phone's shim for Windows) and the 7-Zip helper sources it uses,
#   which loads 7z.dll at run time.
# MSVC only for now: other compilers get src/sevenzip_none.cpp (Extract archive says the engine is missing).
if(MSVC)
    FetchContent_Declare(sevenzip_src
        GIT_REPOSITORY https://github.com/ip7z/7zip.git
        GIT_TAG 26.05
        GIT_SHALLOW TRUE)
    FetchContent_MakeAvailable(sevenzip_src)
    set(SZ ${sevenzip_src_SOURCE_DIR})

    # vcvars64.bat, for nmake: cl.exe is in VC/Tools/MSVC/<version>/bin/Hostx64/x64.
    get_filename_component(_cl_dir "${CMAKE_CXX_COMPILER}" DIRECTORY)
    get_filename_component(_vc_dir "${_cl_dir}/../../../../../.." ABSOLUTE)
    set(VCVARS64 "${_vc_dir}/Auxiliary/Build/vcvars64.bat")
    if(NOT EXISTS "${VCVARS64}")
        message(FATAL_ERROR "vcvars64.bat not found at ${VCVARS64} (needed to build 7z.dll)")
    endif()
    set(SZ_OUT "${CMAKE_BINARY_DIR}/7z")
    file(MAKE_DIRECTORY "${SZ_OUT}")
    file(TO_NATIVE_PATH "${VCVARS64}" _vcvars)
    file(TO_NATIVE_PATH "${SZ}" _sz)
    file(TO_NATIVE_PATH "${SZ_OUT}" _sz_out)
    file(TO_NATIVE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake/build-7z.cmd" _build_7z)
    add_custom_command(OUTPUT "${SZ_OUT}/7z.dll"
        COMMAND cmd /c "${_build_7z}" "${_vcvars}" "${_sz}" "${_sz_out}"
        DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/cmake/build-7z.cmd"
        COMMENT "Building 7z.dll with 7-Zip's makefile (about 2 minutes, once)"
        VERBATIM)
    add_custom_target(sevenzip_dll DEPENDS "${SZ_OUT}/7z.dll")

    set(SZ_HELPERS
        ${SZ}/CPP/Common/IntToString.cpp ${SZ}/CPP/Common/MyString.cpp ${SZ}/CPP/Common/MyVector.cpp
        ${SZ}/CPP/Common/StringConvert.cpp ${SZ}/CPP/Common/StringToInt.cpp ${SZ}/CPP/Common/UTFConvert.cpp
        ${SZ}/CPP/Common/Wildcard.cpp
        ${SZ}/CPP/Windows/FileDir.cpp ${SZ}/CPP/Windows/FileFind.cpp ${SZ}/CPP/Windows/FileIO.cpp
        ${SZ}/CPP/Windows/FileName.cpp ${SZ}/CPP/Windows/PropVariant.cpp ${SZ}/CPP/Windows/PropVariantConv.cpp
        ${SZ}/CPP/Windows/TimeUtils.cpp
        ${SZ}/CPP/7zip/Common/FileStreams.cpp
        ${SZ}/C/Alloc.c)
    add_library(sevenzip_helpers OBJECT ${SZ_HELPERS})  # 7-Zip's own code, without our warning level
    target_include_directories(sevenzip_helpers PRIVATE ${SZ}/CPP)
    target_compile_definitions(sevenzip_helpers PRIVATE UNICODE _UNICODE)
    target_compile_options(sevenzip_helpers PRIVATE /W0)
    add_library(sevenzip STATIC src/sevenzip.cpp $<TARGET_OBJECTS:sevenzip_helpers>)
    target_include_directories(sevenzip PRIVATE ${SZ}/CPP)
    target_compile_definitions(sevenzip PRIVATE UNICODE _UNICODE)
    target_compile_options(sevenzip PRIVATE /utf-8)
    target_link_libraries(sevenzip PUBLIC oleaut32 ole32)
    add_dependencies(sevenzip sevenzip_dll)
else()
    add_library(sevenzip STATIC src/sevenzip_none.cpp)
endif()
target_include_directories(sevenzip PUBLIC src)

# The icon and version info (src/taskdeck.rc.in; the icon is drawn by tools/make_icon.py).
set(TASKDECK_ICON "${CMAKE_CURRENT_SOURCE_DIR}/assets/taskdeck.ico")
configure_file(src/taskdeck.rc.in "${CMAKE_BINARY_DIR}/taskdeck.rc" @ONLY)

add_executable(TaskDeck WIN32
    "${CMAKE_BINARY_DIR}/taskdeck.rc"
    src/main.cpp
    src/shell.cpp
    src/perf.cpp
    src/job_host.cpp
    src/file_dialog.cpp
    src/store.cpp
    src/credentials.cpp
    src/commands.cpp
    src/tray.cpp
    src/native_jobs.cpp
    src/extract_job.cpp
    src/updater.cpp)
target_link_libraries(TaskDeck PRIVATE imgui nlohmann_json::nlohmann_json sevenzip d3d11 dxgi d3dcompiler dwmapi psapi ole32 shell32
    uuid advapi32 winhttp bcrypt)
target_compile_definitions(TaskDeck PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN TASKDECK_BUILD=${TASKDECK_BUILD}
    # Where the job host script (python/) is found when TASKDECK_DESKTOP_DIR isn't set: this source tree.
    TASKDECK_DEV_DIR="${CMAKE_CURRENT_SOURCE_DIR}")

# Tests for the parts without a window (palette ranking, the state file, Credential Manager).
add_executable(core_tests tests/core_tests.cpp src/store.cpp src/credentials.cpp src/commands.cpp)
target_link_libraries(core_tests PRIVATE nlohmann_json::nlohmann_json advapi32)
target_compile_definitions(core_tests PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)

# Extract archive on the phone app's test archives (../taskdeck/tests/fixtures/archives).
add_executable(archive_tests tests/archive_tests.cpp src/extract_job.cpp)
target_link_libraries(archive_tests PRIVATE sevenzip)
target_compile_definitions(archive_tests PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN
    TASKDECK_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/../taskdeck/tests/fixtures/archives")

# 7z.dll next to the exes that load it.
if(MSVC)
    foreach(target TaskDeck archive_tests)
        add_custom_command(TARGET ${target} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different "${SZ_OUT}/7z.dll" "$<TARGET_FILE_DIR:${target}>/7z.dll")
    endforeach()
endif()

foreach(target TaskDeck core_tests archive_tests)
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

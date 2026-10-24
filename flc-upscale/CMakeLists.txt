cmake_minimum_required(VERSION 3.21)

project(FLC_Upscale LANGUAGES C CXX)

set(CMAKE_C_STANDARD 99)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# AUTO uses a library when it is found; ON makes a missing library an error; OFF leaves it out.
set(UPSCALE_WITH_IMAGEMAGICK "AUTO" CACHE STRING "Build the ImageMagick backend (AUTO, ON, OFF)")
set(UPSCALE_WITH_OPENCV      "AUTO" CACHE STRING "Build the OpenCV backend (AUTO, ON, OFF)")
set_property(CACHE UPSCALE_WITH_IMAGEMAGICK PROPERTY STRINGS AUTO ON OFF)
set_property(CACHE UPSCALE_WITH_OPENCV      PROPERTY STRINGS AUTO ON OFF)

add_library(Upscale STATIC src/Upscale.c)
target_include_directories(Upscale PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(Upscale PROPERTIES POSITION_INDEPENDENT_CODE ON)

if(MSVC)
    target_compile_options(Upscale PRIVATE /W4)
    target_compile_definitions(Upscale PRIVATE _CRT_SECURE_NO_WARNINGS)
else()
    target_compile_options(Upscale PRIVATE -Wall -Wextra)
endif()


# --- ImageMagick (MagickWand) ---
# pkg-config first: it carries MAGICKCORE_QUANTUM_DEPTH / MAGICKCORE_HDRI_ENABLE, which ImageMagick 7 headers need.
# find_package(ImageMagick) is the fallback for Windows installs without pkg-config.

set(UPSCALE_HAVE_IMAGEMAGICK OFF)
if(NOT UPSCALE_WITH_IMAGEMAGICK STREQUAL "OFF")
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(MAGICKWAND IMPORTED_TARGET MagickWand)
    endif()
    if(MAGICKWAND_FOUND)
        target_sources(Upscale PRIVATE src/Upscale_ImageMagick.c)
        target_link_libraries(Upscale PRIVATE PkgConfig::MAGICKWAND)
        set(IMAGEMAGICK_VERSION_FOR_UPSCALE "${MAGICKWAND_VERSION}")
        set(UPSCALE_HAVE_IMAGEMAGICK ON)
    else()
        find_package(ImageMagick COMPONENTS MagickWand)
        if(ImageMagick_MagickWand_FOUND)
            target_sources(Upscale PRIVATE src/Upscale_ImageMagick.c)
            target_include_directories(Upscale PRIVATE ${ImageMagick_MagickWand_INCLUDE_DIRS})
            target_link_libraries(Upscale PRIVATE ${ImageMagick_MagickWand_LIBRARY})
            set(UPSCALE_MAGICKCORE_QUANTUM_DEPTH "16" CACHE STRING "MAGICKCORE_QUANTUM_DEPTH of the ImageMagick install (find_package fallback only)")
            set(UPSCALE_MAGICKCORE_HDRI_ENABLE   "1"  CACHE STRING "MAGICKCORE_HDRI_ENABLE of the ImageMagick install (find_package fallback only)")
            target_compile_definitions(Upscale PRIVATE
                MAGICKCORE_QUANTUM_DEPTH=${UPSCALE_MAGICKCORE_QUANTUM_DEPTH}
                MAGICKCORE_HDRI_ENABLE=${UPSCALE_MAGICKCORE_HDRI_ENABLE}
            )
            set(IMAGEMAGICK_VERSION_FOR_UPSCALE "${ImageMagick_VERSION_STRING}")
            set(UPSCALE_HAVE_IMAGEMAGICK ON)
        endif()
    endif()
    if(UPSCALE_HAVE_IMAGEMAGICK)
        target_compile_definitions(Upscale PUBLIC UPSCALE_HAVE_IMAGEMAGICK)
        if(IMAGEMAGICK_VERSION_FOR_UPSCALE VERSION_GREATER_EQUAL "7")
            target_compile_definitions(Upscale PRIVATE UPSCALE_IMAGEMAGICK_7)
        endif()
        message(STATUS "Upscale: ImageMagick backend ON (MagickWand ${IMAGEMAGICK_VERSION_FOR_UPSCALE})")
    elseif(UPSCALE_WITH_IMAGEMAGICK STREQUAL "ON")
        message(FATAL_ERROR "Upscale: UPSCALE_WITH_IMAGEMAGICK=ON but MagickWand was not found")
    else()
        message(STATUS "Upscale: ImageMagick backend OFF (MagickWand not found)")
    endif()
endif()


# --- OpenCV ---

set(UPSCALE_HAVE_OPENCV OFF)
if(NOT UPSCALE_WITH_OPENCV STREQUAL "OFF")
    find_package(OpenCV QUIET COMPONENTS core imgproc imgcodecs OPTIONAL_COMPONENTS dnn_superres)
    if(OpenCV_FOUND)
        target_sources(Upscale PRIVATE src/Upscale_OpenCV.cpp)
        target_include_directories(Upscale PRIVATE ${OpenCV_INCLUDE_DIRS})
        target_link_libraries(Upscale PRIVATE opencv_core opencv_imgproc opencv_imgcodecs)
        target_compile_definitions(Upscale PUBLIC UPSCALE_HAVE_OPENCV)
        if(TARGET opencv_dnn_superres)
            target_link_libraries(Upscale PRIVATE opencv_dnn_superres)
            target_compile_definitions(Upscale PRIVATE UPSCALE_HAVE_OPENCV_SUPERRES)
            set(UPSCALE_OPENCV_SUPERRES_NOTE " with dnn_superres")
        endif()
        set(UPSCALE_HAVE_OPENCV ON)
        message(STATUS "Upscale: OpenCV backend ON (OpenCV ${OpenCV_VERSION}${UPSCALE_OPENCV_SUPERRES_NOTE})")
    elseif(UPSCALE_WITH_OPENCV STREQUAL "ON")
        message(FATAL_ERROR "Upscale: UPSCALE_WITH_OPENCV=ON but OpenCV was not found")
    else()
        message(STATUS "Upscale: OpenCV backend OFF (OpenCV not found)")
    endif()
endif()

if(NOT UPSCALE_HAVE_IMAGEMAGICK AND NOT UPSCALE_HAVE_OPENCV)
    message(WARNING "Upscale: neither ImageMagick nor OpenCV was found; every Upscale_Image() call will return UPSCALE_ERR_NO_BACKEND")
endif()


# --- tests ---

if(PROJECT_IS_TOP_LEVEL)
    include(CTest)
    if(BUILD_TESTING)
        add_executable(test_upscale tests/test_upscale.c)
        target_link_libraries(test_upscale PRIVATE Upscale)
        file(MAKE_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/test_output)
        add_test(NAME test_upscale COMMAND test_upscale ${CMAKE_CURRENT_BINARY_DIR}/test_output)
    endif()
endif()

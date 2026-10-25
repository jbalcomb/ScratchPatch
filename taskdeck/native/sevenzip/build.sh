#!/usr/bin/env bash
# Build 7-Zip's archive library (lib7z.so) and the C shim (lib7zshim.so).
#
#   native/sevenzip/build.sh <7-Zip source dir> <output dir> [CC] [CXX]
#
# Without CC/CXX it builds for this machine (tests); in CI the Android NDK's clang is passed.
set -euo pipefail

SRC=$(cd "$1" && pwd)
OUT=$(mkdir -p "$2" && cd "$2" && pwd)
CC=${3:-gcc}
CXX=${4:-g++}
HERE=$(cd "$(dirname "$0")" && pwd)
JOBS=$(nproc 2>/dev/null || echo 4)

# 1. 7-Zip's full-format library, with its own makefile (all formats, incl. RAR and encryption).
#    Built as a non-installed copy so this script leaves the source tree as it found it.
WORK="$OUT/work"
rm -rf "$WORK" && cp -r "$SRC" "$WORK"
make -C "$WORK/CPP/7zip/Bundles/Format7zF" -f makefile.gcc -j"$JOBS" CC="$CC" CXX="$CXX" >/dev/null
cp "$WORK/CPP/7zip/Bundles/Format7zF/_o/7z.so" "$OUT/lib7z.so"

# 2. The shim: our sz_shim.cpp plus the 7-Zip helper sources it uses (same list as 7-Zip's
#    example client), linked into one shared library. libc++ is linked statically so the .so
#    doesn't depend on the app shipping a matching libc++_shared.so.
C="$WORK/CPP"
FLAGS=(-O2 -fPIC -DNDEBUG -D_REENTRANT -D_FILE_OFFSET_BITS=64 -D_LARGEFILE_SOURCE -I"$C" -fvisibility=hidden)
OBJ="$OUT/obj" && mkdir -p "$OBJ"
SOURCES=(
  "$C/Common/IntToString.cpp" "$C/Common/MyString.cpp" "$C/Common/MyVector.cpp" "$C/Common/NewHandler.cpp"
  "$C/Common/StringConvert.cpp" "$C/Common/StringToInt.cpp" "$C/Common/UTFConvert.cpp" "$C/Common/Wildcard.cpp"
  "$C/Common/MyWindows.cpp"
  "$C/Windows/FileDir.cpp" "$C/Windows/FileFind.cpp" "$C/Windows/FileIO.cpp" "$C/Windows/FileName.cpp"
  "$C/Windows/PropVariant.cpp" "$C/Windows/PropVariantConv.cpp" "$C/Windows/TimeUtils.cpp"
  "$C/7zip/Common/FileStreams.cpp"
  "$HERE/sz_shim.cpp"
)
objs=()
for f in "${SOURCES[@]}"; do
  o="$OBJ/$(basename "${f%.cpp}").o"
  "$CXX" "${FLAGS[@]}" -c "$f" -o "$o"
  objs+=("$o")
done
"$CC" -O2 -fPIC -DNDEBUG -D_REENTRANT -I"$WORK/C" -c "$WORK/C/Alloc.c" -o "$OBJ/Alloc.o"
objs+=("$OBJ/Alloc.o")
"$CXX" -shared -o "$OUT/lib7zshim.so" "${objs[@]}" -static-libstdc++ -ldl -Wl,-z,noexecstack -s

rm -rf "$WORK" "$OBJ"
echo "built: $OUT/lib7z.so $OUT/lib7zshim.so"

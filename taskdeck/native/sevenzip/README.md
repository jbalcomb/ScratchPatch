# 7-Zip engine for TaskDeck

* `sz_shim.cpp`: a plain C API over 7-Zip's archive library (open with password, list, extract with
  progress/cancel, safe paths). Python calls it through `ctypes` (`src/taskdeck/shared/archive.py`).
* `build.sh <7-Zip source> <out> [CC] [CXX]`: builds `lib7z.so` (7-Zip's own Format7zF makefile:
  every format incl. RAR and encryption) and `lib7zshim.so`.
* `7ZIP_VERSION`: the 7-Zip release used (tag in https://github.com/ip7z/7zip).
* `package/`: the `taskdeck_sevenzip` Python package that carries the two `.so` files. The
  "TaskDeck 7-Zip engine" workflow builds it for Android arm64 with the NDK and publishes the wheel to
  the `taskdeck-sevenzip` release; `pyproject.toml` installs it from there.

Local build and tests (Linux):

    git clone --depth 1 --branch $(cat native/sevenzip/7ZIP_VERSION) https://github.com/ip7z/7zip /tmp/7zip
    native/sevenzip/build.sh /tmp/7zip /tmp/7z-linux
    TASKDECK_7Z_DIR=/tmp/7z-linux pytest tests/test_archive.py

License: 7-Zip is GNU LGPL with the unRAR restriction (the RAR code may not be used to create RAR
archives); see https://www.7-zip.org/license.txt. The shim is part of TaskDeck.

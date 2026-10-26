"""Archives (zip, 7z, RAR, tar, gz, ...) through 7-Zip's library.

The native part is two libraries built from 7-Zip's source plus our C shim
(``native/sevenzip``): ``lib7z.so`` (7-Zip's archive library) and ``lib7zshim.so`` (a
plain C API over it). They ship in the ``taskdeck_sevenzip`` package, which the APK
build installs; on Android the build moves them next to the app's other native
libraries. Python on Android turns a bare library name into "./name", so they are loaded
by full location, the way Flet's own loader does: the app's native-library folder
(``ANDROID_NATIVE_LIBRARY_DIR``) or the path inside the installed APK
(``ANDROID_APK_NATIVE_PREFIX``, e.g. ``.../base.apk!/lib/arm64-v8a/``). Elsewhere (tests,
desktop) set ``TASKDECK_7Z_DIR`` to the folder holding them.

Plain ``.tar``/``.tgz``/``.tar.gz``/``.tar.bz2``/``.tar.xz`` go through Python's ``tarfile``
instead: 7-Zip opens the compressed layer and the tar inside separately.
"""

from __future__ import annotations

import ctypes
import os
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

TAR_SUFFIXES = (".tar", ".tgz", ".tar.gz", ".tbz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")

# Error codes from sz_shim.cpp
OK, E_LOAD, E_OPEN_FILE, E_UNSUPPORTED, E_NEED_PASSWORD, E_WRONG_PASSWORD, E_DATA, E_CANCELLED, E_WRITE, \
    E_UNSAFE_PATH, E_UNSUPPORTED_METHOD, E_OTHER = range(12)

MESSAGES = {
    E_LOAD: "The archive engine isn't installed in this app version. Install the latest APK.",
    E_OPEN_FILE: "Can't read the file.",
    E_UNSUPPORTED: "This isn't an archive TaskDeck can open (or it is damaged).",
    E_NEED_PASSWORD: "This archive is password-protected. Enter the password.",
    E_WRONG_PASSWORD: "Wrong password.",
    E_DATA: "The archive is damaged.",
    E_CANCELLED: "Cancelled.",
    E_WRITE: "Can't write the files there (no permission, or the storage is full).",
    E_UNSAFE_PATH: "The archive contains a path that points outside the target folder; nothing was extracted from it.",
    E_UNSUPPORTED_METHOD: "The archive uses a compression method that isn't supported.",
    E_OTHER: "Extracting failed.",
}

Progress = Callable[[int, int], bool]  # (done bytes, total bytes) -> True to cancel


class ArchiveError(Exception):
    def __init__(self, code: int, entry: str = "", detail: str = ""):
        self.code = code
        self.entry = entry
        self.detail = detail  # technical reason (e.g. why the engine didn't load), for diagnosis
        message = MESSAGES.get(code, f"Archive error {code}.")
        if entry and code in (E_DATA, E_WRONG_PASSWORD, E_WRITE, E_UNSAFE_PATH, E_UNSUPPORTED_METHOD):
            message += f" ({entry})"
        super().__init__(message)


@dataclass(frozen=True)
class Entry:
    name: str
    size: int
    is_dir: bool
    encrypted: bool


# ---- the native engine ------------------------------------------------------------------

_engine: ctypes.CDLL | None = None
_engine_lock = threading.Lock()
_PROGRESS = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p)


def _candidates(name: str) -> list[str]:
    folder = os.environ.get("TASKDECK_7Z_DIR")
    out = [str(Path(folder) / name)] if folder else []
    try:
        import taskdeck_sevenzip  # the package that carries the libraries

        out.append(str(Path(taskdeck_sevenzip.__file__).parent / name))
    except ImportError:
        pass
    # Android: the build moved the libraries next to the app's other native libraries.
    native_dir = os.environ.get("ANDROID_NATIVE_LIBRARY_DIR")
    if native_dir:
        out.append(f"{native_dir}/{name}")
    apk_prefix = os.environ.get("ANDROID_APK_NATIVE_PREFIX")
    if apk_prefix:
        out.append(apk_prefix + name)
    out.append(name)
    return out


def engine() -> ctypes.CDLL:
    """Load 7-Zip and the shim once. Raises ArchiveError(E_LOAD) if they aren't installed."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        shim = None
        tried = []
        for path in _candidates("lib7zshim.so"):
            try:
                shim = ctypes.CDLL(path)
                break
            except OSError as exc:
                tried.append(f"{path}: {exc}")
        if shim is None:
            raise ArchiveError(E_LOAD, detail="\n".join(tried))
        shim.sz_load.argtypes = [ctypes.c_char_p]
        shim.sz_version.restype = ctypes.c_char_p
        shim.sz_load_error.restype = ctypes.c_char_p
        shim.sz_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        shim.sz_close.argtypes = [ctypes.c_void_p]
        shim.sz_format.argtypes = [ctypes.c_void_p]
        shim.sz_format.restype = ctypes.c_char_p
        shim.sz_count.argtypes = [ctypes.c_void_p]
        shim.sz_count.restype = ctypes.c_uint32
        shim.sz_item.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                                 ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_int),
                                 ctypes.POINTER(ctypes.c_int)]
        shim.sz_item.restype = ctypes.c_uint32
        shim.sz_extract.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, _PROGRESS, ctypes.c_void_p,
                                    ctypes.c_char_p, ctypes.c_uint32]
        for path in _candidates("lib7z.so"):
            if shim.sz_load(path.encode()) == OK:
                break
            tried.append(f"{path}: {(shim.sz_load_error() or b'').decode(errors='replace')}")
        else:
            raise ArchiveError(E_LOAD, detail="\n".join(tried))
        _engine = shim
        return shim


def available() -> bool:
    return load_problem() is None


def load_problem() -> str | None:
    """None if the engine loads, else why not (message plus the loader's details)."""
    try:
        engine()
        return None
    except ArchiveError as exc:
        return f"{exc}\n{exc.detail}".strip()


def version() -> str:
    return engine().sz_version().decode()


def is_tar(path: str | Path) -> bool:
    return str(path).lower().endswith(TAR_SUFFIXES)


# ---- archives ------------------------------------------------------------------------------

def _pw(password: str | None) -> bytes | None:
    return None if password is None else password.encode()


def safe_target(dest: Path, name: str) -> Path | None:
    """Where an entry lands under dest, or None if its path escapes dest."""
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("/", ".", "")]
    if not parts or ".." in parts:
        return None
    return dest.joinpath(*parts)


class Archive:
    """An open archive. Use as a context manager: ``with Archive(path, password) as a: ...``"""

    def __init__(self, path: str | Path, password: str | None = None):
        self.path = Path(path)
        self.password = password
        self._handle = ctypes.c_void_p()
        self._tar: tarfile.TarFile | None = None
        if is_tar(self.path):
            try:
                self._tar = tarfile.open(self.path)
            except FileNotFoundError:
                raise ArchiveError(E_OPEN_FILE) from None
            except (tarfile.TarError, OSError, EOFError):
                raise ArchiveError(E_UNSUPPORTED) from None
            self.format = "tar"
            return
        lib = engine()
        code = lib.sz_open(str(self.path).encode(), _pw(password), ctypes.byref(self._handle))
        if code != OK:
            raise ArchiveError(code)
        self.format = lib.sz_format(self._handle).decode() or "archive"

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None
        if self._handle:
            engine().sz_close(self._handle)
            self._handle = ctypes.c_void_p()

    def entries(self) -> list[Entry]:
        if self._tar is not None:
            return [Entry(m.name, m.size, m.isdir(), False) for m in self._tar.getmembers()]
        lib = engine()
        out = []
        size, is_dir, enc = ctypes.c_uint64(), ctypes.c_int(), ctypes.c_int()
        buf = ctypes.create_string_buffer(4096)
        for i in range(lib.sz_count(self._handle)):
            n = lib.sz_item(self._handle, i, buf, len(buf), ctypes.byref(size), ctypes.byref(is_dir), ctypes.byref(enc))
            if n >= len(buf):  # long name: ask again with room for it
                buf = ctypes.create_string_buffer(n + 1)
                lib.sz_item(self._handle, i, buf, len(buf), None, None, None)
            out.append(Entry(buf.value.decode("utf-8", "replace"), size.value, bool(is_dir.value), bool(enc.value)))
        return out

    def unsafe_entries(self) -> list[str]:
        """Entries whose path would escape the target folder (checked before extracting)."""
        root = Path("/x")
        return [e.name for e in self.entries() if safe_target(root, e.name) is None]

    def extract(self, dest: str | Path, progress: Progress | None = None, password: str | None = None) -> None:
        """Extract everything into dest (created if needed), replacing existing files."""
        dest = Path(dest)
        bad = self.unsafe_entries()
        if bad:
            raise ArchiveError(E_UNSAFE_PATH, bad[0])
        if self._tar is not None:
            self._extract_tar(dest, progress)
            return
        cancelled = False

        def on_progress(done, total, _user):
            nonlocal cancelled
            if progress and progress(int(done), int(total)):
                cancelled = True
                return 1
            return 0

        callback = _PROGRESS(on_progress)  # keep a reference while the C code runs
        failed = ctypes.create_string_buffer(1024)
        code = engine().sz_extract(self._handle, str(dest).encode(),
                                   _pw(password if password is not None else self.password),
                                   callback, None, failed, len(failed))
        if code != OK:
            raise ArchiveError(E_CANCELLED if cancelled else code, failed.value.decode("utf-8", "replace"))

    def _extract_tar(self, dest: Path, progress: Progress | None) -> None:
        members = self._tar.getmembers()
        total = sum(m.size for m in members if m.isfile())
        done = 0
        dest.mkdir(parents=True, exist_ok=True)
        for m in members:
            target = safe_target(dest, m.name)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with self._tar.extractfile(m) as src, open(target, "wb") as out:
                        while chunk := src.read(1 << 20):
                            out.write(chunk)
                            done += len(chunk)
                            if progress and progress(done, total):
                                raise ArchiveError(E_CANCELLED, m.name)
                except OSError:
                    raise ArchiveError(E_WRITE, m.name) from None
            # links and devices are skipped: not meaningful on a phone, and links can point anywhere

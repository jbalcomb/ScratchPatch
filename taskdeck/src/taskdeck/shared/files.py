"""Phone storage: where things are, folder listings, names and sizes.

On Android this works on real paths under /storage, which needs the "All files access"
permission (see ``taskdeck.ui.file_access``). On a desktop it uses the home folder, so
jobs and tests behave the same.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ANDROID_PHONE = Path("/storage/emulated/0")
ARCHIVE_EXTENSIONS = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",  # two-part extensions first
    ".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz", ".tbz2", ".txz", ".gz", ".bz2", ".xz", ".zst", ".lzma",
    ".cab", ".iso", ".jar", ".apk", ".zipx", ".001",
)


@dataclass(frozen=True)
class Location:
    label: str
    path: Path
    icon: str  # flet icon name


@dataclass(frozen=True)
class Item:
    path: Path
    is_dir: bool
    size: int
    modified: float

    @property
    def name(self) -> str:
        return self.path.name


def is_android() -> bool:
    return hasattr(sys, "getandroidapilevel") or "ANDROID_ARGUMENT" in os.environ or "ANDROID_ROOT" in os.environ


def locations() -> list[Location]:
    """Downloads, the phone's storage and any SD card (on a desktop: Downloads and home)."""
    if is_android():
        out = [Location("Downloads", ANDROID_PHONE / "Download", "DOWNLOAD"),
               Location("Phone", ANDROID_PHONE, "SMARTPHONE")]
        try:
            for entry in sorted(Path("/storage").iterdir()):
                if entry.name not in ("emulated", "self") and entry.is_dir():
                    out.append(Location("SD card", entry, "SD_CARD"))
        except OSError:
            pass
        return out
    home = Path.home()
    downloads = home / "Downloads"
    return ([Location("Downloads", downloads, "DOWNLOAD")] if downloads.is_dir() else []) + \
        [Location("Home", home, "HOME")]


def is_archive(name: str) -> bool:
    low = name.lower()
    return low.endswith(ARCHIVE_EXTENSIONS) or (low.endswith(".001") and ".7z." in low)


def archive_stem(name: str) -> str:
    """The folder name for an archive: its name without the archive extension.
    "CoM2ModWarlord1.5.12.6.2.7z" -> "CoM2ModWarlord1.5.12.6.2", "site.tar.gz" -> "site"."""
    low = name.lower()
    for ext in ARCHIVE_EXTENSIONS:
        if low.endswith(ext) and len(name) > len(ext):
            stem = name[: -len(ext)]
            return stem[:-4] if stem.lower().endswith(".tar") else stem  # "x.tar" left by ".gz"
    return Path(name).stem or name


def list_folder(folder: Path, archives_only: bool = False, show_hidden: bool = False) -> list[Item]:
    """Folders first (A–Z), then files newest first. Raises PermissionError / OSError."""
    folders, files = [], []
    with os.scandir(folder) as it:
        for entry in it:
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
                st = entry.stat()
            except OSError:
                continue
            if is_dir:
                folders.append(Item(Path(entry.path), True, 0, st.st_mtime))
            elif not archives_only or is_archive(entry.name):
                files.append(Item(Path(entry.path), False, st.st_size, st.st_mtime))
    folders.sort(key=lambda i: i.name.lower())
    files.sort(key=lambda i: -i.modified)
    return folders + files


def free_space(path: Path) -> int:
    """Free bytes on the storage holding path (or its nearest existing parent)."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def unique_name(parent: Path, name: str) -> str:
    """name, or "name (2)", "name (3)"... whichever doesn't exist yet in parent."""
    if not (parent / name).exists():
        return name
    n = 2
    while (parent / f"{name} ({n})").exists():
        n += 1
    return f"{name} ({n})"


def valid_folder_name(name: str) -> str | None:
    """An error message, or None if name can be used as a folder name."""
    if not name.strip():
        return "Enter a folder name."
    if name in (".", "..") or "/" in name or "\0" in name:
        return "A folder name can't contain “/”."
    if len(name.encode()) > 255:
        return "That name is too long."
    return None


def display_path(path: Path) -> str:
    """Phone › Download › photos instead of /storage/emulated/0/Download/photos."""
    for loc in sorted(locations(), key=lambda l: -len(str(l.path))):
        try:
            rel = path.relative_to(loc.path)
        except ValueError:
            continue
        on_phone = loc.label == "Downloads" and loc.path == ANDROID_PHONE / "Download"
        base = ["Phone", "Download"] if on_phone else [loc.label]
        return " › ".join(base + list(rel.parts))
    return str(path)


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n):,} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def human_speed(bytes_per_second: float) -> str:
    """512 B/s, 1.2 KB/s, 3.4 MB/s, 1.1 GB/s... (same 1024 steps as human_size)."""
    n = max(0.0, bytes_per_second)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s", "TB/s"):
        if n < 1024 or unit == "TB/s":
            return f"{int(n)} {unit}" if unit == "B/s" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return "less than a minute" if seconds >= 30 else "a few seconds"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min"
    return f"about {minutes // 60} h {minutes % 60:02d} min"

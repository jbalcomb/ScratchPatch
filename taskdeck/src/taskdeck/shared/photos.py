"""Photos on the phone, for the photo picker: finding them newest first, and the small thumbnail a camera stores
inside each JPEG (EXIF), so a grid of hundreds of photos never reads whole photos. Standard library only."""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic")
SKIP_FOLDERS = {"Android", ".thumbnails", ".trashed", "cache"}
MAX_DEPTH = 3       # DCIM/Camera/... is deep enough; the phone's root is not scanned to the bottom
MAX_PHOTOS = 5000   # stop scanning after this many (newest first is then among these)
HEAD_BYTES = 128 * 1024  # an EXIF block (with its thumbnail) is at most 64 KB, near the start of the file


@dataclass(frozen=True)
class Photo:
    path: Path
    modified: float

    @property
    def name(self) -> str:
        return self.path.name


def is_image(name: str) -> bool:
    return name.lower().endswith(IMAGE_EXTENSIONS)


def scan(folder: Path, max_depth: int = MAX_DEPTH, limit: int = MAX_PHOTOS) -> list[Photo]:
    """Images in ``folder`` and its subfolders (hidden ones and Android/ skipped), newest first.
    A folder that can't be read is skipped."""
    found: list[Photo] = []
    todo = [(folder, 0)]
    while todo and len(found) < limit:
        current, depth = todo.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name.startswith(".") or entry.name in SKIP_FOLDERS:
                        continue
                    try:
                        if entry.is_dir():
                            if depth < max_depth:
                                todo.append((Path(entry.path), depth + 1))
                        elif is_image(entry.name):
                            found.append(Photo(Path(entry.path), entry.stat().st_mtime))
                    except OSError:
                        continue
        except OSError:
            continue
    found.sort(key=lambda p: -p.modified)
    return found


def subfolders(folder: Path) -> list[Path]:
    """Visible subfolders, A–Z (for the folder chips)."""
    try:
        with os.scandir(folder) as it:
            out = [Path(e.path) for e in it if not e.name.startswith(".") and e.name not in SKIP_FOLDERS and e.is_dir()]
    except OSError:
        return []
    return sorted(out, key=lambda p: p.name.lower())


def day_label(ts: float, now: float | None = None) -> str:
    """Google Photos style headings: Today, Yesterday, weekday this week, else "Nov 3" (", 2025" in another year)."""
    now = time.time() if now is None else now
    t, n = time.localtime(ts), time.localtime(now)
    days = (time.mktime(n[:3] + (0, 0, 0, 0, 0, -1)) - time.mktime(t[:3] + (0, 0, 0, 0, 0, -1))) / 86400
    if round(days) == 0:
        return "Today"
    if round(days) == 1:
        return "Yesterday"
    if 1 < days < 7:
        return time.strftime("%A", t)
    label = f"{time.strftime('%b', t)} {t.tm_mday}"
    return label if t.tm_year == n.tm_year else f"{label}, {t.tm_year}"


def exif_thumbnail(path: Path) -> tuple[bytes | None, int]:
    """(the JPEG thumbnail stored in the photo's EXIF, or None; the photo's EXIF orientation, 1 when unknown).
    Orientation 3, 6 and 8 mean the picture is shown turned 180°, 90° clockwise and 90° counter-clockwise."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return None, 1
    if head[:2] != b"\xff\xd8":
        return None, 1
    i = 2
    while i + 4 <= len(head) and head[i] == 0xFF:
        marker, size = head[i + 1], struct.unpack(">H", head[i + 2:i + 4])[0]
        if marker == 0xE1 and head[i + 4:i + 10] == b"Exif\x00\x00":
            return _from_tiff(head[i + 10:i + 2 + size])
        if marker in (0xDA, 0xD9):  # image data starts: no EXIF
            break
        i += 2 + size
    return None, 1


def _from_tiff(tiff: bytes) -> tuple[bytes | None, int]:
    try:
        endian = {b"II": "<", b"MM": ">"}[tiff[:2]]
        u16 = lambda off: struct.unpack(endian + "H", tiff[off:off + 2])[0]  # noqa: E731
        u32 = lambda off: struct.unpack(endian + "I", tiff[off:off + 4])[0]  # noqa: E731

        def tags(ifd: int) -> dict[int, int]:
            out = {}
            for n in range(u16(ifd)):
                entry = ifd + 2 + 12 * n
                tag, kind = u16(entry), u16(entry + 2)
                out[tag] = u16(entry + 8) if kind == 3 else u32(entry + 8)
            return out

        ifd0 = u32(4)
        orientation = tags(ifd0).get(0x0112, 1)
        next_ifd = u32(ifd0 + 2 + 12 * u16(ifd0))
        if not next_ifd:
            return None, orientation
        ifd1 = tags(next_ifd)
        start, length = ifd1.get(0x0201), ifd1.get(0x0202)
        if not start or not length or start + length > len(tiff):
            return None, orientation
        thumb = tiff[start:start + length]
        return (thumb if thumb[:2] == b"\xff\xd8" else None), orientation
    except (KeyError, struct.error, IndexError):
        return None, 1

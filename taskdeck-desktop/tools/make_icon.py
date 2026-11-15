"""Draws TaskDeck's icon (assets/taskdeck.ico): a blue rounded square holding a 2x2 deck of tiles, one of them
amber (a pinned job). Standard library only. Each size is drawn on its own grid, so the small ones stay sharp;
edges are smoothed by sampling each pixel 8x8 times.

    python tools/make_icon.py        (from taskdeck-desktop/; writes assets/taskdeck.ico)
"""
from __future__ import annotations

import pathlib
import struct
import zlib

SIZES = [16, 20, 24, 32, 40, 48, 64, 256]
BG_TOP, BG_BOTTOM = (0x5B, 0x8D, 0xF6), (0x2F, 0x5B, 0xD8)
TILE, PINNED = (0xFF, 0xFF, 0xFF), (0xFF, 0xC2, 0x4B)


def rounded(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def layout(n: int) -> tuple[tuple, list]:
    """The background square and the four tiles, in pixels of an n x n icon."""
    m = 0 if n <= 24 else n * 0.04  # small icons use every pixel
    bg = (m, m, n - m, n - m, n * 0.22)
    pad = round(n * 0.19)
    gap = max(1, round(n * 0.09))
    t = (n - 2 * pad - gap) / 2
    r = max(0.6, t * 0.22)
    tiles = []
    for row in range(2):
        for col in range(2):
            x0, y0 = pad + col * (t + gap), pad + row * (t + gap)
            tiles.append(((x0, y0, x0 + t, y0 + t, r), PINNED if (row, col) == (0, 0) else TILE))
    return bg, tiles


def draw(n: int) -> bytes:
    bg, tiles = layout(n)
    ss = 8
    rows = []
    for py in range(n):
        row = bytearray()
        for px in range(n):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    x, y = px + (sx + 0.5) / ss, py + (sy + 0.5) / ss
                    if not rounded(x, y, *bg):
                        continue
                    k = y / n
                    color = tuple(BG_TOP[i] * (1 - k) + BG_BOTTOM[i] * k for i in range(3))
                    for rect, c in tiles:
                        if rounded(x, y, *rect):
                            color = c
                            break
                    for i in range(3):
                        acc[i] += color[i]
                    acc[3] += 1
            a = acc[3] / (ss * ss)
            if acc[3]:
                row += bytes(round(acc[i] / acc[3]) for i in range(3)) + bytes([round(a * 255)])
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return png(n, rows)


def png(n: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\0" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """An .ico with PNG images (read by Windows Vista and later, and by rc.exe)."""
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for n, data in images:
        out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(data for _, data in images)


def main() -> None:
    target = pathlib.Path(__file__).resolve().parent.parent / "assets" / "taskdeck.ico"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(ico([(n, draw(n)) for n in SIZES]))
    (target.parent / "taskdeck-256.png").write_bytes(draw(256))  # for the README and a look at it
    print(target, target.stat().st_size, "bytes")


if __name__ == "__main__":
    main()

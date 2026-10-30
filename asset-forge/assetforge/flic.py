"""Decode Master of Magic FLIC sprites (the graphics inside LBX entries) to palette indices.

Ported from ReMoM MoX/src/FLIC_Draw.c: FLIC_Draw(), FLIC_Draw_Frame(), FLIC_Remap_Draw_Frame(),
Draw_Picture_To_Bitmap() and FLIC_Load_Palette(). Layout, from MoX/src/FLIC_Draw.h:

  0x00 i16 width          0x08 i16 loop_frame
  0x02 i16 height         0x0E i16 palette_header_offset (0 = uses the game palette)
  0x06 i16 frame_count    0x10 u8  full store flag (nonzero: every frame is complete; zero: frame N is drawn over 0..N-1)
                          0x11 u8  remap flag (pixel values >= 232 shade what is underneath)
  0x12     u32[frame_count + 1] frame offsets; frame data starts one byte past its offset

Frames are column-major. Each column starts with an op byte: 0xFF skips the column, otherwise a packet byte count
follows, then runs of (count, skip-down) + pixels. Op 0x80 packs runs: a byte >= 224 repeats the next byte
(b - 224 + 1) times. Op 0x00 copies bytes as they are. The engine treats any other op as an empty column.

The decode yields, per pixel, a palette index and a coverage value:
  COVER_NONE   nothing drawn there (transparent on screen)
  COVER_PIXEL  an ordinary pixel
  COVER_REMAP  a remap (shading) pixel: the index is 232 + remap block; the game recolours whatever is underneath
"""

import struct
from dataclasses import dataclass, field

COVER_NONE = 0
COVER_PIXEL = 1
COVER_REMAP = 2

REMAP_FIRST = 232
REPEAT_FIRST = 224


class FlicError(ValueError):
    pass


@dataclass
class FlicHeader:
    width: int
    height: int
    frame_count: int
    loop_frame: int
    palette_header_offset: int
    full_store: int
    remap: int


@dataclass
class Frame:
    width: int
    height: int
    index: bytearray      # width * height, row-major
    cover: bytearray      # width * height, row-major, COVER_*


@dataclass
class Sprite:
    header: FlicHeader
    frames: list
    palette: list = None          # [(start, [(r, g, b) 6-bit, ...])] palette chunks the sprite loads, or None
    warnings: list = field(default_factory=list)


def read_header(data):
    """Return the FlicHeader if the bytes plausibly hold a sprite, else None."""
    if len(data) < 0x12:
        return None
    w, h, _cur, n, loop = struct.unpack_from("<hhhhh", data, 0)
    pal_ofs = struct.unpack_from("<h", data, 0x0E)[0]
    if not (0 < w <= 640 and 0 < h <= 480 and 0 < n <= 4000):
        return None
    table_end = 0x12 + 4 * (n + 1)
    if len(data) < table_end:
        return None
    offsets = struct.unpack_from(f"<{n + 1}I", data, 0x12)
    # Data entries can start with plausible numbers (HLPENTRY.LBX does); a sprite's frame table is ordered and in bounds.
    if offsets[0] < table_end or offsets[-1] > len(data) or any(a > b for a, b in zip(offsets, offsets[1:])):
        return None
    return FlicHeader(w, h, n, loop, pal_ofs, data[0x10], data[0x11])


def _draw_frame(data, pos, frame, remap, warnings, label):
    """Decode one frame's columns starting at data[pos] onto frame (drawing over what is there)."""
    w, h = frame.width, frame.height
    n = len(data)

    def byte():
        nonlocal pos
        if pos >= n:
            raise FlicError(f"{label}: frame data runs past the end of the entry")
        b = data[pos]
        pos += 1
        return b

    def put(x, y, value, cover):
        if y >= h:
            warnings.append(f"{label}: column {x} writes below the sprite (y={y})")
            return
        frame.index[y * w + x] = value
        frame.cover[y * w + x] = cover

    def pixel(x, y, value):
        if remap and value >= REMAP_FIRST:
            put(x, y, value, COVER_REMAP)
        else:
            put(x, y, value, COVER_PIXEL)

    for x in range(w):
        op = byte()
        if op == 0xFF:
            continue
        packet = byte()
        if op not in (0x80, 0x00):
            warnings.append(f"{label}: column {x} has unknown op 0x{op:02X}; drawn empty, as the engine does")
            continue
        y = 0
        while True:
            seq = byte()
            y += byte()
            packet = (packet - (seq + 2)) & 0xFF
            while seq:
                seq -= 1
                b = byte()
                if op == 0x80 and b >= REPEAT_FIRST:
                    count = b - REPEAT_FIRST + 1
                    if seq == 0:
                        raise FlicError(f"{label}: column {x} repeat op has no value byte")
                    seq -= 1
                    b = byte()
                    for _ in range(count):
                        pixel(x, y, b)
                        y += 1
                else:
                    pixel(x, y, b)
                    y += 1
            if packet == 0:
                break
    return pos


def _palette_chunks(data, hdr, warnings):
    """Return the palette chunks the sprite loads for frame 0, as FLIC_Load_Palette() does, or None."""
    p = hdr.palette_header_offset
    if p == 0:
        return None
    if p < 0 or p + 8 > len(data):
        warnings.append(f"palette header offset 0x{p:X} is outside the entry")
        return None
    data_ofs, start, count = struct.unpack_from("<HHH", data, p)
    end = data_ofs + 3 * count
    if start + count > 256 or end > len(data):
        warnings.append(f"palette (start {start}, count {count}) does not fit")
        return None
    rgb = [tuple(data[data_ofs + 3 * i:data_ofs + 3 * i + 3]) for i in range(count)]
    return [(start, rgb)]


def decode(data, label="sprite"):
    """Decode every frame of a FLIC sprite. Each returned Frame is complete: delta frames are composited."""
    hdr = read_header(data)
    if hdr is None:
        raise FlicError(f"{label}: no sprite header")
    warnings = []
    offsets = struct.unpack_from(f"<{hdr.frame_count + 1}I", data, 0x12)
    frames = []
    canvas = None
    for i in range(hdr.frame_count):
        if hdr.full_store or canvas is None:
            canvas = Frame(hdr.width, hdr.height, bytearray(hdr.width * hdr.height), bytearray(hdr.width * hdr.height))
        start = offsets[i] + 1
        if start > len(data):
            raise FlicError(f"{label}: frame {i} offset 0x{offsets[i]:X} is past the end of the entry")
        _draw_frame(data, start, canvas, hdr.remap, warnings, f"{label} frame {i}")
        frames.append(Frame(canvas.width, canvas.height, bytearray(canvas.index), bytearray(canvas.cover)))
    return Sprite(hdr, frames, _palette_chunks(data, hdr, warnings), warnings)

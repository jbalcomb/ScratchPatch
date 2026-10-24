"""Load and decode the Master of Magic bitmap fonts (FONTS.LBX).

Two sources are supported, and both produce the same raw entry bytes:

* ``FONTS.LBX`` from a game install (entry 0 = font styles, entry 2 = palette).
* ReMoM's generated ``MoX/src/Init_Data/FONTS_LBX.c``, which embeds those
  entries as C initializers.

Layout of entry 0 (see ReMoM ``MoX/src/Fonts.h``, ``struct s_FONT_HEADER``)::

    0x16A  uint16 base_height[8]          cell height per style
    0x17A  uint16 horizontal_spacing[8]   added to every advance
    0x18A  uint16 vertical_spacing[8]     gap between lines
    0x19A  uint8  font_widths[8 * 96]     glyph width, ASCII 32..127
    0x49A  uint16 data_offsets[8 * 96]    glyph data, from start of entry
    0xA9A  glyph data

Glyph data is run-length encoded column by column (``Print_Character_ASM``):

    0x80         end of column
    0x81..0xFF   skip (b & 0x7F) transparent rows
    0x00..0x7F   run of (b >> 4) rows of color index (b & 0x0F)

Color index 0 written by a run is *not* transparent: the main print pass
draws it with ``current_colors[0]`` while the outline/shadow passes skip it.
"""

import re
import struct
from dataclasses import dataclass, field

STYLE_COUNT = 8
GLYPH_COUNT = 96
FIRST_CHAR = 32
HEADER_SIZE = 0xA9A

OFS_BASE_HEIGHT = 0x16A
OFS_HORIZONTAL_SPACING = 0x17A
OFS_VERTICAL_SPACING = 0x18A
OFS_FONT_WIDTHS = 0x19A
OFS_DATA_OFFSETS = 0x49A

PALETTE_ENTRY_SIZE = 5472
OFS_FONT_COLORS = 768
COLOR_SET_COUNT = 16

LBX_MAGIC = 0xFEAD


@dataclass
class Glyph:
    char: int
    width: int
    # (x, y) -> color index, y counted down from the top of the cell
    pixels: dict = field(default_factory=dict)


@dataclass
class FontStyle:
    index: int
    height: int
    horizontal_spacing: int
    vertical_spacing: int
    glyphs: list


@dataclass
class FontData:
    styles: list
    # 256 (r, g, b) tuples, 8 bits per channel
    palette: list
    # 16 color sets x 16 palette indices
    color_sets: list

    def color_set_rgb(self, set_index):
        return [self.palette[i] for i in self.color_sets[set_index]]


def decode_glyph(entry0, offset, width):
    pixels = {}
    x = y = 0
    pos = offset
    while x < width:
        b = entry0[pos]
        pos += 1
        if b == 0x80:
            x += 1
            y = 0
        elif b & 0x80:
            y += b & 0x7F
        else:
            for _ in range(b >> 4):
                pixels[(x, y)] = b & 0x0F
                y += 1
    return pixels


def decode_styles(entry0):
    if len(entry0) < HEADER_SIZE:
        raise ValueError(f"font entry is {len(entry0)} bytes, header alone is {HEADER_SIZE}")

    def u16s(ofs, count):
        return struct.unpack_from(f"<{count}H", entry0, ofs)

    heights = u16s(OFS_BASE_HEIGHT, STYLE_COUNT)
    hspacing = u16s(OFS_HORIZONTAL_SPACING, STYLE_COUNT)
    vspacing = u16s(OFS_VERTICAL_SPACING, STYLE_COUNT)
    widths = entry0[OFS_FONT_WIDTHS:OFS_FONT_WIDTHS + STYLE_COUNT * GLYPH_COUNT]
    offsets = u16s(OFS_DATA_OFFSETS, STYLE_COUNT * GLYPH_COUNT)

    styles = []
    for s in range(STYLE_COUNT):
        glyphs = []
        for c in range(GLYPH_COUNT):
            i = s * GLYPH_COUNT + c
            glyphs.append(Glyph(FIRST_CHAR + c, widths[i], decode_glyph(entry0, offsets[i], widths[i])))
        styles.append(FontStyle(s, heights[s], hspacing[s], vspacing[s], glyphs))
    return styles


def decode_palette(entry2):
    if len(entry2) < OFS_FONT_COLORS + COLOR_SET_COUNT * 16:
        raise ValueError(f"palette entry is only {len(entry2)} bytes")
    # VGA DAC values are 6 bits per channel
    vga = entry2[:768]
    palette = [tuple(((v & 0x3F) << 2) | ((v & 0x3F) >> 4) for v in vga[i:i + 3]) for i in range(0, 768, 3)]
    fc = entry2[OFS_FONT_COLORS:OFS_FONT_COLORS + COLOR_SET_COUNT * 16]
    color_sets = [list(fc[i * 16:(i + 1) * 16]) for i in range(COLOR_SET_COUNT)]
    return palette, color_sets


def build_font_data(entry0, entry2):
    palette, color_sets = decode_palette(entry2)
    return FontData(decode_styles(entry0), palette, color_sets)


# --- sources ---------------------------------------------------------------

def read_lbx_entries(path, wanted):
    with open(path, "rb") as f:
        data = f.read()
    count, magic = struct.unpack_from("<HH", data, 0)
    if magic != LBX_MAGIC:
        raise ValueError(f"{path}: not an LBX file (magic 0x{magic:04X})")
    offsets = struct.unpack_from(f"<{count + 1}I", data, 8)
    out = {}
    for n in wanted:
        if n >= count:
            raise ValueError(f"{path}: has {count} entries, entry {n} requested")
        out[n] = data[offsets[n]:offsets[n + 1]]
    return out


def load_lbx(path, palette_entry=2):
    entries = read_lbx_entries(path, [0, palette_entry])
    return build_font_data(entries[0], entries[palette_entry])


_HEX = re.compile(r"(?<!\w)0x([0-9A-Fa-f]+)(?!\w)")


def _c_initializer_bytes(source, symbol):
    """Pack the hex literals of one generated initializer into bytes.

    The generator writes every value with a fixed number of digits
    (0x00 for uint8_t, 0x0000 for uint16_t), and every field in
    declaration order, so the digit count gives the field width.
    """
    m = re.search(r"^const [^=\n]*\b" + symbol + r"(\[\])?\s*=\s*$", source, re.M)
    if not m:
        raise ValueError(f"{symbol} not found")
    end = source.index("\n};", m.end())
    body = re.sub(r"/\*.*?\*/|//[^\n]*", "", source[m.end():end], flags=re.S)
    out = bytearray()
    for digits in _HEX.findall(body):
        nbytes = (len(digits) + 1) // 2
        out += int(digits, 16).to_bytes(nbytes, "little")
    size = re.search(r"\b" + symbol + r"_SIZE\s*=\s*(\d+)", source)
    if size and int(size.group(1)) != len(out):
        raise ValueError(f"{symbol}: parsed {len(out)} bytes, file says {size.group(1)}")
    return bytes(out)


def load_remom_c(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        source = f.read()
    entry0 = _c_initializer_bytes(source, "FONTS_LBX_ENTRY_0")
    entry2 = _c_initializer_bytes(source, "FONTS_LBX_ENTRY_2")
    return build_font_data(entry0, entry2)

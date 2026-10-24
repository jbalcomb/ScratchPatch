"""Build small FONTS.LBX-format entries in memory, so tests need no game data."""

import struct

from remom_fonts import fontdata as fd


def encode_glyph(rows):
    """rows: equal-length strings, one per pixel row; '.' is transparent,
    a hex digit is a color index. Returns the column-RLE bytes."""
    width = len(rows[0]) if rows else 0
    out = bytearray()
    for x in range(width):
        column = [r[x] for r in rows]
        y = 0
        while y < len(column):
            c = column[y]
            n = 1
            while y + n < len(column) and column[y + n] == c:
                n += 1
            if c == ".":
                if y + n < len(column):  # trailing gaps need no bytes
                    for chunk in range(0, n, 0x7F):
                        out.append(0x80 | min(0x7F, n - chunk))
            else:
                for chunk in range(0, n, 7):
                    out.append((min(7, n - chunk) << 4) | int(c, 16))
            y += n
        out.append(0x80)
    return bytes(out)


def make_entry0(styles):
    """styles: list of dicts {height, hspace, vspace, glyphs: {char: rows}}."""
    header = bytearray(fd.HEADER_SIZE)
    data = bytearray()
    empty_ofs = fd.HEADER_SIZE
    data += b"\x80"  # shared glyph data for width-0 glyphs (never read)
    for s, style in enumerate(styles):
        struct.pack_into("<H", header, fd.OFS_BASE_HEIGHT + 2 * s, style["height"])
        struct.pack_into("<H", header, fd.OFS_HORIZONTAL_SPACING + 2 * s, style.get("hspace", 1))
        struct.pack_into("<H", header, fd.OFS_VERTICAL_SPACING + 2 * s, style.get("vspace", 1))
        for c in range(fd.GLYPH_COUNT):
            i = s * fd.GLYPH_COUNT + c
            rows = style["glyphs"].get(chr(fd.FIRST_CHAR + c))
            if rows is None:
                ofs, width = empty_ofs, 0
            else:
                ofs, width = fd.HEADER_SIZE + len(data), len(rows[0])
                data += encode_glyph(rows)
            header[fd.OFS_FONT_WIDTHS + i] = width
            struct.pack_into("<H", header, fd.OFS_DATA_OFFSETS + 2 * i, ofs)
    return bytes(header + data)


def make_entry2():
    e = bytearray(fd.PALETTE_ENTRY_SIZE)
    # palette index n = gray level n (6-bit), for n < 64
    for n in range(64):
        e[n * 3:n * 3 + 3] = bytes([n, n, n])
    for s in range(fd.COLOR_SET_COUNT):
        for i in range(16):
            e[fd.OFS_FONT_COLORS + s * 16 + i] = (s * 3 + i) % 64
    return bytes(e)


GLYPHS = {
    # index 0 corner pixels, like the game's edge pixels
    "A": ["0110",
          "1..1",
          "1111",
          "1..1",
          "1..1"],
    # a hole
    "O": ["111",
          "1.1",
          "1.1",
          "1.1",
          "111"],
    # pixels touching only at corners, several colors
    "*": ["1.2",
          ".3.",
          "2.1",
          "...",
          "..."],
    # descender
    "g": ["...",
          "222",
          "2.2",
          "222",
          "..2",
          "22."],
}

STYLES = [{"height": 6, "hspace": 1, "vspace": 2, "glyphs": GLYPHS}] * fd.STYLE_COUNT


def font_data():
    return fd.build_font_data(make_entry0(STYLES), make_entry2())

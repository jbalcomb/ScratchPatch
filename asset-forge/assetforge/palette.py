"""Game palettes. The VGA DAC uses 6-bit colour (0-63); these return 8-bit (r, g, b) tuples.

The game's palettes are FONTS.LBX entries 2 and up: Load_Palette(n) in MoX/src/Fonts.c reads entry n + 2,
whose first 768 bytes are 256 RGB triplets. Most screens use Load_Palette(0), so palette 0 is the default.
"""

from .lbx import read_lbx

FONTS_PALETTE_FIRST_ENTRY = 2


def vga6_to_8(v):
    v &= 0x3F
    return (v << 2) | (v >> 4)


def from_vga(raw768):
    return [tuple(vga6_to_8(raw768[3 * i + c]) for c in range(3)) for i in range(len(raw768) // 3)]


def load_game_palette(fonts_lbx_bytes, number=0):
    """Return game palette `number` (as in Load_Palette(number)) from FONTS.LBX, 256 (r, g, b) tuples."""
    entries = read_lbx(fonts_lbx_bytes)
    entry = entries[FONTS_PALETTE_FIRST_ENTRY + number]
    if len(entry.data) < 768:
        raise ValueError(f"FONTS.LBX entry {entry.index} is too short to be a palette")
    return from_vga(entry.data[:768])


def grey_palette():
    """Stand-in when FONTS.LBX is not available: index i is grey level i."""
    return [(i, i, i) for i in range(256)]


def apply_chunks(palette, chunks):
    """Return a copy of palette with the sprite's own palette chunks (6-bit) loaded over it."""
    out = list(palette)
    for start, rgb in chunks or ():
        for i, (r, g, b) in enumerate(rgb):
            out[start + i] = (vga6_to_8(r), vga6_to_8(g), vga6_to_8(b))
    return out

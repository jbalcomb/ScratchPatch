"""Check built fonts against the source bitmaps, pixel for pixel.

Each layer font is rasterized with FreeType at one screen pixel per source
pixel, with default load flags (what SDL_ttf and most apps use), and must
cover exactly the pixels of that layer with no partially covered pixels.
"""

import io

import freetype

from .layers import baseline, layer_pixels, layers_for
from .ttf import build_layer_font


def rendered_pixels(face, char):
    """Pixels drawn for char, as (x, y) with y relative to the baseline."""
    face.load_char(chr(char), freetype.FT_LOAD_RENDER)
    g = face.glyph
    bm = g.bitmap
    on = set()
    partial = 0
    for row in range(bm.rows):
        for col in range(bm.width):
            v = bm.buffer[row * bm.pitch + col]
            if v >= 128:
                on.add((g.bitmap_left + col, row - g.bitmap_top))
            if 0 < v < 255:
                partial += 1
    return on, partial


def verify_style(style):
    """Return a list of mismatch descriptions (empty when exact)."""
    problems = []
    base = baseline(style)
    for layer in layers_for(style):
        buf = io.BytesIO()
        build_layer_font(style, layer).save(buf)
        face = freetype.Face(io.BytesIO(buf.getvalue()))
        face.set_pixel_sizes(0, style.height)
        for g in style.glyphs:
            if g.char > 126:
                continue
            want = {(x, y - base) for x, y in layer_pixels(g, layer)}
            got, partial = rendered_pixels(face, g.char)
            if got != want or partial:
                problems.append(f"font {style.index} {layer.name} {chr(g.char)!r}: "
                                f"{len(want - got)} missing, {len(got - want)} extra, {partial} partial")
    return problems


def verify(data):
    problems = []
    for style in data.styles:
        problems += verify_style(style)
    return problems


if __name__ == "__main__":
    import sys
    from .fontdata import load_lbx, load_remom_c

    path = sys.argv[1]
    data = load_lbx(path) if path.upper().endswith(".LBX") else load_remom_c(path)
    found = verify(data)
    print("\n".join(found) or "all layers match the source bitmaps exactly")
    sys.exit(1 if found else 0)

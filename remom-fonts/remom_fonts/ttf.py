"""Build one TrueType font per (style, layer) with fontTools."""

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from .layers import baseline, layer_pixels
from .options import DEFAULTS
from .vectorize import contours

# Default font units per source pixel (options.pixel_units). The em is the
# style's cell height, so a font size of (cell height x N) px draws every
# source pixel as N x N screen pixels.
PIXEL = DEFAULTS.pixel_units


def glyph_name(char):
    return "space" if char == 32 else f"uni{char:04X}"


def family_name(style, layer, opts=DEFAULTS):
    return f"{opts.family_prefix} {style.index} {layer.name}"


def file_stem(style, layer, opts=DEFAULTS):
    return f"{opts.file_prefix}{style.index}-{layer.name}"


def build_layer_font(style, layer, opts=DEFAULTS):
    PIXEL = opts.pixel_units
    last_char = 127 if opts.include_del else 126
    base = baseline(style, opts)
    ascent = base * PIXEL
    descent = (style.height - base) * PIXEL
    gap = style.vertical_spacing * PIXEL if opts.line_gap_from_spacing else 0

    order = [".notdef"]
    cmap = {}
    glyphs = {}
    metrics = {}

    # .notdef: an empty box the size of the cell
    pen = TTGlyphPen(None)
    w = max(1, style.height // 2) * PIXEL
    for (x0, y0, x1, y1) in [(0, -descent, w, ascent), (PIXEL, -descent + PIXEL, w - PIXEL, ascent - PIXEL)]:
        if x1 <= x0 or y1 <= y0:
            continue
        pts = [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]
        if x0 > 0:
            pts.reverse()  # inner box is a hole
        pen.moveTo(pts[0])
        for p in pts[1:]:
            pen.lineTo(p)
        pen.closePath()
    glyphs[".notdef"] = pen.glyph()
    metrics[".notdef"] = (w + style.horizontal_spacing * PIXEL, 0)

    for g in style.glyphs:
        if g.char > last_char:
            continue
        name = glyph_name(g.char)
        order.append(name)
        cmap[g.char] = name
        pen = TTGlyphPen(None)
        pixels = layer_pixels(g, layer)
        for loop in contours(pixels):
            pts = [(x * PIXEL, (base - y) * PIXEL) for x, y in loop]
            pen.moveTo(pts[0])
            for p in pts[1:]:
                pen.lineTo(p)
            pen.closePath()
        glyphs[name] = pen.glyph()
        lsb = min(x for x, _ in pixels) * PIXEL if pixels else 0
        metrics[name] = ((g.width + style.horizontal_spacing) * PIXEL, lsb)

    family = family_name(style, layer, opts)
    ps_name = file_stem(style, layer, opts).replace(" ", "")

    fb = FontBuilder(style.height * PIXEL, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=ascent, descent=-descent, lineGap=gap)
    fb.setupNameTable({
        "familyName": family,
        "styleName": "Regular",
        "uniqueFontIdentifier": f"{ps_name}-{opts.version}",
        "fullName": family,
        "psName": ps_name,
        "version": f"Version {opts.version}",
        "description": f"Layer {layer.name} of Master of Magic font style {style.index}, "
                       "converted from FONTS.LBX by remom-fonts.",
    })
    fb.setupOS2(
        sTypoAscender=ascent, sTypoDescender=-descent, sTypoLineGap=gap,
        usWinAscent=ascent, usWinDescent=descent,
        fsSelection=0x40 | 0x80,  # REGULAR | USE_TYPO_METRICS
        achVendID="NONE", version=4,
    )
    fb.setupPost()
    fb.setupMaxp()
    return fb.font

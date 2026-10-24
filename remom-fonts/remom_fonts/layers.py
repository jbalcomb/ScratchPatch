"""Split each font style into stackable single-color layers.

Every layer of a style shares the same metrics, so drawing the layers at the
same position, each in its own color, rebuilds the original glyph:

* ``Lnn``    pixels of color index nn; drawn in ``color_set[nn]``.
             L00 holds the explicit index-0 (edge/alias) pixels.
* ``Shadow`` every pixel of index 1..15, all in one color. The game stamps
             this shape at 1-2 pixel offsets in the outline color, behind
             the text, for its shadow/outline styles (index 0 is skipped).
* ``Solid``  every drawn pixel, for plain single-color text.
"""

from collections import Counter
from dataclasses import dataclass

# Letters that sit on the baseline in every style.
_BASELINE_LETTERS = "ABCDEFHIKLMNOPRSTUVWXZ"


@dataclass
class Layer:
    name: str
    # color index this layer is drawn with, or None for Shadow/Solid
    color_index: object
    # predicate on a pixel's color index
    keep: object


def layers_for(style):
    used = Counter()
    for g in style.glyphs:
        used.update(g.pixels.values())
    layers = [Layer(f"L{i:02d}", i, (lambda i: lambda c: c == i)(i)) for i in sorted(used)]
    layers.append(Layer("Shadow", None, lambda c: c != 0))
    layers.append(Layer("Solid", None, lambda c: True))
    return layers


def layer_pixels(glyph, layer):
    return {p for p, c in glyph.pixels.items() if layer.keep(c)}


def baseline(style):
    """Rows from the top of the cell down to the baseline.

    The header only stores the cell height, so take the most common bottom
    edge of the flat-bottomed capitals.
    """
    bottoms = Counter()
    for g in style.glyphs:
        if chr(g.char) in _BASELINE_LETTERS and g.pixels:
            bottoms[max(y for _, y in g.pixels) + 1] += 1
    if not bottoms:
        return style.height
    return bottoms.most_common(1)[0][0]


# Offsets (dx, dy) stamped by Print_Display() for each outline style,
# screen y pointing down. ReMoM MoX/src/Fonts.c, enum e_Font_Shadow.
OUTLINE_STYLES = {
    0: ("None", []),
    1: ("Down", [(1, 1), (0, 1), (1, 0)]),
    2: ("Up", [(-1, 0), (-1, -1), (0, -1)]),
    3: ("Heavy", [(1, 1), (0, 1), (1, 0), (2, 2), (1, 2), (2, 1)]),
    4: ("Outline", [(1, 1), (0, 1), (1, 0), (-1, 0), (-1, -1), (0, -1), (1, -1), (-1, 1)]),
    5: ("Outline Heavy", [(1, 1), (0, 1), (1, 0), (-1, 0), (-1, -1), (0, -1),
                          (2, 2), (1, 2), (2, 1), (1, -1), (-1, 1), (2, 0), (0, 2)]),
}

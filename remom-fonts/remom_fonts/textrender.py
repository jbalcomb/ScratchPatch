"""Lay out and draw text with the generated layer fonts, the way the game does.

The result is a grid of VGA palette indices at the game's own resolution
(one pixel per source pixel). Index 0 is transparent, as in the game's
pictures (ST_TRANSPARENT); index 1 is black (ST_BLACK).

Drawing follows Print_Display() in ReMoM MoX/src/Fonts.c: first the Shadow
layer of the whole text at every outline offset in the outline color, then
each character's color layers in the current color set.

Inline codes switch color sets like the game's control characters:
``{n}`` normal (\\x01), ``{h}`` highlight (\\x02), ``{s}`` special (\\x03).
Write ``{{`` for a literal ``{``.
"""

import json
import os
import re
from dataclasses import dataclass, field

import freetype
import numpy as np

from .layers import OUTLINE_STYLES

TRANSPARENT = 0
BLACK = 1
ALIGNMENTS = ("left", "center", "right")


@dataclass
class TextSettings:
    text: str = "Hello, Wizard!"
    style: int = 2
    normal_set: int = 0
    highlight_set: int = 2
    special_set: int = 3
    # 16 palette indices replacing the normal set (Set_Font_Colors_15), or None
    custom_colors: object = None
    solid: bool = False              # draw the Solid layer only, in normal color 1
    outline_style: int = 0
    outline_color: int = BLACK
    align: str = "left"
    extra_letter_spacing: int = 0
    extra_line_spacing: int = 0


@dataclass
class Rendered:
    indices: np.ndarray                 # (h, w) uint8 palette indices
    palette: list                       # 256 (r, g, b)
    origin: tuple = (0, 0)              # where the text's top-left cell landed
    unknown_chars: list = field(default_factory=list)


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


class FontSet:
    """The fonts listed in a converter manifest.json."""

    def __init__(self, folder):
        self.folder = folder
        with open(os.path.join(folder, "manifest.json")) as f:
            self.manifest = json.load(f)
        self.palette = [_hex(c) for c in self.manifest["palette"]]
        self.color_sets = self.manifest["color_set_indices"]
        self.styles = {s["index"]: s for s in self.manifest["styles"]}
        self._faces = {}
        self._glyphs = {}

    def style_indices(self):
        return sorted(self.styles)

    def layer_names(self, style):
        return [l["name"] for l in self.styles[style]["layers"]]

    def _face(self, style, layer):
        key = (style, layer)
        if key not in self._faces:
            info = self.styles[style]
            entry = next(l for l in info["layers"] if l["name"] == layer)
            face = freetype.Face(os.path.join(self.folder, entry["file"]))
            face.set_pixel_sizes(0, info["height"])
            self._faces[key] = face
        return self._faces[key]

    def glyph(self, style, layer, char):
        """(pixels relative to pen origin on the baseline, advance) or None."""
        key = (style, layer, char)
        if key not in self._glyphs:
            face = self._face(style, layer)
            if face.get_char_index(char) == 0:
                self._glyphs[key] = None
            else:
                face.load_char(chr(char), freetype.FT_LOAD_RENDER)
                g = face.glyph
                bm = g.bitmap
                buf = bm.buffer
                pixels = [(g.bitmap_left + c, r - g.bitmap_top)
                          for r in range(bm.rows) for c in range(bm.width)
                          if buf[r * bm.pitch + c] >= 128]
                self._glyphs[key] = (pixels, g.advance.x >> 6)
        return self._glyphs[key]


_CODE = re.compile(r"\{\{|\{[nhs]\}")


def parse_runs(line):
    """Split one line into (color_set_role, text) runs."""
    runs, role, pos = [], "n", 0
    for m in _CODE.finditer(line):
        if m.start() > pos:
            runs.append((role, line[pos:m.start()]))
        if m.group() == "{{":
            runs.append((role, "{"))
        else:
            role = m.group()[1]
        pos = m.end()
    if pos < len(line):
        runs.append((role, line[pos:]))
    return runs


def _role_colors(fs, settings):
    normal = settings.custom_colors or fs.color_sets[settings.normal_set]
    return {"n": list(normal),
            "h": fs.color_sets[settings.highlight_set],
            "s": fs.color_sets[settings.special_set]}


def layout(fs, settings):
    """Pen positions: list of (x, y_baseline, char, role) plus line widths."""
    info = fs.styles[settings.style]
    layers = fs.layer_names(settings.style)
    probe = "Solid" if "Solid" in layers else layers[0]
    line_height = info["height"] + info["vertical_spacing"] + settings.extra_line_spacing
    hsp = info["horizontal_spacing"]

    lines, unknown = [], []
    for raw in settings.text.split("\n"):
        pens, x = [], 0
        for role, chunk in parse_runs(raw):
            for ch in chunk:
                g = fs.glyph(settings.style, probe, ord(ch))
                if g is None:
                    if ch not in unknown:
                        unknown.append(ch)
                    continue
                pens.append((x, ord(ch), role))
                x += g[1] + settings.extra_letter_spacing
        width = max(0, x - hsp - settings.extra_letter_spacing) if pens else 0
        lines.append((pens, width))

    block = max((w for _, w in lines), default=0)
    placed = []
    for i, (pens, width) in enumerate(lines):
        shift = {"left": 0, "center": (block - width) // 2, "right": block - width}[settings.align]
        y = i * line_height + info["baseline"]
        placed += [(x + shift, y, ch, role) for x, ch, role in pens]
    return placed, unknown


def draw_ops(fs, settings):
    """Ordered (x, y, palette_index) writes; later writes win."""
    placed, unknown = layout(fs, settings)
    layers = fs.styles[settings.style]["layers"]
    colors = _role_colors(fs, settings)
    ops = []

    offsets = OUTLINE_STYLES.get(settings.outline_style, ("", []))[1]
    if offsets and any(l["name"] == "Shadow" for l in layers):
        for dx, dy in offsets:
            for x, y, ch, _ in placed:
                g = fs.glyph(settings.style, "Shadow", ch)
                if g:
                    ops += [(x + px + dx, y + py + dy, settings.outline_color) for px, py in g[0]]

    for x, y, ch, role in placed:
        if settings.solid:
            draw = [l for l in layers if l["name"] == "Solid"]
            fixed = colors[role][1]
        else:
            draw = [l for l in layers if l["color_index"] is not None]
            fixed = None
        for l in draw:
            g = fs.glyph(settings.style, l["name"], ch)
            if not g:
                continue
            color = fixed if fixed is not None else colors[role][l["color_index"]]
            ops += [(x + px, y + py, color) for px, py in g[0]]
    return ops, unknown


def render(fs, settings, canvas=None, position=(0, 0), padding=0, background=TRANSPARENT):
    """Draw text into a palette-index image.

    canvas None: fit the image to the drawn pixels plus padding.
    canvas (w, h): fixed size; the text's top-left cell goes at position,
    and anything outside is clipped.
    """
    ops, unknown = draw_ops(fs, settings)
    if canvas is None:
        if ops:
            xs = [o[0] for o in ops]
            ys = [o[1] for o in ops]
            x0, y0 = min(xs) - padding, min(ys) - padding
            w, h = max(xs) - x0 + 1 + padding, max(ys) - y0 + 1 + padding
        else:
            x0, y0, w, h = -padding, -padding, 2 * padding + 1, 2 * padding + 1
        origin = (-x0, -y0)
    else:
        w, h = canvas
        origin = position
        x0, y0 = -position[0], -position[1]
    img = np.full((h, w), background, dtype=np.uint8)
    for x, y, c in ops:
        ix, iy = x - x0, y - y0
        if 0 <= ix < w and 0 <= iy < h:
            img[iy, ix] = c
    return Rendered(img, fs.palette, origin, unknown)

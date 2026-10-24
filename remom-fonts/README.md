# remom-fonts

Converts the Master of Magic bitmap fonts (`FONTS.LBX`, as used by
[ReMoM](https://github.com/jbalcomb/ReMoM)) into **layered TrueType fonts**,
plus optional per-glyph SVGs and an HTML specimen page.

The original glyphs store a *color index* per pixel, not a color. Each index
becomes its own font ("layer"). Draw every layer at the same position, each in
its own color, and you get the original glyph back, including its shading. Tinting
works the way it does in the game: pick a different color set.

## Usage

```sh
pip install -r requirements.txt

# from a ReMoM checkout (reads MoX/src/Init_Data/FONTS_LBX.c)
python -m remom_fonts --remom ~/src/ReMoM -o out --svg

# or straight from the game file
python -m remom_fonts --lbx /path/to/FONTS.LBX -o out --svg

# check every layer against the source bitmaps with FreeType
python -m remom_fonts.verify ~/src/ReMoM/MoX/src/Init_Data/FONTS_LBX.c
```

Output (`out/` is ignored by git, because it is derived from the game's data):

| Path | What |
|---|---|
| `fonts/ReMoM-Font{s}-{layer}.ttf` | one TrueType font per style and layer |
| `manifest.json` | metrics, layers, the 16 color sets as RGB, the VGA palette, outline offsets |
| `specimen.html` | live preview: stacked layers, color sets, outline styles, scale |
| `svg/font{s}/{hex char}.svg` | with `--svg`: one SVG per glyph, a `<path>` per layer, colored with `--svg-color-set` |

## Layers

| Layer | Pixels | Draw it in |
|---|---|---|
| `L00` | explicit color-index-0 pixels (edge/"alias" pixels) | `color_sets[set][0]` |
| `L01`..`L15` | pixels of that color index | `color_sets[set][n]` |
| `Shadow` | every pixel of index 1..15 | the outline color, at each outline offset, *behind* the text |
| `Solid` | every drawn pixel | one color: plain single-color text |

Only the layers a style actually uses are written. With the stock `FONTS.LBX`:

| Style | Cell | Baseline | Layers |
|---|---|---|---|
| 0 | 6 | 5 | L00 L01 |
| 1 | 6 | 5 | L00 L01 |
| 2 | 9 | 7 | L00 L01 |
| 3 | 9 | 7 | L01 L02 L03 |
| 4 | 10 | 8 | L00 L01 L02 L03 L04 |
| 5 | 16 | 12 | L01 .. L12 (the shaded display font) |
| 6 | 5 | 5 | L01 (symbols) |
| 7 | 10 | 8 | L00 L01 (symbols) |

All layers of a style share the same metrics, so stacking them needs no extra
positioning.

### Drawing order (matches `Print_Display()` in ReMoM `MoX/src/Fonts.c`)

1. For each `(dx, dy)` of the outline style (`manifest.json` → `outline_styles`),
   draw the `Shadow` layer at `(x + dx, y + dy)` in the outline color.
2. Draw `L00`, `L01`, ... at `(x, y)`, each in `color_sets[set][n]`.

Outline styles: 0 none, 1 down, 2 up, 3 heavy, 4 outline, 5 outline heavy.

### Sizes

The em is the style's cell height and one source pixel is 64 font units. So a
font size of `cell height × N` pixels draws each source pixel as an exact
N×N block. Font 2 is crisp at 9, 18, 27 px... Other sizes work but get
anti-aliased edges.

The advance is `glyph width + horizontal_spacing`, as in the game. The line gap is
`vertical_spacing`.

### SDL_ttf sketch

```c
/* one TTF_Font per layer, opened at height * scale */
for (int i = 0; i < n_offsets; i++)
    blit(TTF_RenderText_Solid(shadow, text, outline_rgb), x + dx[i] * scale, y + dy[i] * scale);
for (int l = 0; l < n_layers; l++)
    blit(TTF_RenderText_Solid(layer[l], text, set_rgb[layer_index[l]]), x, y);
```

## How the conversion works

* `fontdata.py` reads entry 0 (the header in `struct s_FONT_HEADER`, and the column RLE
  glyph data) and entry 2 (VGA palette in 6-bit values, then 16 color sets of 16
  palette indices). It decodes the same bytes from either `FONTS.LBX` or ReMoM's
  generated `FONTS_LBX.c`.
* `vectorize.py` traces each layer's pixels into exact square-edged outlines:
  no overlaps, holes wound in reverse, and pixels that touch only at a corner
  kept as separate shapes.
* `ttf.py` builds the fonts with fontTools. The header does not store a baseline,
  so it is taken from the most common bottom edge of the flat-bottomed capitals.
* `verify.py` rasterizes every glyph of every layer with FreeType at 1× and
  checks that it matches the source pixels exactly, with no partly covered pixels.

## Limits

* ASCII 32..126 only. The data has a slot for 127, which the game never prints.
* The palette is entry 2 of `FONTS.LBX`. Other entries hold other palettes; use `--palette-entry` with `--lbx`.
* Palette cycling and color remapping are runtime effects and are not part of the fonts.

## Tests

```sh
pip install pytest
pytest -q
```

The tests build small synthetic fonts in memory, so the repository holds no game data.

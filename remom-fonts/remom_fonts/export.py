"""Write the fonts, a manifest, per-glyph SVGs and an HTML specimen."""

import json
import os

from .layers import OUTLINE_STYLES, baseline, layer_pixels, layers_for
from .ttf import PIXEL, build_layer_font, family_name, file_stem
from .vectorize import svg_path


def hex_rgb(rgb):
    return "#%02x%02x%02x" % rgb


def write_fonts(data, out_dir):
    font_dir = os.path.join(out_dir, "fonts")
    os.makedirs(font_dir, exist_ok=True)
    manifest = {
        "pixel_units": PIXEL,
        "color_sets": [[hex_rgb(c) for c in data.color_set_rgb(s)] for s in range(len(data.color_sets))],
        "color_set_indices": data.color_sets,
        "palette": [hex_rgb(c) for c in data.palette],
        "outline_styles": {str(k): {"name": n, "offsets": o} for k, (n, o) in OUTLINE_STYLES.items()},
        "styles": [],
    }
    for style in data.styles:
        entry = {
            "index": style.index,
            "height": style.height,
            "baseline": baseline(style),
            "horizontal_spacing": style.horizontal_spacing,
            "vertical_spacing": style.vertical_spacing,
            "layers": [],
        }
        for layer in layers_for(style):
            font = build_layer_font(style, layer)
            filename = file_stem(style, layer) + ".ttf"
            font.save(os.path.join(font_dir, filename))
            entry["layers"].append({
                "name": layer.name,
                "color_index": layer.color_index,
                "family": family_name(style, layer),
                "file": "fonts/" + filename,
            })
        manifest["styles"].append(entry)
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def write_svgs(data, out_dir, color_set=0):
    colors = [hex_rgb(c) for c in data.color_set_rgb(color_set)]
    for style in data.styles:
        d = os.path.join(out_dir, "svg", f"font{style.index}")
        os.makedirs(d, exist_ok=True)
        color_layers = [l for l in layers_for(style) if l.color_index is not None]
        for g in style.glyphs:
            if g.char > 126:
                continue
            paths = []
            for layer in color_layers:
                px = layer_pixels(g, layer)
                if px:
                    paths.append(f'  <path id="{layer.name}" fill="{colors[layer.color_index]}" d="{svg_path(px)}"/>')
            w = max(g.width, 1)
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {style.height}" '
                   f'width="{w * 8}" height="{style.height * 8}" shape-rendering="crispEdges">\n'
                   + "\n".join(paths) + "\n</svg>\n")
            with open(os.path.join(d, f"{g.char:02X}.svg"), "w") as f:
                f.write(svg)


def write_specimen(manifest, out_dir):
    template = os.path.join(os.path.dirname(__file__), "specimen.html")
    with open(template) as f:
        html = f.read()
    html = html.replace("/*MANIFEST*/null", json.dumps(manifest))
    with open(os.path.join(out_dir, "specimen.html"), "w") as f:
        f.write(html)

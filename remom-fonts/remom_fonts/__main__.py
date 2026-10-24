"""remom-fonts: convert Master of Magic bitmap fonts into layered TrueType fonts.

    python -m remom_fonts --remom ~/src/ReMoM -o out
    python -m remom_fonts --lbx /path/to/FONTS.LBX -o out
    python -m remom_fonts --preset my-preset.json
"""

import argparse
import os
import sys

from .convert import convert
from .options import ConvertOptions


def parse_baselines(items):
    out = {}
    for item in items or []:
        style, _, rows = item.partition("=")
        out[int(style)] = int(rows)
    return out


def main(argv=None):
    d = ConvertOptions()
    ap = argparse.ArgumentParser(prog="remom_fonts", description=__doc__.strip().splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--remom", metavar="DIR", help="ReMoM checkout (reads MoX/src/Init_Data/FONTS_LBX.c)")
    src.add_argument("--fonts-c", metavar="FILE", help="path to a generated FONTS_LBX.c")
    src.add_argument("--lbx", metavar="FILE", help="FONTS.LBX from the game")
    ap.add_argument("--preset", metavar="JSON", help="load options from a preset saved by the UI; flags override it")
    ap.add_argument("--palette-entry", type=int, help=f"FONTS.LBX palette entry for --lbx (default {d.palette_entry})")
    ap.add_argument("-o", "--out", help=f"output directory (default: {d.out_dir})")
    ap.add_argument("--styles", help="comma-separated styles to convert (default: all)")
    ap.add_argument("--pixel-units", type=int, help=f"font units per source pixel (default {d.pixel_units})")
    ap.add_argument("--baseline", action="append", metavar="STYLE=ROWS", help="override a style's baseline")
    ap.add_argument("--no-line-gap", action="store_true", help="do not use vertical_spacing as the line gap")
    ap.add_argument("--include-del", action="store_true", help="also map char 127")
    ap.add_argument("--no-color-layers", action="store_true")
    ap.add_argument("--no-shadow", action="store_true")
    ap.add_argument("--no-solid", action="store_true")
    ap.add_argument("--shadow-includes-edge", action="store_true", help="put index-0 pixels in the Shadow layer")
    ap.add_argument("--solid-excludes-edge", action="store_true", help="leave index-0 pixels out of the Solid layer")
    ap.add_argument("--family-prefix", help=f'font family prefix (default "{d.family_prefix}")')
    ap.add_argument("--file-prefix", help=f'font file prefix (default "{d.file_prefix}")')
    ap.add_argument("--font-version", help=f"version string written into the fonts (default {d.version})")
    ap.add_argument("--no-specimen", action="store_true")
    ap.add_argument("--svg", action="store_true", help="also write one SVG per glyph")
    ap.add_argument("--svg-color-set", type=int, help="color set used for the SVGs (default 0)")
    ap.add_argument("--svg-scale", type=int, help="SVG width/height multiplier (default 8)")
    args = ap.parse_args(argv)

    if args.preset:
        with open(args.preset) as f:
            opts = ConvertOptions.from_json(f.read())
    else:
        opts = ConvertOptions()

    for kind, value in (("remom", args.remom), ("fonts_c", args.fonts_c), ("lbx", args.lbx)):
        if value:
            opts.source_kind, opts.source_path = kind, value
    if not opts.source_path:
        ap.error("one of --remom, --fonts-c, --lbx (or a --preset with a source) is required")

    simple = {"palette_entry": args.palette_entry, "out_dir": args.out, "pixel_units": args.pixel_units,
              "family_prefix": args.family_prefix, "file_prefix": args.file_prefix,
              "version": args.font_version, "svg_color_set": args.svg_color_set, "svg_scale": args.svg_scale}
    for name, value in simple.items():
        if value is not None:
            setattr(opts, name, value)
    if args.styles:
        opts.styles = [int(s) for s in args.styles.split(",")]
    opts.baseline_overrides.update(parse_baselines(args.baseline))
    flags = {"line_gap_from_spacing": args.no_line_gap, "color_layers": args.no_color_layers,
             "shadow_layer": args.no_shadow, "solid_layer": args.no_solid,
             "solid_includes_edge": args.solid_excludes_edge, "write_specimen": args.no_specimen}
    for name, off in flags.items():
        if off:
            setattr(opts, name, False)
    if args.include_del:
        opts.include_del = True
    if args.shadow_includes_edge:
        opts.shadow_includes_edge = True
    if args.svg:
        opts.write_svgs = True

    try:
        manifest = convert(opts)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for s in manifest["styles"]:
        names = " ".join(l["name"] for l in s["layers"])
        print(f"font {s['index']}: {s['height']}px cell, baseline {s['baseline']}: {names}")
    print(f"wrote {sum(len(s['layers']) for s in manifest['styles'])} fonts to {os.path.join(opts.out_dir, 'fonts')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

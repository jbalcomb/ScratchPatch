"""remom-fonts: convert Master of Magic bitmap fonts into layered TrueType fonts.

    python -m remom_fonts --remom ~/src/ReMoM -o out
    python -m remom_fonts --lbx /path/to/FONTS.LBX -o out
"""

import argparse
import os
import sys

from .export import write_fonts, write_specimen, write_svgs
from .fontdata import load_lbx, load_remom_c


def main(argv=None):
    ap = argparse.ArgumentParser(prog="remom_fonts", description=__doc__.strip().splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--remom", metavar="DIR", help="ReMoM checkout (reads MoX/src/Init_Data/FONTS_LBX.c)")
    src.add_argument("--fonts-c", metavar="FILE", help="path to a generated FONTS_LBX.c")
    src.add_argument("--lbx", metavar="FILE", help="FONTS.LBX from the game")
    ap.add_argument("--palette-entry", type=int, default=2,
                    help="FONTS.LBX palette entry to use with --lbx (default 2)")
    ap.add_argument("-o", "--out", default="out", help="output directory (default: out)")
    ap.add_argument("--svg", action="store_true", help="also write one SVG per glyph")
    ap.add_argument("--svg-color-set", type=int, default=0, help="color set used for the SVGs (default 0)")
    args = ap.parse_args(argv)

    if args.lbx:
        data = load_lbx(args.lbx, args.palette_entry)
    else:
        path = args.fonts_c or os.path.join(args.remom, "MoX", "src", "Init_Data", "FONTS_LBX.c")
        data = load_remom_c(path)

    os.makedirs(args.out, exist_ok=True)
    manifest = write_fonts(data, args.out)
    write_specimen(manifest, args.out)
    if args.svg:
        write_svgs(data, args.out, args.svg_color_set)

    for s in manifest["styles"]:
        names = " ".join(l["name"] for l in s["layers"])
        print(f"font {s['index']}: {s['height']}px cell, baseline {s['baseline']}: {names}")
    print(f"wrote {sum(len(s['layers']) for s in manifest['styles'])} fonts to {os.path.join(args.out, 'fonts')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

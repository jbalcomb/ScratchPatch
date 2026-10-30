"""Export the sprites in Master of Magic LBX archives to PNG, with a catalogue and a browsable gallery.

    python3 -m assetforge.export GAME_DIR OUT_DIR            # every *.LBX in GAME_DIR
    python3 -m assetforge.export UNITS1.LBX OUT_DIR          # one archive
    python3 -m assetforge.export GAME_DIR OUT_DIR --palette 2

Per sprite, under OUT_DIR/<ARCHIVE>/<entry>_<name>/:
  frame_NN.png         RGBA, rendered with the game palette (and the sprite's own palette, if it has one)
  frame_NN_index.png   greyscale + alpha: grey = palette index, alpha 255 pixel / 128 remap (shading) pixel / 0 nothing
  sheet.png            all frames side by side, RGBA
  palette.json         only for sprites with their own palette: the 256 (r, g, b) colours they are drawn with
World-map tiles (TERRAIN.LBX entry 0, plain bitmaps) are listed as archive TERRAIN_TILES, one folder per tile.
OUT_DIR/catalog.csv lists every entry; OUT_DIR/index.html shows them all; OUT_DIR/palette.json is the game palette.

The game palette comes from FONTS.LBX in the same folder as the archives. Without it, sprites render in grey.
"""

import argparse
import csv
import html
import json
import re
import struct
import sys
from pathlib import Path

from PIL import Image

from . import flic
from .lbx import LbxError, read_lbx
from .palette import FONTS_PALETTE_FIRST_ENTRY, apply_chunks, grey_palette, load_game_palette, vga6_to_8

REMAP_TABLE_OFFSET = 0x1500     # remap_colors in the FONTS.LBX palette entry: 24 x (r, g, b, percent), see Load_Palette()
REMAP_BLOCKS = 24

TERRAIN_TILES = "TERRAIN_TILES"     # catalogue name for the tiles inside TERRAIN.LBX entry 0
TERRAIN_FIRST_TILE = 0xC0
TERRAIN_TILE_STRIDE = 384

CATALOG_COLUMNS = ["archive", "entry", "name", "description", "kind", "width", "height", "frames", "loop_frame",
                   "full_store", "remap", "own_palette", "remap_pixels", "folder", "warnings"]


def load_remap_colors(fonts_bytes, number=0):
    """Return 24 (r, g, b, alpha) colours approximating each remap block, for previews."""
    data = read_lbx(fonts_bytes)[FONTS_PALETTE_FIRST_ENTRY + number].data
    out = []
    for i in range(REMAP_BLOCKS):
        o = REMAP_TABLE_OFFSET + 4 * i
        if o + 4 > len(data):
            out.append((0, 0, 0, 128))
            continue
        r, g, b, pct = data[o:o + 4]
        out.append((vga6_to_8(r), vga6_to_8(g), vga6_to_8(b), min(255, pct * 255 // 100)))
    return out


def render_rgba(frame, palette, remap_colors):
    px = bytearray(frame.width * frame.height * 4)
    for i, (idx, cover) in enumerate(zip(frame.index, frame.cover)):
        if cover == flic.COVER_PIXEL:
            px[4 * i:4 * i + 4] = bytes((*palette[idx], 255))
        elif cover == flic.COVER_REMAP:
            px[4 * i:4 * i + 4] = bytes(remap_colors[(idx - flic.REMAP_FIRST) % REMAP_BLOCKS])
    return Image.frombytes("RGBA", (frame.width, frame.height), bytes(px))


def render_index(frame):
    alpha = bytes({flic.COVER_NONE: 0, flic.COVER_PIXEL: 255, flic.COVER_REMAP: 128}[c] for c in frame.cover)
    img = Image.frombytes("L", (frame.width, frame.height), bytes(frame.index))
    img.putalpha(Image.frombytes("L", (frame.width, frame.height), alpha))
    return img


def sheet(images):
    w, h = images[0].size
    out = Image.new("RGBA", (w * len(images), h), (0, 0, 0, 0))
    for i, img in enumerate(images):
        out.paste(img, (i * w, 0))
    return out


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")


def write_frames(folder, frames, palette, remap_colors):
    folder.mkdir(parents=True, exist_ok=True)
    rgba = []
    for n, frame in enumerate(frames):
        img = render_rgba(frame, palette, remap_colors)
        img.save(folder / f"frame_{n:02d}.png")
        render_index(frame).save(folder / f"frame_{n:02d}_index.png")
        rgba.append(img)
    sheet(rgba).save(folder / "sheet.png")


def export_terrain_tiles(entry, out_dir, palette, remap_colors, rows):
    """TERRAIN.LBX entry 0: the world-map tiles, as plain bitmaps rather than FLIC sprites.

    From MoM/src/MainScr_Maps.c: tile k starts at 0xC0 + 384 * k; a 16-byte header (width, height) is followed by
    width * height palette indices, column by column. Draw_Picture() skips index 0, so 0 is transparent.
    """
    data = entry.data
    for k in range((len(data) - TERRAIN_FIRST_TILE) // TERRAIN_TILE_STRIDE):
        start = TERRAIN_FIRST_TILE + k * TERRAIN_TILE_STRIDE
        w, h = struct.unpack_from("<hh", data, start)
        row = dict.fromkeys(CATALOG_COLUMNS, "")
        row.update(archive=TERRAIN_TILES, entry=k, name=f"TILE{k:04d}", description="world map terrain tile", kind="other")
        rows.append(row)
        if not (0 < w and 0 < h and 16 + w * h <= TERRAIN_TILE_STRIDE):
            row.update(warnings=f"tile header says {w}x{h}")
            continue
        pixels = data[start + 16:start + 16 + w * h]
        index, cover = bytearray(w * h), bytearray(w * h)
        for x in range(w):
            for y in range(h):
                v = pixels[x * h + y]
                index[y * w + x] = v
                cover[y * w + x] = flic.COVER_PIXEL if v else flic.COVER_NONE
        folder = Path(TERRAIN_TILES) / f"{k:04d}_{row['name']}"
        write_frames(out_dir / folder, [flic.Frame(w, h, index, cover)], palette, remap_colors)
        row.update(kind="sprite", width=w, height=h, frames=1, loop_frame=0, full_store=1, remap=0, own_palette=0,
                   remap_pixels=0, folder=folder.as_posix())


def export_archive(path, out_dir, palette, remap_colors, rows):
    try:
        entries = read_lbx(path.read_bytes())
    except LbxError as e:
        print(f"skip {path.name}: {e}", file=sys.stderr)
        return
    archive = path.stem.upper()
    if archive == "TERRAIN" and entries:
        export_terrain_tiles(entries[0], out_dir, palette, remap_colors, rows)
    for entry in entries:
        row = dict.fromkeys(CATALOG_COLUMNS, "")
        row.update(archive=archive, entry=entry.index, name=entry.name, description=entry.description, kind="other")
        rows.append(row)
        hdr = flic.read_header(entry.data)
        if hdr is None:
            continue
        label = f"{archive}[{entry.index}]"
        try:
            sprite = flic.decode(entry.data, label)
        except (flic.FlicError, struct.error) as e:
            row.update(kind="undecodable", warnings=str(e))
            continue
        folder = Path(archive) / f"{entry.index:03d}_{safe_name(entry.name) or 'unnamed'}"
        pal = apply_chunks(palette, sprite.palette)
        write_frames(out_dir / folder, sprite.frames, pal, remap_colors)
        if sprite.palette:
            (out_dir / folder / "palette.json").write_text(json.dumps(pal))
        row.update(kind="sprite", width=hdr.width, height=hdr.height, frames=hdr.frame_count, loop_frame=hdr.loop_frame,
                   full_store=int(bool(hdr.full_store)), remap=int(bool(hdr.remap)),
                   own_palette=int(sprite.palette is not None),
                   remap_pixels=sum(f.cover.count(flic.COVER_REMAP) for f in sprite.frames),
                   folder=folder.as_posix(), warnings="; ".join(dict.fromkeys(sprite.warnings)))


def write_gallery(out_dir, rows):
    parts = ["""<!doctype html><meta charset="utf-8"><title>LBX Sprites</title>
<style>
body{font:14px system-ui,sans-serif;margin:16px;background:#202020;color:#ddd}
h2{border-bottom:1px solid #555;margin-top:2em}
.grid{display:flex;flex-wrap:wrap;gap:12px}
figure{margin:0;background:#2c2c2c;padding:6px;max-width:100%;overflow:hidden}
figcaption{font-size:12px;color:#aaa;max-width:320px}
img{image-rendering:pixelated;display:block;max-width:100%;background:repeating-conic-gradient(#3a3a3a 0 25%,#303030 0 50%) 0 0/16px 16px}
.warn{color:#e8a33d}
</style>
<p>Scale: <select id=s onchange="document.querySelectorAll('img').forEach(i=>i.style.width=i.naturalWidth*this.value+'px')">
<option>1<option selected>2<option>3<option>4</select></p>"""]
    by_archive = {}
    for r in rows:
        if r["kind"] != "other":
            by_archive.setdefault(r["archive"], []).append(r)
    for archive, items in sorted(by_archive.items()):
        parts.append(f"<h2>{html.escape(archive)}.LBX</h2><div class=grid>")
        for r in items:
            cap = f"{r['entry']} {html.escape(r['name'])} {html.escape(r['description'])}"
            if r["kind"] == "sprite":
                cap += f"<br>{r['width']}x{r['height']}, {r['frames']} frame(s)"
                img = f"<img src=\"{html.escape(r['folder'])}/sheet.png\" loading=lazy onload=\"this.style.width=this.naturalWidth*2+'px'\">"
            else:
                img = ""
            if r["warnings"]:
                cap += f"<br><span class=warn>{html.escape(r['warnings'][:200])}</span>"
            parts.append(f"<figure>{img}<figcaption>{cap}</figcaption></figure>")
        parts.append("</div>")
    (out_dir / "index.html").write_text("\n".join(parts), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="an .LBX file, or a folder of them")
    ap.add_argument("out", type=Path, help="output folder")
    ap.add_argument("--palette", type=int, default=0, help="game palette number, as in Load_Palette(n) (default 0)")
    ap.add_argument("--fonts", type=Path, help="FONTS.LBX to take the palette from (default: next to the archives)")
    args = ap.parse_args(argv)

    files = sorted(p for p in args.source.iterdir() if p.suffix.upper() == ".LBX") if args.source.is_dir() else [args.source]
    if not files:
        ap.error(f"no .LBX files in {args.source}")
    fonts = args.fonts or next((p for p in (files[0].parent).iterdir() if p.name.upper() == "FONTS.LBX"), None)
    if fonts:
        fonts_bytes = fonts.read_bytes()
        palette = load_game_palette(fonts_bytes, args.palette)
        remap_colors = load_remap_colors(fonts_bytes, args.palette)
    else:
        print("FONTS.LBX not found: rendering in grey (palette index = grey level)", file=sys.stderr)
        palette, remap_colors = grey_palette(), [(0, 0, 0, 128)] * REMAP_BLOCKS

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "palette.json").write_text(json.dumps(palette))
    rows = []
    for path in files:
        export_archive(path, args.out, palette, remap_colors, rows)
        archives = {path.stem.upper(), TERRAIN_TILES} if path.stem.upper() == "TERRAIN" else {path.stem.upper()}
        print(f"{path.name}: {sum(1 for r in rows if r['archive'] in archives and r['kind'] == 'sprite')} sprites")
    with open(args.out / "catalog.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, CATALOG_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    write_gallery(args.out, rows)
    kinds = [r["kind"] for r in rows]
    print(f"{kinds.count('sprite')} sprites, {kinds.count('undecodable')} undecodable, {kinds.count('other')} other entries"
          f" -> {args.out / 'index.html'}")


if __name__ == "__main__":
    main()

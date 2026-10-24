import json
import struct

import pytest
from fontTools.ttLib import TTFont

from remom_fonts import fontdata as fd
from remom_fonts.__main__ import main
from remom_fonts.layers import baseline, layer_pixels, layers_for
from remom_fonts.vectorize import contours
from remom_fonts.verify import verify

from synthetic import GLYPHS, STYLES, encode_glyph, font_data, make_entry0, make_entry2


def pixels_of(rows):
    return {(x, y): int(c, 16) for y, r in enumerate(rows) for x, c in enumerate(r) if c != "."}


def test_decode_round_trips_every_glyph():
    style = font_data().styles[0]
    for ch, rows in GLYPHS.items():
        g = style.glyphs[ord(ch) - 32]
        assert g.width == len(rows[0])
        assert g.pixels == pixels_of(rows)


def test_long_runs_use_several_bytes():
    rows = ["1"] * 9 + ["."] * 3 + ["2"]
    assert fd.decode_glyph(encode_glyph([r for r in rows]), 0, 1) == pixels_of(rows)


def test_palette_is_scaled_from_six_bits():
    data = font_data()
    assert data.palette[63] == (255, 255, 255)
    assert data.palette[32] == (130, 130, 130)
    assert data.color_sets[1][2] == 5


def signed_area(loop):
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(loop, loop[1:] + loop[:1])) / 2


@pytest.mark.parametrize("ch", list(GLYPHS))
def test_contours_cover_exactly_the_pixels(ch):
    px = set(pixels_of(GLYPHS[ch]))
    loops = contours(px)
    # outer contours are clockwise on screen (positive area with y down), holes negative
    assert sum(signed_area(l) for l in loops) == len(px)
    for loop in loops:
        for (x0, y0), (x1, y1) in zip(loop, loop[1:] + loop[:1]):
            assert x0 == x1 or y0 == y1


def test_hole_and_diagonal_shapes():
    o = contours(pixels_of(GLYPHS["O"]))
    assert sorted(signed_area(l) for l in o) == [-3, 15]
    x = contours({(0, 0), (1, 1)})
    assert [signed_area(l) for l in x] == [1, 1]


def test_layers_split_by_color_index():
    style = font_data().styles[0]
    names = [l.name for l in layers_for(style)]
    assert names == ["L00", "L01", "L02", "L03", "Shadow", "Solid"]
    a = style.glyphs[ord("A") - 32]
    by = {l.name: l for l in layers_for(style)}
    assert layer_pixels(a, by["L00"]) == {(0, 0), (3, 0)}
    assert (0, 0) not in layer_pixels(a, by["Shadow"])
    assert layer_pixels(a, by["Solid"]) == set(a.pixels)


def test_baseline_from_capitals():
    assert baseline(font_data().styles[0]) == 5


def test_fonts_render_pixel_exact_with_freetype():
    assert verify(font_data()) == []


def test_c_initializer_parsing():
    src = """
const struct s_FONTS_LBX_PALETTE FONTS_LBX_ENTRY_2 =
{
    .palette_data_0x400 =   /* 0x123 in a comment */
    { 0x01, 0x0203 },
};
const uint32_t FONTS_LBX_ENTRY_2_SIZE = 3;
"""
    assert fd._c_initializer_bytes(src, "FONTS_LBX_ENTRY_2") == b"\x01\x03\x02"
    with pytest.raises(ValueError):
        fd._c_initializer_bytes(src.replace("= 3;", "= 4;"), "FONTS_LBX_ENTRY_2")


def write_fonts_c(path, entry0, entry2):
    def init(symbol, ctype, data):
        body = ", ".join(f"0x{b:02X}" for b in data)
        return f"const {ctype} {symbol} =\n{{\n    {body}\n}};\nconst uint32_t {symbol}_SIZE = {len(data)};\n"
    path.write_text(init("FONTS_LBX_ENTRY_0", "struct s_FONTS_LBX_ENTRY_0", entry0)
                    + init("FONTS_LBX_ENTRY_2", "struct s_FONTS_LBX_PALETTE", entry2))


def write_lbx(path, entries):
    head = struct.pack("<HHHH", len(entries), fd.LBX_MAGIC, 0, 0)
    ofs = 8 + 4 * (len(entries) + 1)
    offsets = []
    for e in entries:
        offsets.append(ofs)
        ofs += len(e)
    offsets.append(ofs)
    path.write_bytes(head + struct.pack(f"<{len(offsets)}I", *offsets) + b"".join(entries))


def test_lbx_and_c_sources_agree(tmp_path):
    e0, e2 = make_entry0(STYLES), make_entry2()
    write_lbx(tmp_path / "FONTS.LBX", [e0, b"\x00" * 4, e2])
    write_fonts_c(tmp_path / "FONTS_LBX.c", e0, e2)
    a = fd.load_lbx(tmp_path / "FONTS.LBX")
    b = fd.load_remom_c(tmp_path / "FONTS_LBX.c")
    assert a.palette == b.palette and a.color_sets == b.color_sets
    assert [g.pixels for g in a.styles[3].glyphs] == [g.pixels for g in b.styles[3].glyphs]


def test_cli_writes_fonts_manifest_specimen_and_svgs(tmp_path):
    write_lbx(tmp_path / "FONTS.LBX", [make_entry0(STYLES), b"", make_entry2()])
    out = tmp_path / "out"
    assert main(["--lbx", str(tmp_path / "FONTS.LBX"), "-o", str(out), "--svg"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    style = manifest["styles"][0]
    assert style["baseline"] == 5
    assert [l["name"] for l in style["layers"]] == ["L00", "L01", "L02", "L03", "Shadow", "Solid"]
    font = TTFont(out / style["layers"][1]["file"])
    assert font["name"].getDebugName(1) == "ReMoM Font 0 L01"
    assert font["hmtx"]["uni0041"][0] == (4 + 1) * 64
    assert "null" not in (out / "specimen.html").read_text().split("const M = ")[1][:10]
    svg = (out / "svg" / "font0" / "41.svg").read_text()
    assert 'id="L00"' in svg and 'id="L01"' in svg

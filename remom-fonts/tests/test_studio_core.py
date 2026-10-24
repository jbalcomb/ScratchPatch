"""Renderer, image export, options and CLI: everything under the UI."""

import json
import os
import shutil
import struct
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image

from remom_fonts import imageexport
from remom_fonts.__main__ import main
from remom_fonts.convert import convert
from remom_fonts.layers import layers_for
from remom_fonts.options import ConvertOptions
from remom_fonts.textrender import FontSet, TextSettings, parse_runs, render

from synthetic import STYLES, font_data, make_entry0, make_entry2
from test_remom_fonts import write_lbx


@pytest.fixture(scope="module")
def fonts(tmp_path_factory):
    d = tmp_path_factory.mktemp("conv")
    write_lbx(d / "FONTS.LBX", [make_entry0(STYLES), b"", make_entry2()])
    convert(ConvertOptions(source_kind="lbx", source_path=str(d / "FONTS.LBX"), out_dir=str(d / "out")))
    return FontSet(str(d / "out"))


def test_single_glyph_uses_color_set_indices(fonts):
    r = render(fonts, TextSettings(text="A", style=0, normal_set=4))
    s4 = fonts.color_sets[4]
    want = np.array([[s4[0], s4[1], s4[1], s4[0]],
                     [s4[1], 0, 0, s4[1]],
                     [s4[1], s4[1], s4[1], s4[1]],
                     [s4[1], 0, 0, s4[1]],
                     [s4[1], 0, 0, s4[1]]], dtype=np.uint8)
    assert np.array_equal(r.indices, want)


def test_outline_goes_behind_and_skips_edge_pixels(fonts):
    r = render(fonts, TextSettings(text="A", style=0, outline_style=1, outline_color=99))
    # shadow down/right: one extra row and column
    assert r.indices.shape == (6, 5)
    s0 = fonts.color_sets[0]
    assert r.indices[0, 1] == s0[1]            # text wins over shadow
    assert r.indices[5, 1] == 99               # shadow below the left leg
    assert r.indices[1, 4] == 99               # shadow right of the right leg
    assert r.indices[0, 4] == 0                # edge pixel (3, 0) casts no shadow


def test_codes_switch_color_sets(fonts):
    assert parse_runs("a{h}b{{c{s}d{n}e") == [("n", "a"), ("h", "b"), ("h", "{"), ("h", "c"), ("s", "d"), ("n", "e")]
    r = render(fonts, TextSettings(text="O{h}O", style=0, normal_set=0, highlight_set=5))
    left, right = r.indices[0, 0], r.indices[0, 4]
    assert left == fonts.color_sets[0][1] and right == fonts.color_sets[5][1]


def test_advance_and_alignment(fonts):
    r = render(fonts, TextSettings(text="OO", style=0))
    assert r.indices.shape[1] == 3 + 1 + 3     # width 3 + hspace 1 + width 3
    r = render(fonts, TextSettings(text="O\nOOO", style=0, align="right"))
    line_h = 6 + 2                              # height + vertical spacing
    assert r.indices[0, :8].tolist().count(0) == 8 and r.indices[0, 8] != 0
    assert r.indices[line_h, 0] != 0


def test_custom_colors_and_solid(fonts):
    custom = list(range(100, 116))
    r = render(fonts, TextSettings(text="*", style=0, custom_colors=custom))
    assert set(np.unique(r.indices)) == {0, 101, 102, 103}
    r = render(fonts, TextSettings(text="*", style=0, solid=True, normal_set=2))
    assert set(np.unique(r.indices)) == {0, fonts.color_sets[2][1]}


def test_fixed_canvas_positions_and_clips(fonts):
    r = render(fonts, TextSettings(text="O", style=0), canvas=(5, 4), position=(2, 1))
    assert r.indices.shape == (4, 5)
    assert r.indices[1, 2] != 0 and r.indices[1, 1] == 0
    assert r.indices[3, 2] != 0                 # clipped below row 3


def test_unknown_characters_are_reported(fonts):
    # Z is in the font with width 0 (drawn as nothing, like the game); é is not in it
    r = render(fonts, TextSettings(text="AéZ", style=0))
    assert r.unknown_chars == ["é"]


def test_mox_picture_layout(fonts):
    r = render(fonts, TextSettings(text="A", style=0))
    data = imageexport.mox_picture_bytes(r)
    w, h, marker = struct.unpack_from("<hhH", data)
    assert (w, h, marker) == (4, 5, 0xDE0A) and len(data) == 16 + w * h
    # column-major: second byte of pixel data is (x=0, y=1)
    assert data[16 + 1] == r.indices[1, 0]
    assert data[16 + h] == r.indices[0, 1]


def test_png_exports(fonts, tmp_path):
    r = render(fonts, TextSettings(text="A", style=0, outline_style=4, outline_color=1))
    imageexport.indexed_png(r, tmp_path / "a.png")
    img = Image.open(tmp_path / "a.png")
    assert img.mode == "P" and img.info.get("transparency") == 0
    assert np.array_equal(np.array(img), r.indices)
    imageexport.rgba_png(r, tmp_path / "b.png", scale=3, background=(1, 2, 3))
    rgba = np.array(Image.open(tmp_path / "b.png"))
    assert rgba.shape == (r.indices.shape[0] * 3, r.indices.shape[1] * 3, 4)
    assert (rgba[..., 3] == 255).all()
    imageexport.c_source(r, tmp_path / "c.h", "3 gold label")
    text = (tmp_path / "c.h").read_text()
    assert "static const uint8_t _3_gold_label[" in text and "_3_GOLD_LABEL_WIDTH" in text


def test_options_round_trip_and_validation():
    o = ConvertOptions(source_path="x", baseline_overrides={2: 6}, styles=[1, 2])
    back = ConvertOptions.from_json(o.to_json())
    assert back == o and back.baseline_for(2) == 6
    assert ConvertOptions().validate() == ["choose a source"]
    bad = ConvertOptions(source_path="x", pixel_units=0, styles=[], color_layers=False,
                         shadow_layer=False, solid_layer=False, file_prefix="a/b")
    assert len(bad.validate()) == 4


def test_shadow_and_solid_edge_options_are_independent():
    style = font_data().styles[0]
    o = ConvertOptions(shadow_includes_edge=True, solid_includes_edge=False)
    by = {l.name: l for l in layers_for(style, o)}
    assert by["Shadow"].keep(0) and not by["Solid"].keep(0)
    o = ConvertOptions(color_layers=False, solid_layer=False)
    assert [l.name for l in layers_for(style, o)] == ["Shadow"]


def test_cli_flags_and_preset(tmp_path):
    write_lbx(tmp_path / "FONTS.LBX", [make_entry0(STYLES), b"", make_entry2()])
    preset = ConvertOptions(source_kind="lbx", source_path=str(tmp_path / "FONTS.LBX"),
                            out_dir=str(tmp_path / "out"), file_prefix="Test-")
    (tmp_path / "p.json").write_text(preset.to_json())
    assert main(["--preset", str(tmp_path / "p.json"), "--styles", "1,3", "--no-solid",
                 "--baseline", "3=4", "--pixel-units", "32", "--no-specimen"]) == 0
    m = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert [s["index"] for s in m["styles"]] == [1, 3]
    assert m["styles"][1]["baseline"] == 4 and m["pixel_units"] == 32
    assert all(l["name"] != "Solid" for s in m["styles"] for l in s["layers"])
    assert os.path.exists(tmp_path / "out" / "fonts" / "Test-1-L01.ttf")
    assert not os.path.exists(tmp_path / "out" / "specimen.html")
    assert main(["--lbx", str(tmp_path / "missing.LBX"), "-o", str(tmp_path / "o2")]) == 2


def test_text_tab_export_without_a_window(fonts, tmp_path):
    pytest.importorskip("imgui_bundle")
    from remom_fonts.studio.text_tab import TextState, TextTab
    messages = []
    state = TextState(font_folder=fonts.folder, export_folder=str(tmp_path), export_name="label")
    state.settings.update(text="AO", style=0, outline_style=4)
    state.formats = {k: True for k in state.formats}
    tab = TextTab(state, messages.append)
    tab.load_fonts()
    tab.do_export(tab.export_paths())
    assert sorted(os.listdir(tmp_path)) == ["label-rgba-1x.png", "label.bin", "label.h", "label.png"]
    assert messages[-1].startswith("Wrote 4 file(s)")


@pytest.mark.skipif(not shutil.which("xvfb-run"), reason="needs xvfb-run")
def test_studio_opens_both_tabs_headless(fonts, tmp_path):
    pytest.importorskip("imgui_bundle")
    for tab in ("text", "converter"):
        shot = tmp_path / f"{tab}.png"
        subprocess.run(
            ["xvfb-run", "-a", "-s", "-screen 0 1400x900x24", sys.executable, "-m", "remom_fonts.studio",
             "--fresh", "--session", str(tmp_path / "s.json"), "--fonts", fonts.folder, "--tab", tab,
             "--frames", "8", "--size", "1200x800", "--screenshot", str(shot)],
            check=True, timeout=120, cwd=os.path.dirname(os.path.dirname(__file__)))
        assert Image.open(shot).size == (1200, 800)

"""Font Converter tab: choose every parameter that shapes the converted fonts.

Element IDs (FC-xx) refer to docs/UE-SPEC.md.
"""

import os
import threading
import time

import numpy as np
from imgui_bundle import imgui

from ..convert import convert, load_source
from ..fontdata import STYLE_COUNT
from ..layers import baseline, layers_for
from ..options import ConvertOptions
from ..ttf import family_name, file_stem
from ..verify import verify
from .widgets import V2, Texture, help_marker, path_input, u32

SOURCE_LABELS = {
    "remom": ("ReMoM checkout", "folder", "folder containing MoX/src/Init_Data/FONTS_LBX.c"),
    "fonts_c": ("FONTS_LBX.c", "open", "path to FONTS_LBX.c"),
    "lbx": ("FONTS.LBX", "open", "path to FONTS.LBX from the game"),
}
PIXEL_UNIT_CHOICES = [16, 32, 64, 128, 256]


class ConverterTab:
    def __init__(self, opts, status, open_in_studio):
        self.opts = opts
        self.status = status
        self.open_in_studio = open_in_studio
        self.data = None
        self.data_error = ""
        self.data_key = None
        self.log = []
        self.busy = ""
        self.result = None
        self.sheet = None
        self.sheet_key = None
        self.sheet_style = 0
        self.sheet_zoom = 3
        self.preset_path = "remom-fonts-preset.json"
        self.log_seen = 0

    # --- background work ----------------------------------------------------

    def _run(self, label, fn):
        if self.busy:
            return
        self.busy = label

        def work():
            started = time.time()
            try:
                fn()
            except Exception as e:  # shown to the user, never fatal
                self.log.append(f"{label} failed: {e}")
                self.status(f"{label} failed: {e}")
            finally:
                self.log.append(f"{label} took {time.time() - started:.1f}s")
                self.busy = ""
        threading.Thread(target=work, daemon=True).start()

    def check_source(self):
        try:
            self.data = load_source(self.opts)
            self.data_error = ""
            self.data_key = (self.opts.source_kind, self.opts.source_path, self.opts.palette_entry)
            self.status(f"Source OK: {len(self.data.styles)} styles")
        except (OSError, ValueError) as e:
            self.data = None
            self.data_error = str(e)
        self.sheet_key = None

    def start_convert(self):
        opts = ConvertOptions.from_json(self.opts.to_json())  # snapshot

        def job():
            self.log.append(f"Converting to {opts.out_dir} ...")
            manifest = convert(opts, progress=lambda m: self.log.append("  " + m))
            n = sum(len(s["layers"]) for s in manifest["styles"])
            self.result = manifest
            self.log.append(f"Wrote {n} fonts to {os.path.join(opts.out_dir, 'fonts')}")
            self.status(f"Converted {n} fonts")
        self._run("Convert", job)

    def start_verify(self):
        opts = ConvertOptions.from_json(self.opts.to_json())

        def job():
            data = load_source(opts)
            problems = verify(data, opts)
            if problems:
                self.log += [f"  {p}" for p in problems[:50]]
            msg = "Verify: every layer matches the source bitmaps" if not problems else \
                f"Verify: {len(problems)} glyph layers differ"
            self.log.append(msg)
            self.status(msg)
        self._run("Verify", job)

    # --- layout -----------------------------------------------------------

    def draw(self):
        avail = imgui.get_content_region_avail()
        imgui.begin_child("fc_options", V2(460, avail.y), imgui.ChildFlags_.borders | imgui.ChildFlags_.resize_x)
        self.draw_options()
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("fc_right", V2(0, avail.y))
        self.draw_actions()
        self.draw_sheet()
        imgui.end_child()

    def draw_options(self):
        o = self.opts

        imgui.separator_text("Source")  # FC-01
        for kind, (label, _, _) in SOURCE_LABELS.items():
            if imgui.radio_button(label, o.source_kind == kind):
                o.source_kind = kind
            imgui.same_line()
        imgui.new_line()
        _, dialog, hint = SOURCE_LABELS[o.source_kind]
        _, o.source_path = path_input("Source", o.source_path, dialog, hint)
        if o.source_kind == "lbx":
            imgui.set_next_item_width(120)
            _, o.palette_entry = imgui.input_int("Palette entry", o.palette_entry)
            o.palette_entry = max(1, o.palette_entry)
            help_marker("FONTS.LBX entry holding the VGA palette and font color sets. The game's default is 2.")
        if imgui.button("Check source"):
            self.check_source()
        if self.data_error:
            imgui.text_colored(imgui.ImVec4(1, 0.45, 0.4, 1), self.data_error)
        elif self.data is not None:
            imgui.same_line()
            imgui.text_disabled(f"{len(self.data.styles)} styles, palette OK")

        imgui.separator_text("Output")  # FC-02
        _, o.out_dir = path_input("Output folder", o.out_dir, "folder", "output folder")

        imgui.separator_text("Styles")  # FC-03
        for s in range(STYLE_COUNT):
            if s % 4:
                imgui.same_line(0, 12)
            label = f"{s}"
            if self.data is not None:
                label += f" ({self.data.styles[s].height}px)"
            checked = s in o.styles
            c, checked = imgui.checkbox(label + f"##st{s}", checked)
            if c:
                o.styles = sorted(set(o.styles) | {s}) if checked else [x for x in o.styles if x != s]
        if imgui.small_button("All"):
            o.styles = list(range(STYLE_COUNT))
        imgui.same_line()
        if imgui.small_button("None"):
            o.styles = []

        imgui.separator_text("Geometry")  # FC-04
        cur = PIXEL_UNIT_CHOICES.index(o.pixel_units) if o.pixel_units in PIXEL_UNIT_CHOICES else 2
        imgui.set_next_item_width(100)
        c, cur = imgui.combo("Units per pixel", cur, [str(v) for v in PIXEL_UNIT_CHOICES])
        if c:
            o.pixel_units = PIXEL_UNIT_CHOICES[cur]
        help_marker("Font units per source pixel. Units per em = cell height x this. Any value renders the "
                    "same; 64 keeps em sizes in the usual range.")
        _, o.line_gap_from_spacing = imgui.checkbox("Line gap from vertical spacing", o.line_gap_from_spacing)
        _, o.include_del = imgui.checkbox("Include character 127", o.include_del)
        help_marker("The data has a slot for 127 (DEL), which the game never prints.")

        if imgui.tree_node("Baselines"):  # FC-05
            if self.data is None:
                imgui.text_disabled("Check the source to see detected baselines.")
            elif imgui.begin_table("baselines", 3, imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.sizing_fixed_fit):
                imgui.table_setup_column("Style")
                imgui.table_setup_column("Detected")
                imgui.table_setup_column("Override")
                imgui.table_headers_row()
                for s in o.styles:
                    style = self.data.styles[s]
                    auto = baseline(style, ConvertOptions())
                    imgui.table_next_row()
                    imgui.table_next_column()
                    imgui.text(f"Font {s}")
                    imgui.table_next_column()
                    imgui.text(f"{auto} of {style.height}")
                    imgui.table_next_column()
                    imgui.push_id(f"bl{s}")
                    on = s in o.baseline_overrides
                    c, on = imgui.checkbox("##on", on)
                    if c:
                        if on:
                            o.baseline_overrides[s] = auto
                        else:
                            o.baseline_overrides.pop(s, None)
                    if on:
                        imgui.same_line()
                        imgui.set_next_item_width(90)
                        _, v = imgui.input_int("##v", o.baseline_overrides[s])
                        o.baseline_overrides[s] = max(0, min(style.height, v))
                    imgui.pop_id()
                imgui.end_table()
            imgui.tree_pop()

        imgui.separator_text("Layers")  # FC-06
        _, o.color_layers = imgui.checkbox("Color layers (L00..L15)", o.color_layers)
        _, o.shadow_layer = imgui.checkbox("Shadow layer", o.shadow_layer)
        imgui.begin_disabled(not o.shadow_layer)
        imgui.same_line()
        _, o.shadow_includes_edge = imgui.checkbox("includes index-0 pixels##sh", o.shadow_includes_edge)
        imgui.end_disabled()
        help_marker("The game skips index-0 (edge) pixels when it stamps outlines, so leave this off to match it.")
        _, o.solid_layer = imgui.checkbox("Solid layer", o.solid_layer)
        imgui.begin_disabled(not o.solid_layer)
        imgui.same_line()
        _, o.solid_includes_edge = imgui.checkbox("includes index-0 pixels##so", o.solid_includes_edge)
        imgui.end_disabled()

        imgui.separator_text("Naming")  # FC-07
        _, o.family_prefix = imgui.input_text("Family prefix", o.family_prefix)
        _, o.file_prefix = imgui.input_text("File prefix", o.file_prefix)
        _, o.version = imgui.input_text("Version", o.version)
        if self.data is not None and o.styles:
            st = self.data.styles[o.styles[0]]
            ls = layers_for(st, o)
            if ls:
                imgui.text_disabled(f'e.g. "{family_name(st, ls[0], o)}" in {file_stem(st, ls[0], o)}.ttf')

        imgui.separator_text("Extras")  # FC-08
        _, o.write_specimen = imgui.checkbox("HTML specimen", o.write_specimen)
        _, o.write_svgs = imgui.checkbox("SVG per glyph", o.write_svgs)
        imgui.begin_disabled(not o.write_svgs)
        imgui.set_next_item_width(100)
        _, o.svg_color_set = imgui.slider_int("SVG color set", o.svg_color_set, 0, 15)
        imgui.set_next_item_width(100)
        _, o.svg_scale = imgui.slider_int("SVG scale", o.svg_scale, 1, 32, "%dx")
        imgui.end_disabled()

        imgui.separator_text("Presets")  # FC-09
        _, self.preset_path = path_input("Preset file", self.preset_path, "save", "preset .json")
        if imgui.button("Save preset"):
            try:
                with open(self.preset_path, "w") as f:
                    f.write(o.to_json())
                self.status(f"Saved preset {self.preset_path}")
            except OSError as e:
                self.status(f"Save failed: {e}")
        imgui.same_line()
        if imgui.button("Load preset"):
            try:
                with open(self.preset_path) as f:
                    loaded = ConvertOptions.from_json(f.read())
                self.opts.__dict__.update(loaded.__dict__)
                self.status(f"Loaded preset {self.preset_path}")
            except (OSError, ValueError, TypeError) as e:
                self.status(f"Load failed: {e}")
        imgui.same_line()
        if imgui.button("Reset to defaults"):
            keep = (o.source_kind, o.source_path, o.out_dir)
            self.opts.__dict__.update(ConvertOptions().__dict__)
            o.source_kind, o.source_path, o.out_dir = keep

    def draw_actions(self):
        o = self.opts
        problems = o.validate()
        imgui.begin_disabled(bool(problems) or bool(self.busy))
        if imgui.button("Convert", V2(140, 32)):  # FC-10
            self.start_convert()
        imgui.same_line()
        if imgui.button("Verify", V2(100, 32)):
            self.start_verify()
        imgui.end_disabled()
        imgui.set_item_tooltip("Rasterize every layer with FreeType and compare it to the source pixels.")
        imgui.same_line()
        imgui.begin_disabled(self.result is None)
        if imgui.button("Open in Text Studio", V2(170, 32)):
            self.open_in_studio(o.out_dir)
        imgui.end_disabled()
        if self.busy:
            imgui.same_line()
            imgui.text(f"{self.busy}... " + "|/-\\"[int(time.time() * 8) % 4])
        for p in problems:
            imgui.text_colored(imgui.ImVec4(1, 0.7, 0.3, 1), p)

        # FC-11 results and log
        if self.result is not None and imgui.collapsing_header("Result", imgui.TreeNodeFlags_.default_open):
            if imgui.begin_table("result", 4, imgui.TableFlags_.borders | imgui.TableFlags_.row_bg
                                 | imgui.TableFlags_.sizing_fixed_fit):
                for h in ("Style", "Cell", "Baseline", "Layers"):
                    imgui.table_setup_column(h)
                imgui.table_headers_row()
                for s in self.result["styles"]:
                    imgui.table_next_row()
                    for v in (f"Font {s['index']}", f"{s['height']}px", str(s["baseline"]),
                              " ".join(l["name"] for l in s["layers"])):
                        imgui.table_next_column()
                        imgui.text(v)
                imgui.end_table()
        if imgui.collapsing_header("Log", imgui.TreeNodeFlags_.default_open):
            imgui.begin_child("log", V2(0, 140), imgui.ChildFlags_.borders)
            for line in self.log[-400:]:
                imgui.text_unformatted(line)
            if len(self.log) != self.log_seen:
                self.log_seen = len(self.log)
                imgui.set_scroll_here_y(1.0)
            imgui.end_child()

    def draw_sheet(self):
        # FC-12: source glyphs with the baseline that will be used
        if not imgui.collapsing_header("Glyph sheet (source)", imgui.TreeNodeFlags_.default_open):
            return
        if self.data is None:
            imgui.text_disabled("Check the source to see its glyphs and baselines here.")
            return
        imgui.set_next_item_width(120)
        _, self.sheet_style = imgui.combo("Style##sheet", self.sheet_style, [f"Font {i}" for i in range(STYLE_COUNT)])
        imgui.same_line()
        imgui.set_next_item_width(120)
        _, self.sheet_zoom = imgui.slider_int("Zoom##sheet", self.sheet_zoom, 1, 8, "%dx")
        style = self.data.styles[self.sheet_style]
        base = baseline(style, self.opts)
        key = (self.sheet_style, id(self.data))
        if key != self.sheet_key:
            self.sheet_key = key
            self.sheet_img, self.sheet_cell = glyph_sheet(self.data, style)
            self.sheet = self.sheet or Texture()
            self.sheet.upload(self.sheet_img)
        imgui.text_disabled(f"baseline row {base} (blue)" + ("  overridden" if self.opts.baseline_for(style.index)
                                                              is not None else "  detected"))
        h, w = self.sheet_img.shape[:2]
        z = self.sheet_zoom
        imgui.begin_child("sheet", V2(0, 0), 0, imgui.WindowFlags_.horizontal_scrollbar)
        p = imgui.get_cursor_screen_pos()
        dl = imgui.get_window_draw_list()
        dl.add_rect_filled(p, V2(p.x + w * z, p.y + h * z), u32((90, 74, 58)))
        dl.add_image(self.sheet.ref(), p, V2(p.x + w * z, p.y + h * z))
        cw, ch = self.sheet_cell
        for row in range(h // ch):
            y = p.y + (row * ch + 1 + base) * z
            dl.add_line(V2(p.x, y), V2(p.x + w * z, y), u32((80, 160, 255), 200))
        imgui.dummy(V2(w * z, h * z))
        imgui.end_child()


def glyph_sheet(data, style, per_row=16):
    """RGBA sheet of a style's glyphs in color set 0, plus (cell w, cell h)."""
    colors = data.color_set_rgb(0)
    glyphs = [g for g in style.glyphs if g.char <= 126]
    cw = max(max((g.width for g in glyphs), default=1), 1) + 2
    ch = style.height + 2
    rows = (len(glyphs) + per_row - 1) // per_row
    img = np.zeros((rows * ch, per_row * cw, 4), dtype=np.uint8)
    for i, g in enumerate(glyphs):
        ox, oy = (i % per_row) * cw + 1, (i // per_row) * ch + 1
        for (x, y), c in g.pixels.items():
            if 0 <= y < style.height:
                img[oy + y, ox + x, :3] = colors[c]
                img[oy + y, ox + x, 3] = 255
    return img, (cw, ch)

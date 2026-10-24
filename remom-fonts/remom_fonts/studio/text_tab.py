"""Text Studio tab: type text, pick a font and its settings, preview, export.

Element IDs (TS-xx) refer to docs/UE-SPEC.md.
"""

import os
from dataclasses import asdict, dataclass, field

import numpy as np
from imgui_bundle import imgui

from .. import imageexport
from ..layers import OUTLINE_STYLES
from ..textrender import ALIGNMENTS, FontSet, TextSettings, render
from .widgets import V2, Texture, help_marker, palette_index_picker, path_input, swatch_row, u32

BACKGROUNDS = ("Checkerboard", "Palette color", "Custom color")
EXPORT_FORMATS = [
    ("indexed_png", "Indexed PNG (game palette)"),
    ("rgba_png", "RGBA PNG"),
    ("mox_picture", "MoX picture (.bin)"),
    ("c_source", "C header (.h)"),
]


@dataclass
class TextState:
    font_folder: str = "out"
    settings: dict = field(default_factory=lambda: asdict(TextSettings()))
    use_custom_colors: bool = False
    custom_colors: list = field(default_factory=lambda: [0] * 16)
    zoom: int = 4
    background: int = 0
    bg_palette_index: int = 1
    bg_custom: list = field(default_factory=lambda: [90, 74, 58])
    show_grid: bool = True
    show_bounds: bool = False
    fit: bool = True
    padding: int = 1
    canvas: list = field(default_factory=lambda: [160, 40])
    position: list = field(default_factory=lambda: [2, 2])
    formats: dict = field(default_factory=lambda: {"indexed_png": True, "rgba_png": True,
                                                   "mox_picture": False, "c_source": False})
    rgba_scale: int = 1
    rgba_use_background: bool = False
    export_name: str = "text"
    export_folder: str = "exports"


class TextTab:
    def __init__(self, state, status):
        self.s = state
        self.status = status
        self.fonts = None
        self.load_error = ""
        self.texture = None
        self.rendered = None
        self.render_key = None
        self.pending_export = None
        self.last_export = ""

    # --- data -------------------------------------------------------------

    def load_fonts(self, folder=None):
        if folder:
            self.s.font_folder = folder
        try:
            self.fonts = FontSet(self.s.font_folder)
            self.load_error = ""
            if self.s.settings["style"] not in self.fonts.styles:
                self.s.settings["style"] = self.fonts.style_indices()[0]
            n = sum(len(st["layers"]) for st in self.fonts.styles.values())
            self.status(f"Loaded {n} fonts in {len(self.fonts.styles)} styles from {self.s.font_folder}")
        except (OSError, ValueError, KeyError) as e:
            self.fonts = None
            self.load_error = f"No fonts loaded: {e}"
        self.render_key = None

    def text_settings(self):
        ts = TextSettings(**{k: v for k, v in self.s.settings.items() if k in TextSettings.__dataclass_fields__})
        ts.custom_colors = list(self.s.custom_colors) if self.s.use_custom_colors else None
        return ts

    def render_now(self):
        ts = self.text_settings()
        if self.s.fit:
            return render(self.fonts, ts, padding=self.s.padding)
        return render(self.fonts, ts, canvas=tuple(self.s.canvas), position=tuple(self.s.position))

    def refresh(self):
        key = (repr(self.s.settings), self.s.use_custom_colors, tuple(self.s.custom_colors), self.s.fit,
               self.s.padding, tuple(self.s.canvas), tuple(self.s.position))
        if key == self.render_key or self.fonts is None:
            return
        self.render_key = key
        self.rendered = self.render_now()
        if self.texture is None:
            self.texture = Texture()
        self.texture.upload(imageexport.to_rgba(self.rendered))

    # --- layout -----------------------------------------------------------

    def draw(self):
        avail = imgui.get_content_region_avail()
        imgui.begin_child("ts_controls", V2(380, avail.y),
                          imgui.ChildFlags_.borders | imgui.ChildFlags_.resize_x)
        self.draw_controls()
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("ts_right", V2(0, avail.y))
        self.refresh()
        export_h = 230
        right = imgui.get_content_region_avail()
        imgui.begin_child("ts_preview", V2(0, max(120, right.y - export_h)), imgui.ChildFlags_.borders)
        self.draw_preview()
        imgui.end_child()
        imgui.begin_child("ts_export", V2(0, 0), imgui.ChildFlags_.borders)
        self.draw_export()
        imgui.end_child()
        imgui.end_child()
        self.draw_replace_modal()

    def draw_controls(self):
        s = self.s
        st = s.settings

        imgui.separator_text("Fonts")  # TS-01
        _, s.font_folder = path_input("Font folder", s.font_folder, "folder", "converter output folder")
        if imgui.button("Load fonts"):
            self.load_fonts()
        help_marker("A folder written by the Font Converter: manifest.json plus fonts/*.ttf.")
        if self.load_error:
            imgui.text_colored(imgui.ImVec4(1, 0.45, 0.4, 1), self.load_error)
        if self.fonts is None:
            imgui.text_wrapped("Convert fonts in the Font Converter tab, or point this at an existing output folder.")
            return

        imgui.separator_text("Text")  # TS-02
        _, st["text"] = imgui.input_text_multiline("##text", st["text"], V2(-1, imgui.get_text_line_height() * 6))
        imgui.text_disabled("{n} normal  {h} highlight  {s} special  {{ = {")
        if self.rendered and self.rendered.unknown_chars:
            imgui.text_colored(imgui.ImVec4(1, 0.7, 0.3, 1),
                               "Not in font (skipped): " + " ".join(repr(c) for c in self.rendered.unknown_chars))

        imgui.separator_text("Font")  # TS-03
        styles = self.fonts.style_indices()
        labels = [self.style_label(i) for i in styles]
        cur = styles.index(st["style"])
        changed, cur = imgui.combo("Style", cur, labels)
        if changed:
            st["style"] = styles[cur]

        imgui.separator_text("Colors")  # TS-04
        sets = self.fonts.color_sets
        pal = self.fonts.palette
        for key, label in (("normal_set", "Normal"), ("highlight_set", "Highlight"), ("special_set", "Special")):
            imgui.set_next_item_width(70)
            _, st[key] = imgui.combo(f"{label}##{key}", st[key], [str(i) for i in range(len(sets))])
            imgui.same_line()
            swatch_row([pal[i] for i in sets[st[key]]], size=11, gap=1)
        _, s.use_custom_colors = imgui.checkbox("Custom normal colors", s.use_custom_colors)  # TS-05
        help_marker("Like Set_Font_Colors_15(): pick each color index's palette entry yourself.")
        if s.use_custom_colors:
            if imgui.button("Copy from normal set"):
                s.custom_colors = list(sets[st["normal_set"]])
            used = self.used_color_indices()
            for i in used:
                _, s.custom_colors[i] = palette_index_picker(f"L{i:02d}", s.custom_colors[i], pal, 18)
        _, st["solid"] = imgui.checkbox("Single color (Solid layer)", st["solid"])  # TS-06
        help_marker("Draws every pixel in normal color 1, ignoring shading.")

        imgui.separator_text("Outline")  # TS-07
        names = [f"{k} {v[0]}" for k, v in OUTLINE_STYLES.items()]
        _, st["outline_style"] = imgui.combo("Outline style", st["outline_style"], names)
        imgui.begin_disabled(st["outline_style"] == 0)
        _, st["outline_color"] = palette_index_picker("Outline color", st["outline_color"], pal)
        imgui.end_disabled()

        imgui.separator_text("Layout")  # TS-08
        for a in ALIGNMENTS:
            if imgui.radio_button(a.capitalize(), st["align"] == a):
                st["align"] = a
            imgui.same_line()
        imgui.new_line()
        _, st["extra_letter_spacing"] = imgui.slider_int("Letter spacing +", st["extra_letter_spacing"], -2, 8)
        _, st["extra_line_spacing"] = imgui.slider_int("Line spacing +", st["extra_line_spacing"], -4, 16)
        help_marker("Added on top of the font's own spacing. 0 matches the game.")

    def style_label(self, i):
        info = self.fonts.styles[i]
        kinds = [l["name"] for l in info["layers"] if l["color_index"] is not None]
        return f"Font {i}  ({info['height']}px, {len(kinds)} color layer{'s' if len(kinds) != 1 else ''})"

    def used_color_indices(self):
        info = self.fonts.styles[self.s.settings["style"]]
        return [l["color_index"] for l in info["layers"] if l["color_index"] is not None]

    def background_rgb(self):
        if self.s.background == 1:
            return self.fonts.palette[self.s.bg_palette_index]
        if self.s.background == 2:
            return tuple(self.s.bg_custom)
        return None

    def draw_preview(self):
        s = self.s
        # TS-10 toolbar
        imgui.set_next_item_width(120)
        _, s.zoom = imgui.slider_int("Zoom", s.zoom, 1, 12, "%dx")
        imgui.same_line()
        imgui.set_next_item_width(140)
        _, s.background = imgui.combo("Background", s.background, list(BACKGROUNDS))
        if self.fonts is not None and s.background == 1:
            imgui.same_line()
            _, s.bg_palette_index = palette_index_picker("bg", s.bg_palette_index, self.fonts.palette, 18)
        elif s.background == 2:
            imgui.same_line()
            imgui.set_next_item_width(160)
            col = [c / 255 for c in s.bg_custom]
            changed, col = imgui.color_edit3("##bgc", col, imgui.ColorEditFlags_.no_inputs)
            if changed:
                s.bg_custom = [int(round(c * 255)) for c in col]
        imgui.same_line()
        _, s.show_grid = imgui.checkbox("Pixel grid", s.show_grid)
        imgui.same_line()
        _, s.show_bounds = imgui.checkbox("Bounds", s.show_bounds)

        if self.rendered is None or self.texture is None:
            imgui.text_disabled("Load fonts to see a preview.")
            return
        h, w = self.rendered.indices.shape
        imgui.text_disabled(f"{w} x {h} px at 1x   colors used: {len(np.unique(self.rendered.indices))}")  # TS-12

        # TS-11 canvas
        imgui.begin_child("canvas", V2(0, 0), 0, imgui.WindowFlags_.horizontal_scrollbar)
        z = s.zoom
        p = imgui.get_cursor_screen_pos()
        p = V2(p.x + 8, p.y + 8)
        q = V2(p.x + w * z, p.y + h * z)
        dl = imgui.get_window_draw_list()
        bg = self.background_rgb()
        if bg is None:
            cell = 8
            dl.add_rect_filled(p, q, u32((200, 200, 200)))
            for cy in range(0, h * z, cell):
                for cx in range((cy // cell) % 2 * cell, w * z, cell * 2):
                    dl.add_rect_filled(V2(p.x + cx, p.y + cy),
                                       V2(min(q.x, p.x + cx + cell), min(q.y, p.y + cy + cell)), u32((150, 150, 150)))
        else:
            dl.add_rect_filled(p, q, u32(bg))
        dl.add_image(self.texture.ref(), p, q)
        if s.show_grid and z >= 4:
            grid = u32((0, 0, 0), 40)
            for x in range(w + 1):
                dl.add_line(V2(p.x + x * z, p.y), V2(p.x + x * z, q.y), grid)
            for y in range(h + 1):
                dl.add_line(V2(p.x, p.y + y * z), V2(q.x, p.y + y * z), grid)
        if s.show_bounds:
            dl.add_rect(p, q, u32((255, 80, 80)))
            ox, oy = self.rendered.origin
            base = self.fonts.styles[s.settings["style"]]["baseline"]
            y = p.y + (oy + base) * z
            dl.add_line(V2(p.x, y), V2(q.x, y), u32((80, 160, 255)))
        imgui.set_cursor_screen_pos(p)
        imgui.invisible_button("canvas_hit", V2(max(1, w * z), max(1, h * z)))
        if imgui.is_item_hovered():
            m = imgui.get_mouse_pos()
            px, py = int((m.x - p.x) // z), int((m.y - p.y) // z)
            if 0 <= px < w and 0 <= py < h:
                idx = int(self.rendered.indices[py, px])
                rgb = self.fonts.palette[idx]
                imgui.set_tooltip(f"x {px}  y {py}\nindex {idx}" + ("  (transparent)" if idx == 0 else "")
                                  + "\n#%02x%02x%02x" % rgb)
        imgui.dummy(V2(0, 8))
        imgui.end_child()

    def draw_export(self):
        s = self.s
        imgui.separator_text("Export")  # TS-13
        if imgui.radio_button("Fit to text", s.fit):
            s.fit = True
        imgui.same_line()
        if imgui.radio_button("Fixed canvas", not s.fit):
            s.fit = False
        imgui.same_line()
        imgui.set_next_item_width(220)
        if s.fit:
            _, s.padding = imgui.slider_int("Padding", s.padding, 0, 16)
        else:
            _, s.canvas = imgui.input_int2("Size W,H", s.canvas)
            s.canvas = [max(1, min(4096, v)) for v in s.canvas]
            imgui.same_line()
            imgui.set_next_item_width(160)
            _, s.position = imgui.input_int2("Text at X,Y", s.position)

        imgui.text("Formats:")
        for key, label in EXPORT_FORMATS:
            imgui.same_line()
            _, s.formats[key] = imgui.checkbox(label, s.formats[key])
        imgui.begin_disabled(not s.formats["rgba_png"])
        imgui.set_next_item_width(120)
        _, s.rgba_scale = imgui.slider_int("RGBA scale", s.rgba_scale, 1, 8, "%dx")
        imgui.same_line()
        _, s.rgba_use_background = imgui.checkbox("RGBA uses preview background", s.rgba_use_background)
        imgui.end_disabled()

        imgui.align_text_to_frame_padding()
        imgui.text("Folder   ")
        imgui.same_line()
        _, s.export_folder = path_input("Export folder", s.export_folder, "folder", "export folder")
        imgui.align_text_to_frame_padding()
        imgui.text("File name")
        imgui.same_line()
        imgui.set_next_item_width(220)
        _, s.export_name = imgui.input_text("##export_name", s.export_name)
        imgui.same_line()
        imgui.text_disabled("  ->  " + ", ".join(os.path.basename(p) for p in self.export_paths().values()))
        ready = self.rendered is not None and any(s.formats.values()) and s.export_name.strip()
        imgui.begin_disabled(not ready)
        if imgui.button("Export", V2(120, 0)):
            self.request_export()
        imgui.end_disabled()
        if self.last_export:
            imgui.same_line()
            imgui.text_wrapped(self.last_export)

    # --- export -----------------------------------------------------------

    def export_paths(self):
        s = self.s
        base = os.path.join(s.export_folder, s.export_name.strip())
        names = {"indexed_png": base + ".png", "rgba_png": base + f"-rgba-{s.rgba_scale}x.png",
                 "mox_picture": base + ".bin", "c_source": base + ".h"}
        return {k: names[k] for k, on in s.formats.items() if on}

    def request_export(self):
        paths = self.export_paths()
        existing = [p for p in paths.values() if os.path.exists(p)]
        if existing:
            self.pending_export = (paths, existing)
            imgui.open_popup("Replace files?")
        else:
            self.do_export(paths)

    def draw_replace_modal(self):
        if self.pending_export is None:
            return
        if not imgui.is_popup_open("Replace files?"):
            imgui.open_popup("Replace files?")
        opened, _ = imgui.begin_popup_modal("Replace files?", None, imgui.WindowFlags_.always_auto_resize)
        if opened:
            paths, existing = self.pending_export
            imgui.text("These files already exist and will be replaced:")
            for p in existing:
                imgui.bullet_text(p)
            if imgui.button("Replace", V2(120, 0)):
                self.do_export(paths)
                self.pending_export = None
                imgui.close_current_popup()
            imgui.same_line()
            if imgui.button("Cancel", V2(120, 0)):
                self.pending_export = None
                imgui.close_current_popup()
            imgui.end_popup()

    def do_export(self, paths):
        r = self.render_now()
        try:
            os.makedirs(self.s.export_folder or ".", exist_ok=True)
            for fmt, path in paths.items():
                bg = self.background_rgb() if (fmt == "rgba_png" and self.s.rgba_use_background) else None
                imageexport.save(r, fmt, path, scale=self.s.rgba_scale if fmt == "rgba_png" else 1,
                                 background=bg, name=self.s.export_name)
            h, w = r.indices.shape
            self.last_export = f"Wrote {len(paths)} file(s), {w}x{h}: " + ", ".join(os.path.basename(p) for p in paths.values())
            self.status(self.last_export)
        except (OSError, ValueError) as e:
            self.last_export = f"Export failed: {e}"
            self.status(self.last_export)

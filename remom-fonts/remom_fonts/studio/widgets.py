"""Small Dear ImGui building blocks shared by both tabs."""

import os

import numpy as np
from imgui_bundle import imgui
from OpenGL import GL

try:
    from imgui_bundle import portable_file_dialogs as pfd
except ImportError:  # pragma: no cover
    pfd = None

V2 = imgui.ImVec2


def rgb4(rgb, a=1.0):
    return imgui.ImVec4(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, a)


def u32(rgb, a=255):
    return imgui.color_convert_float4_to_u32(rgb4(rgb, a / 255))


class Texture:
    """An RGBA numpy image uploaded to OpenGL, drawn with nearest filtering."""

    def __init__(self):
        self.id = None
        self.width = self.height = 0

    def upload(self, rgba):
        rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
        self.height, self.width = rgba.shape[:2]
        if self.id is None:
            self.id = int(GL.glGenTextures(1))
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.id)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, self.width, self.height, 0,
                        GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, rgba)

    def ref(self):
        return imgui.ImTextureRef(self.id)


def help_marker(text):
    imgui.same_line()
    imgui.text_disabled("(?)")
    imgui.set_item_tooltip(text)


def swatch_row(colors, size=14, gap=2):
    """Draw a row of color squares (r, g, b) on the current line."""
    dl = imgui.get_window_draw_list()
    p = imgui.get_cursor_screen_pos()
    p = V2(p.x, p.y + max(0.0, (imgui.get_frame_height() - size) / 2))
    for i, c in enumerate(colors):
        x = p.x + i * (size + gap)
        dl.add_rect_filled(V2(x, p.y), V2(x + size, p.y + size), u32(c))
        dl.add_rect(V2(x, p.y), V2(x + size, p.y + size), u32((128, 128, 128), 160))
    imgui.dummy(V2(len(colors) * (size + gap), max(size, imgui.get_frame_height())))


def palette_index_picker(label, value, palette, size=20):
    """Swatch button that opens a 16x16 palette grid. Returns (changed, value)."""
    changed = False
    imgui.push_id(label)
    if imgui.color_button("##swatch", rgb4(palette[value]), imgui.ColorEditFlags_.no_tooltip, V2(size, size)):
        imgui.open_popup("palette")
    imgui.set_item_tooltip(f"palette index {value}  #%02x%02x%02x" % palette[value])
    imgui.same_line()
    imgui.text(f"{label}: {value}")
    if imgui.begin_popup("palette"):
        imgui.text("Choose a palette index (0 is transparent in pictures)")
        for i in range(256):
            if i % 16:
                imgui.same_line(0, 2)
            flags = imgui.ColorEditFlags_.no_tooltip
            if imgui.color_button(f"##p{i}", rgb4(palette[i]), flags, V2(16, 16)):
                value, changed = i, True
                imgui.close_current_popup()
            if imgui.is_item_hovered():
                imgui.set_tooltip(f"{i}  #%02x%02x%02x" % palette[i])
            if i == value:
                mn, mx = imgui.get_item_rect_min(), imgui.get_item_rect_max()
                imgui.get_window_draw_list().add_rect(mn, mx, u32((255, 255, 0)), 0, 0, 2)
        imgui.set_next_item_width(120)
        c, v = imgui.input_int("index", value)
        if c:
            value, changed = max(0, min(255, v)), True
        imgui.end_popup()
    imgui.pop_id()
    return changed, value


def path_input(label, value, kind="folder", hint="", width=-1):
    """Text field plus a Browse button. kind: folder | open | save."""
    changed = False
    imgui.push_id(label)
    browse_w = 70
    imgui.set_next_item_width((imgui.get_content_region_avail().x - browse_w - 8) if width < 0 else width)
    c, value = imgui.input_text_with_hint(f"##{label}", hint or label, value)
    changed |= c
    imgui.same_line()
    if imgui.button("Browse", V2(browse_w, 0)):
        picked = browse(kind, label, value)
        if picked:
            value, changed = picked, True
    imgui.pop_id()
    return changed, value


def browse(kind, title, current):
    """Native file dialog when one is available, else None."""
    if pfd is None:
        return None
    start = current if current and os.path.exists(current) else os.getcwd()
    try:
        if kind == "folder":
            return pfd.select_folder(title, start).result() or None
        if kind == "open":
            r = pfd.open_file(title, start).result()
            return r[0] if r else None
        return pfd.save_file(title, start).result() or None
    except Exception:
        return None

"""ReMoM Font Studio: a Dear ImGui app with two tabs.

* Text Studio     type text, pick a font and its settings, preview it the
                  way the game draws it, and export pictures for the game.
* Font Converter  choose every parameter of the conversion and run it.

    python -m remom_fonts.studio [--fonts DIR] [--source PATH] [--tab converter]

Layout and behavior are specified in docs/UE-SPEC.md (element IDs AP-xx,
TS-xx and FC-xx appear as comments in the code).
"""

import argparse
import json
import os
import sys
from dataclasses import asdict

from imgui_bundle import hello_imgui, imgui

from ..options import ConvertOptions
from .converter_tab import ConverterTab
from .text_tab import TextState, TextTab

APP_TITLE = "ReMoM Font Studio"
TABS = ("Text Studio", "Font Converter")


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "remom-fonts", "studio.json")


def load_session(path):
    try:
        with open(path) as f:
            raw = json.load(f)
        text = TextState(**{k: v for k, v in raw.get("text", {}).items() if k in TextState.__dataclass_fields__})
        opts = ConvertOptions.from_json(json.dumps(raw.get("converter", {})))
        return text, opts, raw.get("tab", 0)
    except (OSError, ValueError, TypeError):
        return TextState(), ConvertOptions(), 0


class StudioApp:
    def __init__(self, args):
        self.session_file = args.session or config_path()
        text, opts, tab = (TextState(), ConvertOptions(), 0) if args.fresh else load_session(self.session_file)
        if args.fonts:
            text.font_folder = args.fonts
        if args.source:
            opts.source_path = args.source
            opts.source_kind = "lbx" if args.source.upper().endswith(".LBX") else \
                "fonts_c" if args.source.endswith(".c") else "remom"
        if args.out:
            opts.out_dir = args.out
            text.font_folder = args.out
        if args.text is not None:
            text.settings["text"] = args.text.replace("\\n", "\n")
        if args.style is not None:
            text.settings["style"] = args.style
        if args.outline is not None:
            text.settings["outline_style"] = args.outline
        self.tab = TABS.index("Font Converter") if args.tab == "converter" else (0 if args.tab == "text" else tab)
        self.select_tab = True
        self.message = "Ready"
        self.text = TextTab(text, self.set_status)
        self.converter = ConverterTab(opts, self.set_status, self.open_in_studio)
        self.frames = 0
        self.exit_after = args.frames
        self.first_frame = True
        self.check_source_on_start = bool(opts.source_path)
        self.convert_on_start = args.convert

    def set_status(self, msg):
        self.message = msg

    def open_in_studio(self, folder):
        self.text.load_fonts(folder)
        self.tab, self.select_tab = 0, True

    def save_session(self):
        data = {"tab": self.tab, "text": asdict(self.text.s), "converter": json.loads(self.converter.opts.to_json())}
        try:
            os.makedirs(os.path.dirname(self.session_file), exist_ok=True)
            with open(self.session_file, "w") as f:
                json.dump(data, f, indent=1)
        except OSError:
            pass

    # --- frame ------------------------------------------------------------

    def gui(self):
        if self.first_frame:
            self.first_frame = False
            if os.path.exists(os.path.join(self.text.s.font_folder, "manifest.json")):
                self.text.load_fonts()
            if self.check_source_on_start:
                self.converter.check_source()
            if self.convert_on_start:
                self.converter.start_convert()

        if imgui.begin_tab_bar("tabs"):  # AP-02
            for i, name in enumerate(TABS):
                flags = imgui.TabItemFlags_.set_selected if (self.select_tab and self.tab == i) else 0
                selected, _ = imgui.begin_tab_item(name, None, flags)
                if selected:
                    if not self.select_tab:
                        self.tab = i
                    (self.text if i == 0 else self.converter).draw()
                    imgui.end_tab_item()
            self.select_tab = False
            imgui.end_tab_bar()

        self.frames += 1
        if self.exit_after and self.frames >= self.exit_after and not self.converter.busy:
            hello_imgui.get_runner_params().app_shall_exit = True

    def menus(self):  # AP-01
        if imgui.begin_menu("File"):
            if imgui.menu_item("Reload fonts", "", False)[0]:
                self.text.load_fonts()
            if imgui.menu_item("Save session now", "", False)[0]:
                self.save_session()
                self.set_status(f"Session saved to {self.session_file}")
            imgui.separator()
            if imgui.menu_item("Quit", "", False)[0]:
                hello_imgui.get_runner_params().app_shall_exit = True
            imgui.end_menu()
        if imgui.begin_menu("Go"):
            for i, name in enumerate(TABS):
                if imgui.menu_item(name, f"Ctrl+{i + 1}", self.tab == i)[0]:
                    self.tab, self.select_tab = i, True
            imgui.end_menu()
        if imgui.begin_menu("Help"):
            imgui.menu_item("Layout spec: remom-fonts/docs/UE-SPEC.md", "", False, False)
            imgui.menu_item("Color codes in text: {n} {h} {s}", "", False, False)
            imgui.end_menu()

    def shortcuts(self):
        io = imgui.get_io()
        if io.key_ctrl:
            for i in range(len(TABS)):
                if imgui.is_key_pressed(getattr(imgui.Key, f"_{i + 1}")):
                    self.tab, self.select_tab = i, True

    def status_bar(self):  # AP-03
        imgui.text(self.message)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="remom_fonts.studio", description=APP_TITLE)
    ap.add_argument("--fonts", help="converter output folder to load in Text Studio")
    ap.add_argument("--source", help="ReMoM checkout, FONTS_LBX.c or FONTS.LBX for the converter")
    ap.add_argument("--out", help="converter output folder (also loaded in Text Studio)")
    ap.add_argument("--tab", choices=("text", "converter"))
    ap.add_argument("--text", help="initial text (\\n for new lines)")
    ap.add_argument("--style", type=int, help="initial font style")
    ap.add_argument("--outline", type=int, help="initial outline style 0-5")
    ap.add_argument("--convert", action="store_true", help="run the conversion on start")
    ap.add_argument("--fresh", action="store_true", help="ignore the saved session")
    ap.add_argument("--session", help="session file (default ~/.config/remom-fonts/studio.json)")
    ap.add_argument("--size", default="1400x900", help="window size WxH")
    ap.add_argument("--frames", type=int, default=0, help="quit after this many frames (for screenshots/tests)")
    ap.add_argument("--screenshot", help="save the last frame to this PNG on exit")
    args = ap.parse_args(argv)

    app = StudioApp(args)
    w, h = (int(v) for v in args.size.lower().split("x"))

    rp = hello_imgui.RunnerParams()
    rp.app_window_params.window_title = APP_TITLE
    rp.app_window_params.window_geometry.size = (w, h)
    rp.imgui_window_params.default_imgui_window_type = \
        hello_imgui.DefaultImGuiWindowType.provide_full_screen_window
    rp.imgui_window_params.show_menu_bar = True
    rp.imgui_window_params.show_menu_app = False
    rp.imgui_window_params.show_menu_view = False
    rp.imgui_window_params.show_status_bar = True
    rp.imgui_window_params.show_status_fps = False
    rp.ini_folder_type = hello_imgui.IniFolderType.temp_folder
    rp.ini_filename = "remom-fonts-studio.ini"
    rp.fps_idling.enable_idling = not args.frames

    def show_gui():
        app.shortcuts()
        app.gui()

    rp.callbacks.show_gui = show_gui
    rp.callbacks.show_menus = app.menus
    rp.callbacks.show_status = app.status_bar
    if not args.frames:
        rp.callbacks.before_exit = app.save_session
    hello_imgui.run(rp)

    if args.screenshot:
        from PIL import Image
        shot = hello_imgui.final_app_window_screenshot()
        if shot.size:
            Image.fromarray(shot).save(args.screenshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())

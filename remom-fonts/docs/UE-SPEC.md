# ReMoM Font Studio: User Experience Specification

| | |
|---|---|
| Product | ReMoM Font Studio (`python -m remom_fonts.studio`) |
| Toolkit | Dear ImGui via imgui-bundle (GLFW + OpenGL 3) |
| Spec version | 1.0 (matches the first implementation) |
| Status | Draft for discussion |

This document describes what the Studio shows, where, and how each control
behaves. Its purpose is to let us discuss layout changes precisely: every
element has a stable ID (`AP-xx` app shell, `TS-xx` Text Studio, `FC-xx` Font
Converter). The same IDs appear as comments in the code
(`remom_fonts/studio/*.py`), so a change agreed here maps to one place there.

**How to propose a change:** name the element ID(s) and say what should change
and why. For example: "TS-13: move Export to the left panel under TS-08 so the
preview gets the full height". Add it to [§12 Open questions](#12-open-questions)
or the [change log](#13-change-log) once agreed.

---

## 1. Purpose and users

| User | Wants to |
|---|---|
| Game/UI developer (ReMoM) | See exactly how a string will look in-game in each font, color set and outline style, then save it as a picture the engine can draw. |
| Artist / modder | Try wording, colors and outlines for menus, buttons and titles, and export them as PNGs at game resolution or scaled up. |
| Tool maintainer | Re-run the font conversion with different parameters (layers, baselines, naming, units) and check the result against the source. |

Primary tasks, in order of frequency:

1. **Preview text** in a chosen style and colors (Text Studio).
2. **Export** that text as game-ready pictures (Text Studio).
3. **Convert** fonts with chosen parameters, then verify them (Font Converter).

## 2. Terms

| Term | Meaning |
|---|---|
| Style | One of the 8 fonts in `FONTS.LBX` (0–7). Each has a cell height, a baseline and spacing. |
| Layer | One TrueType font per style and color index (`L00`..`L15`), plus `Shadow` and `Solid`. Stacked, they rebuild the original glyph. |
| Palette index | 0–255, an entry in the game's 256-color VGA palette. **0 is transparent** in pictures (`ST_TRANSPARENT`). **1 is black** (`ST_BLACK`). |
| Color set | 16 palette indices (one per color index). `FONTS.LBX` has 16 sets. The game picks a *normal*, a *highlight* and a *special* set. |
| Outline style | 0 None, 1 Down, 2 Up, 3 Heavy, 4 Outline, 5 Outline Heavy: the `Shadow` layer stamped at 1–2 px offsets behind the text (`Print_Display()`). |
| Edge pixels | Pixels stored with color index 0. Drawn in color 0 of the set, but skipped when stamping outlines. |
| MoX picture | The game's in-memory picture: a 16-byte header, then palette indices column by column (`Create_Picture()`). |
| Game pixel | One source pixel. The preview zooms these; exports are at 1 game pixel = 1 image pixel unless scaled. |

## 3. App shell

```
+------------------------------------------------------------------------+
| File  Go  Help                                              (AP-01)    |
+------------------------------------------------------------------------+
| [Text Studio] [Font Converter]                              (AP-02)    |
|                                                                        |
|                       active tab content                               |
|                                                                        |
+------------------------------------------------------------------------+
| status message                                              (AP-03)    |
+------------------------------------------------------------------------+
```

| ID | Element | Behavior |
|---|---|---|
| AP-01 | Menu bar | **File**: Reload fonts, Save session now, Quit. **Go**: Text Studio (Ctrl+1), Font Converter (Ctrl+2). **Help**: pointers to this spec and the color codes (informational). |
| AP-02 | Tab bar | Two tabs. The last active tab is restored on start. Converter → "Open in Text Studio" switches tabs. |
| AP-03 | Status bar | One line: the latest result or error from either tab ("Loaded 45 fonts…", "Wrote 2 file(s)…", "Convert failed: …"). |

Window: default 1400×900, resizable; title "ReMoM Font Studio". Every panel
stretches with the window. The left panels can be resized by dragging their right edge.

**Session:** on quit, all Text Studio and Converter settings are saved to
`~/.config/remom-fonts/studio.json` and restored on the next start (`--fresh`
ignores it). ImGui's own window state goes in the temp folder.

**Command line** (for scripting and screenshots): `--fonts DIR`, `--source PATH`,
`--out DIR`, `--tab text|converter`, `--text`, `--style`, `--outline`,
`--convert`, `--size WxH`, `--frames N`, `--screenshot PNG`, `--session FILE`, `--fresh`.

## 4. Text Studio

```
+---------------- TS left (380 px, resizable) ---+------------- TS right -------------------------+
| -- Fonts ------------------------------ TS-01  | Zoom[4x] Background[Checker v] [x]Grid [ ]Bounds|
| [folder path.....................] [Browse]    |                                         TS-10   |
| [Load fonts] (?)                               | 74 x 25 px at 1x   colors used: 6       TS-12   |
| -- Text ------------------------------- TS-02  | +---------------------------------------------+ |
| +------------------------------------------+   | |                                             | |
| | multi-line text                          |   | |      zoomed preview (scrolls)       TS-11   | |
| +------------------------------------------+   | |                                             | |
| {n} normal {h} highlight {s} special {{ = {    | +---------------------------------------------+ |
| -- Font ------------------------------- TS-03  | -- Export ----------------------------- TS-13   |
| [Font 2 (9px, 2 color layers)      v] Style    | (o) Fit to text ( ) Fixed canvas  Padding [1]   |
| -- Colors ----------------------------- TS-04  | Formats: [x]Indexed PNG [x]RGBA PNG [ ].bin [ ].h|
| [0 v] Normal    ■■■■■■■■■■■■■■■■               | RGBA scale [1x]  [ ] RGBA uses preview background|
| [2 v] Highlight ■■■■■■■■■■■■■■■■               | Folder    [exports......................][Browse]|
| [3 v] Special   ■■■■■■■■■■■■■■■■               | File name [text      ] -> text.png, text-rgba-1x |
| [ ] Custom normal colors (?)            TS-05  | [Export]  Wrote 2 file(s), 74x25: ...           |
| [ ] Single color (Solid layer) (?)      TS-06  |                                                 |
| -- Outline ---------------------------- TS-07  |                                                 |
| [0 None                            v]          |                                                 |
| ■ Outline color: 1                             |                                                 |
| -- Layout ----------------------------- TS-08  |                                                 |
| (o)Left ( )Center ( )Right                     |                                                 |
| Letter spacing + [0]   Line spacing + [0] (?)  |                                                 |
+------------------------------------------------+-------------------------------------------------+
```

The preview redraws on every change. No "apply" button.

### Left panel: what to draw

| ID | Element | Control | Values / range | Default | Behavior | Game equivalent |
|---|---|---|---|---|---|---|
| TS-01 | Font folder | path field + Browse + **Load fonts** + help | a converter output folder (`manifest.json`, `fonts/*.ttf`) | `out` | Loads every layer font listed in the manifest. Loaded automatically on start if the folder has a manifest. On failure shows a red "No fonts loaded: …" and hides TS-02..TS-08. | `Load_Font_File()` |
| TS-02 | Text | multi-line text box, 6 lines tall | any text; `\n` = new line; codes `{n}` `{h}` `{s}`, `{{` = literal `{` | `Hello, Wizard!` | Characters not in the font are skipped and listed in orange below the box. | control chars `\x01`–`\x03` in `Print_String()` |
| TS-03 | Style | combo | the styles in the manifest, labeled "Font N (H px, K color layers)" | Font 2 | Switches font. Colors, outline and layout settings are kept. | `Set_Font_Style(font_idx, …)` |
| TS-04 | Normal / Highlight / Special set | three combos (0–15), each followed by 16 swatches | color sets 0–15 | 0 / 2 / 3 | Swatches show the set's 16 colors in order (index 0 first). | `Set_Font_Style(…, color1, color2, color3)` |
| TS-05 | Custom normal colors | checkbox, "Copy from normal set" button, one palette picker per color index the style uses (`L01: 200`) | palette 0–255 per index | off; all 0 | Replaces the normal set with your own 16 indices. Highlight and special are unaffected. | `Set_Font_Colors_15()` |
| TS-06 | Single color (Solid layer) | checkbox + help | — | off | Draws every pixel in normal color index 1 and ignores shading. | — |
| TS-07 | Outline style, Outline color | combo; palette picker (disabled when style is 0) | styles 0–5; palette 0–255 | 0 None; 1 (black) | The outline is drawn behind the text, never over it. Edge pixels cast no outline. | `Set_Font_Style_Outline()`, `Set_Outline_Color()` |
| TS-08 | Alignment, Letter spacing +, Line spacing + | radio buttons; sliders | left/center/right; −2..8; −4..16 | left; 0; 0 | Alignment is within the text block (the widest line). Spacing is added to the font's own; 0 matches the game. | `Print`, `Print_Centered`, `Print_Right` |

**Palette picker** (TS-05, TS-07, TS-10): a swatch plus "label: index". Clicking
it opens a 16×16 grid of the 256 palette colors. Hover shows the index and hex;
the current entry has a yellow frame. There is an exact-index field at the
bottom. Choosing a color closes the grid.

### Right panel: preview and export

| ID | Element | Control | Values / range | Default | Behavior |
|---|---|---|---|---|---|
| TS-10 | Preview toolbar | Zoom slider; Background combo (+ palette picker or color editor); Pixel grid; Bounds | zoom 1–12×; Checkerboard / Palette color / Custom color | 4×, Checkerboard, grid on, bounds off | Background only affects the preview (and the RGBA export if TS-13 asks). Grid shows at 4× and above. Bounds draws the image edge (red) and the first line's baseline (blue). |
| TS-11 | Preview canvas | scrollable zoomed image | — | — | Exactly what will be exported, at the chosen zoom, with nearest-neighbor scaling. Hovering a pixel shows "x, y, palette index (transparent), #rrggbb". |
| TS-12 | Image info | text | — | — | "W × H px at 1x   colors used: N" |
| TS-13 | Export | Fit / Fixed canvas radios; Padding or Size W,H + Text at X,Y; format checkboxes; RGBA scale; RGBA-uses-background; Folder (+Browse); File name with the resulting file names; **Export** | padding 0–16; canvas 1–4096; scale 1–8× | Fit, padding 1; canvas 160×40 at 2,2; indexed + RGBA on; 1×; off; `exports`; `text` | **Export** is disabled when nothing is loaded, no format is checked, or the name is empty. If any target file exists, a modal "Replace files?" lists them (Replace / Cancel). The result is shown next to the button and in AP-03. |

### Export formats

| Format | File | Content | Use |
|---|---|---|---|
| Indexed PNG | `name.png` | 8-bit, the FONTS.LBX palette, index 0 transparent, 1× | Game asset pipelines; keeps exact palette indices. |
| RGBA PNG | `name-rgba-Nx.png` | true color, scaled N×, transparent or preview background | Docs, web, modern UIs. |
| MoX picture | `name.bin` | 16-byte header (`width`, `height`, `0xDE0A`, 0, 0, zero padding) + `width×height` indices, column-major | Load and draw with ReMoM's picture routines. |
| C header | `name.h` | the MoX picture bytes as `static const uint8_t name[]`, plus `NAME_WIDTH`/`NAME_HEIGHT` | Embed in the build like `Init_Data/`. |

Fit to text crops to the drawn pixels (outline included) plus the padding.
Fixed canvas puts the top-left of the text cell at X,Y and clips anything outside.

## 5. Font Converter

```
+------------- FC left (460 px, resizable) ------+------------- FC right ---------------------------+
| -- Source ----------------------------- FC-01  | [Convert] [Verify] [Open in Text Studio]  FC-10  |
| (o)ReMoM checkout ( )FONTS_LBX.c ( )FONTS.LBX  | validation messages (orange)                     |
| [path..........................] [Browse]      | v Result ------------------------------- FC-11   |
| Palette entry [2] (LBX only)                   |   Style | Cell | Baseline | Layers               |
| [Check source]  8 styles, palette OK           | v Log                                            |
| -- Output ----------------------------- FC-02  |   font 0 L00 ... Wrote 45 fonts ...              |
| [out...........................] [Browse]      | v Glyph sheet (source) --------------- FC-12     |
| -- Styles ----------------------------- FC-03  |   [Font 0 v] Style  Zoom [3x]                    |
| [x]0 (6px) [x]1 (6px) [x]2 (9px) [x]3 (9px)    |   baseline row 5 (blue)  detected                |
| [x]4 (10px)[x]5 (16px)[x]6 (5px) [x]7 (10px)   |   +------------------------------------------+   |
| [All] [None]                                   |   | ! " # $ % ...   (glyphs, blue baselines) |   |
| -- Geometry --------------------------- FC-04  |   +------------------------------------------+   |
| [64 v] Units per pixel (?)                     |                                                  |
| [x] Line gap from vertical spacing             |                                                  |
| [ ] Include character 127 (?)                  |                                                  |
| > Baselines                             FC-05  |                                                  |
| -- Layers ----------------------------- FC-06  |                                                  |
| [x] Color layers (L00..L15)                    |                                                  |
| [x] Shadow layer  [ ] includes index-0 pixels  |                                                  |
| [x] Solid layer   [x] includes index-0 pixels  |                                                  |
| -- Naming ----------------------------- FC-07  |                                                  |
| Family prefix / File prefix / Version          |                                                  |
| e.g. "ReMoM Font 0 L00" in ReMoM-Font0-L00.ttf |                                                  |
| -- Extras ----------------------------- FC-08  |                                                  |
| [x] HTML specimen  [ ] SVG per glyph           |                                                  |
| SVG color set [0]  SVG scale [8x]              |                                                  |
| -- Presets ---------------------------- FC-09  |                                                  |
| [preset.json.................] [Browse]        |                                                  |
| [Save preset] [Load preset] [Reset to defaults]|                                                  |
+------------------------------------------------+--------------------------------------------------+
```

| ID | Element | Control | Values / range | Default | Behavior | CLI flag |
|---|---|---|---|---|---|---|
| FC-01 | Source | 3 radios, path + Browse (folder or file per kind), Palette entry (LBX only), **Check source** | ReMoM checkout / FONTS_LBX.c / FONTS.LBX; entry ≥ 1 | ReMoM checkout, empty, 2 | Check source loads and decodes the data; shows "8 styles, palette OK" or the error in red. Runs on start if a source is set. Enables style heights (FC-03), detected baselines (FC-05), name example (FC-07) and the glyph sheet (FC-12). | `--remom` `--fonts-c` `--lbx` `--palette-entry` |
| FC-02 | Output folder | path + Browse | any folder (created if missing) | `out` | Fonts go in `fonts/`, plus `manifest.json`, `specimen.html`, `svg/`. Existing files with the same names are replaced. | `-o` |
| FC-03 | Styles | 8 checkboxes in 2 rows (with heights after Check source), All, None | any subset | all | Only checked styles are written and listed in the manifest. | `--styles 0,2,5` |
| FC-04 | Units per pixel; Line gap from vertical spacing; Include character 127 | combo; checkboxes | 16/32/64/128/256 | 64; on; off | Units per em = cell height × units. Rendering is identical for any value. The line gap uses the game's vertical spacing when on, and is 0 when off. | `--pixel-units` `--no-line-gap` `--include-del` |
| FC-05 | Baselines | collapsible table: Style / Detected ("7 of 9") / Override (checkbox + number) | 0..cell height | detected | Checking Override starts at the detected value. The glyph sheet (FC-12) shows the baseline in use live. | `--baseline 2=7` |
| FC-06 | Layers | 3 checkboxes, each with an "includes index-0 pixels" sub-option for Shadow and Solid | — | on, on (edge off), on (edge on) | At least one layer kind must stay on. Shadow without edge pixels matches the game. | `--no-color-layers` `--no-shadow` `--no-solid` `--shadow-includes-edge` `--solid-excludes-edge` |
| FC-07 | Naming | 3 text fields + live example | non-empty; the file prefix cannot contain `/\:*?"<>|` | `ReMoM Font`, `ReMoM-Font`, `0.1` | The example updates as you type. | `--family-prefix` `--file-prefix` `--font-version` |
| FC-08 | Extras | checkboxes; SVG color set, SVG scale sliders (disabled unless SVG is on) | set 0–15; scale 1–32× | specimen on, SVG off, 0, 8× | — | `--no-specimen` `--svg` `--svg-color-set` `--svg-scale` |
| FC-09 | Presets | path + Browse, Save, Load, Reset to defaults | `.json` | `remom-fonts-preset.json` | Save writes every FC option. Load replaces them. Reset keeps the source and output and restores everything else. Presets also work with the CLI. | `--preset` |
| FC-10 | Actions | **Convert**, **Verify**, **Open in Text Studio**, spinner, validation list | — | — | Convert/Verify are disabled while invalid (reasons listed in orange) or busy. Both run in the background, so the UI stays live. Open in Text Studio is enabled after a convert; it loads the output folder in TS-01 and switches tab. | — |
| FC-11 | Result, Log | table; scrolling log (140 px) | — | — | Result lists style, cell, baseline, layers. The log shows each layer as it is written, timing, verify mismatches (first 50), and errors. It scrolls to the newest line. | — |
| FC-12 | Glyph sheet | style combo, zoom 1–8×, image | — | Font 0, 3× | Source glyphs (not the converted fonts) in color set 0, 16 per row, with the baseline in use drawn in blue on every row. Text below says whether it is detected or overridden. | — |

Validation messages (FC-10), shown all at once:
"choose a source", "choose an output folder", "select at least one style",
"pixel units must be 1..256", "enable at least one kind of layer",
"SVG color set must be 0..15", "SVG scale must be 1..64",
"family and file prefixes cannot be empty",
"file prefix has characters not allowed in file names".

## 6. Key flows

1. **First run.** Converter → choose ReMoM checkout → Check source → Convert →
   Open in Text Studio → type → Export. (With `--source` and `--convert`, this
   starts already converted.)
2. **Make a button label.** Text Studio → Style 2 → Outline 4, color 1 → Fixed
   canvas = button size, Text at X,Y = inset → Export Indexed PNG + MoX picture.
3. **Fix a baseline.** Converter → Check source → Glyph sheet, pick the style →
   Baselines → Override → watch the blue line → Convert → Verify.
4. **Reuse settings.** Converter → Save preset → later Load preset, or
   `python -m remom_fonts --preset file.json`.

## 7. States and feedback

| Situation | What the user sees |
|---|---|
| No fonts loaded | TS-01 only, plus a red reason (when there is one) and a hint to convert first. Preview area: "Load fonts to see a preview." |
| Characters not in font | Orange "Not in font (skipped): 'é'" under TS-02. They are left out of the image. |
| Export target exists | Modal "Replace files?" with the list; Replace / Cancel. |
| Export done / failed | Message next to Export and in AP-03. |
| Source missing or bad | Red message under Check source; style labels lose their heights; FC-05/FC-12 show "Check the source…". |
| Converting / verifying | Buttons disabled, "Convert... /" spinner, log fills. |
| Background error | Logged as "Convert failed: …" and shown in AP-03. The app keeps running. |

## 8. Visual conventions

* Default Dear ImGui dark theme. Errors are red `#FF7366`; warnings and validation are orange `#FFB34D`; hints are disabled grey text.
* Sections inside panels use separator titles (`-- Fonts ---`); secondary groups use collapsing headers or tree nodes.
* Game pixels are always scaled by whole numbers with nearest-neighbor filtering. The preview never smooths.
* Swatches: 11–20 px squares with a thin grey border; the palette grid uses 16 px cells.
* Help markers `(?)` explain game behavior on hover; they never hold required information.

## 9. Keyboard and accessibility

* Ctrl+1 / Ctrl+2 switch tabs. Tab / Shift+Tab move between text fields.
* Ctrl+Enter is not bound. Text box Enter inserts a new line.
* High-DPI screens are scaled automatically by hello_imgui.
* Color is never the only signal: palette pickers always show the index number, and the hover tooltips give hex values.

## 10. Platform notes

* Needs OpenGL 3. Works on Windows, macOS and Linux (including headless under Xvfb for tests and screenshots).
* Browse buttons use native dialogs (portable-file-dialogs). On Linux they need `zenity` or `kdialog`. Without one, Browse does nothing and the path fields still work.

## 11. Out of scope (v1)

Editing glyphs; kerning; non-ASCII; palette cycling and color remapping
(runtime effects); importing pictures back; batch export from a list of strings.

## 12. Open questions

Numbered so they can be discussed and closed one by one.

| # | Question | Notes |
|---|---|---|
| OQ-1 | Should TS-13 Export move into the left panel so the preview gets the full right side? | The right panel currently splits preview and export at a fixed 230 px. |
| OQ-2 | In Fixed canvas mode, should alignment be relative to the canvas instead of the text block? | Today Center/Right align lines to each other, not to the canvas. |
| OQ-3 | Batch export: one picture per line of TS-02? | Useful for menus with many labels. |
| OQ-4 | Show highlight/special swatches only when the text uses `{h}`/`{s}`? | Would shorten TS-04. |
| OQ-5 | Should the Converter be a separate window (dockable) so both tabs show at once? | hello_imgui supports docking layouts. |
| OQ-6 | Named presets for Text Studio (button styles, titles) in addition to the session? | Today only the Converter has presets. |
| OQ-7 | Should exports default to the ReMoM asset folder when a ReMoM checkout is the source? | Fewer clicks for game work. |

## 13. Change log

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-10-24 | First version, matching the first implementation. |

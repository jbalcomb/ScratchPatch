"""Every parameter that changes what the converter writes.

The CLI and the Font Converter UI both build a ConvertOptions and hand it to
convert.convert(). Options save to and load from JSON presets.
"""

import json
from dataclasses import asdict, dataclass, field, fields

from .fontdata import STYLE_COUNT

SOURCE_KINDS = ("remom", "fonts_c", "lbx")


@dataclass
class ConvertOptions:
    # input
    source_kind: str = "remom"         # remom checkout | FONTS_LBX.c | FONTS.LBX
    source_path: str = ""
    palette_entry: int = 2             # FONTS.LBX entry holding the palette (lbx only)

    # output
    out_dir: str = "out"
    styles: list = field(default_factory=lambda: list(range(STYLE_COUNT)))

    # geometry
    pixel_units: int = 64              # font units per source pixel
    baseline_overrides: dict = field(default_factory=dict)  # {style: rows from top}
    line_gap_from_spacing: bool = True # vertical_spacing becomes the line gap
    include_del: bool = False          # also map char 127 (the game never prints it)

    # layers
    color_layers: bool = True          # L00..L15
    shadow_layer: bool = True
    shadow_includes_edge: bool = False # game skips index 0 in outline passes
    solid_layer: bool = True
    solid_includes_edge: bool = True

    # naming
    family_prefix: str = "ReMoM Font"
    file_prefix: str = "ReMoM-Font"
    version: str = "0.1"

    # extras
    write_specimen: bool = True
    write_svgs: bool = False
    svg_color_set: int = 0
    svg_scale: int = 8

    def validate(self):
        problems = []
        if self.source_kind not in SOURCE_KINDS:
            problems.append(f"source_kind must be one of {', '.join(SOURCE_KINDS)}")
        if not self.source_path:
            problems.append("choose a source")
        if not self.out_dir:
            problems.append("choose an output folder")
        if not self.styles:
            problems.append("select at least one style")
        if any(not 0 <= s < STYLE_COUNT for s in self.styles):
            problems.append(f"styles are 0..{STYLE_COUNT - 1}")
        if not 1 <= self.pixel_units <= 256:
            problems.append("pixel units must be 1..256")
        if not (self.color_layers or self.shadow_layer or self.solid_layer):
            problems.append("enable at least one kind of layer")
        if not 0 <= self.svg_color_set < 16:
            problems.append("SVG color set must be 0..15")
        if not 1 <= self.svg_scale <= 64:
            problems.append("SVG scale must be 1..64")
        if not self.family_prefix.strip() or not self.file_prefix.strip():
            problems.append("family and file prefixes cannot be empty")
        if any(c in self.file_prefix for c in '/\\:*?"<>|'):
            problems.append("file prefix has characters not allowed in file names")
        return problems

    def baseline_for(self, style_index):
        return self.baseline_overrides.get(style_index, self.baseline_overrides.get(str(style_index)))

    def to_json(self):
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def from_json(cls, text):
        raw = json.loads(text)
        known = {f.name for f in fields(cls)}
        opts = cls(**{k: v for k, v in raw.items() if k in known})
        opts.baseline_overrides = {int(k): int(v) for k, v in opts.baseline_overrides.items()}
        return opts


DEFAULTS = ConvertOptions()

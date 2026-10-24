"""Run a conversion from a ConvertOptions. Shared by the CLI and the UI."""

import os

from .export import write_fonts, write_specimen, write_svgs
from .fontdata import load_lbx, load_remom_c

REMOM_FONTS_C = os.path.join("MoX", "src", "Init_Data", "FONTS_LBX.c")


def load_source(opts):
    if opts.source_kind == "lbx":
        return load_lbx(opts.source_path, opts.palette_entry)
    path = opts.source_path
    if opts.source_kind == "remom":
        path = os.path.join(path, REMOM_FONTS_C)
    return load_remom_c(path)


def convert(opts, progress=None):
    """Write everything opts asks for. Returns the manifest dict."""
    problems = opts.validate()
    if problems:
        raise ValueError("; ".join(problems))
    data = load_source(opts)
    os.makedirs(opts.out_dir, exist_ok=True)
    manifest = write_fonts(data, opts.out_dir, opts, progress)
    if opts.write_specimen:
        write_specimen(manifest, opts.out_dir)
    if opts.write_svgs:
        if progress:
            progress("SVGs")
        write_svgs(data, opts.out_dir, opts)
    return manifest

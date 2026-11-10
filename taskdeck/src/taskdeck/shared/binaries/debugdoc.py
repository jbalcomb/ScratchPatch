"""Debug information written out for the agent (watcom.py reads it):

    debug/DEBUG.md                 all of it in one markdown file: modules, every global symbol, and per
                                   module its routines (with parameters and locals), variables, types
                                   (structs with their fields, enums with their constants...) and line numbers
    debug/symbols-NNN.txt          every global symbol, one per line, by address, with the chunk file holding it
    debug/modules/NNNN-<name>.md   one module's part of DEBUG.md, for reading a module at a time

Addresses are given as the debug info writes them (segment:offset; for an LE program the segment is
the object number), with the linear address and the file offset where the file holds the bytes.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable

from .formats import Exe, h
from .watcom import DebugInfo, Module

ChunkFor = Callable[[int | None], str | None]


def resolve(exe: Exe, segment: int | None, offset: int | None) -> tuple[str, int | None]:
    """(address text, file offset) for a debug address. LE/LX: the segment is an object number. A DOS
    program: a paragraph number relative to the load segment."""
    if segment is None or offset is None:
        return "-", None
    if exe.locate is not None:
        linear, off = exe.locate(segment, offset)
        if linear is not None:
            return f"{segment}:{h(offset, 8)} (linear {h(linear, 8)})", off
        return f"{segment}:{h(offset, 8)}", None
    if exe.format == "MZ":
        header = int(exe.facts.get("header_bytes") or 0)
        image = segment * 16 + offset
        off = header + image
        return f"{segment:04X}:{offset:04X}", off if off < exe.size else None
    return f"{segment}:{h(offset, 8)}", None


def cell(text: object) -> str:
    """Safe in a markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def module_file(m: Module) -> str:
    stem = PureWindowsPath(m.name).name if "\\" in m.name else PurePosixPath(m.name).name
    return f"debug/modules/{m.index:04d}-{re.sub(r'[^A-Za-z0-9_.]+', '-', stem).strip('-').lower() or 'module'}.md"


def symbols_lines(exe: Exe, info: DebugInfo, chunk_for: ChunkFor) -> list[str]:
    lines = ["# address  file offset  kind  name  module  chunk file"]
    for g in sorted(info.globals, key=lambda g: (g.segment, g.offset)):
        where, off = resolve(exe, g.segment, g.offset)
        chunk = chunk_for(off)
        lines.append(f"{where}  {h(off, 8) if off is not None else '-':<10}  {g.kind_text:<12}  {g.name}  "
                     f"{info.modules[g.module].name if g.module < len(info.modules) else '?'}"
                     + (f"  {chunk}" if chunk else ""))
    return lines


def chunk_symbols(exe: Exe, info: DebugInfo) -> dict[int, list[str]]:
    """File offset -> names of the global symbols there (for the chunk files' headers)."""
    out: dict[int, list[str]] = {}
    for g in info.globals:
        _, off = resolve(exe, g.segment, g.offset)
        if off is not None:
            out.setdefault(off, []).append(g.name)
    return out


def document(exe: Exe, info: DebugInfo, name: str, chunk_for: ChunkFor) -> tuple[str, dict[str, str]]:
    """(DEBUG.md, {module file: its markdown})."""
    detailed = [m for m in info.modules if m.symbols or m.types or m.lines]
    out = [f"# Debug info of {name}", "",
           f"Watcom debug info, version {info.version}, at file offset {h(info.start, 8)} ({info.size:,} bytes). "
           f"Languages: {', '.join(info.languages) or '-'}. Segments: "
           f"{', '.join(str(s) for s in info.segments)}"
           + (" (LE object numbers)" if exe.locate is not None else "") + ".", "",
           f"- {len(info.modules):,} modules (object files), {len(detailed):,} of them with local symbols, types "
           "and line numbers",
           f"- {len(info.globals):,} global symbols",
           f"- {sum(1 for m in info.modules for s in m.symbols if s.kind == 'routine'):,} routines, "
           f"{sum(1 for m in info.modules for s in m.symbols if s.kind == 'local'):,} local variables, "
           f"{sum(len(m.types) for m in info.modules):,} type records, "
           f"{sum(len(m.lines) for m in info.modules):,} line numbers", "",
           "Addresses are segment:offset as the debug info gives them"
           + (", with the linear address the program runs at" if exe.locate is not None else "")
           + "; file offsets and chunk files say where the bytes are in the parsed folder. Every module's details "
           "are also in their own file under `debug/modules/`, and every global symbol, one per line, in "
           "`debug/symbols-*.txt`.", ""]
    if info.warnings:
        out += ["## Warnings", ""] + [f"- {w}" for w in info.warnings] + [""]
    globals_by_module: dict[int, list] = {}
    for g in info.globals:
        globals_by_module.setdefault(g.module, []).append(g)
    out += ["## Modules", "", "| # | Module | Language | Code and data ranges | Globals | Routines | Locals | Types "
            "| Lines | Details |", "|---|---|---|---|---|---|---|---|---|---|"]
    for m in info.modules:
        ranges = ", ".join(f"{r.segment}:{h(r.start, 8)}+{h(r.size)}" for r in m.ranges) or "-"
        has = m.symbols or m.types or m.lines or globals_by_module.get(m.index)
        out.append(f"| {m.index} | {cell(m.name)} | {m.language or '-'} | {ranges} | "
                   f"{len(globals_by_module.get(m.index, [])):,} | "
                   f"{sum(1 for s in m.symbols if s.kind == 'routine'):,} | "
                   f"{sum(1 for s in m.symbols if s.kind == 'local'):,} | {len(m.types):,} | {len(m.lines):,} | "
                   + (f"`{module_file(m)}` |" if has else "- |"))
    out += ["", "## Global symbols", "", f"All {len(info.globals):,}, by address.", "",
            "| Address | File offset | Kind | Name | Module | Chunk file |", "|---|---|---|---|---|---|"]
    for g in sorted(info.globals, key=lambda g: (g.segment, g.offset)):
        where, off = resolve(exe, g.segment, g.offset)
        chunk = chunk_for(off)
        mod = info.modules[g.module].name if g.module < len(info.modules) else "?"
        out.append(f"| {where} | {h(off, 8) if off is not None else '-'} | {g.kind_text} | {cell(g.name)} | {cell(mod)} | "
                   f"{f'`{chunk}`' if chunk else '-'} |")
    out += ["", "## Modules in detail", ""]
    files = {}
    for m in info.modules:
        if not (m.symbols or m.types or m.lines or globals_by_module.get(m.index)):
            continue
        body = _module(exe, info, m, globals_by_module.get(m.index, []), chunk_for)
        files[module_file(m)] = f"# {m.name}\n\nModule {m.index} of the debug info of {name}.\n\n" + "\n".join(body) + "\n"
        out += [f"### {m.index}: {cell(m.name)}", ""] + body + [""]
    return "\n".join(out) + "\n", files


def _module(exe: Exe, info: DebugInfo, m: Module, globals_: list, chunk_for: ChunkFor) -> list[str]:
    out = []
    if m.ranges:
        out.append("Ranges: " + ", ".join(f"{resolve(exe, r.segment, r.start)[0]}, {r.size:,} bytes" for r in m.ranges))
        out.append("")
    if globals_:
        out += ["#### Global symbols", "", "| Address | Kind | Name | Chunk file |", "|---|---|---|---|"]
        for g in sorted(globals_, key=lambda g: (g.segment, g.offset)):
            where, off = resolve(exe, g.segment, g.offset)
            chunk = chunk_for(off)
            out.append(f"| {where} | {g.kind_text} | {cell(g.name)} | {f'`{chunk}`' if chunk else '-'} |")
        out.append("")
    variables = [s for s in m.symbols if s.kind == "module variable"]
    if variables:
        out += ["#### Module variables", "", "| Address | Name | Type |", "|---|---|---|"]
        for s in variables:
            where = s.location or resolve(exe, s.segment, s.offset)[0]
            out.append(f"| {cell(where)} | {cell(s.name)} | {cell(m.type_name(s.type) or '-')} |")
        out.append("")
    out += _routines(exe, m, chunk_for)
    other = [s for s in m.symbols if s.kind in ("member scope", "unknown")]
    if other:
        out += ["#### Other local records", ""]
        out += [f"- {s.kind} {cell(s.name)} {s.raw}".rstrip() for s in other]
        out.append("")
    if m.types:
        out += _types(m)
    if m.lines:
        out += ["#### Line numbers", "", "| Line | Address | File offset |", "|---|---|---|"]
        for ln in m.lines:
            where, off = resolve(exe, ln.segment, ln.offset)
            out.append(f"| {ln.line} | {where} | {h(off, 8) if off is not None else '-'} |")
        out.append("")
    return out


def _routines(exe: Exe, m: Module, chunk_for: ChunkFor) -> list[str]:
    out = []
    current: list[str] | None = None
    def close(lines: list[str]) -> list[str]:
        if lines[-2:] == ["| Local | Location | Type |", "|---|---|---|"]:
            lines = lines[:-2] + ["No local variables."]
        return lines + [""]

    for s in m.symbols:
        if s.kind in ("routine", "block"):
            if current:
                out += close(current)
            where, off = resolve(exe, s.segment, s.offset)
            chunk = chunk_for(off)
            if s.kind == "routine":
                current = [f"#### {cell(s.name) or '(unnamed routine)'}", "",
                           f"- Address: {where}, {s.size:,} bytes" + (f", in `{chunk}`" if chunk else ""),
                           f"- Type: {m.type_name(s.type) or '-'}"
                           + (f"; returns in {s.location}" if s.location and s.location != "none" else ""),
                           f"- Prologue {s.prologue} bytes, epilogue {s.epilogue} bytes, return address at frame "
                           f"offset {h(s.return_address)}" + (", far" if s.far else ""),
                           ]
                if s.parameters:
                    current.append(f"- Parameter locations: {', '.join(s.parameters)}")
                current += ["", "| Local | Location | Type |", "|---|---|---|"]
            else:
                current = [f"#### Block at {where}, {s.size:,} bytes", "", "| Local | Location | Type |",
                           "|---|---|---|"]
        elif s.kind == "local" and current is not None:
            current.append(f"| {cell(s.name)} | {cell(s.location)} | {cell(m.type_name(s.type) or '-')} |")
    if current:
        out += close(current)
    return out


def _types(m: Module) -> list[str]:
    names_of: dict[int, list[str]] = {}  # struct/enum index -> the names given to it
    by = {t.index: t for t in m.types if t.index is not None}
    for t in m.types:
        if t.kind == "typedef" and t.refs.get("base"):
            scope = by.get(t.refs.get("scope"))
            names_of.setdefault(t.refs["base"], []).append(f"{scope.name} {t.name}" if scope else t.name)
    out = ["#### Types", "", "| # | Kind | Name | Details |", "|---|---|---|---|"]
    for t in m.types:
        if t.kind in ("end",):
            continue
        detail = t.detail
        if t.kind == "typedef":
            scope = by.get(t.refs.get("scope"))
            detail = f"{(scope.name + ' ') if scope else ''}name for {m.type_name(t.refs.get('base', 0))} (#{t.refs.get('base')})"
        elif t.kind in ("pointer", "array", "subrange"):
            detail = f"{t.detail}; of {m.type_name(t.refs.get('base', 0))} (#{t.refs.get('base')})"
        elif t.kind == "procedure":
            params = ", ".join(m.type_name(p) or f"#{p}" for p in t.refs.get("params", []))
            detail = f"{t.detail}; returns {m.type_name(t.refs.get('return', 0)) or 'void'} ({params})"
        name = t.name or ", ".join(names_of.get(t.index or -1, []))
        out.append(f"| {t.index if t.index is not None else '-'} | {t.kind} | {cell(name)} | {cell(detail or t.raw)} |")
    out.append("")
    for t in m.types:
        if t.kind == "struct" and t.members:
            name = ", ".join(names_of.get(t.index or -1, [])) or f"struct #{t.index}"
            out += [f"##### {cell(name)} (#{t.index}, {t.detail})", "", "| Field | Offset / bits | Type |",
                    "|---|---|---|"]
            fields = sorted(t.members, key=lambda f: int(f[1].split()[1].rstrip(",")) if f[1].startswith("offset ") else -1)
            out += [f"| {cell(n)} | {cell(d)} | {cell(m.type_name(typ) or '-')} |" for n, d, typ in fields]
            out.append("")
        elif t.kind == "enum" and t.members:
            name = ", ".join(names_of.get(t.index or -1, [])) or f"enum #{t.index}"
            out += [f"##### {cell(name)} (#{t.index}, {t.detail})", "", "| Constant | Value |", "|---|---|"]
            out += [f"| {cell(n)} | {cell(d)} |" for n, d, _ in t.members]
            out.append("")
    return out

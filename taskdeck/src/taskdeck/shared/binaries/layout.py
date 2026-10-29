"""Writing a parsed executable to a folder an AI agent can walk through or search in small pieces.

    _<EXE NAME>/                    named like the executable with "_" in front, e.g. _WIZARDS.EXE/
      README.md                     how the folder is organised (read this first)
      SUMMARY.md                    what the file is, its layout, entry point, imports...
      metadata.json                 hashes, sizes, format and decoded header fields (machine-readable)
      index.json                    every region and every file, with its byte range
      headers/headers.txt           all decoded header fields
      tables/<table>-NNN.txt        relocations, sections, imports, resources, segments
      regions/NN-<region>/<first>-<last>.txt   hex dump of the region, one file per chunk
      strings/ascii-NNN.txt, strings/utf16-NNN.txt   text found in the file, with offsets
      exports/exports-NNN.txt, exports/exports.json  exported functions: ordinal, name, address, chunk

Every chunk file starts with a short header naming the region, its byte range, and the previous and next
chunk, so the agent can walk the file without the index. Nothing in the output depends on when it was
made, so parsing the same file again gives identical files (and a Drive upload skips them).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from taskdeck.core import Cancelled

from .formats import Exe, Region, h

GENERATOR = "TaskDeck binary parser 2"
EARLIER = ("TaskDeck exe_parse",)  # what earlier versions wrote in index.json: their folders are ours too
LINES_PER_FILE = 1000  # table and string files
BYTES_PER_LINE = 16
MAX_STRING = 400
EXPORTS_IN_SUMMARY = 40  # characters kept of a very long string


class NotOurs(Exception):
    """The output folder exists and wasn't written by this job: it's left alone."""


@dataclass
class Written:
    root: Path
    files: list[Path] = field(default_factory=list)  # relative to root
    chunks: int = 0
    strings: int = 0

    @property
    def bytes(self) -> int:
        return sum((self.root / f).stat().st_size for f in self.files)


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.]+", "-", text).strip("-").lower() or "region"


def chunk_name(start: int, end: int) -> str:
    return f"{start:08X}-{end - 1:08X}"


def owned_by_us(folder: Path) -> bool:
    try:
        return json.loads((folder / "index.json").read_text(encoding="utf-8")).get("generator", "").startswith(("TaskDeck binary parser", *EARLIER))
    except (OSError, ValueError, AttributeError):
        return False


def write(exe: Exe, data: bytes, source: Path, dest: Path, chunk: int, min_string: int = 4,
          cancelled: Callable[[], bool] = lambda: False) -> Written:
    """Write the folder ``dest`` (named like the executable). Built in a temporary folder next to it and
    swapped in at the end, so an interrupted run never leaves half a folder under the real name. An
    existing ``dest`` is replaced only if this job wrote it."""
    if dest.exists() and not owned_by_us(dest):
        raise NotOurs(f"{dest} already exists and wasn't made by TaskDeck. Move it or choose another folder.")
    work = dest.with_name(f".{dest.name}.partial")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        out = _Writer(exe, data, source, work, chunk, min_string, cancelled).run()
        if dest.exists():
            shutil.rmtree(dest)
        work.rename(dest)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    out.root = dest
    return out


class _Writer:
    def __init__(self, exe: Exe, data: bytes, source: Path, root: Path, chunk: int, min_string: int,
                 cancelled: Callable[[], bool]):
        self.exe, self.data, self.source, self.root = exe, data, source, root
        self.chunk, self.min_string, self.cancelled = chunk, min_string, cancelled
        self.out = Written(root)
        self.name = source.name
        self.index: dict = {"generator": GENERATOR, "file": self.name, "size": exe.size, "chunk_bytes": chunk,
                            "paths": "relative to this folder", "regions": [], "tables": {}, "strings": {}}

    def run(self) -> Written:
        self.region_dirs = [f"regions/{i:02d}-{slug(r.name)}" for i, r in enumerate(self.exe.regions)]
        self.chunks = [(i, s, min(s + self.chunk, r.end)) for i, r in enumerate(self.exe.regions)
                       for s in range(r.start, r.end, self.chunk)]
        self._regions()
        self._headers()
        for name, lines in self.exe.tables.items():
            self.index["tables"][name] = self._paged(f"tables/{slug(name)}", lines, f"{self.name} · {name}")
        self._strings()
        self._exports()
        meta = self._metadata()
        self._put("metadata.json", json.dumps(meta, indent=2) + "\n")
        self._put("SUMMARY.md", self._summary(meta))
        self._put("README.md", README.format(name=self.name, chunk=self.chunk, generator=GENERATOR))
        self._put("index.json", json.dumps(self.index, indent=1) + "\n")
        return self.out

    # ---- files ------------------------------------------------------------------------------------

    def _put(self, rel: str, text: str | bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
        self.out.files.append(Path(rel))

    def _paged(self, prefix: str, lines: list[str], title: str) -> list[dict]:
        """Long lists split into files of LINES_PER_FILE lines: prefix-001.txt, prefix-002.txt..."""
        pages = [lines[i:i + LINES_PER_FILE] for i in range(0, len(lines), LINES_PER_FILE)] or [[]]
        out = []
        for n, page in enumerate(pages, 1):
            rel = f"{prefix}-{n:03d}.txt"
            first = n * LINES_PER_FILE - LINES_PER_FILE + 1
            head = f"# {title} · lines {first:,}-{first + len(page) - 1:,} of {len(lines):,} (file {n} of {len(pages)})\n"
            self._put(rel, head + "\n".join(page) + "\n")
            out.append({"file": rel, "first_line": first, "lines": len(page)})
        return out

    # ---- regions as hex dumps -----------------------------------------------------------------------

    def _chunk_rel(self, k: int) -> str:
        i, s, e = self.chunks[k]
        return f"{self.region_dirs[i]}/{chunk_name(s, e)}.txt"

    def _regions(self) -> None:
        entries = {i: {"n": i, "name": r.name, "kind": r.kind, "start": r.start, "end": r.end, "size": r.size,
                       "address": r.address or None, "base": r.base, "note": r.note, "dir": self.region_dirs[i],
                       "chunks": []} for i, r in enumerate(self.exe.regions)}
        for k, (i, s, e) in enumerate(self.chunks):
            if self.cancelled():
                raise Cancelled()
            r = self.exe.regions[i]
            rel = self._chunk_rel(k)
            prev = self._chunk_rel(k - 1) if k else "(start of file)"
            nxt = self._chunk_rel(k + 1) if k + 1 < len(self.chunks) else "(end of file)"
            span = f"file {h(s, 8)}-{h(e - 1, 8)} ({e - s:,} bytes)"
            if r.base is not None:
                span += f" · {r.address} {h(r.base + s - r.start, 8)}-{h(r.base + e - 1 - r.start, 8)}"
            head = [f"# {self.name} · region {i:02d} {r.name} ({r.kind}) · chunk {k + 1} of {len(self.chunks)}",
                    f"# {span}",
                    f"# prev: {prev}",
                    f"# next: {nxt}",
                    "# columns: file offset" + (f" | {r.address}" if r.base is not None else "") + " | bytes | ASCII"]
            body = [self._hex_line(off, min(off + BYTES_PER_LINE, e), r) for off in range(s, e, BYTES_PER_LINE)]
            self._put(rel, "\n".join(head + body) + "\n")
            item = {"file": rel, "start": s, "end": e}
            entries[i]["chunks"].append(item)
            self.out.chunks += 1
        self.index["regions"] = list(entries.values())

    def _hex_line(self, start: int, end: int, r: Region) -> str:
        row = self.data[start:end]
        hexes = " ".join(f"{b:02X}" for b in row)
        if len(row) > 8:  # a gap in the middle, like hexdump -C
            hexes = hexes[:23] + " " + hexes[23:]
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        addr = f"{r.base + start - r.start:08X}  " if r.base is not None else ""
        return f"{start:08X}  {addr}{hexes:<49}  |{text}|"

    # ---- headers, strings ---------------------------------------------------------------------------

    def _headers(self) -> None:
        if not self.exe.headers:
            return
        parts = [f"# {self.name} · decoded headers ({self.exe.format})"]
        for title, rows in self.exe.headers.items():
            width = max((len(n) for n, _, _ in rows), default=0)
            parts.append(f"\n## {title}")
            parts.extend(f"{n:<{width}}  {v:<12}  {m}".rstrip() for n, v, m in rows)
        self._put("headers/headers.txt", "\n".join(parts) + "\n")

    def _strings(self) -> None:
        for kind, pattern, width in (("ascii", rb"[\x20-\x7e\t]{%d,}", 1), ("utf16", rb"(?:[\x20-\x7e]\x00){%d,}", 2)):
            lines = []
            for m in re.finditer(pattern % self.min_string, self.data):
                text = m.group().decode("ascii" if width == 1 else "utf-16-le")
                r = self.exe.region_at(m.start())
                where = r.name if r else "?"
                if r and r.base is not None:
                    where += f" {r.address} {h(r.base + m.start() - r.start, 8)}"
                lines.append(f"{m.start():08X}  [{where}]  {text[:MAX_STRING]}{'…' if len(text) > MAX_STRING else ''}")
            if lines:
                self.index["strings"][kind] = self._paged(f"strings/{kind}", ["# file offset  [region address]  text"] + lines,
                                                          f"{self.name} · {kind} strings of {self.min_string}+ characters")
                self.out.strings += len(lines)

    # ---- metadata and summary -----------------------------------------------------------------------

    def _metadata(self) -> dict:
        d = self.data
        stat = self.source.stat()
        entry = None
        if self.exe.entry:
            off, how = self.exe.entry
            entry = {"file_offset": off, "header": how, "chunk": self._chunk_for(off)}
        return {
            "file": {"name": self.name, "size": self.exe.size, "md5": hashlib.md5(d).hexdigest(),
                     "sha1": hashlib.sha1(d).hexdigest(), "sha256": hashlib.sha256(d).hexdigest(),
                     "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")},
            "format": self.exe.format,
            "description": self.exe.description,
            "dll": self.exe.dll,
            "entry_point": entry,
            "facts": self.exe.facts,
            "headers": {t: {n: v for n, v, _ in rows} for t, rows in self.exe.headers.items()},
            "regions": [{"name": r.name, "kind": r.kind, "start": r.start, "end": r.end, "size": r.size,
                         "note": r.note} for r in self.exe.regions],
            "warnings": self.exe.warnings,
            "output": {"generator": GENERATOR, "chunk_bytes": self.chunk, "chunks": self.out.chunks,
                       "strings": self.out.strings, "min_string_length": self.min_string},
        }

    def _chunk_for(self, offset: int | None) -> str | None:
        if offset is None:
            return None
        if not hasattr(self, "_starts"):
            self._starts = [s for _, s, _ in self.chunks]
        k = bisect.bisect_right(self._starts, offset) - 1
        return self._chunk_rel(k) if k >= 0 and offset < self.chunks[k][2] else None

    def _exports(self) -> None:
        """Exports: what other modules link to. One line each (for grep), paged, plus exports.json."""
        exe = self.exe
        if not exe.exports:
            return
        rows = []
        lines = ["# ordinal  file offset  address  name  chunk file (forwarders: the DLL.function they pass on to)"]
        for e in exe.exports:
            chunk = self._chunk_for(e.offset)
            name = e.name or "(by ordinal only)"
            where = h(e.offset, 8) if e.offset is not None else "-"
            lines.append(f"{e.ordinal:6d}  {where:<10}  {e.address:<34}  {name}" + (f"  {chunk}" if chunk else ""))
            row = {"ordinal": e.ordinal, "name": e.name, "file_offset": e.offset, "address": e.address,
                   "chunk": chunk}
            if e.rva is not None:
                row["rva"] = e.rva
            if e.forwarder:
                row["forwarder"] = e.forwarder
            if exe.format == "NE":
                row["resident_name"] = e.resident
            rows.append(row)
        self.index["exports"] = self._paged("exports/exports", lines, f"{self.name} · exports")
        self._put("exports/exports.json", json.dumps({
            "file": self.name, "dll_name": exe.facts.get("export_dll_name") or exe.facts.get("module_name") or "",
            "count": len(rows), "exports": rows}, indent=1) + "\n")
        self.index["exports_json"] = "exports/exports.json"

    def _summary(self, meta: dict) -> str:
        exe, f = self.exe, meta["file"]
        out = [f"# {self.name}", "", exe.description or exe.format, "",
               "| | |", "|---|---|",
               f"| Size | {exe.size:,} bytes ({h(exe.size)}) |",
               f"| Format | {exe.format} |",
               f"| MD5 | `{f['md5']}` |", f"| SHA-256 | `{f['sha256']}` |",
               f"| Modified | {f['modified']} |",
               f"| Chunks | {self.out.chunks:,} of up to {self.chunk:,} bytes |", ""]
        if meta["entry_point"]:
            e = meta["entry_point"]
            out += ["## Entry point", "",
                    f"{e['header']} → file offset {h(e['file_offset'], 8)}, in `{e['chunk']}`", ""]
        out += ["## Layout", "",
                "Every byte of the file belongs to exactly one region. Offsets are file offsets, end exclusive.", "",
                "| # | Region | Kind | File offsets | Size | Address of start | Folder | Note |",
                "|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(exe.regions):
            addr = f"{r.address} {h(r.base, 8)}" if r.base is not None else ""
            out.append(f"| {i:02d} | {r.name} | {r.kind} | {h(r.start, 8)}-{h(r.end, 8)} | {r.size:,} | {addr} | "
                       f"`{self.region_dirs[i]}/` | {r.note} |")
        out.append("")
        facts = {k: v for k, v in exe.facts.items() if not isinstance(v, (dict, list))}
        if facts:
            out += ["## Key facts", ""] + [f"- **{k.replace('_', ' ')}**: {_fmt(v)}" for k, v in facts.items()] + [""]
        if isinstance(exe.facts.get("imports"), dict):
            dlls = exe.facts["imports"]
            out += ["## Imports", "", f"{sum(dlls.values()):,} functions from {len(dlls)} DLLs "
                    f"(full list: `tables/imports-*.txt`)", ""] + [f"- {d}: {n}" for d, n in dlls.items()] + [""]
        if exe.exports:
            ex = exe.exports
            named = sum(1 for e in ex if e.name)
            fwd = sum(1 for e in ex if e.forwarder)
            dll_name = exe.facts.get("export_dll_name") or exe.facts.get("module_name") or ""
            out += ["## Exports", "",
                    f"{len(ex):,} exports" + (f" of {dll_name}" if dll_name else "") + f": {named:,} by name, "
                    f"{len(ex) - named:,} by ordinal only" + (f", {fwd:,} forwarded to other DLLs" if fwd else "") + ".",
                    "Full list with addresses and the chunk file holding each: `exports/exports-*.txt` "
                    "(one per line, for grep) and `exports/exports.json`.", ""]
            shown = ex[:EXPORTS_IN_SUMMARY]
            out += ["| Ordinal | Name | Address |", "|---|---|---|"]
            out += [f"| {e.ordinal} | {e.name or '(by ordinal only)'} | {e.address} |" for e in shown]
            if len(ex) > len(shown):
                out.append(f"| … | {len(ex) - len(shown):,} more in `exports/` | |")
            out.append("")
        if exe.facts.get("imported_modules"):
            out += ["## Imported modules", ""] + [f"- {m}" for m in exe.facts["imported_modules"]] + [""]
        if isinstance(exe.facts.get("resources"), dict):
            out += ["## Resources", ""] + [f"- {t}: {n}" for t, n in exe.facts["resources"].items()] + [""]
        out += ["## Other files", "",
                "- `headers/headers.txt`: every decoded header field" if exe.headers else "",
                *(f"- `tables/{slug(t)}-*.txt`: {t} ({len(lines):,} lines)" for t, lines in exe.tables.items()),
                *(f"- `strings/{k}-*.txt`: {sum(p['lines'] for p in v) - 1:,} {k} strings" for k, v in self.index["strings"].items()),
                *([f"- `exports/exports-*.txt`, `exports/exports.json`: {len(exe.exports):,} exports"]
                  if exe.exports else []),
                "- `metadata.json`: hashes, header fields and facts, machine-readable",
                "- `index.json`: every region and chunk file with its byte range",
                "- `README.md`: how to walk and search this folder", ""]
        if exe.warnings:
            out += ["## Warnings", ""] + [f"- {w}" for w in exe.warnings] + [""]
        return "\n".join(line for line in out if line is not None) + "\n"


def _fmt(v: object) -> str:
    return h(v) if isinstance(v, int) and not isinstance(v, bool) and v > 9 else str(v)


README = """# {name}: parsed executable

Made by {generator} (TaskDeck's "Parse executable" job) from the file `{name}`. It's split into small text
files so you can read or search one piece at a time instead of loading the whole binary.

## Start here

1. `SUMMARY.md`: what the file is, its **layout** (every region with its byte range), the entry point and
   which chunk file holds it, imports, resources.
2. `metadata.json`: the same facts plus hashes and every decoded header field, as JSON.
3. `index.json`: every region and every chunk file with its byte range. Use it to find the file holding a
   given offset without listing folders.

## Walking the bytes

`regions/NN-<region>/` holds one region of the file (header, relocation table, load module or section,
overlay, ...), numbered in file order. Each region is cut into chunks of up to {chunk:,} bytes, one file per
chunk, named by the **first and last file offset** in hex: `00001000-00001FFF.txt`.

Each chunk file starts with five `#` lines: the region, the byte range (file offsets, and the region's own
address where it has one: RVA for Windows sections, offset in the load image for DOS), and the **previous and
next** chunk files, so you can walk the file in order. Then one line per 16 bytes:

    file offset  [address]  16 bytes in hex                                    |ASCII|

To find offset X: take the chunk whose name range contains it (or look it up in `index.json`).
For a DOS (MZ) program, image offset = file offset - header size; a segment:offset pair from the
relocation table or a disassembler is image offset = segment * 16 + offset (segments relative to the load
segment).

## Searching

- Text: `strings/ascii-*.txt` and `strings/utf16-*.txt`, one string per line with its file offset and
  region. grep them first; they're much smaller than the hex dumps.
- Bytes: grep the hex dumps for a pattern like `55 8B EC` (push bp; mov bp,sp). A pattern can be split
  across two lines (and, rarely, two chunk files); search with fewer bytes if you get no hit.
- Exports (`exports/`): every exported function or variable, one per line: ordinal, file offset,
  address, name, and the chunk file holding it. `exports/exports.json` has the same as JSON. Forwarded
  exports name the DLL and function they pass on to.
- Tables (`tables/`): relocations, sections, imports, resources, segments, depending on the format.
  Long tables are split into files of 1,000 lines.

Paths in these files are relative to this folder.
"""

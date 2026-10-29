"""A program and its supporting DLLs parsed into one base folder, side by side:

    _Caster/                       the base folder ("_" + the program's name without extension)
      PARSED.md                    what's here, and how the files use each other (read this first)
      parsed.json                  the same, machine-readable (TaskDeck's marker for this folder)
      cross-reference/imports-NNN.txt   each import of a file here resolved to the DLL export here
      Caster.exe/                  one folder per file, as described in layout.py
      FOO.DLL/

The cross-reference matches every import (by name or ordinal) of every file in the set against the exports
of the DLLs in the set, giving the export's address and the chunk file holding its code. Imports from DLLs
that aren't in the set (Windows' own, usually) are counted in PARSED.md.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import layout
from .formats import Exe, h

GENERATOR = "TaskDeck binary set 1"
INDEX = "parsed.json"
OVERVIEW = "PARSED.md"
XREF = "cross-reference"


@dataclass
class Member:
    """One parsed file of the set."""

    path: Path
    exe: Exe
    written: layout.Written
    _by_name: dict = field(default_factory=dict, repr=False)
    _by_ordinal: dict = field(default_factory=dict, repr=False)
    _chunks: dict | None = field(default=None, repr=False)

    @property
    def folder(self) -> str:
        return self.path.name

    def export(self, name: str, ordinal: int | None):
        """The export an import names (by name, or by ordinal when it has no name), or None."""
        if not self._by_ordinal:
            for e in self.exe.exports:
                self._by_ordinal.setdefault(e.ordinal, e)
                if e.name:
                    self._by_name.setdefault(e.name, e)
        return self._by_name.get(name) if name else self._by_ordinal.get(ordinal)

    def chunk(self, ordinal: int) -> str | None:
        """The chunk file holding an export's code, as written in its exports.json."""
        if self._chunks is None:
            try:
                rows = json.loads((self.written.root / "exports" / "exports.json").read_text(encoding="utf-8"))["exports"]
            except (OSError, ValueError, KeyError):
                rows = []
            self._chunks = {r["ordinal"]: r.get("chunk") for r in rows}
        return self._chunks.get(ordinal)


def owned(base: Path) -> bool:
    """The base folder has no index yet, or ours."""
    index = base / INDEX
    if not index.exists():
        return True
    try:
        return json.loads(index.read_text(encoding="utf-8")).get("generator", "").startswith("TaskDeck binary set")
    except (OSError, ValueError, AttributeError):
        return False


def check(base: Path) -> None:
    if not owned(base):
        raise layout.NotOurs(f"{base / INDEX} exists and wasn't made by TaskDeck. Choose another folder.")


def remove_stale(base: Path, keep: set[str]) -> list[str]:
    """Folders an earlier run of this set wrote and this one didn't (a DLL left out this time): removed,
    so the agent never reads results of a DLL that's no longer part of the set. Only folders TaskDeck
    wrote are touched."""
    try:
        before = json.loads((base / INDEX).read_text(encoding="utf-8")).get("files", [])
    except (OSError, ValueError, AttributeError):
        return []
    gone = []
    for entry in before:
        name = str(entry.get("folder", "")).rstrip("/")
        folder = base / name
        if name and name not in keep and "/" not in name and folder.is_dir() and layout.owned_by_us(folder):
            shutil.rmtree(folder)
            gone.append(name)
    return gone


def finish(base: Path, members: list[Member]) -> dict:
    """Write PARSED.md, parsed.json and the cross-reference. Returns what the summary shows."""
    dlls = {m.path.name.lower(): m for m in members if m.exe.dll or m is not members[0]}
    for m in list(dlls.values()):  # a DLL may be imported under the name in its export directory
        own = str(m.exe.facts.get("export_dll_name") or m.exe.facts.get("module_name") or "").lower()
        if own:
            dlls.setdefault(own, m)
            dlls.setdefault(own + ".dll", m)
    lines = ["# importer  slot (IAT RVA)  DLL!function  ->  export in this folder: ordinal, file offset, chunk file"]
    resolved = missing = 0
    elsewhere: dict[str, int] = {}  # imports from DLLs not in the set
    for m in members:
        for imp in m.exe.imports:
            target = dlls.get(imp.dll.lower())
            what = imp.name or f"ordinal {imp.ordinal}"
            if target is None or target is m:
                elsewhere[imp.dll] = elsewhere.get(imp.dll, 0) + 1
                continue
            export = target.export(imp.name, imp.ordinal)
            head = f"{m.folder}  {h(imp.slot, 8)}  {imp.dll}!{what}  ->  "
            if export is None:
                missing += 1
                lines.append(head + f"NOT EXPORTED by {target.folder}")
                continue
            resolved += 1
            chunk = target.chunk(export.ordinal)
            if export.forwarder:
                where = f"forwarded to {export.forwarder}"
            elif export.offset is not None:
                where = f"file {h(export.offset, 8)}"
            else:
                where = export.address
            lines.append(head + f"{target.folder} ordinal {export.ordinal}"
                         + (f" ({export.readable})" if export.readable else "")
                         + f", {where}" + (f", {target.folder}/{chunk}" if chunk else ""))
    shutil.rmtree(base / XREF, ignore_errors=True)  # this set's own folder: rewritten every time
    xref_files = layout.paged(base, f"{XREF}/imports", lines, f"{base.name} · imports resolved to the DLLs here") \
        if resolved or missing else []
    summary = {"resolved": resolved, "not_exported": missing, "elsewhere": elsewhere}
    (base / INDEX).write_text(json.dumps({
        "generator": GENERATOR, "base": base.name,
        "files": [{"file": m.path.name, "folder": m.folder + "/", "role": "program" if i == 0 and not m.exe.dll else "DLL",
                   "format": m.exe.format, "dll": m.exe.dll, "imports": len(m.exe.imports),
                   "exports": len(m.exe.exports)} for i, m in enumerate(members)],
        "cross_reference": [f["file"] for f in xref_files], **summary}, indent=1) + "\n", encoding="utf-8")
    (base / OVERVIEW).write_text(_overview(base, members, summary, xref_files), encoding="utf-8", newline="\n")
    return summary


def _overview(base: Path, members: list[Member], summary: dict, xref_files: list[dict]) -> str:
    out = [f"# {base.name}", "", f"{len(members)} file{'s' * (len(members) != 1)} parsed side by side, one folder each. "
           "Each folder has its own README.md, SUMMARY.md (layout, entry point, exports, imports), metadata.json "
           "and index.json; start with the SUMMARY.md of the file you're working on.", "",
           "| Folder | Format | Imports | Exports |", "|---|---|---|---|"]
    out += [f"| `{m.folder}/` | {m.exe.description or m.exe.format} | {len(m.exe.imports):,} | {len(m.exe.exports):,} |"
            for m in members]
    out.append("")
    if xref_files:
        out += ["## How they use each other", "",
                f"{summary['resolved']:,} imports resolved to an export of a DLL here"
                + (f"; {summary['not_exported']:,} name a DLL here that doesn't export them" if summary["not_exported"] else "")
                + ". Each with the export's ordinal, file offset and chunk file: "
                f"`{XREF}/imports-*.txt` (one per line, for grep).", ""]
    if summary["elsewhere"]:
        out += ["## Imports from DLLs not in this folder", ""]
        out += [f"- {dll}: {n:,}" for dll, n in sorted(summary["elsewhere"].items(), key=lambda kv: -kv[1])]
        out.append("")
    return "\n".join(out) + "\n"

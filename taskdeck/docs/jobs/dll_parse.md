# Parse DLL

The same as [Parse executable](exe_parse.md), for libraries: `.dll`, `.drv`, `.ocx`, `.cpl`, `.sys`, `.vxd`,
`.386`. Same inputs, same folder (`_KERNEL32.DLL/` next to the file, and the same name in Drive), same Drive
push. Both jobs share one settings set (the Google Drive folder), so a program and its DLLs end up side by
side. Code: `src/taskdeck/jobs/dll_parse/` on top of `src/taskdeck/shared/binaries/`. Tests:
`tests/test_dll_parse.py`.

## How a DLL differs from an EXE

* **Same file format.** A Windows DLL is a PE file (or NE for 16-bit Windows) like a program; one header
  flag marks it as a library (PE `IMAGE_FILE_DLL`, NE "library module"). So the parser, layout and chunks
  are the same.
* **Exports are the point.** A program mostly *imports*; a DLL exists to *export* functions (and data) for
  other modules, by **name** or by **ordinal** (a number). The exports table is the DLL's public interface
  and the map an agent needs to connect a program's imports to the code that implements them.
* **Forwarded exports.** A PE export can point to another DLL's function instead of code
  ("`KERNEL32.HeapAlloc`"); there's nothing in this file to read for it.
* **Entry point is optional.** `DllMain` (PE) or `LibMain` (NE) runs when the DLL loads; it may be missing.
* **Rebased.** A DLL is often loaded at another address than its preferred image base, so its base
  relocation table matters more than a program's (not decoded yet, see below).

## The exports output

For every export, in both jobs (programs can export too):

```
exports/exports-001.txt     one per line:  ordinal  file offset  address  name  chunk file
exports/exports.json        the same as JSON (plus the RVA, the forwarder, NE resident/non-resident)
SUMMARY.md › Exports        count (by name / by ordinal only / forwarded) and the first 40
```

* **PE**: the export directory: ordinal base, names (several names for one ordinal are listed separately),
  ordinal-only entries "(by ordinal only)", forwarders "forwarded to DLL.Function", RVA and VA, file offset,
  and the chunk file holding the code. Checked against `pefile` on real 32- and 64-bit DLLs.
* **NE (16-bit)**: the entry table gives each ordinal's `segment:offset` (fixed and movable entries;
  constants shown as "constant 1234"); names come from the resident and non-resident name tables. The
  module name and description (first entries of those tables) are in the facts.

Parse DLL warns when the file isn't marked as a DLL, or exports nothing; Parse executable warns when it's
given a DLL. Both still parse it.

## Open questions

* **What does the agent do with the exports?** If it matches a program's imports to DLLs, a cross-reference
  (for each import in `_GAME.EXE`, the export and chunk in `_FOO.DLL` that implements it) could be written
  when both are parsed into the same folder.
* **32-bit or 16-bit DLLs?** Both are decoded; NE has only been tested on hand-built files. A real 16-bit
  DLL from the project would be the best test.
* **C++ names.** MSVC and Borland exports of C++ functions are *mangled* (`?Draw@Window@@QAEXH@Z`). Should
  the list add the readable form (`Window::Draw(int)`)? Needs a demangler (MSVC and Borland differ).
* **Base relocations** (`.reloc`): decoding them would show every place that holds an absolute address.
  Useful for a DLL; not done yet.
* **Imports by ordinal**: a program importing "ordinal 12" from a DLL is resolved only with that DLL's exports;
  the cross-reference above would also name those.

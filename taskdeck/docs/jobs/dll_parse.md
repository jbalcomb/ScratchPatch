# Parse DLL

The same as [Parse executable](exe_parse.md), for libraries: `.dll`, `.drv`, `.ocx`, `.cpl`, `.sys`, `.vxd`,
`.386`. Same inputs (without supporting DLLs), same base folder (`_KERNEL32/` next to the file, holding
`KERNEL32.DLL/`; the same name in Drive), same Drive push. Both jobs share one settings set (the Google Drive folder), so a program and its DLLs end up side by
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
  relocation table matters more than a program's (decoded, see below).

## The exports output

For every export, in both jobs (programs can export too):

```
exports/exports-001.txt     one per line:  ordinal  file offset  address  name  chunk file
exports/exports.json        the same as JSON (plus the RVA, the forwarder, the C++ name, NE resident/non-resident)
SUMMARY.md › Exports        count (by name / by ordinal only / forwarded) and the first 40
```

* **PE**: the export directory: ordinal base, names (several names for one ordinal are listed separately),
  ordinal-only entries "(by ordinal only)", forwarders "forwarded to DLL.Function", RVA and VA, file offset,
  and the chunk file holding the code. Checked against `pefile` on real 32- and 64-bit DLLs.
* **NE (16-bit)**: the entry table gives each ordinal's `segment:offset` (fixed and movable entries;
  constants shown as "constant 1234"); names come from the resident and non-resident name tables. The
  module name and description (first entries of those tables) are in the facts.

**C++ names**: exports with a mangled C++ name (Microsoft `?Draw@Window@@QAEXH@Z`, Borland
`@Window@Draw$qi`) also get their qualified name, `[Window::Draw]`, including constructors, destructors and
operators. Parameter and return types aren't decoded yet; templates and anonymous namespaces get no
readable name rather than a guess (`shared/binaries/names.py`).

## Base relocations

`tables/base-relocations-NNN.txt` (both jobs, any PE file that has them): every place in the file holding
an absolute address, which the loader patches when the module isn't loaded at its preferred image base.
One per line: RVA, file offset, type (HIGHLOW for 32-bit, DIR64 for 64-bit), the address stored there and
what it points to (section and RVA). Counts are in the summary and `metadata.json`. Checked against
`pefile` on real 32- and 64-bit DLLs.

Parse DLL warns when the file isn't marked as a DLL, or exports nothing; Parse executable warns when it's
given a DLL. Both still parse it.

## Decided

* **Done**: Parse executable takes supporting DLLs, parsed side by side in one base folder with an import →
  export cross-reference ([exe_parse.md](exe_parse.md)).

* 32-bit DLLs first: that's the example at hand. 16-bit (NE) exports are decoded but only tested on
  hand-built files.

## Future

* **16-bit cross-reference**: Parse executable's supporting DLLs match PE imports only; NE programs import
  through segment relocation records, not decoded yet.
* **16-bit DLLs**: test with a real one; decode NE segment relocations (the 16-bit equivalent of base
  relocations, stored after each segment's data).
* **Full C++ names**: parameter and return types (`Window::Draw(int)`), templates; Microsoft and Borland
  schemes differ.

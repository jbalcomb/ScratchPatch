# Parse executable

Splits an executable into small, labelled text files so an AI agent doing reverse engineering can read or
search it one piece at a time, instead of loading a whole binary into its context. Optionally mirrors the
result to Google Drive, where the agent (or a person) can pick it up.

Code: `src/taskdeck/shared/binaries/` (`formats.py` reads headers, `layout.py` writes the folder, `drive.py`
mirrors it, `job.py` the fields and steps), shared with [Parse DLL](dll_parse.md); the job itself is
`src/taskdeck/jobs/exe_parse/`. Tests: `tests/test_exe_parse.py`.

## Inputs

| Field | |
|---|---|
| Executable | `.exe`, `.com`, `.ovl`, `.scr` (DLLs: [Parse DLL](dll_parse.md), or as supporting DLLs below), up to 64 MB. **Browse…** opens TaskDeck's own file browser (real paths; Android's picker would hand back a copy in the app's cache). |
| Supporting DLLs | Optional. One per line; **Add…** opens the file browser in the executable's folder. Each is parsed into its own folder next to the executable's, and the executable's imports are matched to their exports. The note under the field lists the DLLs the executable imports, and which of them are next to it. |
| Save into (folder) | The **base folder** for all results. Default: `_` + the executable's name without extension, next to it (`Caster.exe` → `_Caster`). A relative folder is next to the executable too. The note under the field shows the full path. |
| Chunk size | Bytes per hex-dump file: 1, 2 (default), 4, 8 or 16 KB. 2 KB is 128 lines, about 4,000-5,000 tokens. |
| Shortest string | Minimum run of printable characters listed as a string (default 4). |
| Push to Google Drive | Mirror the base folder to *Google Drive folder* (job setting, default `TaskDeck/Executables`). The full Drive path is shown under the switch. Needs Google Drive connected (Settings → Accounts). |

## Output

A base folder (same name on the phone and in Drive) with one folder per parsed file, named exactly like the
file, and the set's overview:

```
_Caster/
  PARSED.md                 what's here and how the files use each other (start here)
  parsed.json               the same, machine-readable
  cross-reference/imports-NNN.txt   each import resolved to a supporting DLL's export: ordinal, file
                            offset, chunk file (or NOT EXPORTED); imports from other DLLs are counted
  Caster.exe/               the executable's results, as below
  FOO.DLL/                  each supporting DLL's, the same way (exports, base relocations...)

Caster.exe/
  README.md                 how to walk and search the folder (written for the agent)
  SUMMARY.md                summary and layout: format, hashes, entry point and the chunk holding it,
                            every region with its byte range, imports/resources, warnings
  metadata.json             hashes, size, dates, format, every decoded header field, facts, regions
  index.json                every region and every chunk/table/string file with its byte or line range
  headers/headers.txt       all decoded header fields with their meaning
  tables/<name>-NNN.txt     relocations (MZ), sections, imports, exports, resources (PE), segments (NE);
                            1,000 lines per file
  regions/NN-<region>/<first>-<last>.txt   hex dump, one file per chunk, named by file offsets
  strings/ascii-NNN.txt, strings/utf16-NNN.txt   strings with file offset, region and address
```

* **Regions** cover every byte exactly once, in file order: headers, relocation table, the DOS load module
  or each PE section / NE segment, the overlay, and anything no header describes ("unclaimed").
* Each chunk file starts with the region, its byte range (file offsets plus the region's own address: RVA
  for PE, offset in the load image for DOS) and the **previous and next chunk**, so the agent can walk the
  file without the index.
* Output doesn't depend on when it was made: parsing again gives identical files, and pushing again only
  uploads what changed.
* An existing output folder is replaced only if this job made it (it has our `index.json`).

## What's decoded

| Format | Decoded |
|---|---|
| DOS MZ | Header fields, relocation table (seg:off, image and file offsets), load module, entry CS:IP, overlay (recognises Borland FBOV, an appended MZ, zip/RAR/7z) |
| PE32 / PE32+ | COFF and optional headers, data directories, sections, imports, exports (see [Parse DLL](dll_parse.md)), base relocations, resource tree, entry point, overlay (notes an Authenticode signature) |
| NE (Win16, OS/2) | Header, segment table, module name, imported modules, entry point |
| LE / LX | Header, objects (each with its linear address; regions per object, address column = linear address), page map, entry point, fixups (`tables/fixups-*.txt`: every place holding an address and what it points to), entry table and names |
| Bound to a DOS extender | DOS/4G(W) and similar: the extender's loader, the extender itself (DOS/16M "BW"), and the program's own DOS stub are separate regions, and the LE program inside is decoded as above |
| Watcom debug info | See **Debug info** below |
| Anything else | Laid out as raw data |

## Debug info

Watcom debug info (version 3, at the end of the file; what `wlink debug all` writes) is decoded completely
(`shared/binaries/watcom.py`, from the Open Watcom headers and debugger sources) into `debug/` in the file's
folder:

```
debug/DEBUG.md                all of it in one markdown file (the agent can grep it):
                              modules (object files) with their code/data ranges, every global symbol by
                              address (file offset, chunk file), and per module: routines (address, size,
                              prologue/epilogue, prototype, return and parameter locations, local variables
                              with their stack or register location and type), module variables, types
                              (typedefs, pointers, arrays, enums with constants, structs/unions with fields
                              by offset, procedures) and line numbers (line -> address)
debug/symbols-NNN.txt         every global symbol, one per line, by address, with its chunk file
debug/modules/NNNN-<name>.md  one module's part of DEBUG.md
```

Every chunk file's header also names the symbols that start in it (`# symbols: Read_Key_ (0x...)`), the
summary names the entry point's symbol, and records the decoder doesn't know are kept as hex. Tested on
ORION2.EXE (Master of Orion II): 388 modules, 8,598 global symbols, 816 routines, 4,197 locals, 2,850 type
records, 15,271 line numbers, nothing undecoded; symbols checked against the code (function prologues).
Only modules compiled with full debug info (`-d2`) have locals, types and lines; others have their globals
and address ranges.

## Google Drive

A one-time copy (docs/JOB_KINDS.md): same name and content is skipped, a different file is uploaded fresh
and the old one trashed. Unlike Extract Archive, files and folders in Drive the local folder no longer has
(e.g. chunks from an earlier parse with another chunk size) are moved to Drive's trash too, so an agent never
reads a stale chunk. Uploads reuse Extract Archive's parallel uploader (`taskdeck/shared/drive_uploads.py`).

The upload runs as a **background task**, like Extract Archive's: the parse result shows right away, and
below it a bar for *Checking what's already in Google Drive* and one for *Uploading to Google Drive* (files
done, speed, time left), "Paused: no connection" while the network is gone, Cancel, the notification while
TaskDeck is in the background, and **Open in Drive** when it's done.

If the upload fails or is stopped, **Retry upload** sends the folder already on the phone again (no new
parse; what's already in Drive is skipped). If Google refused the saved sign-in (revoked, or expired after
7 days while the consent screen is in Testing), **Connect again** signs in again and then retries by itself. Run is disabled until it finishes. The
generic job screen offers this to any job (`Context.services["start_task"]`); the widgets are shared with
Extract Archive (`taskdeck/ui/task_view.py`).

## Decided

* **Chunk size stays at 2 KB** (the default) until there's data on how the job performs and how well the
  pieces work for the agent. Then revisit it, including the segment-first split below.
* **Disassembly is out of scope.**
* **The agent gets the Google Drive folder shared with it directly.** Parsing the same file again changes
  nothing it sees (identical files, skipped on upload); there's no reason to parse again anyway.

## Future

* Other debug formats: Microsoft CodeView (`NBxx` at the end of the file) and Borland Turbo Debugger, for DOS
  programs not built with Watcom.
* Watcom debug info version 2 (older tools): detected, not decoded.

* **DOS executables with overlays.** Today the overlay is one region (a Borland FBOV overlay is recognised,
  not decoded). Decode the overlay header and the overlay segments and split by them.
* **Master of Magic as the test target** (MAGIC.EXE and WIZARDS.EXE, both already fully disassembled by
  hand, much of their code in Borland overlays). Needed to start:
  1. The two executables, somewhere this job's tests can read them (a Google Drive folder, or the repo if
     that's acceptable).
  2. The segment list from the existing disassembly (start, code/data, name), to check the segments the
     job works out against it.
* **Split by logical units instead of fixed chunks.** DOS MZ headers don't list segments, but they can be
  worked out from the relocation table (the segment values at the fixup sites), the initial CS and SS, and
  far calls/jumps (code) vs. SS/DGROUP (data); then show `seg:off` addresses. NE segments and PE sections
  are already regions; 64-bit PE `.pdata` lists function ranges. Object-file (.OBJ) boundaries only come
  back from a linker .MAP or debug symbols. A segment can be 64 KB, so keep a size cap for long ones.
* **Raw .bin chunks** next to the hex dumps.
* **Annotations going back**: should the agent's findings (function and data names) be stored next to the
  chunks and kept when the executable is parsed again?

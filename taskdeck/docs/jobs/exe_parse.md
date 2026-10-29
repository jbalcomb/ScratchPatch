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
| LE / LX | Detected; the image is one region (not decoded further) |
| Anything else | Laid out as raw data |

## Google Drive

A one-time copy (docs/JOB_KINDS.md): same name and content is skipped, a different file is uploaded fresh
and the old one trashed. Unlike Extract Archive, files and folders in Drive the local folder no longer has
(e.g. chunks from an earlier parse with another chunk size) are moved to Drive's trash too, so an agent never
reads a stale chunk. Uploads reuse Extract Archive's parallel uploader (`taskdeck/shared/drive_uploads.py`).

The upload runs as a **background task**, like Extract Archive's: the parse result shows right away, and
below it a bar for *Checking what's already in Google Drive* and one for *Uploading to Google Drive* (files
done, speed, time left), "Paused: no connection" while the network is gone, Cancel, the notification while
TaskDeck is in the background, and **Open in Drive** when it's done. Run is disabled until it finishes. The
generic job screen offers this to any job (`Context.services["start_task"]`); the widgets are shared with
Extract Archive (`taskdeck/ui/task_view.py`).

## Decided

* **Chunk size stays at 2 KB** (the default) until there's data on how the job performs and how well the
  pieces work for the agent. Then revisit it, including the segment-first split below.
* **Disassembly is out of scope.**
* **The agent gets the Google Drive folder shared with it directly.** Parsing the same file again changes
  nothing it sees (identical files, skipped on upload); there's no reason to parse again anyway.

## Future

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

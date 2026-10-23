# PawNTFS — a clean-room, NTFS-compatible file system driver for Windows

*Working name; easy to change before anything ships.*

## Goal

A Windows file system driver, written in C, that reads and writes **genuine NTFS volumes** — byte-for-byte
the same on-disk format Windows itself produces — and behaves the way applications expect NTFS to behave.
It **installs next to Microsoft's `ntfs.sys`** rather than replacing it: both drivers are present, and
PawNTFS mounts only the volumes it is told to.

Because the format is NTFS itself, a volume can move freely between PawNTFS and `ntfs.sys`, Windows tools
(`chkdsk`, `fsutil`, Explorer, backup software) keep working on it, and no custom bootloader is ever needed.

## Decisions so far

| Question | Decision |
|---|---|
| Replace `ntfs.sys` or sit beside it? | **Beside it.** PawNTFS registers as its own file system and claims only opted-in volumes. |
| `$LogFile` (journal) fidelity | **Two steps.** First milestone: write, then leave `$LogFile` in the "reset" state Windows accepts (what ntfs-3g does; not crash-safe in NTFS's way). Final goal: full journaling in Windows' own `$LogFile` format, so `ntfs.sys` and `chkdsk` can replay our log after a crash and vice versa. |
| What sources may we use? | **Documentation only** — see [Clean-room rules](#clean-room-rules). |
| Order of work | The roadmap below. |

## Roadmap

Each milestone has an exit test; we don't start the next one until it passes.

| # | Milestone | Exit test |
|---|---|---|
| 0 | **Format spec** — [`docs/NTFS_FORMAT.md`](docs/NTFS_FORMAT.md) | Every structure the read-only parser needs is written down, with each claim marked verified or not. |
| 1 | **Portable read-only core** (C11, no OS headers): boot sector, fixups, MFT records, attributes, runlists, attribute lists, directory B+trees, `$UpCase` collation, resident/non-resident/sparse/compressed (LZNT1) data, hard links, ADS, reparse points, security descriptors via `$Secure`. Plus a `ntfsdump`-style CLI. | Lists and reads every file on images produced by `mkntfs` **and by Windows**, matching a manifest generated on the source side. Fuzzed against corrupted images without crashing. |
| 2 | **Write support, "reset log" mode**: allocation (`$Bitmap`, `$MFT:$BITMAP`, MFT zone), create/delete/rename, index insert/delete with B+tree splits/merges, extend/truncate, hard links, ADS, `$Secure` dedup, `$UsnJrnl` records, `$STANDARD_INFORMATION` timestamps, `$Volume` dirty flag. | Windows `chkdsk /scan` reports **no errors** on images after randomized workloads; files written by us read back identically under Windows, and vice versa. |
| 3 | **Full `$LogFile` journaling** in Windows' format (LFS restart/record pages, NTFS redo/undo operations, checkpoints with open-attribute/dirty-page/transaction tables). | Crash-injection: cut power at every write of a workload, then (a) our recovery and (b) Windows' recovery each yield a volume `chkdsk` accepts, with every committed operation present and every uncommitted one absent. |
| 4 | **Windows kernel driver** (WDK, `ntifs.h`): mount/dismount, IRP dispatch, Cache Manager integration, oplocks, byte-range locks, FSCTLs, filter-manager compatibility. Starts in test-signing mode on a VM. | Explorer, `robocopy`, `fsutil`, Defender and the Windows file system test suites run against a PawNTFS-mounted volume. |
| 5 | Hardening, performance, signing, installer. | — |

Milestones 0–3 are developed and tested on Linux, in portable C, against disk images. Milestone 4 needs a
Windows machine (or VM) with Visual Studio and the WDK.

## How "install next to `ntfs.sys`" works (to be confirmed in milestone 4)

When Windows mounts a volume it offers it to each registered disk file system in turn until one accepts.
A file system driver that registers after `ntfs.sys` is asked first, so PawNTFS can accept the volumes it
has been told to own (by volume GUID or serial number, from its registry configuration) and return
`STATUS_UNRECOGNIZED_VOLUME` for everything else, leaving those to `ntfs.sys`. The system volume stays
with `ntfs.sys` until PawNTFS has earned trust; the exact registration-order behavior is an assumption we
will verify on a real system before relying on it.

## Clean-room rules

1. **Allowed:** published specifications and documentation — Microsoft's open specifications ([MS-FSCC],
   [MS-XCA], [MS-DTYP]), Microsoft Learn's driver documentation, WDK headers, the Windows-driver-samples
   repository (FastFAT, CDFS, minifilter samples), independent format write-ups (Joachim Metz's
   libfsntfs documentation, the Linux-NTFS project's NTFS documentation), books and papers.
2. **Allowed:** *running* other tools — Windows itself, `chkdsk`, `fsutil`, ntfs-3g's `mkntfs`/`ntfsinfo`
   — and inspecting the bytes they produce. Observed behavior is fair game.
3. **Not allowed:** reading the source code of other NTFS implementations (ntfs-3g, Linux ntfs3/ntfs,
   ReactOS, Paragon, etc.), and anything derived from leaked Microsoft source. If you have read such code,
   note it before working on the related area.
4. **Every non-obvious fact in the spec cites its source** — a document, or "observed" with how it was
   observed. Facts not yet confirmed are marked ⚠.

## Layout

```
fs/
  README.md            this file
  docs/NTFS_FORMAT.md  the on-disk format spec (milestone 0)
```

[MS-FSCC]: https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/
[MS-XCA]: https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-xca/
[MS-DTYP]: https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/

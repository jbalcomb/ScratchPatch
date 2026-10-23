# NTFS on-disk format — working specification

This is the reference PawNTFS is built from. It is written from documentation and from inspecting real
volumes, never from other implementations' source code (see the clean-room rules in `../README.md`).

**Confidence markers** — every structure or rule carries one:

| Mark | Meaning |
|---|---|
| **[obs]** | Observed in bytes on a real image. Unless noted, the image came from `mkntfs` (ntfs-3g 2022.10.3), 64 MiB, 4 KiB clusters, 512-byte sectors, made 2026-10-23. **`mkntfs` output is not Windows output** — anything that must match Windows exactly needs a second observation on a Windows-formatted image. |
| **[doc]** | Stated in a Microsoft open specification or Microsoft Learn page (named inline). |
| **[ref]** | From independent format documentation (libfsntfs docs, Linux-NTFS docs). Believed right; not yet observed by us. |
| **⚠** | Unverified or from memory. Must be confirmed before code depends on it. |

Target: **NTFS 3.1**, the version every Windows from XP to 11 creates. [obs: `$VOLUME_INFORMATION` = 3.1]

---

## 1. Conventions

- All integers are **little-endian**. [obs]
- Timestamps are **FILETIME**: unsigned 64-bit count of 100 ns intervals since 1601-01-01 UTC. [doc: MS-DTYP 2.3.3]
- Names are **UTF-16LE**, counted (not NUL-terminated), length in 16-bit units. [obs]
- **Cluster** = allocation unit. **LCN** = logical cluster number (from the start of the volume).
  **VCN** = virtual cluster number (from the start of an attribute's data).
- **File reference** (64 bits) = MFT record number in the low 48 bits, record **sequence number** in the
  high 16. A reference whose sequence number doesn't match the record's current one is stale. [obs: root's
  parent reference is `0x0005000000000005` = record 5, sequence 5]

---

## 2. Volume layout and boot sector

The volume starts with `$Boot` (the boot sector plus loader code, 8 KiB on our image). A **backup boot
sector** occupies the volume's last sector, which lies *outside* the sector count the boot sector
advertises. [obs: total sectors = 131071 on a 131072-sector volume; `NTFS    ` found at sector 131071]

### Boot sector (sector 0) [obs unless marked]

| Off | Size | Field | Our image |
|---|---|---|---|
| 0x00 | 3 | Jump instruction | `EB 52 90` |
| 0x03 | 8 | OEM ID — **`"NTFS    "`** (how Windows recognizes NTFS) | |
| 0x0B | 2 | Bytes per sector | 512 |
| 0x0D | 1 | Sectors per cluster. ⚠ Values > 0x80 encode 2^(256−value) for clusters > 64 KiB (Windows 10 1709+) | 8 |
| 0x0E | 2 | Reserved sectors — must be 0 | |
| 0x10 | 5 | Always 0 (FAT fields) | |
| 0x15 | 1 | Media descriptor | `F8` |
| 0x16 | 2 | Always 0 | |
| 0x18 | 2 | Sectors per track (geometry; needed to boot) | |
| 0x1A | 2 | Number of heads (geometry; needed to boot) | |
| 0x1C | 4 | Hidden sectors = partition start (needed to boot) | |
| 0x20 | 4 | Always 0 | |
| 0x24 | 4 | ⚠ Drive number / flags; usually `0x00800080` | `0x00800080` |
| 0x28 | 8 | Total sectors (volume size − 1 sector) | 131071 |
| 0x30 | 8 | LCN of `$MFT` | 4 |
| 0x38 | 8 | LCN of `$MFTMirr` | 8191 |
| 0x40 | 1 | **Clusters per file record**, signed: if negative, the size is 2^(−value) bytes | `F6` → 1024 |
| 0x41 | 3 | Unused | |
| 0x44 | 1 | **Clusters per index block**, same encoding | `01` → 4096 |
| 0x45 | 3 | Unused | |
| 0x48 | 8 | Volume serial number | |
| 0x50 | 4 | Checksum (unused, 0) ⚠ | |
| 0x54 | 426 | Boot code | |
| 0x1FE | 2 | Signature `55 AA` | |

Record size is 1024 bytes and index block size is 4096 bytes on every volume we expect to meet, but
code must use the boot sector's values. ⚠ Windows on 4Kn (4096-byte sector) disks uses 4096-byte file
records.

---

## 3. Multi-sector fixups ("update sequence") [obs]

MFT records (`FILE`), index blocks (`INDX`) and log pages (`RSTR`, `RCRD`) span several sectors. To detect
a torn write, NTFS overwrites the **last 2 bytes of every 512-byte stride** with an *update sequence
number* (USN — unrelated to the change journal's USN) and saves the real bytes in an array in the header.

Header fields (same position in every multi-sector structure):

| Off | Size | Field |
|---|---|---|
| 0x00 | 4 | Magic: `FILE`, `INDX`, `RSTR`, `RCRD` (`BAAD` = marked bad by chkdsk) |
| 0x04 | 2 | Offset of the update sequence array (USA) |
| 0x06 | 2 | USA count = 1 + number of 512-byte strides |

USA[0] is the current USN; USA[i] holds the original last two bytes of stride *i*.

- **Read:** for each stride, its last 2 bytes must equal USA[0], else the structure is torn/corrupt; then
  put USA[i] back. [obs: verified on every system record of our image]
- **Write:** increment USA[0] (⚠ skipping 0 and 0xFFFF), save each stride's last 2 bytes into USA[i],
  stamp USA[0] over them.
- Strides are 512 bytes regardless of the device's sector size. ⚠ Confirm on a 4Kn volume.

Observed: `FILE` records have USA at 0x30 with count 3 (1024 bytes); `INDX` blocks at 0x28 with count 9
(4096 bytes).

---

## 4. The MFT and file records

`$MFT` is a file whose `$DATA` is an array of fixed-size file records; record *n* lives at byte
*n × record size* of that data. `$MFT` describes itself (record 0), so the first extent must be readable
from the boot sector's `$MFT` LCN alone. ⚠ Find out how many records Windows requires inside that first
extent.

### File record header [obs]

| Off | Size | Field | Notes |
|---|---|---|---|
| 0x00 | 4 | `FILE` | |
| 0x04 | 2 | USA offset | 0x30 |
| 0x06 | 2 | USA count | 3 |
| 0x08 | 8 | `$LogFile` sequence number (LSN) of the last change | |
| 0x10 | 2 | Sequence number | Incremented when the record is freed for reuse |
| 0x12 | 2 | Hard link count | Counts `$FILE_NAME`s in the Win32/POSIX namespaces ⚠ (a Win32+DOS pair counts once) |
| 0x14 | 2 | Offset of the first attribute | 0x38 |
| 0x16 | 2 | Flags | 0x01 in use · 0x02 directory (has `$I30`) · 0x04 ⚠ "in `$Extend`" · 0x08 has a view index (observed on `$Secure`: flags 0x09) |
| 0x18 | 4 | Bytes used | 8-byte aligned, includes the end marker |
| 0x1C | 4 | Bytes allocated | = record size |
| 0x20 | 8 | Base record reference | 0 for a base record; else the base this extension record belongs to |
| 0x28 | 2 | Next attribute ID | |
| 0x2A | 2 | Padding | |
| 0x2C | 4 | This record's number | NTFS 3.1 only |
| 0x30 | — | USA, then attributes from 0x38 | |

After the last attribute comes the end marker `FF FF FF FF` (then 4 more bytes, ⚠ contents).

### Reserved records

| # | Name | Contents |
|---|---|---|
| 0 | `$MFT` | `$DATA` = the MFT; `$BITMAP` = which records are in use [obs] |
| 1 | `$MFTMirr` | Copy of the first records of the MFT (4 on our image) [obs] |
| 2 | `$LogFile` | The journal (§10) [obs] |
| 3 | `$Volume` | `$VOLUME_NAME`, `$VOLUME_INFORMATION`, empty `$DATA`, `$SECURITY_DESCRIPTOR` [obs] |
| 4 | `$AttrDef` | Table of attribute definitions (§8) [obs: 2560 bytes = 16 entries] |
| 5 | `.` | Root directory [obs] |
| 6 | `$Bitmap` | Cluster allocation bitmap, 1 bit per cluster, 1 = used [obs] |
| 7 | `$Boot` | `$DATA` covers the boot sector and loader |
| 8 | `$BadClus` | Named `$DATA:$Bad`, sparse, as long as the volume; bad clusters are allocated in it [ref] |
| 9 | `$Secure` | `$DATA:$SDS`, indexes `$SDH`, `$SII` (§8) [obs] |
| 10 | `$UpCase` | 128 KiB uppercase table, plus `$DATA:$Info` (32 bytes) [obs] |
| 11 | `$Extend` | Directory of `$ObjId`, `$Quota`, `$Reparse`, `$UsnJrnl`, `$RmMetadata` … [obs: it is a directory] |
| 12–15 | — | Reserved, marked in use [ref] |
| 16–23 | — | Reserved for MFT extension records of system files [ref] ⚠ |
| 24+ | — | `$Extend` children, then user files |

Every system file is named in the root directory's index, with `$Extend` children in `$Extend`'s index.
[obs: all parent references point at record 5]

### Extension records and `$ATTRIBUTE_LIST`

When a file's attributes don't fit in one record, they spill into **extension records** (header base
reference ≠ 0) and the base record gains an `$ATTRIBUTE_LIST` (§6.2) listing where every attribute
lives. A huge `$DATA` can also be split across records by VCN range, each piece a separate non-resident
attribute with its own `start_vcn`/`last_vcn`; each piece's runlist starts its LCN deltas from zero.
[obs: 61 hard links spread `$FILE_NAME`s over 13 extension records; a 12,000-run sparse file split
`$DATA` over 35 records]

### `$MFT` growth and the MFT zone

`$MFT` grows by appending clusters; the volume reserves an **MFT zone** (default 12.5% ⚠) after the MFT
that ordinary files avoid until space runs out. [ref] The zone is an allocation policy, not stored state
⚠ — confirm it isn't stored anywhere.

---

## 5. Attributes

A record holds a sequence of attributes sorted by **type code**, then by **name**. [obs: `$SDH` sorts
before `$SII`] ⚠ Exact name order (binary vs upcased) to be confirmed. Each attribute is 8-byte aligned.

### 5.1 Common header [obs]

| Off | Size | Field |
|---|---|---|
| 0x00 | 4 | Type code (§6) |
| 0x04 | 4 | Total length of this attribute |
| 0x08 | 1 | Non-resident flag |
| 0x09 | 1 | Name length (UTF-16 units) — 0 for the unnamed stream |
| 0x0A | 2 | Name offset |
| 0x0C | 2 | Flags: 0x0001 compressed (0x00FF = compression method mask ⚠), 0x4000 encrypted, 0x8000 sparse |
| 0x0E | 2 | Attribute ID (unique within the record) |

### 5.2 Resident form [obs]

| Off | Size | Field |
|---|---|---|
| 0x10 | 4 | Value length |
| 0x14 | 2 | Value offset |
| 0x16 | 1 | Indexed flag (1 on `$FILE_NAME`) |
| 0x17 | 1 | Padding |
| 0x18 | — | Name (if any), then value 8-aligned. Observed: unnamed → value at 0x18; `$I30`-named → value at 0x20 |

### 5.3 Non-resident form [obs]

| Off | Size | Field |
|---|---|---|
| 0x10 | 8 | Starting VCN |
| 0x18 | 8 | Last VCN |
| 0x20 | 2 | Runlist ("mapping pairs") offset — 0x40 unnamed, 0x48 with a 4-char name |
| 0x22 | 2 | Compression unit, log₂ clusters (0 = not compressed; 4 = 16 clusters) |
| 0x24 | 4 | Padding |
| 0x28 | 8 | Allocated size (bytes, whole clusters) |
| 0x30 | 8 | Data size (logical file length) |
| 0x38 | 8 | Initialized size — bytes past this read as zero without touching disk |
| 0x40 | 8 | Compressed size — present only when compressed or sparse [ref] ⚠ (header grows to 0x48) |

Sizes are only meaningful in the piece with starting VCN 0. [ref]

### 5.4 Runlists [obs]

A runlist is a sequence of runs ended by a `00` byte. Each run:

```
header byte: low nibble = L (bytes of length), high nibble = F (bytes of offset)
L bytes: run length in clusters, unsigned
F bytes: LCN delta from the previous run's LCN, signed; F = 0 means a sparse run (no clusters)
```

Observed: `$MFT` `$DATA` = `11 07 04` → 7 clusters at LCN 4. Root `$I30` allocation = `21 01 05 08` → 1
cluster at LCN 0x805.

Writers should encode each number in the fewest bytes that still keep the correct sign. ⚠ Confirm Windows
does so byte-for-byte (matters for matching `chkdsk`'s view, not for reading).

---

## 6. Attribute types

| Code | Name | Resident? | Notes |
|---|---|---|---|
| 0x10 | `$STANDARD_INFORMATION` | always | §6.1 |
| 0x20 | `$ATTRIBUTE_LIST` | either | §6.2 |
| 0x30 | `$FILE_NAME` | always | §6.3; one per hard link and per DOS name |
| 0x40 | `$OBJECT_ID` | always | 16-byte GUID, optionally + birth volume/object/domain IDs (64 bytes) [ref] |
| 0x50 | `$SECURITY_DESCRIPTOR` | either | Only on files from NTFS 1.x and some system files; NTFS 3.x uses `$Secure` [obs: present on `$Volume`, root] |
| 0x60 | `$VOLUME_NAME` | always | The label, UTF-16 [obs] |
| 0x70 | `$VOLUME_INFORMATION` | always | §6.4 |
| 0x80 | `$DATA` | either | Unnamed = the file's contents; named = alternate data streams |
| 0x90 | `$INDEX_ROOT` | always | §7 |
| 0xA0 | `$INDEX_ALLOCATION` | never | §7 |
| 0xB0 | `$BITMAP` | either | Index block usage, or `$MFT` record usage |
| 0xC0 | `$REPARSE_POINT` | either | §6.5 |
| 0xD0 | `$EA_INFORMATION` | always | Sizes of the EA set [ref] |
| 0xE0 | `$EA` | either | Extended attributes (OS/2 style; used by WSL for Linux metadata) [ref] |
| 0x100 | `$LOGGED_UTILITY_STREAM` | either | `$EFS` (encryption), `$TXF_DATA` (TxF) [ref] |

The authoritative list for a volume is its own `$AttrDef` (§8).

### 6.1 `$STANDARD_INFORMATION` [obs]

| Off | Size | Field |
|---|---|---|
| 0x00 | 8 | Creation time |
| 0x08 | 8 | Last write time |
| 0x10 | 8 | MFT record change time |
| 0x18 | 8 | Last access time |
| 0x20 | 4 | File attributes (`FILE_ATTRIBUTE_*`, [doc: MS-FSCC 2.6]) |
| 0x24 | 4 | Max versions ⚠ |
| 0x28 | 4 | Version number ⚠ |
| 0x2C | 4 | Class ID ⚠ |
| 0x30 | 4 | Owner ID (quota) — NTFS 3.x |
| 0x34 | 4 | Security ID (key into `$Secure`) — NTFS 3.x |
| 0x38 | 8 | Quota charged — NTFS 3.x |
| 0x40 | 8 | Update sequence number (last `$UsnJrnl` record) — NTFS 3.x |

**Size is 72 bytes in the NTFS 3.x form and 48 in the old form.** Our `mkntfs` image has *both* (72 on
`$MFT`, `$UpCase`; 48 on `$Volume`, root). Readers must accept both; writers must write 72. [obs]

### 6.2 `$ATTRIBUTE_LIST` entry [ref]

| Off | Size | Field |
|---|---|---|
| 0x00 | 4 | Type |
| 0x04 | 2 | Entry length |
| 0x06 | 1 | Name length |
| 0x07 | 1 | Name offset |
| 0x08 | 8 | Starting VCN |
| 0x10 | 8 | Reference of the record holding the attribute |
| 0x18 | 2 | Attribute ID |
| 0x1A | — | Name, entry 8-aligned |

### 6.3 `$FILE_NAME` [obs]

| Off | Size | Field |
|---|---|---|
| 0x00 | 8 | Parent directory reference |
| 0x08 | 8 | Creation time |
| 0x10 | 8 | Last write time |
| 0x18 | 8 | MFT change time |
| 0x20 | 8 | Last access time |
| 0x28 | 8 | Allocated size |
| 0x30 | 8 | Data size |
| 0x38 | 4 | File attributes (0x10000000 = directory index present ⚠) |
| 0x3C | 4 | Reparse tag if a reparse point, else EA size ⚠ |
| 0x40 | 1 | Name length (≤ 255) |
| 0x41 | 1 | Namespace: 0 POSIX · 1 Win32 · 2 DOS · 3 Win32 and DOS |
| 0x42 | — | Name |

The times and sizes are a **"duplicated information"** snapshot used to answer directory listings
without opening every file. ⚠ When Windows refreshes them (in the record and in the parent's index
entry) is behavior we must match — open question.

A long name that is not a valid 8.3 name gets **two** `$FILE_NAME`s: Win32 (1) and DOS (2). A name that
is already valid 8.3 gets one with namespace 3. [obs: system files use 3] ⚠ 8.3 generation is
configurable per volume (`fsutil 8dot3name`); default differs on non-system volumes.

### 6.4 `$VOLUME_INFORMATION` [obs]

| Off | Size | Field |
|---|---|---|
| 0x00 | 8 | Zero |
| 0x08 | 1 | Major version (3) |
| 0x09 | 1 | Minor version (1) |
| 0x0A | 2 | Flags: 0x0001 **dirty** · 0x0002 resize log · 0x0004 upgrade on mount · 0x0008 mounted on NT4 · 0x0010 USN delete underway · 0x0020 repair object IDs · 0x8000 chkdsk modified [ref] |

### 6.5 `$REPARSE_POINT` [doc: MS-FSCC 2.1.2]

`ReparseTag` (4) · `ReparseDataLength` (2) · reserved (2) · for non-Microsoft tags (bit 31 clear) a 16-byte
GUID · data. Max 16 KiB of data. Every reparse point is also indexed in `$Extend\$Reparse:$R`. Symlinks
(`IO_REPARSE_TAG_SYMLINK`), junctions (`MOUNT_POINT`), WOF/system-compressed files, OneDrive placeholders
and dedup all live here; the file system stores and indexes them, and filter drivers interpret most.

---

## 7. Indexes (directories and view indexes)

An index is a B+ tree. Small ones live entirely in `$INDEX_ROOT`; larger ones put nodes in fixed-size
`INDX` blocks in `$INDEX_ALLOCATION`, with a `$BITMAP` of the same name marking which blocks are used.
Directories use the name **`$I30`** and index `$FILE_NAME` values. [obs]

### 7.1 `$INDEX_ROOT` value [obs]

| Off | Size | Field | Root dir |
|---|---|---|---|
| 0x00 | 4 | Indexed attribute type (0x30; 0 for view indexes) | 0x30 |
| 0x04 | 4 | Collation rule | 1 |
| 0x08 | 4 | Index block size in bytes | 4096 |
| 0x0C | 1 | Clusters per index block | 1 |
| 0x0D | 3 | Padding | |
| 0x10 | 16 | Index header | |

### 7.2 Index header (in `$INDEX_ROOT` at 0x10, in `INDX` at 0x18) [obs]

| Off | Size | Field |
|---|---|---|
| 0x00 | 4 | Offset of the first entry (relative to this header) |
| 0x04 | 4 | Size of entries in use (relative to this header) |
| 0x08 | 4 | Allocated size of entries |
| 0x0C | 1 | Flags: 0x01 = has child nodes ("large index") |
| 0x0D | 3 | Padding |

Observed: root directory = `10 / 28 / 28 / flags 1` (one end entry pointing to a child);
`$Extend` = flags 0 (all entries in the root).

### 7.3 `INDX` block [obs]

`INDX` · USA offset 0x28 · USA count 9 · LSN (8) · **VCN of this block** (8) at 0x10 · index header at
0x18 · USA at 0x28 · entries. Observed root block: first entry at 0x28+0x18 = 0x40, 1232 bytes in use of
4072. When the index block is smaller than a cluster, VCNs count 512-byte units instead of clusters
[obs: 64 KiB clusters with 4 KiB index blocks; a 187-block directory reads correctly only that way].

### 7.4 Index entry [ref, partly obs]

| Off | Size | Field |
|---|---|---|
| 0x00 | 8 | `$I30`: file reference. View index: data offset (2), data length (2), reserved (4) |
| 0x08 | 2 | Entry length |
| 0x0A | 2 | Key length |
| 0x0C | 2 | Flags: 0x01 has child node · 0x02 last entry (no key) |
| 0x0E | 2 | Padding |
| 0x10 | — | Key (`$I30`: a copy of the `$FILE_NAME` value), then view-index data |
| end−8 | 8 | If flag 0x01: VCN of the child block holding keys less than this one |

Every node ends with a "last entry" with no key, which may point to a child holding the greatest keys.

### 7.5 Collation rules [obs for 0x01, 0x10, 0x12]

| Value | Rule | Used by |
|---|---|---|
| 0x00 | Binary | — |
| 0x01 | File name — compare names after mapping through **this volume's `$UpCase`** | `$I30` |
| 0x02 | Unicode string ⚠ | — |
| 0x10 | Unsigned 32-bit | `$SII`, `$Quota:$Q` |
| 0x11 | SID | `$Quota:$O` |
| 0x12 | Security hash (hash, then security ID) | `$SDH` |
| 0x13 | Sequence of unsigned 32-bit values | `$ObjId:$O`, `$Reparse:$R` |

⚠ Exact `$I30` ordering details to pin down with test volumes: ties between names that upcase equal
(POSIX namespace / case-sensitive directories), and how Win32 and DOS entries for the same file
interleave.

**Collation must always use the volume's own `$UpCase`**, never a built-in table: Windows versions write
different tables at format time, and indexes on disk are sorted by whichever one the volume has. [ref]

---

## 8. Other system files

**`$AttrDef`** — array of 160-byte entries [obs: 2560 bytes / 16]: name (64 UTF-16 units) · type (4) ·
display rule (4) · collation rule (4) · flags (4) · minimum size (8) · maximum size (8). ⚠ Flag meanings
(indexable, must be resident, may be non-resident).

**`$Bitmap`** — 1 bit per cluster, LSB first ⚠. [obs: 2048 bytes for 16383 clusters, allocated 4096]
Bits past the last cluster ⚠ (set or clear?).

**`$Secure`** — shared security descriptors, so identical ACLs are stored once. [obs: `$SDS` data 262396
bytes; `$SDH` collation 0x12; `$SII` collation 0x10]
- `$SDS`: entries of {hash (4), security ID (4), offset in `$SDS` (8), entry length (4)} followed by a
  self-relative security descriptor [doc: MS-DTYP 2.4.6], 16-byte aligned. Data is written in 256 KiB
  blocks, each **mirrored** in the following 256 KiB. [ref] ⚠
- `$SII`: key = security ID; data = the 20-byte `$SDS` entry header. [ref]
- `$SDH`: key = (hash, security ID); data = the same header + 4 padding bytes ⚠. [ref]
- ⚠ Hash: for each 32-bit word *w* of the descriptor, `h = rotl(h, 3) + w`. [ref] Must verify against
  Windows-written entries.
- Security IDs start at 0x100 ⚠.

**`$UpCase`** — 65536 UTF-16 units, mapping each code unit to its uppercase. [obs: `a`→`A`, `é`→`É`,
`ÿ`→`Ÿ`; `ß` and `ı` map to themselves] `$UpCase:$Info` (32 bytes) records which table it is ⚠ (length,
CRC64, OS version — field layout to confirm on a Windows image; added in Windows 8 ⚠).

**`$Extend`** children [ref, ⚠ record numbers vary]:
- `$ObjId` — index `$O`: object ID → file reference + birth IDs.
- `$Quota` — indexes `$O` (SID → owner ID) and `$Q` (owner ID → quota record).
- `$Reparse` — index `$R`: key (reparse tag, file reference), no data.
- `$UsnJrnl` — `$J` (sparse stream of change records) and `$Max` (§11). Exists only once enabled.
- `$RmMetadata` — Transactional NTFS (TxF) and repair data, Vista+. TxF is deprecated; we must preserve
  these files but need not implement TxF.
- ⚠ `$Deleted` — newer Windows 10 builds, for POSIX-style delete. To confirm.

---

## 9. File data

- **Resident data** sits inside the record when small (roughly < 700 bytes in a 1 KiB record).
- **Sparse files**: runs with no offset (§5.4) read as zeros; the attribute has flag 0x8000 and a
  compressed-size field. [ref]
- **Initialized size**: everything from initialized size to data size reads as zero, which lets
  `SetEndOfFile` extend without writing. [ref]
- **Compression (LZNT1)** [doc: MS-XCA 2.5, Microsoft Learn: compression requires clusters ≤ 4 KiB]:
  data is split into compression units of 2^cu clusters (16 → 64 KiB). Each unit is stored either
  uncompressed (all clusters allocated), entirely sparse (all zeros), or as LZNT1 data in its first *n*
  clusters followed by a sparse run filling the rest of the unit [obs: 512-byte clusters, text file → 2
  allocated + 14 sparse clusters per unit]. LZNT1 compresses 4 KiB chunks, each with a 2-byte header
  (size−3 in bits 0–11, signature `011` in bits 12–14, compressed flag in bit 15). [doc] ⚠ A chunk that
  decompresses to less than 4 KiB but isn't the last is treated as zero-padded to 4 KiB; reads of real
  data agree, but we haven't yet seen a compressor emit such a chunk.
- **Windows 10 "system compression"** (XPRESS/LZX) is *not* NTFS compression: `wof.sys` stores it as a
  reparse point plus a named `WofCompressedData` stream. We only need to store those faithfully. [ref]
- **EFS encryption**: data encrypted by the EFS layer, keys in `$LOGGED_UTILITY_STREAM:$EFS`. Out of scope
  until late; we must preserve such files untouched.

---

## 10. `$LogFile` (the journal)

The hardest part of compatibility, and the least documented. Everything in this section beyond the
first bullet is **⚠ [ref]** until observed on Windows-written logs.

- [obs] `mkntfs` writes a 2 MiB `$LogFile` (512 clusters on a 64 MiB volume) **filled entirely with
  `0xFF`**. That "empty" state is what Windows treats as a reset log and initializes on mount. That is
  the basis of milestone 2 ("reset log" mode).

**Layout.** Two **restart pages** (`RSTR`, one system page each, normally 4 KiB) at the start, then
**record pages** (`RCRD`) used as a circular buffer. The file is maintained by the Log File Service (LFS);
NTFS is its client, named `"NTFS"`.

**Restart page header** (`RSTR`): USA offset/count · chkdsk LSN (8) · system page size (4) · log page
size (4) · restart area offset (2) · LFS minor version (2) · LFS major version (2) · USA.

**Restart area**: current LSN · client count · client free/in-use lists · flags (⚠ 0x0002 = clean
shutdown) · sequence-number bits · restart area length · client array offset · log file size · last-LSN
data length · log record header length · log page data offset · restart log open count. Then client
records: oldest LSN · client restart LSN · prev/next client · sequence number · client name `"NTFS"`.

**Record page header** (`RCRD`): USA · last LSN or file offset · flags · page count · page position ·
next record offset · last end LSN.

**Log record header** (0x30 bytes): this LSN · client previous LSN · client undo-next LSN · client data
length · client ID · record type (1 = client record, 2 = client restart) · transaction ID · flags (0x0001
= record continues on the next page).

**LSNs** combine a file offset (in 8-byte units) with a wrap counter in the high bits; the split is set by
"sequence-number bits".

**NTFS client data** (after the LFS header): redo operation (2) · undo operation (2) · redo offset (2) ·
redo length (2) · undo offset (2) · undo length (2) · target attribute (2) · LCNs to follow (2) · record
offset (2) · attribute offset (2) · cluster block offset (2) · reserved (2) · target VCN (8) · LCN
array. Then the redo bytes and undo bytes.

**Operations**: 0x00 Noop · 0x01 CompensationLogRecord · 0x02 InitializeFileRecordSegment ·
0x03 DeallocateFileRecordSegment · 0x04 WriteEndOfFileRecordSegment · 0x05 CreateAttribute ·
0x06 DeleteAttribute · 0x07 UpdateResidentValue · 0x08 UpdateNonresidentValue · 0x09 UpdateMappingPairs ·
0x0A DeleteDirtyClusters · 0x0B SetNewAttributeSizes · 0x0C AddIndexEntryRoot · 0x0D DeleteIndexEntryRoot
· 0x0E AddIndexEntryAllocation · 0x0F DeleteIndexEntryAllocation · 0x10 WriteEndOfIndexBuffer ·
0x11 SetIndexEntryVcnRoot · 0x12 SetIndexEntryVcnAllocation · 0x13 UpdateFileNameRoot ·
0x14 UpdateFileNameAllocation · 0x15 SetBitsInNonresidentBitMap · 0x16 ClearBitsInNonresidentBitMap ·
0x17 HotFix · 0x18 EndTopLevelAction · 0x19 PrepareTransaction · 0x1A CommitTransaction ·
0x1B ForgetTransaction · 0x1C OpenNonresidentAttribute · 0x1D OpenAttributeTableDump ·
0x1E AttributeNamesDump · 0x1F DirtyPageTableDump · 0x20 TransactionTableDump ·
0x21 UpdateRecordDataRoot · 0x22 UpdateRecordDataAllocation · ⚠ further codes added in Windows 8+.

**Recovery model** (ARIES-style): a checkpoint writes the open-attribute table, attribute names, dirty
page table and transaction table, then the client restart record points at it. Recovery runs analysis
from the checkpoint, redoes every logged change whose page LSN is older than the record, then undoes
the transactions that never committed. Note the file record header's LSN field (§4) exists for exactly
this comparison.

**LFS versions** ⚠: LFS 1.1 has been around since NT; Windows 8 introduced **LFS 2.0**, which older
Windows can't replay. We need to learn what Windows 10/11 write, and whether they downgrade on clean
shutdown so older systems can mount.

---

## 11. `$UsnJrnl` (change journal)

- `$J`: sparse stream of change records; the start is freed as the journal wraps, so offsets (= USNs)
  only grow. [ref]
- `$Max` (32 bytes): maximum size · allocation delta · journal ID · lowest valid USN. ⚠
- Records are `USN_RECORD_V2` [doc: MS-FSCC 2.4 / Microsoft Learn `USN_RECORD_V2`], 8-byte aligned. ⚠
  Records don't cross 4 KiB page boundaries in `$J` (padding instead).
- Each file's latest USN is also stored in `$STANDARD_INFORMATION` (§6.1). Search indexing, backup and
  antivirus software depend on this journal; once enabled it must be maintained on every change.

---

## 12. Names and namespace rules

- Maximum component length 255 UTF-16 units. [doc]
- On disk, only `\0` and `/` are invalid in the POSIX namespace ⚠; the Win32 namespace also forbids
  `< > : " \ | ? *` and 0x01–0x1F. Reserved device names (`CON`, `NUL`, …) and trailing-dot/space
  stripping are enforced by the Win32 layer above the file system, not by NTFS. [ref]
- Lookups are case-insensitive via `$UpCase`, preserving the stored case. Windows 10 1803+ allows
  **per-directory case sensitivity** ⚠ (where the flag is stored is an open question).
- `file:stream:$TYPE` syntax addresses alternate data streams; parsing happens in the file system.
  [doc: MS-FSCC 2.1.5]

---

## 13. Interoperability hazards

- **Hibernated volumes / Fast Startup**: Windows may leave a volume "mounted" inside a hibernation image.
  Writing to such a volume and then resuming Windows corrupts it. We must detect this (⚠ how: dirty flag,
  `hiberfil.sys` presence, `$LogFile` state) and refuse to mount read-write.
- **Dirty flag**: set in `$VOLUME_INFORMATION` while mounted read-write; a set flag at boot makes Windows
  run `chkdsk`.
- **`BAAD` records** and `chkdsk` results must be handled exactly as Windows does.
- **Windows-only fields we don't understand** must be preserved byte-for-byte when we rewrite a record.

---

## 14. Open questions (to settle with Windows-formatted images)

1. Restart and record page field offsets in `$LogFile`; LFS 1.1 vs 2.0 on Windows 10/11.
2. `$UpCase:$Info` layout.
3. `$Secure` hash algorithm and the `$SDH` padding.
4. Where the per-directory case-sensitivity flag is stored.
5. Exact `$I30` tie-break ordering (POSIX names, Win32/DOS pairs).
6. When "duplicated information" in `$FILE_NAME` and index entries is refreshed.
7. How many MFT records must lie in `$MFT`'s first extent; where Windows places `$MFT`/`$MFTMirr`.
8. `$Bitmap` bits beyond the end of the volume.
9. Record flag 0x04 and the bytes after the end marker.
10. Cluster sizes > 64 KiB encoding, and 4Kn sector volumes.
11. Detecting hibernated volumes reliably.

Most of these can be answered by formatting small VHDs on Windows, making controlled changes (one file
created, renamed, etc.), and diffing the bytes. Planned as the first task once a Windows machine is
available.

## References

- [MS-FSCC] File System Control Codes — https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/
- [MS-XCA] Xpress Compression Algorithm (includes LZNT1) — https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-xca/
- [MS-DTYP] Windows Data Types (FILETIME, SECURITY_DESCRIPTOR, SID) — https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/
- libfsntfs documentation, "New Technologies File System (NTFS)" by Joachim Metz — https://github.com/libyal/libfsntfs/tree/main/documentation (the documentation folder only)
- Linux-NTFS project, "NTFS Documentation" by Richard Russon and Yuval Fledel — the documentation, not the driver source

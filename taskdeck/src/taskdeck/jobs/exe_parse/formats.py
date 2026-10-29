"""Reading an executable's headers: DOS MZ, and the new-style header it may point to (PE, NE, LE/LX).

Pure Python, no third-party parser (the app runs on Android). Malformed or truncated files never raise:
what can't be read is left out and noted in ``Exe.warnings``.

The result is a list of **regions** that together cover every byte of the file (headers, relocation
table, load module or sections, overlay, gaps), plus the decoded header fields and tables.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

MZ_FIELDS = (  # (name, struct code, meaning), at offset 0
    ("e_magic", "2s", "signature 'MZ'"),
    ("e_cblp", "H", "bytes in the last 512-byte page (0 = whole page)"),
    ("e_cp", "H", "512-byte pages in the file image"),
    ("e_crlc", "H", "relocation entries"),
    ("e_cparhdr", "H", "header size in 16-byte paragraphs"),
    ("e_minalloc", "H", "minimum extra paragraphs"),
    ("e_maxalloc", "H", "maximum extra paragraphs"),
    ("e_ss", "H", "initial SS (relative to the load segment)"),
    ("e_sp", "H", "initial SP"),
    ("e_csum", "H", "checksum"),
    ("e_ip", "H", "initial IP"),
    ("e_cs", "H", "initial CS (relative to the load segment)"),
    ("e_lfarlc", "H", "file offset of the relocation table"),
    ("e_ovno", "H", "overlay number"),
)

PE_MACHINES = {0x14C: "i386", 0x8664: "x86-64", 0x1C0: "ARM", 0x1C4: "ARMv7 Thumb-2", 0xAA64: "ARM64",
               0x200: "IA-64", 0x166: "MIPS R4000", 0x1F0: "PowerPC", 0x0: "unknown"}
PE_SUBSYSTEMS = {1: "native", 2: "Windows GUI", 3: "Windows console", 5: "OS/2 console", 7: "POSIX console",
                 9: "Windows CE GUI", 10: "EFI application", 11: "EFI boot driver", 12: "EFI runtime driver",
                 14: "Xbox", 16: "Windows boot application"}
PE_DIRECTORIES = ("export", "import", "resource", "exception", "certificate", "base relocation", "debug",
                  "architecture", "global ptr", "TLS", "load config", "bound import", "IAT", "delay import",
                  "CLR runtime header", "reserved")
PE_CHARACTERISTICS = {0x1: "relocs stripped", 0x2: "executable", 0x20: "large address aware", 0x100: "32-bit",
                      0x200: "debug stripped", 0x1000: "system", 0x2000: "DLL"}
SECTION_FLAGS = {0x20: "code", 0x40: "initialized data", 0x80: "uninitialized data", 0x20000000: "execute",
                 0x40000000: "read", 0x80000000: "write", 0x10000000: "shared", 0x02000000: "discardable"}
RESOURCE_TYPES = {1: "CURSOR", 2: "BITMAP", 3: "ICON", 4: "MENU", 5: "DIALOG", 6: "STRING", 7: "FONTDIR",
                  8: "FONT", 9: "ACCELERATOR", 10: "RCDATA", 11: "MESSAGETABLE", 12: "GROUP_CURSOR",
                  14: "GROUP_ICON", 16: "VERSION", 17: "DLGINCLUDE", 19: "PLUGPLAY", 20: "VXD",
                  21: "ANICURSOR", 22: "ANIICON", 23: "HTML", 24: "MANIFEST"}
NE_TARGET_OS = {0: "unknown", 1: "OS/2", 2: "Windows", 3: "European DOS 4", 4: "Windows 386", 5: "BOSS"}

# Overlay data appended after the DOS load image, recognised by its first bytes.
OVERLAY_SIGNATURES = (
    (b"FBOV", "Borland overlay (VROOMM / TLINK)"),
    (b"MZ", "another MZ executable (overlay or stub)"),
    (b"PK\x03\x04", "zip archive (self-extractor?)"),
    (b"Rar!", "RAR archive (self-extractor?)"),
    (b"7z\xbc\xaf", "7-Zip archive (self-extractor?)"),
)


@dataclass
class Region:
    """A byte range of the file [start, end) with what it is.

    ``base`` is the address of ``start`` in the address column of the hex dumps: the RVA for PE
    sections and headers, the offset in the load image for a DOS load module, None where it has no
    address (relocation table, overlay)."""

    name: str
    kind: str  # header, relocations, code, data, section, segment, overlay, gap, ...
    start: int
    end: int
    base: int | None = None
    address: str = ""  # what ``base`` is, for the column heading: "RVA", "image offset"
    note: str = ""

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass
class Exe:
    """Everything read from one executable."""

    size: int
    format: str = "unknown"  # MZ (DOS), PE32, PE32+, NE, LE, LX
    description: str = ""
    regions: list[Region] = field(default_factory=list)
    # Ordered tables of (field, value, meaning) per header, e.g. {"MZ header": [...], "PE optional header": [...]}
    headers: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    # Facts for metadata.json and the summary (JSON-friendly values).
    facts: dict[str, object] = field(default_factory=dict)
    # Larger tables written to their own (chunked) files: name -> lines
    tables: dict[str, list[str]] = field(default_factory=dict)
    entry: tuple[int, str] | None = None  # (file offset, how it's written in the header)
    warnings: list[str] = field(default_factory=list)

    def region_at(self, offset: int) -> Region | None:
        return next((r for r in self.regions if r.start <= offset < r.end), None)


def h(n: int, width: int = 0) -> str:
    return f"0x{n:0{width}X}"


def _flags(value: int, names: dict[int, str]) -> str:
    return ", ".join(name for bit, name in names.items() if value & bit) or "-"


def _cstr(data: bytes, offset: int, limit: int = 256) -> str:
    end = data.find(b"\0", offset, offset + limit)
    raw = data[offset:end if end >= 0 else offset + limit]
    return raw.decode("latin-1")


def _read(data: bytes, fmt: str, offset: int) -> tuple | None:
    size = struct.calcsize("<" + fmt)
    if offset < 0 or offset + size > len(data):
        return None
    return struct.unpack_from("<" + fmt, data, offset)


# ---- top level --------------------------------------------------------------------------------------

def parse(data: bytes) -> Exe:
    exe = Exe(len(data))
    if data[:2] in (b"MZ", b"ZM"):
        _parse_mz(data, exe)
    elif data[:4] == b"\x7fELF":
        exe.format, exe.description = "ELF", "ELF executable (not decoded: TaskDeck reads DOS and Windows files)"
        exe.warnings.append("ELF isn't decoded yet; the file is laid out as raw data.")
    else:
        exe.description = "No MZ signature: not a DOS or Windows executable (a .COM file is raw code)."
        exe.warnings.append("No executable header found; the file is laid out as raw data.")
    _fill_gaps(exe)
    return exe


def _fill_gaps(exe: Exe) -> None:
    """Sort the regions, clip overlaps and name every byte no header claims, so the regions cover the file."""
    out: list[Region] = []
    pos = 0
    for r in sorted((r for r in exe.regions if r.size > 0), key=lambda r: (r.start, -r.end)):
        r.start, r.end = max(r.start, 0), min(r.end, exe.size)
        if r.end <= pos:
            continue  # inside an earlier region (e.g. a section that overlaps the headers)
        if r.start < pos:
            if r.base is not None:
                r.base += pos - r.start
            r.start = pos
        if r.start > pos:
            out.append(Region("unclaimed", "gap", pos, r.start, note="not described by any header"))
        out.append(r)
        pos = r.end
    if pos < exe.size:
        out.append(Region("unclaimed" if out else "file", "gap" if out else "data", pos, exe.size,
                          note="not described by any header" if out else ""))
    exe.regions = out


# ---- DOS MZ -----------------------------------------------------------------------------------------

def _parse_mz(data: bytes, exe: Exe) -> None:
    values = _read(data, "".join(f[1] for f in MZ_FIELDS), 0)
    if values is None:
        exe.format, exe.description = "MZ (truncated)", "Truncated MZ header"
        exe.warnings.append("The file is shorter than an MZ header.")
        return
    mz = dict(zip((f[0] for f in MZ_FIELDS), values))
    exe.headers["MZ header"] = [(name, data[:2].decode("latin-1") if name == "e_magic" else h(mz[name], 4), meaning)
                                for name, _, meaning in MZ_FIELDS]
    header_size = mz["e_cparhdr"] * 16
    image_end = mz["e_cp"] * 512 - ((512 - mz["e_cblp"]) if mz["e_cblp"] else 0)
    if not 0 < image_end <= exe.size:
        exe.warnings.append(f"The MZ header gives an image end of {h(image_end)}, outside the file "
                            f"({h(exe.size)} bytes); using the file size.")
        image_end = exe.size
    header_size = min(header_size, image_end)

    new = _new_header(data, mz)
    if new:
        lfanew, sig = new
        exe.regions.append(Region("DOS header", "header", 0, min(header_size, 0x40) or 0x40, note="MZ header"))
        exe.regions.append(Region("DOS stub", "code", min(header_size, 0x40) or 0x40, lfanew,
                                  note="16-bit program run under DOS"))
        exe.headers["MZ header"].append(("e_lfanew", h(lfanew, 8), "file offset of the new-style header"))
        exe.facts["new_header_offset"] = lfanew
        {"PE": _parse_pe, "NE": _parse_ne, "LE": _parse_le, "LX": _parse_le}[sig](data, exe, lfanew)
        return

    exe.format = "MZ"
    exe.description = "DOS executable (MZ)"
    exe.facts.update(header_bytes=header_size, image_end=image_end, load_module_bytes=image_end - header_size,
                     relocations=mz["e_crlc"], min_extra_paragraphs=mz["e_minalloc"],
                     max_extra_paragraphs=mz["e_maxalloc"],
                     initial_cs_ip=f"{mz['e_cs']:04X}:{mz['e_ip']:04X}", initial_ss_sp=f"{mz['e_ss']:04X}:{mz['e_sp']:04X}")
    exe.regions.append(Region("MZ header", "header", 0, min(0x1C, header_size)))
    reloc_start, reloc_end = mz["e_lfarlc"], mz["e_lfarlc"] + mz["e_crlc"] * 4
    if mz["e_crlc"] and 0x1C <= reloc_start and reloc_end <= header_size:
        exe.regions.append(Region("relocation table", "relocations", reloc_start, reloc_end,
                                  note=f"{mz['e_crlc']:,} segment:offset fixups"))
        relocs = struct.unpack_from(f"<{mz['e_crlc'] * 2}H", data, reloc_start)
        lines = []
        for i in range(mz["e_crlc"]):
            off, seg = relocs[2 * i], relocs[2 * i + 1]
            image = seg * 16 + off
            lines.append(f"{i:5d}  {seg:04X}:{off:04X}  image {h(image, 6)}  file {h(header_size + image, 8)}")
        exe.tables["relocations"] = ["#  n  seg:off    offset in the load image   file offset"] + lines
    elif mz["e_crlc"]:
        exe.warnings.append("The relocation table lies outside the header; it wasn't decoded.")
    extra_end = reloc_start if 0x1C < reloc_start <= header_size else 0x1C
    if extra_end > 0x1C:
        exe.regions.append(Region("header extra", "header", 0x1C, extra_end,
                                  note="between the MZ fields and the relocation table (often linker data)"))
    padding = reloc_end if "relocations" in exe.tables else extra_end
    if header_size > padding:
        exe.regions.append(Region("header padding", "header", padding, header_size, note="rest of the MZ header"))
    exe.regions.append(Region("load module", "code", header_size, image_end, 0, "image offset",
                              note="code and data loaded into memory; image offset 0 = load segment:0000"))
    entry_image = mz["e_cs"] * 16 + mz["e_ip"]
    if header_size + entry_image < image_end:
        exe.entry = (header_size + entry_image, f"CS:IP {mz['e_cs']:04X}:{mz['e_ip']:04X} (image {h(entry_image, 6)})")
    if image_end < exe.size:
        exe.regions.append(_overlay(data, image_end, exe.size))
    exe.facts["overlay_bytes"] = exe.size - image_end


def _overlay(data: bytes, start: int, end: int) -> Region:
    head = data[start:start + 8]
    what = next((label for sig, label in OVERLAY_SIGNATURES if head.startswith(sig)), "")
    return Region("overlay", "overlay", start, end, note=what or "data after the load image (overlays, resources, ...)")


def _new_header(data: bytes, mz: dict) -> tuple[int, str] | None:
    """The new-style header's offset and signature, if e_lfanew (at 0x3C) points to one.
    Plain DOS programs often have garbage there, so it counts only when the signature matches."""
    got = _read(data, "I", 0x3C)
    if got is None or mz["e_lfarlc"] < 0x40 and mz["e_lfarlc"] != 0:
        return None
    lfanew = got[0]
    if not 0x40 <= lfanew < len(data) - 4:
        return None
    if data[lfanew:lfanew + 4] == b"PE\0\0":
        return lfanew, "PE"
    sig = data[lfanew:lfanew + 2]
    if sig in (b"NE", b"LE", b"LX"):
        return lfanew, sig.decode()
    return None


# ---- PE (Windows) -----------------------------------------------------------------------------------

def _parse_pe(data: bytes, exe: Exe, pe: int) -> None:
    coff = _read(data, "HHIIIHH", pe + 4)
    if coff is None:
        exe.format = "PE (truncated)"
        exe.warnings.append("The PE header is cut off.")
        return
    machine, nsect, stamp, symptr, nsyms, opt_size, chars = coff
    opt = pe + 24
    magic = (_read(data, "H", opt) or (0,))[0]
    plus = magic == 0x20B
    exe.format = "PE32+" if plus else "PE32"
    exe.headers["PE file header (COFF)"] = [
        ("Machine", h(machine, 4), PE_MACHINES.get(machine, "?")),
        ("NumberOfSections", str(nsect), ""),
        ("TimeDateStamp", h(stamp, 8), "link time (seconds since 1970, UTC; often fake or a hash)"),
        ("PointerToSymbolTable", h(symptr, 8), ""),
        ("NumberOfSymbols", str(nsyms), ""),
        ("SizeOfOptionalHeader", h(opt_size, 4), ""),
        ("Characteristics", h(chars, 4), _flags(chars, PE_CHARACTERISTICS)),
    ]
    if plus:
        fmt, names = "HBBIIIIIQIIHHHHHHIIIIHHQQQQII", (
            "Magic", "MajorLinkerVersion", "MinorLinkerVersion", "SizeOfCode", "SizeOfInitializedData",
            "SizeOfUninitializedData", "AddressOfEntryPoint", "BaseOfCode", "ImageBase", "SectionAlignment",
            "FileAlignment", "MajorOperatingSystemVersion", "MinorOperatingSystemVersion", "MajorImageVersion",
            "MinorImageVersion", "MajorSubsystemVersion", "MinorSubsystemVersion", "Win32VersionValue",
            "SizeOfImage", "SizeOfHeaders", "CheckSum", "Subsystem", "DllCharacteristics", "SizeOfStackReserve",
            "SizeOfStackCommit", "SizeOfHeapReserve", "SizeOfHeapCommit", "LoaderFlags", "NumberOfRvaAndSizes")
    else:
        fmt, names = "HBBIIIIIIIIIHHHHHHIIIIHHIIIIII", (
            "Magic", "MajorLinkerVersion", "MinorLinkerVersion", "SizeOfCode", "SizeOfInitializedData",
            "SizeOfUninitializedData", "AddressOfEntryPoint", "BaseOfCode", "BaseOfData", "ImageBase",
            "SectionAlignment", "FileAlignment", "MajorOperatingSystemVersion", "MinorOperatingSystemVersion",
            "MajorImageVersion", "MinorImageVersion", "MajorSubsystemVersion", "MinorSubsystemVersion",
            "Win32VersionValue", "SizeOfImage", "SizeOfHeaders", "CheckSum", "Subsystem", "DllCharacteristics",
            "SizeOfStackReserve", "SizeOfStackCommit", "SizeOfHeapReserve", "SizeOfHeapCommit", "LoaderFlags",
            "NumberOfRvaAndSizes")
    values = _read(data, fmt, opt)
    sections_at = opt + opt_size
    if values is None:
        exe.warnings.append("The PE optional header is cut off.")
        o: dict = {}
        dirs: list[tuple[int, int]] = []
    else:
        o = dict(zip(names, values))
        meanings = {"Subsystem": PE_SUBSYSTEMS.get(o["Subsystem"], "?"),
                    "Magic": "PE32+ (64-bit)" if plus else "PE32 (32-bit)" if magic == 0x10B else "?"}
        exe.headers["PE optional header"] = [(n, h(v), meanings.get(n, "")) for n, v in o.items()]
        dirs_at = opt + struct.calcsize("<" + fmt)
        dirs = []
        for i in range(min(o["NumberOfRvaAndSizes"], 16)):
            d = _read(data, "II", dirs_at + 8 * i)
            if d is None:
                break
            dirs.append(d)
        exe.headers["PE data directories"] = [(PE_DIRECTORIES[i], h(rva, 8), f"size {h(size)}" if rva or size else "-")
                                              for i, (rva, size) in enumerate(dirs)]
    exe.description = (f"Windows {'DLL' if chars & 0x2000 else 'executable'} ({exe.format}, "
                       f"{PE_MACHINES.get(machine, h(machine, 4))}, {PE_SUBSYSTEMS.get(o.get('Subsystem', 0), 'subsystem ?')})")

    sections = []
    for i in range(nsect):
        s = _read(data, "8sIIIIIIHHI", sections_at + 40 * i)
        if s is None:
            exe.warnings.append(f"The section table is cut off after {i} sections.")
            break
        name = s[0].rstrip(b"\0").decode("latin-1") or f"section{i}"
        sections.append((name, *s[1:]))
    header_end = min(o.get("SizeOfHeaders") or sections_at + 40 * nsect, exe.size)
    exe.regions.append(Region("PE headers", "header", pe, header_end, pe, "RVA",
                              note="PE signature, file header, optional header, section table"))
    rows = ["#  name      RVA        virt size  file offset  file size  flags"]
    raw_end = header_end
    for i, (name, vsize, rva, rsize, rptr, *_rest, flags) in enumerate(sections):
        rows.append(f"{i:2d} {name:<9} {h(rva, 8)} {h(vsize, 8)} {h(rptr, 8)}   {h(rsize, 8)}  "
                    f"{h(flags, 8)} {_flags(flags, SECTION_FLAGS)}")
        if rsize and rptr < exe.size:
            kind = "code" if flags & 0x20 else "data"
            exe.regions.append(Region(name, kind, rptr, min(rptr + rsize, exe.size), rva, "RVA",
                                      note=_flags(flags, SECTION_FLAGS)))
            raw_end = max(raw_end, min(rptr + rsize, exe.size))
    exe.tables["sections"] = rows

    def to_offset(rva: int) -> int | None:
        for name, vsize, srva, rsize, rptr, *_ in sections:
            if srva <= rva < srva + max(vsize, rsize) and rva - srva < rsize:
                return rptr + rva - srva
        return rva if rva < header_end else None

    exe.facts.update(machine=PE_MACHINES.get(machine, h(machine, 4)), sections=len(sections),
                     link_time=datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds"),
                     image_base=o.get("ImageBase"), subsystem=PE_SUBSYSTEMS.get(o.get("Subsystem", 0)),
                     dll=bool(chars & 0x2000))
    if o.get("AddressOfEntryPoint"):
        ep = o["AddressOfEntryPoint"]
        off = to_offset(ep)
        if off is not None:
            exe.entry = (off, f"RVA {h(ep, 8)} (VA {h(o['ImageBase'] + ep, 8)})")

    directory = dict(zip(PE_DIRECTORIES, (rva for rva, _ in dirs)))
    if directory.get("import"):
        _pe_imports(data, exe, directory["import"], to_offset, plus)
    if directory.get("export"):
        _pe_exports(data, exe, directory["export"], to_offset)
    if directory.get("resource"):
        _pe_resources(data, exe, directory["resource"], to_offset)
    cert = directory.get("certificate", 0)  # the only directory given as a file offset, not an RVA
    if raw_end < exe.size:
        overlay = _overlay(data, raw_end, exe.size)
        if cert and cert >= raw_end:
            overlay.note += "; contains the Authenticode signature (certificate table)"
        exe.regions.append(overlay)
    exe.facts["overlay_bytes"] = exe.size - raw_end


def _pe_imports(data: bytes, exe: Exe, rva: int, to_offset, plus: bool) -> None:
    lines, dlls = [], {}
    at = to_offset(rva)
    thunk_fmt, ordinal_flag = ("Q", 1 << 63) if plus else ("I", 1 << 31)
    for _ in range(4096):
        desc = _read(data, "IIIII", at) if at is not None else None
        if desc is None or not any(desc):
            break
        lookup, _stamp, _fwd, name_rva, iat = desc
        name_off = to_offset(name_rva)
        dll = _cstr(data, name_off) if name_off is not None else "?"
        names = []
        t = to_offset(lookup or iat)
        slot = iat
        for _ in range(65536):
            v = _read(data, thunk_fmt, t) if t is not None else None
            if not v or not v[0]:
                break
            v = v[0]
            if v & ordinal_flag:
                names.append((slot, f"ordinal {v & 0xFFFF}"))
            else:
                hint_off = to_offset(v & 0x7FFFFFFF)
                names.append((slot, _cstr(data, hint_off + 2) if hint_off is not None else "?"))
            t += struct.calcsize(thunk_fmt)
            slot += struct.calcsize(thunk_fmt)
        dlls[dll] = len(names)
        lines.append(f"{dll}  ({len(names)} imports)")
        lines.extend(f"    IAT {h(s, 8)}  {n}" for s, n in names)
        at += 20
    if lines:
        exe.tables["imports"] = lines
        exe.facts["imports"] = dlls


def _pe_exports(data: bytes, exe: Exe, rva: int, to_offset) -> None:
    at = to_offset(rva)
    d = _read(data, "IIHHIIIIIII", at) if at is not None else None
    if d is None:
        return
    name_rva, base, nfuncs, nnames, funcs_rva, names_rva, ords_rva = d[4:]
    name_off = to_offset(name_rva)
    exe.facts["export_name"] = _cstr(data, name_off) if name_off is not None else ""
    funcs_off, names_off, ords_off = to_offset(funcs_rva), to_offset(names_rva), to_offset(ords_rva)
    named = {}
    for i in range(min(nnames, 65536)):
        n = _read(data, "I", names_off + 4 * i) if names_off is not None else None
        o = _read(data, "H", ords_off + 2 * i) if ords_off is not None else None
        if n is None or o is None:
            break
        noff = to_offset(n[0])
        named[o[0]] = _cstr(data, noff) if noff is not None else "?"
    lines = ["ordinal  RVA         name"]
    for i in range(min(nfuncs, 65536)):
        f = _read(data, "I", funcs_off + 4 * i) if funcs_off is not None else None
        if f is None:
            break
        if f[0]:
            lines.append(f"{base + i:7d}  {h(f[0], 8)}  {named.get(i, '(no name)')}")
    exe.tables["exports"] = lines
    exe.facts["exports"] = len(lines) - 1


def _pe_resources(data: bytes, exe: Exe, rva: int, to_offset) -> None:
    root = to_offset(rva)
    if root is None:
        return
    lines = ["type / name / language      data RVA    size       file offset"]
    counts: dict[str, int] = {}

    def label(entry_name: int) -> str:
        if entry_name & 0x80000000:
            off = root + (entry_name & 0x7FFFFFFF)
            n = _read(data, "H", off)
            return data[off + 2:off + 2 + 2 * n[0]].decode("utf-16-le", "replace") if n else "?"
        return str(entry_name)

    def walk(table: int, path: list[str], depth: int) -> None:
        hdr = _read(data, "IIHHHH", table)
        if hdr is None or depth > 3:
            return
        for i in range(min(hdr[4] + hdr[5], 4096)):
            e = _read(data, "II", table + 16 + 8 * i)
            if e is None:
                return
            name = label(e[0])
            if depth == 0:
                name = RESOURCE_TYPES.get(int(name), name) if name.isdigit() else name
            if e[1] & 0x80000000:
                walk(root + (e[1] & 0x7FFFFFFF), path + [name], depth + 1)
            else:
                leaf = _read(data, "II", root + e[1])
                if leaf is None:
                    continue
                off = to_offset(leaf[0])
                lines.append(f"{' / '.join(path + [name]):<27} {h(leaf[0], 8)}  {h(leaf[1], 8)}  "
                             f"{h(off, 8) if off is not None else '?'}")
                counts[path[0] if path else name] = counts.get(path[0] if path else name, 0) + 1

    walk(root, [], 0)
    if len(lines) > 1:
        exe.tables["resources"] = lines
        exe.facts["resources"] = counts


# ---- NE (16-bit Windows, OS/2) ----------------------------------------------------------------------

NE_FIELDS = (  # (name, struct code), at the NE header; table offsets are relative to it unless noted
    ("ne_magic", "2s"), ("ne_ver", "B"), ("ne_rev", "B"), ("ne_enttab", "H"), ("ne_cbenttab", "H"),
    ("ne_crc", "I"), ("ne_flags", "H"), ("ne_autodata", "H"), ("ne_heap", "H"), ("ne_stack", "H"),
    ("ne_ip", "H"), ("ne_cs", "H"), ("ne_sp", "H"), ("ne_ss", "H"), ("ne_cseg", "H"), ("ne_cmod", "H"),
    ("ne_cbnrestab", "H"), ("ne_segtab", "H"), ("ne_rsrctab", "H"), ("ne_restab", "H"), ("ne_modtab", "H"),
    ("ne_imptab", "H"), ("ne_nrestab", "I"), ("ne_cmovent", "H"), ("ne_align", "H"), ("ne_cres", "H"),
    ("ne_exetyp", "B"), ("ne_flagsothers", "B"),
)


def _parse_ne(data: bytes, exe: Exe, ne: int) -> None:
    exe.format = "NE"
    v = _read(data, "".join(code for _, code in NE_FIELDS), ne)
    if v is None:
        exe.warnings.append("The NE header is cut off.")
        return
    n = dict(zip((name for name, _ in NE_FIELDS), v))
    exe.headers["NE header"] = [(k, val.decode("latin-1") if isinstance(val, bytes) else h(val),
                                 "file offset" if k == "ne_nrestab" else "") for k, val in n.items()]
    exe.description = f"16-bit {NE_TARGET_OS.get(n['ne_exetyp'], 'NE')} executable (NE)"
    tables_end = max(ne + n["ne_enttab"] + n["ne_cbenttab"], n["ne_nrestab"] + n["ne_cbnrestab"], ne + 0x40)
    exe.regions.append(Region("NE header and tables", "header", ne, min(tables_end, exe.size),
                              note="NE header; segment, resource, name, module and entry tables"))
    align = 1 << (n["ne_align"] or 9)
    rows = [" # file offset  file size  flags   min alloc"]
    seg_offsets = []
    for i in range(n["ne_cseg"]):
        s = _read(data, "HHHH", ne + n["ne_segtab"] + 8 * i)
        if s is None:
            exe.warnings.append(f"The segment table is cut off after {i} segments.")
            break
        sector, length, flags, minalloc = s
        start, size = sector * align, length or (0x10000 if sector else 0)
        seg_offsets.append(start)
        kind = "data" if flags & 1 else "code"
        rows.append(f"{i + 1:2d} {h(start, 8)}   {h(size, 6)}   {h(flags, 4)}  {h(minalloc, 4)}  {kind}")
        if sector and start < exe.size:
            exe.regions.append(Region(f"segment {i + 1}", kind, start, min(start + size, exe.size), 0,
                                      "segment offset", note=kind))
    exe.tables["segments"] = rows
    modules = []
    for i in range(n["ne_cmod"]):
        ref = _read(data, "H", ne + n["ne_modtab"] + 2 * i)
        if ref is None:
            break
        modules.append(_pascal(data, ne + n["ne_imptab"] + ref[0]))
    if modules:
        exe.tables["imported modules"] = modules
    exe.facts.update(target_os=NE_TARGET_OS.get(n["ne_exetyp"]), segments=n["ne_cseg"], imported_modules=modules,
                     module_name=_pascal(data, ne + n["ne_restab"]))
    if 0 < n["ne_cs"] <= len(seg_offsets):
        exe.entry = (seg_offsets[n["ne_cs"] - 1] + n["ne_ip"], f"segment {n['ne_cs']}:{n['ne_ip']:04X}")


def _pascal(data: bytes, at: int) -> str:
    return data[at + 1:at + 1 + data[at]].decode("latin-1") if 0 <= at < len(data) else ""


# ---- LE / LX (VxD, OS/2, DOS extenders) -------------------------------------------------------------

def _parse_le(data: bytes, exe: Exe, le: int) -> None:
    sig = data[le:le + 2].decode()
    exe.format = sig
    exe.description = f"{sig} executable (32-bit linear: VxD, OS/2 or DOS extender); header only"
    exe.regions.append(Region(f"{sig} image", "data", le, exe.size, note="header and pages (not decoded further)"))
    exe.warnings.append(f"{sig} objects and pages aren't decoded yet; the image is laid out as one region.")

"""LE and LX: 32-bit linear executables (DOS extenders such as DOS/4G, OS/2, Windows VxDs).

An LE program is made of **objects** (code, data, stack), each loaded at its own linear address and
stored in the file as pages. The loader patches every pointer listed in the **fixup** tables. Layout per
the Open Watcom headers (bld/watcom/h/exeflat.h):

* Table offsets in the header are relative to the LE header, except the data pages, the non-resident
  names and the debug info, which are relative to the start of the "file" the LE belongs to: the DOS
  stub whose ``e_lfanew`` points at it. In a program bound to DOS/4G that stub sits after the extender,
  in the middle of the real file.
* LE page map entries are a 24-bit page number (big-endian) and a flags byte; page n is stored at
  data pages + (n - 1) * page size. LX entries are an offset (shifted), a size and flags.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

HEADER = (  # (name, struct code), from os2_flat_header
    ("signature", "2s"), ("byte_order", "B"), ("word_order", "B"), ("level", "I"), ("cpu_type", "H"),
    ("os_type", "H"), ("version", "I"), ("flags", "I"), ("num_pages", "I"), ("start_obj", "I"), ("eip", "I"),
    ("stack_obj", "I"), ("esp", "I"), ("page_size", "I"), ("last_page_or_shift", "I"), ("fixup_size", "I"),
    ("fixup_cksum", "I"), ("loader_size", "I"), ("loader_cksum", "I"), ("objtab_off", "I"), ("num_objects", "I"),
    ("objmap_off", "I"), ("idmap_off", "I"), ("rsrc_off", "I"), ("num_rsrcs", "I"), ("resname_off", "I"),
    ("entry_off", "I"), ("moddir_off", "I"), ("num_moddirs", "I"), ("fixpage_off", "I"), ("fixrec_off", "I"),
    ("impmod_off", "I"), ("num_impmods", "I"), ("impproc_off", "I"), ("cksum_off", "I"), ("page_off", "I"),
    ("num_preload", "I"), ("nonres_off", "I"), ("nonres_size", "I"), ("nonres_cksum", "I"),
    ("autodata_obj", "I"), ("debug_off", "I"), ("debug_len", "I"),
)
CPU = {1: "80286", 2: "80386", 3: "80486", 4: "Pentium"}
OS = {0: "unknown", 1: "OS/2", 2: "Windows", 3: "DOS 4.x", 4: "Windows 386 (VxD)"}
OBJ_FLAGS = {0x1: "read", 0x2: "write", 0x4: "execute", 0x8: "resource", 0x10: "discardable", 0x20: "shared",
             0x40: "preload", 0x2000: "32-bit"}
SOURCES = {0: "byte", 2: "selector", 3: "16:16 pointer", 5: "16-bit offset", 6: "16:32 pointer", 7: "32-bit offset",
           8: "32-bit self-relative"}
MAX_FIXUPS = 1_000_000


@dataclass
class Run:
    """A stretch of an object stored contiguously in the file."""

    linear: int  # linear address of its first byte
    offset: int  # file offset of its first byte
    size: int


@dataclass
class Object:
    number: int
    base: int  # linear address
    size: int  # virtual size (may be more than is stored: zero-filled)
    flags: int
    runs: list[Run] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "code" if self.flags & 0x4 else "data"

    def file_offset(self, offset: int) -> int | None:
        """The file offset of byte ``offset`` of this object, or None if it isn't stored (zero-filled)."""
        linear = self.base + offset
        for r in self.runs:
            if r.linear <= linear < r.linear + r.size:
                return r.offset + linear - r.linear
        return None


def parse(data: bytes, exe, le: int, file_base: int, h, read, region_cls, names_fn) -> None:
    """Fill ``exe`` (formats.Exe) from the LE/LX header at ``le``; ``file_base`` is where its file starts."""
    sig = data[le:le + 2].decode()
    exe.format = sig
    values = read(data, "".join(c for _, c in HEADER), le)
    if values is None:
        exe.warnings.append(f"The {sig} header is cut off.")
        exe.regions.append(region_cls(f"{sig} image", "data", le, exe.size))
        return
    hd = dict(zip((n for n, _ in HEADER), values))
    is_lx = sig == "LX"
    exe.headers[f"{sig} header"] = [(n, v.decode() if isinstance(v, bytes) else h(v),
                                     {"cpu_type": CPU.get(v, ""), "os_type": OS.get(v, "")}.get(n, "")
                                     if not isinstance(v, bytes) else "") for n, v in hd.items()]
    exe.description = (f"32-bit {OS.get(hd['os_type'], '')} program ({sig}, {CPU.get(hd['cpu_type'], 'x86')})"
                       if hd["os_type"] != 4 else f"Windows virtual device driver ({sig})")
    page_size = hd["page_size"] or 4096
    pages_at = file_base + hd["page_off"]

    def page_location(n: int) -> tuple[int, int] | None:
        """(file offset, stored size) of page n (1-based)."""
        at = le + hd["objmap_off"] + 4 * (n - 1) if not is_lx else le + hd["objmap_off"] + 8 * (n - 1)
        if at + (8 if is_lx else 4) > len(data):
            return None
        if is_lx:
            off, size, _flags = struct.unpack_from("<IHH", data, at)
            return file_base + hd["page_off"] + (off << hd["last_page_or_shift"]), size
        number = (data[at] << 16) | (data[at + 1] << 8) | data[at + 2]
        if number == 0:
            return None
        size = hd["last_page_or_shift"] if number == hd["num_pages"] else page_size
        return pages_at + (number - 1) * page_size, size

    objects: list[Object] = []
    rows = [" # linear base  virtual size  flags  pages        file offset"]
    for i in range(min(hd["num_objects"], 255)):
        rec = read(data, "IIIIII", le + hd["objtab_off"] + 24 * i)
        if rec is None:
            exe.warnings.append(f"The object table is cut off after {i} objects.")
            break
        size, base, flags, mapidx, mapsize, _ = rec
        obj = Object(i + 1, base, size, flags)
        for p in range(mapsize):
            loc = page_location(mapidx + p)
            if loc is None:
                continue
            off, stored = loc
            stored = min(stored, page_size)  # a whole page is stored, even past the object's virtual size
            linear = base + p * page_size
            last = obj.runs[-1] if obj.runs else None
            if last and last.linear + last.size == linear and last.offset + last.size == off:
                last.size += stored
            else:
                obj.runs.append(Run(linear, off, stored))
        objects.append(obj)
        flag_text = ", ".join(n for b, n in OBJ_FLAGS.items() if flags & b)
        first = obj.runs[0].offset if obj.runs else None
        rows.append(f"{i + 1:2d} {h(base, 8)}   {h(size, 8)}    {h(flags, 4)} {mapidx:4d}+{mapsize:<5d}  "
                    f"{h(first, 8) if first is not None else '-':<10}  {obj.kind}: {flag_text}")
        for k, r in enumerate(obj.runs):
            if r.offset < exe.size and r.size:
                part = f" (part {k + 1})" if len(obj.runs) > 1 else ""
                exe.regions.append(region_cls(f"object {i + 1}{part}", obj.kind, r.offset,
                                              min(r.offset + r.size, exe.size), r.linear, "linear address",
                                              note=f"{obj.kind}, linear {h(base, 8)}; {flag_text}"))
    exe.objects = objects
    exe.tables["objects"] = rows

    # The loader section (object table, page map, names, entry table) is followed by the fixup section;
    # LE's "loader size" counts both.
    fixups_at = le + hd["fixpage_off"] if hd["fixpage_off"] else le + hd["objtab_off"] + hd["loader_size"]
    exe.regions.append(region_cls(f"{sig} header and loader tables", "header", le, min(fixups_at, exe.size),
                                  note="header, object table, page map, names, entry table"))
    if hd["fixup_size"]:
        exe.regions.append(region_cls(f"{sig} fixup tables", "relocations", fixups_at,
                                      min(fixups_at + hd["fixup_size"], exe.size),
                                      note="where the loader patches pointers"))

    def locate(obj: int, offset: int) -> tuple[int | None, int | None]:
        """(linear address, file offset) of object ``obj`` byte ``offset``."""
        if 0 < obj <= len(objects):
            o = objects[obj - 1]
            return o.base + offset, o.file_offset(offset)
        return None, None

    exe.locate = locate
    if hd["start_obj"]:
        linear, off = locate(hd["start_obj"], hd["eip"])
        if off is not None:
            exe.entry = (off, f"object {hd['start_obj']}:{h(hd['eip'], 8)} (linear {h(linear, 8)})")
    _fixups(data, exe, le, hd, objects, h, is_lx)
    _exports(data, exe, le, file_base, hd, objects, names_fn, h)
    exe.facts.update(objects=len(objects), pages=hd["num_pages"], page_size=page_size,
                     cpu=CPU.get(hd["cpu_type"], hd["cpu_type"]), os=OS.get(hd["os_type"], hd["os_type"]))
    stack_linear, _ = locate(hd["stack_obj"], hd["esp"])
    if stack_linear is not None:
        exe.facts["initial_esp"] = f"object {hd['stack_obj']}:{h(hd['esp'], 8)} (linear {h(stack_linear, 8)})"
    if hd["debug_off"] and hd["debug_len"]:
        exe.facts["le_debug_info"] = f"file {h(file_base + hd['debug_off'], 8)}, {hd['debug_len']:,} bytes"
    pages_end = max((r.offset + r.size for o in objects for r in o.runs), default=0)
    exe.facts["data_pages_end"] = pages_end


def _fixups(data: bytes, exe, le: int, hd: dict, objects: list[Object], h, is_lx: bool) -> None:
    """Every fixup: where (linear address and file offset), what kind, and what it points to."""
    if not hd["fixpage_off"] or not hd["num_pages"]:
        return
    page_size = hd["page_size"] or 4096
    page_to_obj: dict[int, tuple[Object, int]] = {}  # page number (1-based, logical) -> (object, index in it)
    for o in objects:
        rec_at = le + hd["objtab_off"] + 24 * (o.number - 1)
        mapidx, mapsize = struct.unpack_from("<II", data, rec_at + 12)
        for p in range(mapsize):
            page_to_obj[mapidx + p] = (o, p)
    table = le + hd["fixpage_off"]
    records = le + hd["fixrec_off"]
    lines = ["# source linear  file offset  type                  -> target"]
    counts: dict[str, int] = {}
    for page in range(1, hd["num_pages"] + 1):
        se = struct.unpack_from("<II", data, table + 4 * (page - 1)) if table + 4 * page + 4 <= len(data) else None
        if se is None:
            break
        at, end = records + se[0], records + se[1]
        owner = page_to_obj.get(page)
        while at < end and at + 2 <= len(data) and len(lines) <= MAX_FIXUPS:
            src, flags = data[at], data[at + 1]
            at += 2
            listed = bool(src & 0x20)
            if listed:
                count = data[at]
                at += 1
                sources = []
            else:
                sources = [struct.unpack_from("<h", data, at)[0]]
                at += 2
            kind = flags & 0x03
            if kind == 0:  # internal: object, offset
                if flags & 0x40:
                    obj = struct.unpack_from("<H", data, at)[0]
                    at += 2
                else:
                    obj = data[at]
                    at += 1
                target_off = None
                if (src & 0x0F) != 2:  # a selector fixup has no offset
                    if flags & 0x10:
                        target_off = struct.unpack_from("<I", data, at)[0]
                        at += 4
                    else:
                        target_off = struct.unpack_from("<H", data, at)[0]
                        at += 2
                if 0 < obj <= len(objects) and target_off is not None:
                    t = objects[obj - 1]
                    target = f"object {obj}:{h(target_off, 8)} (linear {h(t.base + target_off, 8)}, {t.kind})"
                else:
                    target = f"object {obj}" + (f":{h(target_off, 8)}" if target_off is not None else " (selector)")
            elif kind in (1, 2):  # import by ordinal or by name
                if flags & 0x40:
                    mod = struct.unpack_from("<H", data, at)[0]
                    at += 2
                else:
                    mod = data[at]
                    at += 1
                if kind == 1:
                    if flags & 0x80:
                        what, at = data[at], at + 1
                    elif flags & 0x10:
                        what, at = struct.unpack_from("<I", data, at)[0], at + 4
                    else:
                        what, at = struct.unpack_from("<H", data, at)[0], at + 2
                    target = f"import module {mod} ordinal {what}"
                else:
                    if flags & 0x10:
                        what, at = struct.unpack_from("<I", data, at)[0], at + 4
                    else:
                        what, at = struct.unpack_from("<H", data, at)[0], at + 2
                    target = f"import module {mod} name at {h(what)}"
            else:  # internal via the entry table
                if flags & 0x40:
                    what, at = struct.unpack_from("<H", data, at)[0], at + 2
                else:
                    what, at = data[at], at + 1
                target = f"entry {what}"
            if flags & 0x04:  # additive
                if flags & 0x20:
                    add, at = struct.unpack_from("<I", data, at)[0], at + 4
                else:
                    add, at = struct.unpack_from("<H", data, at)[0], at + 2
                target += f" + {h(add)}"
            if listed:
                sources = list(struct.unpack_from(f"<{count}h", data, at))
                at += 2 * count
            name = SOURCES.get(src & 0x0F, f"type {src & 0x0F}")
            counts[name] = counts.get(name, 0) + len(sources)
            for s in sources:
                if owner:
                    o, idx = owner
                    linear = o.base + idx * page_size + s
                    off = o.file_offset(idx * page_size + s)
                    where = f"{h(linear, 8)}       {h(off, 8) if off is not None else '-':<10}"
                else:
                    where = f"page {page}+{h(s, 4)}  {'-':<10}"
                lines.append(f"{where}  {name:<20}  -> {target}")
    if len(lines) > MAX_FIXUPS:
        exe.warnings.append(f"Only the first {MAX_FIXUPS:,} fixups are listed.")
    exe.tables["fixups"] = lines
    exe.facts["fixups"] = len(lines) - 1
    exe.facts["fixup_types"] = counts


def _exports(data: bytes, exe, le: int, file_base: int, hd: dict, objects: list[Object], names_fn, h) -> None:
    """Entry table (ordinal -> object:offset) with names from the resident and non-resident tables."""
    from .formats import Export

    resident = names_fn(data, le + hd["resname_off"], le + hd["entry_off"])
    nonres = names_fn(data, file_base + hd["nonres_off"], file_base + hd["nonres_off"] + hd["nonres_size"]) \
        if hd["nonres_off"] else []
    if resident:
        exe.facts["module_name"] = resident[0][0]
    names: dict[int, tuple[str, bool]] = {}
    for table, res in ((resident[1:], True), (nonres[1:], False)):
        for name, ordinal in table:
            names.setdefault(ordinal, (name, res))
    at, ordinal = le + hd["entry_off"], 1
    entries: dict[int, tuple[int, int]] = {}
    while at + 2 <= len(data):
        count, kind = data[at], data[at + 1]
        at += 2
        if count == 0:
            break
        if kind == 0:
            ordinal += count
            continue
        obj = struct.unpack_from("<H", data, at)[0] if kind != 4 else 0
        at += 2
        width = {1: 3, 2: 5, 3: 5, 4: 7}.get(kind)
        if width is None:
            break
        for _ in range(count):
            if at + width > len(data):
                break
            if kind == 1:
                off = struct.unpack_from("<H", data, at + 1)[0]
            elif kind == 2:
                off = struct.unpack_from("<H", data, at + 1)[0]
            elif kind == 3:
                off = struct.unpack_from("<I", data, at + 1)[0]
            else:
                off = None
            if off is not None:
                entries[ordinal] = (obj, off)
            ordinal, at = ordinal + 1, at + width
    for ordinal in sorted(set(entries) | set(names)):
        name, res = names.get(ordinal, ("", True))
        obj, off = entries.get(ordinal, (0, 0))
        if 0 < obj <= len(objects):
            o = objects[obj - 1]
            exe.exports.append(Export(ordinal, name, o.file_offset(off), f"object {obj}:{h(off, 8)} "
                                                                         f"(linear {h(o.base + off, 8)})",
                                      resident=res))
        else:
            exe.exports.append(Export(ordinal, name, None, "no entry point", resident=res))
    if exe.exports:
        exe.facts["exports"] = len(exe.exports)

"""Watcom debug information (versions 2 and 3): what Watcom C/C++ and the Watcom linker append to a
program built with debug info (``debug all``).

Layout, from the Open Watcom sources (bld/watcom/h/wdbginfo.h and the debugger's reader, bld/dip/watcom):

    ... program ...
    source language table        language names ("C\\0")
    segment address table        the segments (LE objects) addresses refer to
    section(s): header, then demand-loaded locals / types / lines, then modules, globals, address map
    master header (14 bytes)     signature 0x8386, versions, sizes; the last bytes of the file

* **Modules**: one per object file (source file or library module), with offsets to its demand-loaded
  locals, types and line numbers (a link table of block offsets, from the section start).
* **Globals**: every public function and variable: name, segment:offset, module, kind (code/data/static).
* **Address map**: per segment, consecutive ranges and the module each belongs to.
* **Locals** (per module): module variables, routines (address range, prologue/epilogue sizes, return
  type, parameter locations), nested blocks, local variables with their location (a small stack-machine
  expression: [EBP-0x34], a register, an address...).
* **Types** (per module): scalars, typedefs, pointers, arrays, subranges, enums with their constants,
  structs/unions with their fields (offset, bit fields, type), procedures (return and parameter types).
* **Lines**: line number -> code offset, per address range.

Records that this decoder doesn't know are kept raw (hex), so nothing in the file is lost.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

SIGNATURE = 0x8386
MASTER = struct.Struct("<HBBBBHHI")  # master_dbg_header
REGS = ("AL", "AH", "BL", "BH", "CL", "CH", "DL", "DH", "AX", "BX", "CX", "DX", "SI", "DI", "BP", "SP", "CS", "SS",
        "DS", "ES", "ST0", "ST1", "ST2", "ST3", "ST4", "ST5", "ST6", "ST7", "EAX", "EBX", "ECX", "EDX", "ESI", "EDI",
        "EBP", "ESP", "FS", "GS")
GLOBAL_KINDS = {0x1: "static", 0x2: "data", 0x4: "code"}
OPERATORS = ("ind2", "ind4", "ind_addr286", "ind_addr386", "zero-extend byte", "zero-extend word", "make far ptr",
             "pop", "xchg", "add", "dup", "nop")
SCALAR_CLASS = {0x00: "int", 0x10: "unsigned", 0x20: "float", 0x30: "void"}
POINTERS = ("near", "far", "huge", "near deref", "far deref", "huge deref", "near386", "far386", "near386 deref",
            "far386 deref")


@dataclass
class Global:
    name: str
    segment: int
    offset: int
    module: int  # index into modules
    kind: int

    @property
    def kind_text(self) -> str:
        return ", ".join(n for b, n in GLOBAL_KINDS.items() if self.kind & b) or "-"


@dataclass
class Range:
    segment: int
    start: int  # offset in the segment
    size: int
    module: int


@dataclass
class Symbol:
    """A record of the locals section."""

    kind: str  # "module variable", "local", "routine", "block", "member scope", "base", "unknown"
    name: str = ""
    type: int = 0  # type index (in the module's types)
    segment: int | None = None
    offset: int | None = None  # absolute in the segment (routines/blocks: their start)
    size: int | None = None
    location: str = ""  # local variables, return value
    parent: int | None = None  # routines/blocks: offset of the enclosing block record (0 = none)
    record_offset: int = 0  # where the record is in the module's locals (blocks refer to it)
    prologue: int | None = None
    epilogue: int | None = None
    return_address: int | None = None  # offset of the return address from the frame
    parameters: list[str] = field(default_factory=list)  # parameter locations
    raw: str = ""  # hex, for records not decoded
    far: bool = False


@dataclass
class TypeRecord:
    index: int | None  # None: struct fields, enum constants, cue tables (no index of their own)
    kind: str
    name: str = ""
    detail: str = ""
    refs: dict = field(default_factory=dict)  # e.g. {"base": 12}, {"return": 3, "params": [4, 5]}
    members: list = field(default_factory=list)  # struct fields / enum constants: (name, detail, type index)
    raw: str = ""


@dataclass
class Line:
    line: int
    segment: int
    offset: int


@dataclass
class Module:
    index: int
    name: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    types: list[TypeRecord] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    ranges: list[Range] = field(default_factory=list)

    def type_name(self, index: int, depth: int = 0) -> str:
        """A readable name for type ``index``: "int", "struct foo", "char *", "short[10]"..."""
        if not index:
            return ""
        by = self._by_index()
        t = by.get(index)
        if t is None or depth > 8:
            return f"type {index}"
        if t.kind == "typedef" and t.refs.get("scope"):  # a tag: "struct foo"
            scope = by.get(t.refs["scope"])
            return f"{scope.name} {t.name}" if scope else t.name
        if t.kind in ("scalar", "typedef", "scope") and t.name:
            return t.name
        if t.kind == "pointer":
            return f"{self.type_name(t.refs.get('base', 0), depth + 1) or 'void'} *"
        if t.kind == "array":
            return f"{self.type_name(t.refs.get('base', 0), depth + 1)}[{t.refs.get('count', '?')}]"
        if t.kind in ("struct", "enum"):
            return t.name or f"{t.kind} #{index}"
        if t.kind == "procedure":
            params = ", ".join(self.type_name(p, depth + 1) or f"type {p}" for p in t.refs.get("params", []))
            return f"{self.type_name(t.refs.get('return', 0), depth + 1) or 'void'} ({params or 'void'})"
        if t.kind == "subrange":
            return f"{self.type_name(t.refs.get('base', 0), depth + 1)} {t.detail}"
        return f"{t.kind} #{index}"

    def _by_index(self) -> dict[int, TypeRecord]:
        cache = self.__dict__.get("_types_by_index")
        if cache is None:
            cache = {t.index: t for t in self.types if t.index is not None}
            self.__dict__["_types_by_index"] = cache
        return cache


@dataclass
class DebugInfo:
    start: int  # file offset of the debug info
    size: int
    version: str
    languages: list[str]
    segments: list[int]
    modules: list[Module] = field(default_factory=list)
    globals: list[Global] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find(data: bytes) -> tuple[int, int] | None:
    """(start, size) of Watcom debug info at the end of the file, if the master header is there."""
    if len(data) < MASTER.size:
        return None
    sig, major, _minor, _omaj, _omin, _lang, _seg, size = MASTER.unpack_from(data, len(data) - MASTER.size)
    if sig != SIGNATURE or major not in (2, 3) or not MASTER.size < size <= len(data):
        return None
    return len(data) - size, size


def parse(data: bytes) -> DebugInfo | None:
    found = find(data)
    if not found:
        return None
    start, size = found
    _, major, minor, omaj, omin, lang_size, seg_size, _ = MASTER.unpack_from(data, len(data) - MASTER.size)
    langs = [s.decode("latin-1") for s in data[start:start + lang_size].split(b"\0") if s]
    seg_at = start + lang_size
    segments = list(struct.unpack_from(f"<{seg_size // 2}H", data, seg_at))
    info = DebugInfo(start, size, f"{major}.{minor} (objects {omaj}.{omin})", langs, segments)
    if major != 3:
        info.warnings.append(f"Watcom debug info version {major}: only the version 3 layout is decoded.")
        return info
    sec, end = seg_at + seg_size, len(data) - MASTER.size
    while sec + 18 <= end:
        mod_off, gbl_off, addr_off, sec_size, _sec_id = struct.unpack_from("<IIIIH", data, sec)
        if not sec_size or sec + sec_size > end:
            break
        _section(data, info, sec, mod_off, gbl_off, addr_off, sec_size, lang_size, start)
        sec += sec_size
    return info


def _section(data, info: DebugInfo, sec: int, mod_off: int, gbl_off: int, addr_off: int, sec_size: int,
             lang_size: int, start: int) -> None:
    first = len(info.modules)
    demand = []
    at = sec + mod_off
    while at + 21 <= sec + gbl_off:
        language, *di = struct.unpack_from("<HIHIHIH", data, at)
        n = data[at + 20]
        name = data[at + 21:at + 21 + n].decode("latin-1")
        lang = data[start + language:data.find(b"\0", start + language, start + lang_size)].decode("latin-1") \
            if language < lang_size else ""
        info.modules.append(Module(len(info.modules), name, lang))
        demand.append(di)
        at += 21 + n
    at = sec + gbl_off
    while at + 10 <= sec + addr_off:
        off, seg, mod, kind = struct.unpack_from("<IHHB", data, at)
        n = data[at + 9]
        info.globals.append(Global(data[at + 10:at + 10 + n].decode("latin-1"), seg, off, first + mod, kind))
        at += 10 + n
    # Address map; line numbers refer to its entries by their offset in this section part.
    entry_at: dict[int, Range] = {}
    at = sec + addr_off
    while at + 8 <= sec + sec_size:
        off, seg, count = struct.unpack_from("<IHH", data, at)
        at += 8
        pos = off
        for _ in range(count & 0x7FFF):
            if at + 6 > sec + sec_size:
                break
            size, mod = struct.unpack_from("<IH", data, at)
            r = Range(seg, pos, size, first + mod)
            entry_at[at - (sec + addr_off)] = r
            if first + mod < len(info.modules):
                info.modules[first + mod].ranges.append(r)
            pos += size
            at += 6
    for m, di in zip(info.modules[first:], demand):
        for kind, (link_off, entries) in zip(("locals", "types", "lines"), zip(di[0::2], di[1::2])):
            for block in _blocks(data, sec, link_off, entries):
                try:
                    if kind == "locals":
                        m.symbols += _locals(block, m)
                    elif kind == "types":
                        m.types += _types(block, len([t for t in m.types if t.index is not None]))
                    else:
                        m.lines += _lines(block, entry_at)
                except (struct.error, IndexError, ValueError, KeyError) as exc:
                    info.warnings.append(f"{m.name}: {kind} cut short ({exc}).")


def _blocks(data: bytes, sec: int, link_off: int, entries: int) -> list[bytes]:
    """The demand-loaded blocks of one kind of a module: a link table of offsets (from the section
    start), one more than there are blocks, so each block ends where the next begins."""
    if not entries:
        return []
    links = struct.unpack_from(f"<{entries + 1}I", data, sec + link_off)
    return [data[sec + a:sec + b] for a, b in zip(links, links[1:]) if b > a]


def _index(b: bytes, at: int) -> tuple[int, int]:
    """A type index: one byte, or two when the high bit is set (GetIndex)."""
    v = b[at]
    if v & 0x80:
        return ((v & 0x7F) << 8) | b[at + 1], at + 2
    return v, at + 1


def location(b: bytes, at: int) -> tuple[str, int]:
    """A location expression as text, and where it ends."""
    first = b[at]
    if first & 0x80:  # an expression of several entries
        end = at + (first & 0x7F)
        parts, at = [], at + 1
        while at < end:
            text, at = _loc_entry(b, at)
            parts.append(text)
        return " ".join(parts) or "none", end
    return _loc_entry(b, at)


def _loc_entry(b: bytes, at: int) -> tuple[str, int]:
    c = b[at]
    cls, sub = c & 0xF0, c & 0x0F
    at += 1
    if cls == 0x00:
        return "none", at
    if cls == 0x10:  # BP offset
        fmt, n = {0: ("<b", 1), 1: ("<h", 2), 2: ("<i", 4)}.get(sub, ("<b", 1))
        v = struct.unpack_from(fmt, b, at)[0]
        return f"[EBP{'+' if v >= 0 else '-'}0x{abs(v):X}]", at + n
    if cls == 0x20:  # constant / address
        if sub == 0:
            off, seg = struct.unpack_from("<HH", b, at)
            return f"address {seg}:0x{off:04X}", at + 4
        if sub == 1:
            off, seg = struct.unpack_from("<IH", b, at)
            return f"address {seg}:0x{off:08X}", at + 6
        fmt, n = {2: ("<b", 1), 3: ("<h", 2), 4: ("<i", 4)}.get(sub, ("<b", 1))
        return f"constant {struct.unpack_from(fmt, b, at)[0]}", at + n
    if cls == 0x30:  # several registers, high first
        regs = [_reg(r) for r in b[at:at + sub + 1]]
        return ":".join(regs), at + sub + 1
    if cls == 0x40:
        return _reg(sub), at
    if cls == 0x50:  # indirect through a register
        if sub in (0, 2):
            return f"[{_reg(b[at])}]", at + 1
        return f"[{_reg(b[at + 1])}:{_reg(b[at])}]", at + 2
    if cls == 0x60:
        name = OPERATORS[sub] if sub < len(OPERATORS) else f"op {sub}"
        if sub == 8:
            return f"{name} {b[at]}", at + 1
        return name, at
    return f"?{c:02X}", at


def _reg(i: int) -> str:
    return REGS[i] if i < len(REGS) else f"reg{i}"


def _locals(b: bytes, module: Module) -> list[Symbol]:
    out = []
    base: tuple[int, int] | None = None  # (segment, offset) set by a NEW_BASE record
    at = 0
    while at + 2 <= len(b):
        length, cls = b[at], b[at + 1]
        if length < 2:
            break
        rec, p = b[at:at + length], 2
        s = Symbol("unknown", record_offset=at)
        kind = cls & 0xF0
        try:
            if cls == 0x10:  # module variable, 16-bit address
                s.offset, s.segment = struct.unpack_from("<HH", rec, p)
                s.kind, (s.type, p) = "module variable", _index(rec, p + 4)
            elif cls == 0x12:  # module variable, 32-bit address
                s.offset, s.segment = struct.unpack_from("<IH", rec, p)
                s.kind, (s.type, p) = "module variable", _index(rec, p + 6)
            elif cls in (0x11, 0x13):  # local / module variable at a location
                s.location, p = location(rec, p)
                s.kind, (s.type, p) = ("local" if cls == 0x11 else "module variable"), _index(rec, p)
            elif kind == 0x20 and cls != 0x26:  # blocks and routines
                wide = cls >= 0x23
                if wide:
                    start, s.size, s.parent = struct.unpack_from("<IIH", rec, p)
                    p += 10
                else:
                    start, s.size, s.parent = struct.unpack_from("<HHH", rec, p)
                    p += 6
                seg, off = base if base else _module_base(module)
                s.segment, s.offset = seg, off + start
                s.kind = "block"
                if cls in (0x21, 0x22, 0x24, 0x25):
                    s.kind, s.far = "routine", cls in (0x22, 0x25)
                    s.prologue, s.epilogue = rec[p], rec[p + 1]
                    p += 2
                    if wide:
                        s.return_address = struct.unpack_from("<I", rec, p)[0]
                        p += 4
                    else:
                        s.return_address = struct.unpack_from("<H", rec, p)[0]
                        p += 2
                    s.type, p = _index(rec, p)
                    s.location, p = location(rec, p)  # where the return value is
                    count = rec[p]
                    p += 1
                    for _ in range(count):
                        loc, p = location(rec, p)
                        s.parameters.append(loc)
            elif cls == 0x26:  # member scope (C++)
                s.parent = struct.unpack_from("<H", rec, p)[0]
                s.kind, (s.type, p) = "member scope", _index(rec, p + 2)
                if p < len(rec):
                    s.location, p = location(rec, p + 1)
            elif kind == 0x30:  # a new code base for the records that follow
                sub = cls & 0x0F
                if sub == 0 and base:
                    base = (base[0] + struct.unpack_from("<H", rec, p)[0], 0)
                elif sub == 1:
                    off, seg = struct.unpack_from("<HH", rec, p)
                    base = (seg, off)
                elif sub == 2:
                    off, seg = struct.unpack_from("<IH", rec, p)
                    base = (seg, off)
                s.kind = "base"
                s.segment, s.offset = base if base else (None, None)
                p = length
            else:
                s.raw = rec.hex(" ")
                p = length
            s.name = rec[p:length].decode("latin-1") if p < length else ""
        except (struct.error, IndexError, ValueError, KeyError):  # a malformed record: kept raw
            s.kind, s.raw, s.name = "unknown", rec.hex(" "), ""
        out.append(s)
        at += length
    return out


def _module_base(module: Module) -> tuple[int, int]:
    """Where a module's code starts (its first code range), for blocks before any NEW_BASE record."""
    r = module.ranges[0] if module.ranges else None
    return (r.segment, r.start) if r else (0, 0)


def _types(b: bytes, numbered: int) -> list[TypeRecord]:
    """Type records; ``numbered`` is how many indexed records the module had before this block."""
    out = []
    at, index = 0, numbered
    open_list: TypeRecord | None = None  # the struct/enum whose fields/constants follow
    while at + 2 <= len(b):
        length, cls = b[at], b[at + 1]
        if length < 2:
            break
        rec = b[at:at + length]
        kind, sub = cls & 0xF0, cls & 0x0F
        member = (kind == 0x60 and sub != 0) or (kind == 0x50 and sub != 0)
        has_index = not member and cls != 0x13 and cls != 0x14
        if has_index:
            index += 1
        t = TypeRecord(index if has_index else None, "unknown")
        try:
            _type_record(rec, cls, t)
        except (struct.error, IndexError, ValueError, KeyError):  # a malformed record: kept raw
            t.kind, t.raw = "unknown", rec.hex(" ")
        if member and open_list is not None:
            open_list.members.append((t.name, t.detail, t.refs.get("type", 0)))
        else:
            out.append(t)
            open_list = t if t.kind in ("struct", "enum") else None
        if cls == 0x14:  # end of the type information
            break
        at += length
    return out


def _type_record(rec: bytes, cls: int, t: TypeRecord) -> None:
    kind, sub, p = cls & 0xF0, cls & 0x0F, 2
    if kind == 0x10:
        if sub == 0:  # scalar: class and size, then its name
            sc = rec[2]
            t.kind, t.detail = "scalar", f"{SCALAR_CLASS.get(sc & 0x70, 'scalar')} {(sc & 0x0F) + 1} bytes"
            t.name = rec[3:].decode("latin-1")
        elif sub == 1:  # scope: "struct", "union", "enum", "class"
            t.kind, t.name = "scope", rec[2:].decode("latin-1")
        elif sub == 2:  # a name for a type (typedef, or a tag within a scope)
            scope, p = _index(rec, 2)
            base, p = _index(rec, p)
            t.kind, t.name, t.refs = "typedef", rec[p:].decode("latin-1"), {"scope": scope, "base": base}
        elif sub == 3:
            t.kind, t.detail, t.raw = "cue table", "source file table (C++)", rec.hex(" ")
        elif sub == 4:
            t.kind = "end"
    elif kind == 0x20:  # array
        if sub in (0, 1, 2):
            fmt, n = (("<B", 1), ("<H", 2), ("<I", 4))[sub]
            high = struct.unpack_from(fmt, rec, 2)[0]
            base, _ = _index(rec, 2 + n)
            t.kind, t.refs, t.detail = "array", {"base": base, "count": high + 1}, f"0..{high}"
        elif sub == 3:
            idx, p = _index(rec, 2)
            base, _ = _index(rec, p)
            t.kind, t.refs, t.detail = "array", {"base": base, "index": idx}, f"index type {idx}"
        else:
            t.kind, t.raw = "array (descriptor)", rec.hex(" ")
    elif kind == 0x30:  # subrange
        fmt, n = {0: ("<bb", 2), 1: ("<hh", 4), 2: ("<ii", 8)}[sub]
        lo, hi = struct.unpack_from(fmt, rec, 2)
        base, _ = _index(rec, 2 + n)
        t.kind, t.refs, t.detail = "subrange", {"base": base, "low": lo, "high": hi}, f"{lo}..{hi}"
    elif kind == 0x40:  # pointer
        base, p = _index(rec, 2)
        t.kind, t.refs, t.detail = "pointer", {"base": base}, POINTERS[sub] if sub < len(POINTERS) else str(sub)
    elif kind == 0x50:
        if sub == 0:  # enum list: count, scalar type
            count, sc = struct.unpack_from("<HB", rec, 2)
            t.kind, t.detail = "enum", f"{count} constants, {SCALAR_CLASS.get(sc & 0x70, '')} {(sc & 0x0F) + 1} bytes"
        else:
            fmt, n = {1: ("<b", 1), 2: ("<h", 2), 3: ("<i", 4), 4: ("<q", 8)}[sub]
            t.kind = "constant"
            t.detail = str(struct.unpack_from(fmt, rec, 2)[0])
            t.name = rec[2 + n:].decode("latin-1")
    elif kind == 0x60:
        if sub == 0:  # struct list: field count, size (newer objects)
            count = struct.unpack_from("<H", rec, 2)[0]
            size = struct.unpack_from("<I", rec, 4)[0] if rec[0] > 4 else None  # absent in older objects
            t.kind, t.detail = "struct", f"{count} fields" + (f", {size} bytes" if size is not None else "")
            t.refs = {"size": size}
        elif sub in (1, 2, 3, 4, 5, 6):  # field / bit field at an offset of 1, 2 or 4 bytes
            width = {1: 1, 2: 2, 3: 4, 4: 1, 5: 2, 6: 4}[sub]
            off = int.from_bytes(rec[2:2 + width], "little")
            p = 2 + width
            detail = f"offset {off}"
            if sub >= 4:
                detail += f", bits {rec[p]}..{rec[p] + rec[p + 1] - 1}"
                p += 2
            typ, p = _index(rec, p)
            t.kind, t.detail, t.refs, t.name = "field", detail, {"type": typ}, rec[p:].decode("latin-1")
        elif sub in (7, 8):  # field at a location (attributes byte, location)
            loc, p = location(rec, 3)
            detail = f"at {loc}, attributes 0x{rec[2]:02X}"
            if sub == 8:
                detail += f", bits {rec[p]}..{rec[p] + rec[p + 1] - 1}"
                p += 2
            typ, p = _index(rec, p)
            t.kind, t.detail, t.refs, t.name = "field", detail, {"type": typ}, rec[p:].decode("latin-1")
        elif sub == 9:  # inherited base class
            loc, p = location(rec, 2)
            typ, p = _index(rec, p)
            t.kind, t.detail, t.refs = "field", f"base class at {loc}", {"type": typ}
            t.name = "(base class)"
    elif kind == 0x70:  # procedure: return type, parameter types
        ret, p = _index(rec, 2)
        count = rec[p]
        p += 1
        params = []
        while len(params) < count and p < len(rec):
            v, p = _index(rec, p)
            params.append(v)
        t.kind = "procedure"
        t.refs = {"return": ret, "params": params}
        t.detail = ("near", "far", "near386", "far386", "ext parms")[sub] if sub < 5 else str(sub)
        if len(params) < count:
            t.detail += f", {count} parameters (rest in an extension record)"
    elif kind == 0x80:  # character block (Fortran)
        t.kind, t.raw = "character block", rec.hex(" ")
    else:
        t.raw = rec.hex(" ")


def _lines(b: bytes, entry_at: dict[int, Range]) -> list[Line]:
    out, at = [], 0
    while at + 6 <= len(b):
        seg_ref, count = struct.unpack_from("<IH", b, at)
        at += 6
        r = entry_at.get(seg_ref)
        for _ in range(count):
            if at + 6 > len(b):
                break
            line, off = struct.unpack_from("<HI", b, at)
            at += 6
            out.append(Line(line, r.segment if r else 0, (r.start if r else 0) + off))
    return out

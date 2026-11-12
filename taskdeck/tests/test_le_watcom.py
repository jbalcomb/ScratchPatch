"""Programs bound to a DOS extender (LE inside), and Watcom debug info.

The synthetic file has the shape of a DOS/4GW game: a DOS loader, the extender ("BW"), the program's own
DOS stub pointing at its LE header, two objects (code, data), one fixup, and Watcom debug info with a
module variable, a routine with a parameter and a local, types (a typedef, a struct, a procedure) and
line numbers. ORION2.EXE (Master of Orion II), the real thing, is tested when TASKDECK_ORION2 names it.
"""

import json
import os
import struct
from pathlib import Path

import pytest

from taskdeck.jobs import exe_parse
from taskdeck.shared.binaries import formats, watcom

ORION2 = Path(os.environ.get("TASKDECK_ORION2", "/nonexistent/ORION2.EXE"))


def rec(cls: int, body: bytes) -> bytes:
    return bytes([len(body) + 2, cls]) + body


def debug_block() -> bytes:
    """Watcom debug info v3 for: module 0 "test.c" (locals, types, lines) and module 1 "lib.c"."""
    locals_ = (rec(0x12, struct.pack("<IH", 0x40, 2) + b"\x03counter")  # module variable, type 3
               + rec(0x32, struct.pack("<IH", 0x20, 1))  # code base 1:0x20
               + rec(0x24, struct.pack("<IIHBBI", 0, 0x30, 0, 3, 1, 4) + b"\x04" + b"\x30\x1c" + b"\x01" + b"\x30\x1c"
                     + b"start")  # routine: type 4, returns in EAX, 1 parameter in EAX
               + rec(0x11, b"\x10\xf8" + b"\x03" + b"i"))  # local at [EBP-8], type 3
    types = (rec(0x11, b"struct") + rec(0x10, b"\x03int") + rec(0x12, b"\x00\x02count_t")
             + rec(0x72, b"\x02\x01\x02")  # near386 procedure: returns int, (int)
             + rec(0x60, struct.pack("<HI", 2, 8)) + rec(0x61, b"\x04\x02y") + rec(0x61, b"\x00\x02x")
             + rec(0x12, b"\x01\x05point"))  # struct point = type 5
    lines = struct.pack("<IH", 8, 2) + struct.pack("<HI", 10, 0) + struct.pack("<HI", 12, 8)
    body = bytearray()
    at = 18  # after the section header
    pos = {}
    for key, block in (("locals", locals_), ("types", types), ("lines", lines)):
        pos[key] = at + len(body)
        body += block
    links = {}
    for key, block in (("locals", locals_), ("types", types), ("lines", lines)):
        links[key] = at + len(body)
        body += struct.pack("<II", pos[key], pos[key] + len(block))
    mod_off = at + len(body)
    body += struct.pack("<HIHIHIH", 0, links["locals"], 1, links["types"], 1, links["lines"], 1) + b"\x06test.c"
    body += struct.pack("<HIHIHIH", 0, 0, 0, 0, 0, 0, 0) + b"\x05lib.c"
    gbl_off = at + len(body)
    body += struct.pack("<IHHB", 0x20, 1, 0, 4) + b"\x06start_"
    body += struct.pack("<IHHB", 0x40, 2, 0, 2) + b"\x08_counter"
    addr_off = at + len(body)
    body += struct.pack("<IHH", 0, 1, 2) + struct.pack("<IH", 0x30, 0) + struct.pack("<IH", 0xD0, 1)
    body += struct.pack("<IHH", 0, 2, 1) + struct.pack("<IH", 0x80, 0)
    section = struct.pack("<IIIIH", mod_off, gbl_off, addr_off, at + len(body), 0) + bytes(body)
    head = b"C\x00" + struct.pack("<HH", 1, 2)
    total = len(head) + len(section) + 14
    return head + section + struct.pack("<HBBBBHHI", 0x8386, 3, 0, 1, 3, 2, 4, total)


def bound_program(with_debug: bool = True) -> bytes:
    data = bytearray(0x380)
    # the DOS loader: 32-byte header, 64 bytes of code naming the extender
    struct.pack_into("<2s13H", data, 0, b"MZ", 0x60, 1, 0, 2, 0, 0xFFFF, 0, 0x100, 0, 0, 0, 0x1C, 0)
    data[0x30:0x37] = b"DOS/4GW"
    data[0x60:0x62] = b"BW"  # the extender (DOS/16M format)
    stub, le = 0xA0, 0xE0
    data[stub:stub + 2] = b"MZ"
    struct.pack_into("<I", data, stub + 0x3C, le - stub)
    values = dict(signature=b"LE", byte_order=0, word_order=0, level=0, cpu_type=2, os_type=1, version=0, flags=0x200,
                  num_pages=2, start_obj=1, eip=0x20, stack_obj=2, esp=0x200, page_size=0x100,
                  last_page_or_shift=0x80, fixup_size=0x15, fixup_cksum=0, loader_size=0x41, loader_cksum=0,
                  objtab_off=0xC4, num_objects=2, objmap_off=0xF4, idmap_off=0, rsrc_off=0, num_rsrcs=0,
                  resname_off=0xFC, entry_off=0x104, moddir_off=0, num_moddirs=0, fixpage_off=0x105,
                  fixrec_off=0x111, impmod_off=0, num_impmods=0, impproc_off=0, cksum_off=0, page_off=0x200 - stub,
                  num_preload=0, nonres_off=0, nonres_size=0, nonres_cksum=0, autodata_obj=0, debug_off=0, debug_len=0)
    from taskdeck.shared.binaries.le import HEADER

    struct.pack_into("<" + "".join(c for _, c in HEADER), data, le, *(values[n] for n, _ in HEADER))
    struct.pack_into("<6I", data, le + 0xC4, 0x100, 0x10000, 0x2005, 1, 1, 0)  # object 1: code
    struct.pack_into("<6I", data, le + 0xC4 + 24, 0x200, 0x20000, 0x2003, 2, 1, 0)  # object 2: data
    data[le + 0xF4:le + 0xFC] = b"\x00\x00\x01\x00\x00\x00\x02\x00"  # page map
    data[le + 0xFC:le + 0x105] = b"\x04TEST\x00\x00\x00" + b"\x00"  # resident names; empty entry table
    struct.pack_into("<3I", data, le + 0x105, 0, 9, 9)  # fixup page table
    struct.pack_into("<BBhBI", data, le + 0x111, 0x07, 0x10, 0x10, 2, 8)  # 32-bit offset at 1:0x10 -> 2:0x8
    struct.pack_into("<I", data, 0x210, 8)
    data[0x220:0x224] = b"\x53\x51\x52\x56"  # start_: push ebx, ecx, edx, esi
    return bytes(data) + (debug_block() if with_debug else b"")


def test_a_bound_program_is_found_inside_the_extender():
    exe = formats.parse(bound_program(with_debug=False))
    assert exe.format == "LE (DOS/4GW)" and exe.description == "32-bit protected-mode DOS program (LE, bound to DOS/4GW)"
    names = [r.name for r in exe.regions]
    assert names[:6] == ["extender loader: MZ header", "extender loader", "DOS/4GW extender", "program: DOS stub",
                         "LE header and loader tables", "LE fixup tables"]
    obj1 = next(r for r in exe.regions if r.name == "object 1")
    assert (obj1.start, obj1.base, obj1.kind) == (0x200, 0x10000, "code")
    obj2 = next(r for r in exe.regions if r.name == "object 2")
    assert (obj2.start, obj2.end) == (0x300, 0x380)  # the last page: 0x80 bytes
    assert exe.entry == (0x220, "object 1:0x00000020 (linear 0x00010020)")
    assert exe.tables["fixups"][1].split() == ["0x00010010", "0x00000210", "32-bit", "offset", "->", "object",
                                               "2:0x00000008", "(linear", "0x00020008,", "data)"]
    assert exe.facts["module_name"] == "TEST" and exe.facts["extender"] == "DOS/4GW"


def test_watcom_debug_info_is_decoded():
    info = watcom.parse(bound_program())
    assert info.version.startswith("3.0") and info.languages == ["C"] and info.segments == [1, 2]
    assert [m.name for m in info.modules] == ["test.c", "lib.c"]
    assert [(g.name, g.segment, g.offset, g.kind_text) for g in info.globals] == [
        ("start_", 1, 0x20, "code"), ("_counter", 2, 0x40, "data")]
    m = info.modules[0]
    kinds = [(s.kind, s.name) for s in m.symbols]
    assert kinds == [("module variable", "counter"), ("base", ""), ("routine", "start"), ("local", "i")]
    routine = m.symbols[2]
    assert (routine.segment, routine.offset, routine.size, routine.prologue, routine.epilogue) == (1, 0x20, 0x30, 3, 1)
    assert routine.location == "EAX" and routine.parameters == ["EAX"]
    assert m.type_name(routine.type) == "int (int)"
    assert m.symbols[3].location == "[EBP-0x8]" and m.type_name(m.symbols[3].type) == "count_t"
    struct_ = next(t for t in m.types if t.kind == "struct")
    assert struct_.index == 5 and struct_.members == [("y", "offset 4", 2), ("x", "offset 0", 2)]
    assert m.type_name(6) == "struct point"
    assert [(ln.line, ln.segment, ln.offset) for ln in m.lines] == [(10, 1, 0), (12, 1, 8)]
    assert [(r.segment, r.start, r.size) for r in m.ranges] == [(1, 0, 0x30), (2, 0, 0x80)]


def test_the_job_writes_the_debug_info(tmp_path):
    src = tmp_path / "GAME.EXE"
    src.write_bytes(bound_program())
    report = exe_parse.PLUGIN.run({"exe": str(src), "chunk": "1 KB"})
    assert report.ok, report.error
    rows = dict(report.result.rows)
    assert rows["Entry point"] == "object 1:0x00000020 (linear 0x00010020) (start_)"
    assert rows["Debug info"].startswith("Watcom 3.0: 2 symbols, 2 modules")
    out = tmp_path / "_GAME" / "GAME.EXE"
    doc = (out / "debug" / "DEBUG.md").read_text()
    for expected in ("# Debug info of GAME.EXE", "| 1:0x00000020 (linear 0x00010020) | 0x00000220 | code | start_ |",
                     "#### start", "- Type: int (int); returns in EAX", "| i | [EBP-0x8] | count_t |",
                     "##### struct point (#5, 2 fields, 8 bytes)", "| x | offset 0 | int |", "| 12 | 1:0x00000008"):
        assert expected in doc, expected
    assert doc.index("| x | offset 0 |") < doc.index("| y | offset 4 |")  # fields by offset
    assert (out / "debug" / "modules" / "0000-test.c.md").read_text().startswith("# test.c")
    symbols = (out / "debug" / "symbols-001.txt").read_text().splitlines()
    assert symbols[2].split()[:6] == ["1:0x00000020", "(linear", "0x00010020)", "0x00000220", "code", "start_"]
    chunk = next(p for p in (out / "regions").rglob("*.txt") if "object-1" in str(p))
    assert "# symbols: start_ (0x00000220)" in chunk.read_text()
    summary = (out / "SUMMARY.md").read_text()
    assert "## Debug info" in summary and "the entry point is `start_`" in summary
    assert json.loads((out / "index.json").read_text())["debug"]["document"] == "debug/DEBUG.md"
    assert any(r["name"] == "Watcom debug info" for r in json.loads((out / "metadata.json").read_text())["regions"])


def test_a_dos_program_with_debug_info_keeps_it_out_of_the_overlay():
    """A plain DOS program built with Watcom: the debug info at the end is its own region, not overlay."""
    head = struct.pack("<2s13H", b"MZ", 0x60, 1, 0, 2, 0, 0xFFFF, 0, 0x100, 0, 0, 0, 0x1C, 0).ljust(0x20, b"\0")
    data = head + bytes(0x40) + b"OVERLAY!" * 4 + debug_block()
    exe = formats.parse(data)
    names = [r.name for r in exe.regions]
    assert names[-2:] == ["overlay", "Watcom debug info"] and exe.debug is not None
    overlay = exe.regions[-2]
    assert overlay.end - overlay.start == 32


@pytest.mark.parametrize("cut", [20, 100, 600, 900])
def test_damaged_files_never_raise(cut):
    data = bound_program()
    exe = formats.parse(data[:cut] + data[cut + 7:])
    assert exe.regions[-1].end == exe.size


@pytest.mark.skipif(not ORION2.is_file(), reason="set TASKDECK_ORION2 to ORION2.EXE (Master of Orion II)")
def test_orion2():
    exe = formats.parse(ORION2.read_bytes())
    assert exe.format == "LE (DOS/4GW)" and len(exe.objects) == 2 and exe.facts["fixups"] == 51_363
    info = exe.debug
    assert len(info.modules) == 388 and len(info.globals) == 8_598
    assert sum(1 for m in info.modules for s in m.symbols if s.kind == "routine") == 816
    assert not any(s.kind == "unknown" for m in info.modules for s in m.symbols)
    assert not any(t.kind == "unknown" for m in info.modules for t in m.types)
    main = next(g for g in info.globals if g.name == "main_")
    assert info.modules[main.module].name == "L:\\MOX\\MOX.C"

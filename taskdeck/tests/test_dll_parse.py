"""Parse DLL: exports (PE with forwarders and ordinal-only entries, 16-bit NE), and the job."""

import json
import struct
from pathlib import Path

import pytest

from taskdeck.jobs import dll_parse, exe_parse
from taskdeck.shared.binaries import formats, names

JANSI = Path("/opt/apache-maven-3.9.11/lib/jansi-native/Windows/x86_64/jansi.dll")  # a real DLL, if installed


def pe_dll(first: bytes = b"Alpha") -> bytes:
    """A PE32 DLL exporting Alpha (ordinal 1), an unnamed function (2) and Beta, forwarded to
    KERNEL32.HeapAlloc (3), with one base relocation: at RVA 0x1100, an address in .edata."""
    data = bytearray(0x800)
    data[0:2] = b"MZ"
    struct.pack_into("<H", data, 0x18, 0x40)
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x14C, 3, 0, 0, 0, 0xE0, 0x2102)  # 0x2000: DLL
    opt = 0x98
    struct.pack_into("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII", data, opt, 0x10B, 14, 0, 0x200, 0x200, 0, 0x1000, 0x1000,
                     0x2000, 0x10000000, 0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, 0x4000, 0x200, 0, 2, 0,
                     0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    struct.pack_into("<II", data, opt + 96, 0x2000, 0x100)  # export directory
    struct.pack_into("<II", data, opt + 96 + 5 * 8, 0x3000, 12)  # base relocations
    sect = opt + 0xE0
    struct.pack_into("<8sIIIIIIHHI", data, sect, b".text", 0x200, 0x1000, 0x200, 0x200, 0, 0, 0, 0, 0x60000020)
    struct.pack_into("<8sIIIIIIHHI", data, sect + 40, b".edata", 0x200, 0x2000, 0x200, 0x400, 0, 0, 0, 0, 0x40000040)
    struct.pack_into("<8sIIIIIIHHI", data, sect + 80, b".reloc", 0x200, 0x3000, 0x200, 0x600, 0, 0, 0, 0, 0x42000040)
    struct.pack_into("<IIHH", data, 0x600, 0x1000, 12, (3 << 12) | 0x100, 0)  # HIGHLOW at 0x1100, padding
    struct.pack_into("<I", data, 0x300, 0x10002004)  # the address stored there
    e = 0x400  # RVA 0x2000
    struct.pack_into("<IIHHIIIIIII", data, e, 0, 0x5F5E1000, 0, 0, 0x2070, 1, 3, 2, 0x2040, 0x2050, 0x2060)
    struct.pack_into("<III", data, e + 0x40, 0x1010, 0x1020, 0x2080)  # addresses; the last is a forwarder
    struct.pack_into("<II", data, e + 0x50, 0x20A0, 0x20C0)  # names
    struct.pack_into("<HH", data, e + 0x60, 0, 2)  # their indexes into the addresses
    data[e + 0x70:e + 0x79] = b"TEST.DLL\0"
    data[e + 0x80:e + 0x93] = b"KERNEL32.HeapAlloc\0"
    data[e + 0xA0:e + 0xA1 + len(first)] = first + b"\0"
    data[e + 0xC0:e + 0xC5] = b"Beta\0"
    return bytes(data)


def ne_dll() -> bytes:
    """A 16-bit Windows DLL: INIT (ordinal 1, resident) at 1:0004, HELPER (2, non-resident) at 1:0010,
    ordinal 3 unnamed at 1:0020, and a constant (4)."""
    data = bytearray(0x200)
    data[0:2] = b"MZ"
    struct.pack_into("<H", data, 0x18, 0x40)
    struct.pack_into("<I", data, 0x3C, 0x80)
    ne = 0x80
    #                              magic ver rev enttab cbent crc flags  auto heap stk ip  cs sp ss cseg cmod cbnres
    struct.pack_into("<2sBBHHIHHHHHHHHHHHHHHHHIHHHBB", data, ne, b"NE", 5, 1, 0x70, 14, 0, 0x8000, 0, 0, 0,
                     4, 1, 0, 0, 1, 0, 0x14, 0x40, 0x50, 0x50, 0x60, 0x60, 0x150, 0, 4, 0, 2, 0)
    #                segtab rsrc restab modtab imptab nrestab(abs) cmovent align cres exetyp
    struct.pack_into("<HHHH", data, ne + 0x40, 0x18, 0x80, 0, 0x80)  # segment 1 at 0x18 << 4 = 0x180
    data[ne + 0x50:ne + 0x5D] = b"\x04TEST\x00\x00" + b"\x04INIT\x01\x00"
    data[ne + 0x5D] = 0
    # entry table: 3 fixed entries in segment 1, then 1 constant (segment 0xFE)
    data[ne + 0x70:ne + 0x7E] = bytes([3, 1, 1]) + struct.pack("<H", 4) + bytes([1]) + struct.pack("<H", 0x10) + \
        bytes([1]) + struct.pack("<H", 0x20) + bytes([1, 0xFE, 1]) + struct.pack("<H", 0x1234)
    data[0x150:0x164] = b"\x08Test DLL\x00\x00" + b"\x06HELPER\x02\x00"
    return bytes(data)


def test_pe_dll_exports_with_forwarders_and_ordinal_only_entries():
    exe = formats.parse(pe_dll())
    assert exe.dll and exe.format == "PE32" and exe.description.startswith("Windows DLL")
    assert [(e.ordinal, e.name) for e in exe.exports] == [(1, "Alpha"), (2, ""), (3, "Beta")]
    alpha, unnamed, beta = exe.exports
    assert (alpha.rva, alpha.offset, alpha.address) == (0x1010, 0x210, "RVA 0x00001010 (VA 0x10001010)")
    assert unnamed.offset == 0x220
    assert beta.forwarder == "KERNEL32.HeapAlloc" and beta.offset is None
    assert exe.facts["export_dll_name"] == "TEST.DLL" and exe.facts["exports_forwarded"] == 1
    assert exe.entry[1].startswith("DllMain at RVA 0x00001000")


def test_ne_dll_exports_from_the_entry_table_and_both_name_tables():
    exe = formats.parse(ne_dll())
    assert exe.dll and exe.description == "16-bit Windows DLL (NE)"
    got = [(e.ordinal, e.name, e.address, e.offset, e.resident) for e in exe.exports]
    assert got == [(1, "INIT", "segment 1:0004", 0x184, True), (2, "HELPER", "segment 1:0010", 0x190, False),
                   (3, "", "segment 1:0020", 0x1A0, True), (4, "", "constant 1234", None, True)]
    assert exe.facts["module_name"] == "TEST" and exe.facts["description"] == "Test DLL"


@pytest.mark.skipif(not JANSI.is_file(), reason="no real DLL on this machine")
def test_a_real_dll():
    exe = formats.parse(JANSI.read_bytes())  # counts checked against pefile
    assert exe.dll and exe.format == "PE32+" and len(exe.exports) == 41
    assert all(e.name and e.offset for e in exe.exports)
    assert exe.facts["base_relocation_types"] == {"DIR64": 28}


def test_base_relocations_say_where_each_address_points():
    exe = formats.parse(pe_dll())
    assert exe.facts["base_relocations"] == 1 and exe.facts["base_relocation_types"] == {"HIGHLOW": 1}
    assert exe.tables["base relocations"][1].split() == ["0x00001100", "0x00000300", "HIGHLOW", "0x10002004",
                                                         "->", ".edata", "0x00002004"]


@pytest.mark.parametrize("symbol, name", [
    ("?Draw@Window@@QAEXH@Z", "Window::Draw"), ("??0Window@@QAE@XZ", "Window::Window"),
    ("??1Window@@UAE@XZ", "Window::~Window"), ("??4Window@@QAEAAV0@ABV0@@Z", "Window::operator="),
    ("?f@B@A@@QAEXXZ", "A::B::f"), ("??_7Window@@6B@", "Window::`vftable'"),
    ("@Window@Draw$qi", "Window::Draw"), ("@Window@$bctr$qv", "Window::Window"),
    ("@Window@$bdtr$qv", "Window::~Window"), ("@App@Win@$badd$qi", "App::Win::operator+"),
    # not C++, or not decoded (templates, anonymous namespaces): nothing rather than a guess
    ("JNI_OnLoad", ""), ("_DllMain@12", ""), ("??$vector@H@std@@QAE@XZ", ""), ("?x@?A0x1@@3HA", ""), ("?", ""),
    ("@Vec@%T1%@size$qv", ""),
])
def test_cpp_names(symbol, name):
    assert names.readable(symbol) == name


def test_cpp_exports_show_their_readable_name(tmp_path):
    report, out = run(tmp_path, dll_parse.PLUGIN, pe_dll(b"?Draw@Window@@QAEXH@Z"), "WIN.DLL")
    assert report.ok, report.error
    assert "?Draw@Window@@QAEXH@Z  [Window::Draw]" in (out / "exports/exports-001.txt").read_text()
    assert json.loads((out / "exports/exports.json").read_text())["exports"][0]["cpp_name"] == "Window::Draw"
    summary = (out / "SUMMARY.md").read_text()
    assert "| 1 | ?Draw@Window@@QAEXH@Z | Window::Draw |" in summary and "C++ names: 1." in summary


def run(tmp_path, plugin, data, name, **inputs):
    src = tmp_path / name
    src.write_bytes(data)
    return plugin.run({plugin.file_field: str(src), **inputs}), tmp_path / f"_{Path(name).stem}" / name


def test_job_writes_the_exports(tmp_path):
    report, out = run(tmp_path, dll_parse.PLUGIN, pe_dll(), "TEST.DLL", chunk="1 KB")
    assert report.ok, report.error
    rows = dict(report.result.rows)
    assert rows["Exports"] == "3 (2 by name)" and rows["Full path"] == str(out.parent)
    assert report.result.warning is None
    lines = (out / "exports/exports-001.txt").read_text().splitlines()
    assert lines[2].split()[:3] == ["1", "0x00000210", "RVA"] and lines[2].endswith("Alpha  regions/03-.text/00000200-000003FF.txt")
    assert "(by ordinal only)" in lines[3] and "forwarded to KERNEL32.HeapAlloc" in lines[4]
    j = json.loads((out / "exports/exports.json").read_text())
    assert j["dll_name"] == "TEST.DLL" and j["count"] == 3
    assert j["exports"][2] == {"ordinal": 3, "name": "Beta", "file_offset": None,
                               "address": "forwarded to KERNEL32.HeapAlloc", "chunk": None,
                               "forwarder": "KERNEL32.HeapAlloc"}
    summary = (out / "SUMMARY.md").read_text()
    assert "## Exports" in summary and "| 1 | Alpha |" in summary and "1 forwarded" in summary
    assert json.loads((out / "metadata.json").read_text())["dll"] is True
    assert json.loads((out / "index.json").read_text())["exports_json"] == "exports/exports.json"


def test_each_job_says_when_it_got_the_other_kind(tmp_path):
    report, _ = run(tmp_path, dll_parse.PLUGIN, b"MZ" + bytes(200), "PROG.DLL")
    assert report.ok and "isn't marked as a DLL" in report.result.warning
    report, _ = run(tmp_path, exe_parse.PLUGIN, pe_dll(), "LIB.EXE")
    assert report.ok and "This file is a DLL" in report.result.warning


def test_dll_job_shares_the_drive_folder_setting_and_shows_the_path():
    assert dll_parse.PLUGIN.settings_id == "exe_parse"
    assert dll_parse.PLUGIN.field_notes({"dll": "/x/KERNEL.DLL", "drive": True}, {"drive_folder": ""}) == \
        {"drive": "Goes to Google Drive › My Drive › TaskDeck › Executables › _KERNEL", "output": "Saves into /x/_KERNEL"}
    assert dll_parse.PLUGIN.field_notes({}, {})["drive"].endswith("_LIBRARY")
    assert not any(f.name == "dlls" for f in dll_parse.PLUGIN.fields)  # supporting DLLs: Parse executable only


# ---- Parse executable with supporting DLLs --------------------------------------------------------

def pe_importer() -> bytes:
    """A PE32 program importing Alpha, ordinal 2 and Missing from TEST.DLL, and ExitProcess from KERNEL32."""
    data = bytearray(0x600)
    data[0:2] = b"MZ"
    struct.pack_into("<H", data, 0x18, 0x40)
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x14C, 2, 0, 0, 0, 0xE0, 0x102)
    opt = 0x98
    struct.pack_into("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII", data, opt, 0x10B, 14, 0, 0x200, 0x200, 0, 0x1000, 0x1000,
                     0x2000, 0x400000, 0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, 0x3000, 0x200, 0, 3, 0,
                     0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    struct.pack_into("<II", data, opt + 96 + 8, 0x2000, 60)  # import directory
    sect = opt + 0xE0
    struct.pack_into("<8sIIIIIIHHI", data, sect, b".text", 0x200, 0x1000, 0x200, 0x200, 0, 0, 0, 0, 0x60000020)
    struct.pack_into("<8sIIIIIIHHI", data, sect + 40, b".idata", 0x200, 0x2000, 0x200, 0x400, 0, 0, 0, 0, 0xC0000040)
    i = 0x400  # RVA 0x2000
    struct.pack_into("<IIIII", data, i, 0x2040, 0, 0, 0x20C0, 0x2060)
    struct.pack_into("<IIIII", data, i + 20, 0x2050, 0, 0, 0x20D0, 0x2070)
    for at in (0x40, 0x60):
        struct.pack_into("<IIII", data, i + at, 0x2080, 0x80000002, 0x2090, 0)
    for at in (0x50, 0x70):
        struct.pack_into("<II", data, i + at, 0x20A0, 0)
    data[i + 0x82:i + 0x88] = b"Alpha\0"
    data[i + 0x92:i + 0x9A] = b"Missing\0"
    data[i + 0xA2:i + 0xAE] = b"ExitProcess\0"
    data[i + 0xC0:i + 0xC9] = b"TEST.DLL\0"
    data[i + 0xD0:i + 0xDD] = b"KERNEL32.dll\0"
    return bytes(data)


def program_and_dll(tmp_path):
    (tmp_path / "PROG.EXE").write_bytes(pe_importer())
    (tmp_path / "TEST.DLL").write_bytes(pe_dll())
    return tmp_path / "PROG.EXE", tmp_path / "TEST.DLL"


def test_supporting_dlls_go_side_by_side_with_a_cross_reference(tmp_path):
    exe, dll = program_and_dll(tmp_path)
    report = exe_parse.PLUGIN.run({"exe": str(exe), "dlls": f"{dll}\n", "chunk": "1 KB"})
    assert report.ok, report.error
    base = tmp_path / "_PROG"
    assert (base / "PROG.EXE" / "SUMMARY.md").is_file() and (base / "TEST.DLL" / "exports" / "exports.json").is_file()
    rows = dict(report.result.rows)
    assert rows["Supporting DLLs"] == "TEST.DLL (3 exports)"
    assert rows["Imports resolved"] == "2 to the DLLs here, 1 not exported by them"
    assert rows["Full path"] == str(base)
    xref = (base / "cross-reference" / "imports-001.txt").read_text().splitlines()[2:]
    assert xref == [
        "PROG.EXE  0x00002060  TEST.DLL!Alpha  ->  TEST.DLL ordinal 1, file 0x00000210, "
        "TEST.DLL/regions/03-.text/00000200-000003FF.txt",
        "PROG.EXE  0x00002064  TEST.DLL!ordinal 2  ->  TEST.DLL ordinal 2, file 0x00000220, "
        "TEST.DLL/regions/03-.text/00000200-000003FF.txt",
        "PROG.EXE  0x00002068  TEST.DLL!Missing  ->  NOT EXPORTED by TEST.DLL"]
    index = json.loads((base / "parsed.json").read_text())
    assert [(f["file"], f["role"]) for f in index["files"]] == [("PROG.EXE", "program"), ("TEST.DLL", "DLL")]
    assert index["elsewhere"] == {"KERNEL32.dll": 1}
    overview = (base / "PARSED.md").read_text()
    assert "`TEST.DLL/`" in overview and "2 imports resolved" in overview and "- KERNEL32.dll: 1" in overview


def test_a_dll_left_out_next_time_is_removed(tmp_path):
    exe, dll = program_and_dll(tmp_path)
    exe_parse.PLUGIN.run({"exe": str(exe), "dlls": str(dll)})
    report = exe_parse.PLUGIN.run({"exe": str(exe)})
    assert report.ok and "Removed the results of TEST.DLL" in report.result.warning
    assert not (tmp_path / "_PROG" / "TEST.DLL").exists() and not (tmp_path / "_PROG" / "cross-reference").exists()
    assert (tmp_path / "_PROG" / "PROG.EXE" / "SUMMARY.md").is_file()


def test_drive_gets_only_the_sets_files(tmp_path):
    from test_exe_parse import FakeDrive
    from taskdeck.core import Context

    exe, dll = program_and_dll(tmp_path)
    (tmp_path / "_PROG").mkdir()
    (tmp_path / "_PROG" / "my notes.txt").write_text("not part of the set")
    fake = FakeDrive()
    ctx = Context({}, services={"google_drive": lambda: fake})
    report = exe_parse.PLUGIN.run({"exe": str(exe), "dlls": str(dll), "drive": True}, ctx=ctx)
    assert report.ok, report.error
    tree = fake.tree()
    assert {p.split("/")[3] for p in tree} == {"PROG.EXE", "TEST.DLL", "PARSED.md", "parsed.json", "cross-reference"}
    assert dict(report.result.rows)["Google Drive"] == "Google Drive › My Drive › TaskDeck › Executables › _PROG"

    report = exe_parse.PLUGIN.run({"exe": str(exe), "drive": True}, ctx=Context({}, services={"google_drive": lambda: fake}))
    assert report.ok, report.error
    assert not any(p.startswith("TaskDeck/Executables/_PROG/TEST.DLL/") for p in fake.tree())


def test_the_note_lists_the_dlls_it_imports_and_which_are_next_to_it(tmp_path):
    exe, _ = program_and_dll(tmp_path)
    notes = exe_parse.PLUGIN.field_notes({"exe": str(exe)}, {})
    assert notes["dlls"] == "PROG.EXE imports from TEST.DLL, KERNEL32.dll. Next to it: TEST.DLL."
    assert notes["output"] == f"Saves into {tmp_path / '_PROG'}"


def test_two_files_with_the_same_name_are_refused(tmp_path):
    exe, dll = program_and_dll(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "TEST.DLL").write_bytes(pe_dll())
    report = exe_parse.PLUGIN.run({"exe": str(exe), "dlls": f"{dll}\n{tmp_path / 'other' / 'TEST.DLL'}"})
    assert not report.ok and "Two files are named TEST.DLL" in report.error

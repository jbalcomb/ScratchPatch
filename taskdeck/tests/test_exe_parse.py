"""Parse Executable: header parsing (MZ, PE, NE), the folder layout, and the Google Drive mirror."""

import json
import struct
from pathlib import Path

import pytest

from taskdeck.core import Cancelled, Context, Task, TaskManager
from taskdeck.core.tasks import DONE
from taskdeck.jobs import exe_parse
from taskdeck.jobs.exe_parse import drive, formats, layout
from taskdeck.core.copying import Existing
from taskdeck.shared import gdrive


def dos_exe(overlay: bytes = b"FBOV" + b"\x00" * 60) -> bytes:
    """A DOS program: 64-byte header, 2 relocations, 1,000 bytes of load module, then an overlay."""
    header, image = 64, bytes(range(256)) * 3 + b"Hello, DOS world!$" + b"\x90" * 214
    end = header + len(image)
    mz = struct.pack("<2s13H", b"MZ", end % 512, -(-end // 512), 2, header // 16, 0x10, 0xFFFF,
                     0x50, 0x100, 0, 0x10, 0x1, 0x1C, 0)
    relocs = struct.pack("<4H", 0x0003, 0x0000, 0x0008, 0x0002)
    return (mz + relocs).ljust(header, b"\0") + image + overlay


def ne_exe() -> bytes:
    """A minimal 16-bit Windows (NE) file: one code segment at 0x100, module TEST importing KERNEL."""
    data = bytearray(0x180)
    data[0:2] = b"MZ"
    struct.pack_into("<H", data, 0x18, 0x40)  # e_lfarlc: new-style executable
    struct.pack_into("<I", data, 0x3C, 0x80)
    ne = 0x80
    struct.pack_into("<2sBBHHIHHHHHHHHHHHHHHHHIHHHBB", data, ne, b"NE", 5, 1, 0x62, 2, 0, 0, 0, 0, 0,
                     4, 1, 0, 0, 1, 1, 0, 0x40, 0x50, 0x50, 0x58, 0x5A, 0, 0, 4, 0, 2, 0)
    struct.pack_into("<HHHH", data, ne + 0x40, 0x10, 0x80, 0, 0x80)  # segment 1: sector 0x10 << 4 = 0x100
    data[ne + 0x50:ne + 0x55] = b"\x04TEST"
    struct.pack_into("<H", data, ne + 0x58, 1)
    data[ne + 0x5B:ne + 0x62] = b"\x06KERNEL"
    return bytes(data)


def pe_exe() -> bytes:
    """A small PE32: headers, .text with the entry point, .idata importing two functions from KERNEL32."""
    data = bytearray(0x600)
    data[0:2] = b"MZ"
    struct.pack_into("<H", data, 0x18, 0x40)
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x14C, 2, 0x5F5E1000, 0, 0, 0xE0, 0x102)
    opt = 0x98
    struct.pack_into("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII", data, opt, 0x10B, 14, 0, 0x200, 0x200, 0, 0x1010, 0x1000,
                     0x2000, 0x400000, 0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, 0x3000, 0x200, 0, 3, 0,
                     0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    struct.pack_into("<II", data, opt + 96 + 8, 0x2000, 40)  # import directory
    sect = opt + 0xE0
    struct.pack_into("<8sIIIIIIHHI", data, sect, b".text", 0x200, 0x1000, 0x200, 0x200, 0, 0, 0, 0, 0x60000020)
    struct.pack_into("<8sIIIIIIHHI", data, sect + 40, b".idata", 0x200, 0x2000, 0x200, 0x400, 0, 0, 0, 0, 0xC0000040)
    data[0x210:0x213] = b"\x55\x8B\xEC"  # entry point: push ebp; mov ebp, esp
    idata = 0x400  # RVA 0x2000
    struct.pack_into("<IIIII", data, idata, 0x2040, 0, 0, 0x2080, 0x2060)
    struct.pack_into("<III", data, idata + 0x40, 0x2090, 0x20A0, 0)  # lookup table
    struct.pack_into("<III", data, idata + 0x60, 0x2090, 0x20A0, 0)  # IAT
    data[idata + 0x80:idata + 0x8D] = b"KERNEL32.dll\0"
    data[idata + 0x92:idata + 0x9E] = b"ExitProcess\0"
    data[idata + 0xA2:idata + 0xAF] = b"GetVersionA\0\0"
    return bytes(data) + b"OVERLAY DATA"


# ---- formats --------------------------------------------------------------------------------------

def covers_file(exe: formats.Exe) -> bool:
    pos = 0
    for r in exe.regions:
        if r.start != pos or r.end <= r.start:
            return False
        pos = r.end
    return pos == exe.size


def test_dos_mz_regions_relocations_entry_and_overlay():
    data = dos_exe()
    exe = formats.parse(data)
    assert exe.format == "MZ" and covers_file(exe)
    assert [r.name for r in exe.regions] == ["MZ header", "relocation table", "header padding", "load module", "overlay"]
    load = exe.regions[3]
    assert (load.start, load.end, load.base) == (64, 1064, 0)
    assert "Borland" in exe.regions[4].note
    assert exe.facts["initial_cs_ip"] == "0001:0010"
    assert exe.entry == (64 + 0x20, "CS:IP 0001:0010 (image 0x000020)")
    relocs = exe.tables["relocations"]
    assert "0000:0003" in relocs[1] and "file 0x00000043" in relocs[1]
    assert "0002:0008" in relocs[2] and "image 0x000028" in relocs[2]


def test_pe_sections_imports_entry_and_overlay():
    exe = formats.parse(pe_exe())
    assert exe.format == "PE32" and covers_file(exe)
    names = [r.name for r in exe.regions]
    assert names == ["DOS header", "DOS stub", "PE headers", ".text", ".idata", "overlay"]
    text = exe.regions[3]
    assert (text.start, text.base, text.address) == (0x200, 0x1000, "RVA")
    assert exe.entry == (0x210, "RVA 0x00001010 (VA 0x00401010)")
    assert exe.facts["imports"] == {"KERNEL32.dll": 2}
    assert any("ExitProcess" in line for line in exe.tables["imports"])
    assert exe.facts["overlay_bytes"] == len(b"OVERLAY DATA")


def test_ne_segments_and_modules():
    exe = formats.parse(ne_exe())
    assert exe.format == "NE" and covers_file(exe)
    assert exe.facts["module_name"] == "TEST"
    assert exe.facts["imported_modules"] == ["KERNEL"]
    seg = next(r for r in exe.regions if r.name == "segment 1")
    assert (seg.start, seg.end) == (0x100, 0x180)
    assert exe.entry == (0x104, "segment 1:0004")


@pytest.mark.parametrize("data", [b"", b"MZ", b"MZ" + b"\xff" * 100, b"\x7fELF" + b"\0" * 60, b"not an exe at all",
                                  pe_exe()[:0x120], dos_exe()[:40]])
def test_malformed_files_never_raise_and_still_cover_the_file(data):
    exe = formats.parse(data)
    assert covers_file(exe) or not data


def test_random_garbage_never_raises():
    import random

    rng = random.Random(1)
    for base in (dos_exe(), pe_exe(), ne_exe()):
        for _ in range(200):
            data = bytearray(base)
            for _ in range(rng.randint(1, 20)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            assert covers_file(formats.parse(bytes(data)))


# ---- the folder ---------------------------------------------------------------------------------

def run_job(tmp_path, data: bytes, name="GAME.EXE", **inputs):
    src = tmp_path / "in" / name
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(data)
    report = exe_parse.PLUGIN.run({"exe": str(src), "output": str(tmp_path / "out"), "chunk": "1 KB", **inputs})
    return report, tmp_path / "out" / f"_{name}"


def test_writes_a_folder_named_like_the_executable_with_an_underscore(tmp_path):
    report, out = run_job(tmp_path, dos_exe())
    assert report.ok, report.error
    assert out.name == "_GAME.EXE"
    for f in ("README.md", "SUMMARY.md", "metadata.json", "index.json", "headers/headers.txt",
              "tables/relocations-001.txt", "strings/ascii-001.txt"):
        assert (out / f).is_file(), f
    meta = json.loads((out / "metadata.json").read_text())
    assert meta["file"]["size"] == len(dos_exe()) and len(meta["file"]["sha256"]) == 64
    assert meta["entry_point"]["chunk"] == "regions/03-load-module/00000040-00000427.txt"
    summary = (out / "SUMMARY.md").read_text()
    assert "## Layout" in summary and "regions/04-overlay/" in summary and "Borland" in summary
    assert "Hello, DOS world" in (out / "strings/ascii-001.txt").read_text()


def test_chunks_cover_every_byte_and_link_to_each_other(tmp_path):
    data = dos_exe(overlay=b"FBOV" + bytes(3000))
    _, out = run_job(tmp_path, data)
    index = json.loads((out / "index.json").read_text())
    chunks = [c for r in index["regions"] for c in r["chunks"]]
    assert all(c["end"] - c["start"] <= 1024 for c in chunks)
    assert [c["start"] for c in chunks] == [0] + [c["end"] for c in chunks[:-1]] and chunks[-1]["end"] == len(data)
    rebuilt = b""
    for i, c in enumerate(chunks):
        lines = (out / c["file"]).read_text().splitlines()
        assert lines[2] == "# prev: " + (chunks[i - 1]["file"] if i else "(start of file)")
        assert lines[3] == "# next: " + (chunks[i + 1]["file"] if i + 1 < len(chunks) else "(end of file)")
        for line in lines[5:]:
            hexes = line.split("|")[0].split("  ", 2 if "load-module" in c["file"] else 1)[-1]
            rebuilt += bytes.fromhex(hexes)
    assert rebuilt == data


def test_rerun_is_identical(tmp_path):
    _, out = run_job(tmp_path, pe_exe(), name="T.EXE")
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    report, _ = run_job(tmp_path, pe_exe(), name="T.EXE")
    assert report.ok
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_never_overwrites_a_folder_it_did_not_make(tmp_path):
    (tmp_path / "out" / "_GAME.EXE").mkdir(parents=True)
    (tmp_path / "out" / "_GAME.EXE" / "mine.txt").write_text("keep me")
    report, out = run_job(tmp_path, dos_exe())
    assert not report.ok and "wasn't made by TaskDeck" in report.error
    assert (out / "mine.txt").read_text() == "keep me"


def test_saves_next_to_the_executable_by_default(tmp_path):
    src = tmp_path / "games" / "GAME.EXE"
    src.parent.mkdir()
    src.write_bytes(dos_exe())
    report = exe_parse.PLUGIN.run({"exe": str(src)})
    assert report.ok, report.error
    assert (tmp_path / "games" / "_GAME.EXE" / "SUMMARY.md").is_file() and src.is_file()
    assert dict(report.result.rows)["Full path"] == str(tmp_path / "games" / "_GAME.EXE")


def test_drive_needs_a_connected_account(tmp_path):
    report, _ = run_job(tmp_path, dos_exe(), drive=True)
    assert not report.ok and "Connect Google Drive" in report.error


# ---- Google Drive -------------------------------------------------------------------------------

class FakeDrive:
    """In-memory Drive: folders and files by id."""

    def __init__(self):
        self.items = {"root": ("folder", "My Drive", None, b"")}  # id -> (type, name, parent, content)
        self.next = 0
        self.trashed: list[str] = []
        self.uploads = 0
        self.throttle = type("T", (), {"hits": 0})()
        self.keep_waiting = None

    def _new(self, kind, name, parent, content=b""):
        self.next += 1
        fid = f"id{self.next}"
        self.items[fid] = (kind, name, parent, content)
        return fid

    def children_files(self, parent):
        folders, files = {}, {}
        for fid, (kind, name, p, content) in self.items.items():
            if p == parent and fid not in self.trashed:
                if kind == "folder":
                    folders[name] = fid
                else:
                    import hashlib

                    files.setdefault(name, []).append(Existing(fid, len(content), hashlib.md5(content).hexdigest()))
        return folders, files

    def children(self, parent):
        folders, files = self.children_files(parent)
        return folders, {n: c[0].id for n, c in files.items()}

    def create_folder(self, name, parent="root"):
        from taskdeck.shared.gdrive import Folder

        return Folder(self._new("folder", name, parent), name)

    def upload(self, path, parent, name=None, progress=None, replace=None, not_ids=()):
        self.uploads += 1
        return self._new("file", name or path.name, parent, Path(path).read_bytes())

    def trash(self, fid):
        self.trashed.append(fid)

    def tree(self, parent="root", prefix=""):
        folders, files = self.children_files(parent)
        out = {prefix + n for n in files}
        for n, fid in folders.items():
            out |= self.tree(fid, prefix + n + "/")
        return out


def new_task() -> Task:
    return Task(0, "upload", "exe_parse")


def test_push_mirrors_the_folder_and_skips_what_is_there(tmp_path):
    _, out = run_job(tmp_path, dos_exe())
    fake, task = FakeDrive(), new_task()
    message = drive.push(task, fake, out, "TaskDeck/Executables")
    local = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert task.result.display() == "Google Drive › My Drive › TaskDeck › Executables › _GAME.EXE"
    assert message.startswith(f"Uploaded {len(local)} files") and "Skipped" not in message
    assert fake.tree() == {"TaskDeck/Executables/_GAME.EXE/" + p for p in local}
    assert all(p.is_file() for p in (out / f for f in local))  # the local folder is kept
    checking, uploading = task.phases
    assert (checking.done, checking.total, uploading.done, uploading.total) == (len(local),) * 4

    fake.uploads, task = 0, new_task()
    message = drive.push(task, fake, out, "TaskDeck/Executables")
    assert fake.uploads == 0 and f"Skipped {len(local)} already there" in message
    assert task.phases[1].label == f"Uploading to Google Drive ({len(local)} already there)"


def test_push_trashes_chunks_of_an_earlier_parse(tmp_path):
    fake = FakeDrive()
    _, out = run_job(tmp_path, dos_exe(overlay=b"FBOV" + bytes(3000)))
    drive.push(new_task(), fake, out, "X")
    run_job(tmp_path, dos_exe(overlay=b"FBOV" + bytes(3000)), chunk="4 KB")
    message = drive.push(new_task(), fake, out, "X")
    local = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert "left from an earlier parse to the trash" in message
    assert fake.tree() == {"X/_GAME.EXE/" + p for p in local}


def test_push_cancel_and_errors_say_what_was_uploaded(tmp_path):
    _, out = run_job(tmp_path, dos_exe())
    task = new_task()
    task.cancel_requested = True
    with pytest.raises(Cancelled, match="Stopped. 0 files were uploaded"):
        drive.push(task, FakeDrive(), out, "X")

    class Broken(FakeDrive):
        def upload(self, *a, **kw):
            raise gdrive.DriveError("Your Google Drive is full.")

    with pytest.raises(RuntimeError, match="Your Google Drive is full"):
        drive.push(new_task(), Broken(), out, "X")


def test_job_pushes_in_a_background_task_in_the_app(tmp_path):
    src = tmp_path / "GAME.EXE"
    src.write_bytes(dos_exe())
    fake, manager = FakeDrive(), TaskManager(start_thread=lambda fn: fn())
    ctx = Context({}, services={"google_drive": lambda: fake,
                                "start_task": lambda title, work: manager.start(title, "exe_parse", work)})
    report = exe_parse.PLUGIN.run({"exe": str(src), "output": str(tmp_path / "out"), "drive": True}, ctx=ctx)
    assert report.ok, report.error
    rows = dict(report.result.rows)
    assert rows["Google Drive"] == "Google Drive › My Drive › TaskDeck › Executables › _GAME.EXE"
    assert rows["Upload"].startswith("In the background")
    assert rows["Full path"] == str(tmp_path / "out" / "_GAME.EXE")
    task = manager.latest("exe_parse")
    assert task.status == DONE and task.title == "Uploading _GAME.EXE to Google Drive › My Drive › TaskDeck › Executables"
    assert task.result.display().endswith("TaskDeck › Executables › _GAME.EXE")
    assert any(p.startswith("TaskDeck/Executables/_GAME.EXE/regions/") for p in fake.tree())


def test_job_uploads_right_away_without_the_app(tmp_path):
    src = tmp_path / "GAME.EXE"
    src.write_bytes(dos_exe())
    fake = FakeDrive()
    ctx = Context({}, services={"google_drive": lambda: fake})
    report = exe_parse.PLUGIN.run({"exe": str(src), "output": str(tmp_path / "out"), "drive": True}, ctx=ctx)
    assert report.ok, report.error
    assert dict(report.result.rows)["Upload"].startswith("Uploaded")


def test_relative_output_folder_is_next_to_the_executable(tmp_path):
    src = tmp_path / "GAME.EXE"
    src.write_bytes(dos_exe())
    report = exe_parse.PLUGIN.run({"exe": str(src), "output": "Parsed"})
    assert report.ok, report.error
    assert (tmp_path / "Parsed" / "_GAME.EXE" / "SUMMARY.md").is_file()


def test_drive_path_is_shown_under_the_switch():
    notes = exe_parse.PLUGIN.field_notes({"exe": "/storage/emulated/0/Games/MOM/WIZARDS.EXE", "drive": True},
                                         {"drive_folder": "RE/Master of Magic"})
    assert notes == {"drive": "Goes to Google Drive › My Drive › RE › Master of Magic › _WIZARDS.EXE"}
    notes = exe_parse.PLUGIN.field_notes({"exe": "", "drive": False}, {"drive_folder": ""})
    assert notes == {"drive": "Would go to Google Drive › My Drive › TaskDeck › Executables › _EXECUTABLE.EXE"}

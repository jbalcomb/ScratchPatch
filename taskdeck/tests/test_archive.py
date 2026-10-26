"""7-Zip engine through the C shim. Needs lib7z.so + lib7zshim.so (TASKDECK_7Z_DIR); CI builds them."""

import io
import tarfile
from pathlib import Path

import pytest

from taskdeck.shared import archive
from taskdeck.shared.archive import Archive, ArchiveError

FIX = Path(__file__).parent / "fixtures" / "archives"
CONTENT = {"docs/readme.txt": b"hello archive\n", "docs/notes/a.txt": b"note a\n", "big.txt": b"x" * 3000}

needs_engine = pytest.mark.skipif(not archive.available(), reason="7-Zip engine not built (set TASKDECK_7Z_DIR)")


def files_under(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@needs_engine
@pytest.mark.parametrize("name,fmt", [("plain.zip", "zip"), ("plain.7z", "7z")])
def test_list_and_extract(tmp_path, name, fmt):
    with Archive(FIX / name) as a:
        assert a.format.lower() == fmt
        files = {e.name: e.size for e in a.entries() if not e.is_dir}
        assert files == {k: len(v) for k, v in CONTENT.items()}
        seen = []
        a.extract(tmp_path / "out", progress=lambda done, total: seen.append((done, total)) and False)
    assert files_under(tmp_path / "out") == CONTENT
    assert seen and seen[-1][0] == seen[-1][1] == sum(map(len, CONTENT.values()))


@needs_engine
@pytest.mark.parametrize("name", ["aes.zip", "zipcrypto.zip", "data-encrypted.7z"])
def test_encrypted_data(tmp_path, name):
    with Archive(FIX / name) as a:  # names are readable without the password
        assert all(e.encrypted for e in a.entries() if not e.is_dir)
        with pytest.raises(ArchiveError) as info:
            a.extract(tmp_path / "none")
        assert info.value.code == archive.E_NEED_PASSWORD
        with pytest.raises(ArchiveError) as info:
            a.extract(tmp_path / "wrong", password="nope")
        assert info.value.code == archive.E_WRONG_PASSWORD
        a.extract(tmp_path / "right", password="Secret1")
    assert files_under(tmp_path / "right") == CONTENT


@needs_engine
def test_encrypted_names(tmp_path):
    with pytest.raises(ArchiveError) as info:
        Archive(FIX / "headers-encrypted.7z")
    assert info.value.code == archive.E_NEED_PASSWORD
    with pytest.raises(ArchiveError) as info:
        Archive(FIX / "headers-encrypted.7z", password="nope")
    assert info.value.code == archive.E_WRONG_PASSWORD
    with Archive(FIX / "headers-encrypted.7z", password="Secret1") as a:
        assert "docs/readme.txt" in [e.name for e in a.entries()]
        a.extract(tmp_path)
    assert files_under(tmp_path) == CONTENT


@needs_engine
def test_overwrites_existing_files(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.txt").write_bytes(b"old and longer than the new content")
    with Archive(FIX / "plain.zip") as a:
        a.extract(tmp_path)
    assert (tmp_path / "docs" / "readme.txt").read_bytes() == b"hello archive\n"


@needs_engine
def test_refuses_paths_outside_the_folder(tmp_path):
    with Archive(FIX / "traversal.zip") as a:
        assert a.unsafe_entries() == ["../evil.txt"]
        with pytest.raises(ArchiveError) as info:
            a.extract(tmp_path / "out")
    assert info.value.code == archive.E_UNSAFE_PATH
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "out").exists()


@needs_engine
def test_cancel(tmp_path):
    with Archive(FIX / "plain.7z") as a, pytest.raises(ArchiveError) as info:
        a.extract(tmp_path, progress=lambda done, total: True)
    assert info.value.code == archive.E_CANCELLED


@needs_engine
def test_single_file_gz_and_bad_files(tmp_path):
    with Archive(FIX / "single.txt.gz") as a:
        a.extract(tmp_path)
    assert list(files_under(tmp_path).values()) == [b"hello archive\n"]
    with pytest.raises(ArchiveError) as info:
        Archive(FIX / "not-an-archive.zip")
    assert info.value.code == archive.E_UNSUPPORTED
    with pytest.raises(ArchiveError) as info:
        Archive(tmp_path / "missing.7z")
    assert info.value.code == archive.E_OPEN_FILE
    assert archive.version().startswith("2")


def test_tar_gz_uses_python(tmp_path):  # no native engine needed
    path = tmp_path / "t.tar.gz"
    with tarfile.open(path, "w:gz") as t:
        for name, data in CONTENT.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    with Archive(path) as a:
        assert a.format == "tar" and sorted(e.name for e in a.entries()) == sorted(CONTENT)
        a.extract(tmp_path / "out")
    assert files_under(tmp_path / "out") == CONTENT


def test_missing_engine_message(monkeypatch):
    monkeypatch.setattr(archive, "_engine", None)
    monkeypatch.setattr(archive, "_candidates", lambda name: ["/nonexistent/" + name])
    with pytest.raises(ArchiveError, match="Install the latest APK"):
        archive.engine()


RAR_TEXT = {f"{c}.txt": f"This is from {c}.txt".encode() for c in "abcd"}


@needs_engine
def test_rar5(tmp_path):
    with Archive(FIX / "rar5-plain.rar") as a:
        assert a.format == "Rar5" and len(a.entries()) == 4
        a.extract(tmp_path)
    assert sorted(files_under(tmp_path)) == ["test1.bin", "test2.bin", "test3.bin", "test4.bin"]


@needs_engine
@pytest.mark.parametrize("name", ["rar4-encrypted-names.rar", "rar5-encrypted-names.rar"])
def test_rar_with_encrypted_names(tmp_path, name):
    for password, code in ((None, archive.E_NEED_PASSWORD), ("wrong", archive.E_WRONG_PASSWORD)):
        with pytest.raises(ArchiveError) as info:
            Archive(FIX / name, password)
        assert info.value.code == code
    with Archive(FIX / name, "password") as a:
        a.extract(tmp_path)
    assert {k: v[:18] for k, v in files_under(tmp_path).items()} == RAR_TEXT


@needs_engine
def test_wrong_password_names_the_file(tmp_path):
    with Archive(FIX / "rar5-mixed-passwords.rar") as a, pytest.raises(ArchiveError) as info:
        a.extract(tmp_path, password="password")
    assert info.value.code == archive.E_WRONG_PASSWORD and info.value.entry == "d.txt"
    assert "(d.txt)" in str(info.value)


@needs_engine
def test_engine_check_job(tmp_path, monkeypatch):
    from taskdeck.jobs import archive_test

    monkeypatch.setattr(archive_test, "default_data_dir", lambda: tmp_path)
    report = archive_test.PLUGIN.run({"path": str(FIX / "headers-encrypted.7z"), "password": "Secret1", "extract": True})
    assert report.ok, report.error
    rows = dict(report.result.rows)
    assert rows["Format"] == "7z" and rows["Files"] == "3 (+2 folders)" and rows["Encrypted"] == "3 of 3 files"
    assert files_under(tmp_path / "extract-test" / "headers-encrypted") == CONTENT
    bad = archive_test.PLUGIN.run({"path": str(FIX / "headers-encrypted.7z"), "password": "nope"})
    assert not bad.ok and bad.error == "Wrong password."


def test_android_library_locations(monkeypatch):
    monkeypatch.delenv("TASKDECK_7Z_DIR", raising=False)
    monkeypatch.setenv("ANDROID_NATIVE_LIBRARY_DIR", "/data/app/x/lib/arm64")
    monkeypatch.setenv("ANDROID_APK_NATIVE_PREFIX", "/data/app/x/base.apk!/lib/arm64-v8a/")
    found = archive._candidates("lib7zshim.so")
    assert found[-3:] == ["/data/app/x/lib/arm64/lib7zshim.so", "/data/app/x/base.apk!/lib/arm64-v8a/lib7zshim.so",
                          "lib7zshim.so"]


def test_load_problem_explains(monkeypatch):
    monkeypatch.setattr(archive, "_engine", None)
    monkeypatch.setattr(archive, "_candidates", lambda name: ["/nonexistent/" + name])
    problem = archive.load_problem()
    assert problem.startswith("The archive engine isn't installed") and "/nonexistent/lib7zshim.so" in problem

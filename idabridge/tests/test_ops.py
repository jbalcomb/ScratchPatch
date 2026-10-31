from pathlib import Path

import pytest
from pydantic import ValidationError

from idabridge import ops


def test_idc_string_escapes():
    assert ops.idc_str('a"b\\c\nd\te') == '"a\\"b\\\\c\\nd\\te"'
    with pytest.raises(ValueError):
        ops.idc_str("nul\x00")
    with pytest.raises(ValueError):
        ops.idc_str("emoji \U0001F600")


def test_address_number_or_name():
    assert ops.address_args("0x401000") == ["0x401000", '""']
    assert ops.address_args("401000h") == ["0x401000", '""']
    assert ops.address_args("4198400") == ["0x401000", '""']
    assert ops.address_args("WinMain") == ["BADADDR", '"WinMain"']


@pytest.mark.parametrize("bad", [
    {"file": "../secret.exe", "address": "0x1"},
    {"file": "C:/Windows/notepad.exe", "address": "0x1"},
    {"file": "a.exe", "address": "0x100000000"},
    {"file": "a.exe", "address": 'x"); Exit(0); //'},
    {"file": "a.exe", "address": "0x1", "surprise": 1},
])
def test_refuses_bad_params(bad):
    with pytest.raises(ValidationError):
        ops.OPS["disassemble"].params(**bad)


@pytest.mark.parametrize("name", ["../x.asm", "a/b.asm", "C:x", "result.tsv", "job.idc", ""])
def test_refuses_bad_output_names(name):
    with pytest.raises(ValidationError):
        ops.OPS["export"].params(file="a.exe", kind="asm", output_name=name)


def test_find_bytes_pattern():
    p = ops.OPS["find_bytes"].params(file="a.exe", pattern="55  8b ec ? 83")
    assert p.pattern == "55 8b ec ? 83"
    with pytest.raises(ValidationError):
        ops.OPS["find_bytes"].params(file="a.exe", pattern='55"); Exit(1); //')


def test_every_op_has_an_idc_file():
    idc = Path(ops.__file__).parent / "idc" / "ops"
    for op in ops.OPS.values():
        if op.name != "run_idc":
            assert (idc / f"{op.idc}.idc").is_file(), op.name
            assert f"static op_{op.name}(" in (idc / f"{op.idc}.idc").read_text()


def test_script_for_export_writes_into_job_folder():
    op = ops.OPS["export"]
    p = op.params(file="a.exe", kind="asm", output_name="out.asm", start="0x401000")
    script = ops.build_script(op, p, ops.Ctx(Path("C:/jobs/1")), Path("C:/idc"), Path("C:/jobs/1/result.tsv"), False)
    assert 'rc = op_export(out, OFILE_ASM, "C:/jobs/1/out.asm", 0x401000, BADADDR);' in script
    assert "SaveBase" not in script
    assert script.splitlines()[0] == "#include <idc.idc>"


def test_write_ops_save():
    op = ops.OPS["rename"]
    p = op.params(file="a.exe", address="sub_401000", new_name="DrawSprite")
    script = ops.build_script(op, p, ops.Ctx(Path("/j")), Path("/idc"), Path("/j/result.tsv"), True)
    assert 'op_rename(out, BADADDR, "sub_401000", "DrawSprite")' in script
    assert "SaveBase" in script


def test_parse_result():
    text = "#INFO\tk\tv\\tw\n0x1\t0x5\t4\tfoo\\tbar\t0x0\n#TOTAL\t9\n#ERROR\toops\n#END\t1\n"
    r = ops.parse_result(text, ["start", "end", "size", "name", "flags"])
    assert r["info"] == {"k": "v\tw"}
    assert r["rows"] == [{"start": "0x1", "end": "0x5", "size": "4", "name": "foo\tbar", "flags": "0x0"}]
    assert (r["total"], r["error"], r["completed"], r["rc"]) == (9, "oops", True, 1)
    assert ops.parse_result("x\n", None)["completed"] is False

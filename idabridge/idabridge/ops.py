"""The operations: one entry per IDA command, shared by the REST API, the MCP tools and the CLI.

Each operation has a Pydantic model for its parameters, an IDC file under idc/ops/, the IDC call that
main() makes, the column names of its rows, and a mode:

* read:   runs on a copy of the database, so up to IDABRIDGE_MAX_PARALLEL can run at once, even on one file.
* write:  changes the database: runs on the database itself, one at a time per file, and saves it.
* create: (re)builds the database from the input file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BADADDR = "BADADDR"
_NUM = re.compile(r"^(0x[0-9a-fA-F]+|[0-9a-fA-F]+h|\d+)$")
_NAME = re.compile(r"^[A-Za-z_$?@.][\w$?@.:<>~]*$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,120}$")
_HEXPAT = re.compile(r"^([0-9A-Fa-f]{2}|\?)( ([0-9A-Fa-f]{2}|\?))*$")


# ---------------------------------------------------------------- IDC literals

def idc_str(value: str) -> str:
    """A Python string as an IDC string literal."""
    if "\x00" in value:
        raise ValueError("text may not contain NUL characters")
    try:
        value.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise ValueError("IDA 5.5 only takes Windows-1252 text") from exc
    out = (value.replace("\\", "\\\\").replace('"', '\\"')
           .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{out}"'


def idc_int(value: int) -> str:
    return f"0x{value:X}" if value >= 0 else str(value)


def parse_number(text: str) -> int:
    text = text.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    if text.lower().endswith("h"):
        return int(text[:-1], 16)
    return int(text)


def address_args(address: str | None) -> list[str]:
    """`ea, name` arguments: a number becomes ea, anything else is looked up by name inside IDA."""
    if address is None:
        return [BADADDR, '""']
    if _NUM.match(address):
        return [idc_int(parse_number(address)), '""']
    return [BADADDR, idc_str(address)]


def ea_arg(address: str | None) -> str:
    return BADADDR if address is None else idc_int(parse_number(address))


# ---------------------------------------------------------------- parameter models

class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="Input binary, relative to the server's IDA_INPUT_DIR (e.g. 'game/MAGIC.EXE').")
    timeout: int | None = Field(None, ge=5, description="Seconds before IDA is stopped. Default: server setting.")

    @field_validator("file")
    @classmethod
    def _file(cls, v: str) -> str:
        p = v.replace("\\", "/").strip("/")
        if not p or ".." in p.split("/") or ":" in p:
            raise ValueError("file must be a relative path inside IDA_INPUT_DIR")
        return p


class Paged(Base):
    offset: int = Field(0, ge=0, description="Rows to skip.")
    limit: int = Field(500, ge=1, le=20000, description="Most rows to return.")


def _address_field(required: bool = True):
    desc = "Address (0x401000, 401000h or decimal) or a name IDA knows (sub_401000, WinMain, an import)."
    return Field(... if required else None, description=desc, min_length=1, max_length=256)


def _check_address(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.strip()
    if _NUM.match(v):
        if parse_number(v) > 0xFFFFFFFF:
            raise ValueError("IDA 5.5 (32-bit) addresses are at most 0xFFFFFFFF")
        return v
    if not _NAME.match(v):
        raise ValueError("not an address or a valid IDA name")
    return v


def check_filename(v: str) -> str:
    if not _FILENAME.match(v) or v.endswith(".") or v.lower().startswith(("result.", "job.", "ida.")):
        raise ValueError("file names must be plain: letters, digits, space, . _ - (and not result.*, job.* or ida.*)")
    return v


def _check_number(v: str | None) -> str | None:
    if v is None:
        return v
    if not _NUM.match(v.strip()):
        raise ValueError("expected an address such as 0x401000")
    return _check_address(v)


class Addressed(Base):
    address: str = _address_field()

    @field_validator("address")
    @classmethod
    def check_address(cls, v):
        return _check_address(v)


class NoParams(Base):
    pass


class FunctionsParams(Paged):
    filter: str = Field("", max_length=200, description="Only names containing this text (case-sensitive).")


class NamesParams(Paged):
    filter: str = Field("", max_length=200, description="Only names containing this text (case-sensitive).")
    segment: str = Field("", max_length=64, description="Only this segment, e.g. '.idata' for PE imports.")


class StringsParams(Paged):
    filter: str = Field("", max_length=200, description="Only strings containing this text (case-sensitive).")
    min_length: int = Field(4, ge=1, le=1000)


class DisassembleParams(Addressed):
    whole_function: bool = Field(False, description="True: the whole function holding the address.")
    count: int = Field(200, ge=1, le=20000, description="Most items (instructions/data lines).")


class XrefsParams(Addressed):
    limit: int = Field(1000, ge=1, le=20000)


class ReadBytesParams(Addressed):
    size: int = Field(256, ge=1, le=65536)


class FindBytesParams(Base):
    pattern: str = Field(..., description="Hex bytes separated by spaces, '?' for any byte: '55 8B EC ? 83'.")
    start: str | None = Field(None, description="Address to start from. Default: the lowest address.")
    limit: int = Field(100, ge=1, le=10000)

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        v = " ".join(v.split())
        if not _HEXPAT.match(v):
            raise ValueError("pattern must be hex bytes separated by spaces, with ? as a wildcard")
        return v

    @field_validator("start")
    @classmethod
    def check_start(cls, v):
        return _check_number(v)


class AnalyzeParams(Base):
    force: bool = Field(False, description="Throw away the existing database (and every rename/comment) and analyse again.")
    processor: str | None = Field(None, pattern=r"^[A-Za-z0-9_]{1,16}$", description="IDA processor (-p), e.g. 'metapc'.")


class RangeParams(Base):
    start: str | None = Field(None, description="Default: the lowest address.")
    end: str | None = Field(None, description="Default: the highest address.")
    @field_validator("start", "end")
    @classmethod
    def check_range(cls, v):
        return _check_number(v)


class RenameParams(Addressed):
    new_name: str = Field(..., max_length=255, pattern=r"^[A-Za-z_$?@][\w$?@]*$")


class CommentParams(Addressed):
    text: str = Field(..., max_length=4000, description="The comment; empty removes it.")
    repeatable: bool = False


class MakeFunctionParams(Addressed):
    end: str | None = Field(None, description="End address. Default: IDA finds it.")
    @field_validator("end")
    @classmethod
    def check_end(cls, v):
        return _check_number(v)


class MakeDataParams(Addressed):
    kind: Literal["byte", "word", "dword", "qword", "float", "double", "string"]


class SetTypeParams(Addressed):
    declaration: str = Field(..., max_length=2000, description="C declaration, e.g. 'int __cdecl f(char *s);'")


EXPORT_KINDS = {"map": ("OFILE_MAP", ".map"), "exe": ("OFILE_EXE", ".exe"), "idc": ("OFILE_IDC", ".idc"),
                "lst": ("OFILE_LST", ".lst"), "asm": ("OFILE_ASM", ".asm"), "dif": ("OFILE_DIF", ".dif")}


class ExportParams(RangeParams):
    kind: Literal["asm", "lst", "map", "idc", "dif", "exe"] = Field(
        ..., description="asm: assembler source; lst: listing; map: MAP file; idc: IDC script that rebuilds the "
                         "database; dif: patched bytes; exe: patched executable (only some formats).")
    output_name: str = Field(..., description="File name for the result (no folders), e.g. 'magic.asm'.")

    @field_validator("output_name")
    @classmethod
    def check_output_name(cls, v: str) -> str:
        return check_filename(v)


class RunIdcParams(Base):
    code: str = Field(..., max_length=100_000, description=(
        "Body of an IDC function `static user_code(out, outdir)` for IDA 5.5. Write rows with "
        "fprintf(out, \"...\\n\"); write extra files into outdir (a folder path ending in '/'). "
        "Return 0 for success, non-zero for failure."))
    save: bool = Field(False, description="True: run on the database itself and save it. False: on a copy.")
    output_files: list[str] = Field(default_factory=list, max_length=20,
                                    description="Names of files the code writes into outdir, to return.")

    @field_validator("output_files")
    @classmethod
    def check_files(cls, v):
        return [check_filename(n) for n in v]


# ---------------------------------------------------------------- the registry

@dataclass
class Ctx:
    job_dir: Path

    def path(self, name: str) -> str:
        return (self.job_dir / name).as_posix()


@dataclass
class Op:
    name: str
    summary: str
    params: type[Base]
    mode: Literal["read", "write", "create"]
    columns: list[str] | None
    call: Callable[[Base, Ctx], list[str]]
    output_files: Callable[[Base], list[str]] = field(default=lambda p: [])
    idc_file: str | None = None  # default: the op's name

    @property
    def idc(self) -> str:
        return self.idc_file or self.name


OPS: dict[str, Op] = {}


def _op(name, summary, params, mode, columns, call, **kw):
    OPS[name] = Op(name, summary, params, mode, columns, call, **kw)


_op("info", "Summary of the database: processor, address range, entry address, counts.",
    NoParams, "read", None, lambda p, c: [])
_op("analyze", "Create the IDA database for a file (done automatically on first use), or rebuild it with force.",
    AnalyzeParams, "create", None, lambda p, c: [], idc_file="analyze")
_op("list_segments", "List segments with their address ranges.",
    NoParams, "read", ["name", "start", "end", "size"], lambda p, c: [])
_op("list_functions", "List functions (paged), optionally filtered by name.",
    FunctionsParams, "read", ["start", "end", "size", "name", "flags"],
    lambda p, c: [idc_str(p.filter), str(p.offset), str(p.limit)])
_op("list_entry_points", "List entry points and exports.",
    NoParams, "read", ["ordinal", "ea", "name"], lambda p, c: [])
_op("list_names", "List named addresses (paged); segment='.idata' lists PE imports.",
    NamesParams, "read", ["ea", "name", "segment", "kind"],
    lambda p, c: [idc_str(p.filter), idc_str(p.segment), str(p.offset), str(p.limit)])
_op("list_strings", "List the strings IDA has defined (paged).",
    StringsParams, "read", ["ea", "length", "type", "segment", "text"],
    lambda p, c: [idc_str(p.filter), str(p.min_length), str(p.offset), str(p.limit)])
_op("disassemble", "Disassembly from an address, or of the whole function holding it.",
    DisassembleParams, "read", ["ea", "bytes", "label", "text", "comment"],
    lambda p, c: [*address_args(p.address), str(p.count), "1" if p.whole_function else "0"])
_op("function_info", "A function's bounds, flags, frame size, comments, callers and callees.",
    Addressed, "read", ["relation", "ea", "name"], lambda p, c: [*address_args(p.address)])
_op("xrefs_to", "Code and data references to an address.",
    XrefsParams, "read", ["kind", "ea", "function", "text"], lambda p, c: [*address_args(p.address), str(p.limit)])
_op("xrefs_from", "Code and data references from an address.",
    XrefsParams, "read", ["kind", "ea", "name"], lambda p, c: [*address_args(p.address), str(p.limit)])
_op("read_bytes", "Hex dump of bytes at an address.",
    ReadBytesParams, "read", ["ea", "hex", "ascii"], lambda p, c: [*address_args(p.address), str(p.size)])
_op("find_bytes", "Search for a byte pattern.",
    FindBytesParams, "read", ["ea", "segment", "function"],
    lambda p, c: [idc_str(p.pattern), ea_arg(p.start) if p.start else "MinEA()", str(p.limit)])
_op("export", "Produce an .asm/.lst/.map/.idc/.dif/.exe file and return it.",
    ExportParams, "read", None,
    lambda p, c: [EXPORT_KINDS[p.kind][0], idc_str(c.path(p.output_name)), ea_arg(p.start), ea_arg(p.end)],
    output_files=lambda p: [p.output_name])
_op("reanalyze", "Run auto-analysis again over a range and save.",
    RangeParams, "write", None, lambda p, c: [ea_arg(p.start), ea_arg(p.end)])
_op("rename", "Rename an address (function, label, data) and save.",
    RenameParams, "write", None, lambda p, c: [*address_args(p.address), idc_str(p.new_name)])
_op("set_comment", "Set the comment at an address and save.",
    CommentParams, "write", None,
    lambda p, c: [*address_args(p.address), idc_str(p.text), "1" if p.repeatable else "0"])
_op("set_function_comment", "Set a function's comment and save.",
    CommentParams, "write", None,
    lambda p, c: [*address_args(p.address), idc_str(p.text), "1" if p.repeatable else "0"])
_op("make_code", "Turn bytes into an instruction and save.",
    Addressed, "write", None, lambda p, c: [*address_args(p.address)])
_op("make_function", "Create a function and save.",
    MakeFunctionParams, "write", None, lambda p, c: [*address_args(p.address), ea_arg(p.end)])
_op("make_data", "Turn an item into data (byte/word/dword/qword/float/double/string) and save.",
    MakeDataParams, "write", None, lambda p, c: [*address_args(p.address), idc_str(p.kind)])
_op("undefine", "Undefine the item at an address and save.",
    Addressed, "write", None, lambda p, c: [*address_args(p.address)])
_op("set_type", "Apply a C declaration to an address and save.",
    SetTypeParams, "write", None, lambda p, c: [*address_args(p.address), idc_str(p.declaration)])
_op("run_idc", "Run your own IDC code (only when the server allows it: IDABRIDGE_ALLOW_IDC=1).",
    RunIdcParams, "read", None, lambda p, c: [idc_str(c.job_dir.as_posix() + "/")],
    output_files=lambda p: list(p.output_files))


def mode_of(op: Op, params: Base) -> str:
    if op.name == "run_idc":
        return "write" if params.save else "read"  # type: ignore[attr-defined]
    return op.mode


# ---------------------------------------------------------------- the generated script

def build_script(op: Op, params: Base, ctx: Ctx, idc_dir: Path, result_path: Path, save: bool) -> str:
    args = ", ".join(["out", *op.call(params, ctx)])
    lines = ["#include <idc.idc>", f'#include "{(idc_dir / "common.idc").as_posix()}"']
    if op.name == "run_idc":
        lines += ["", "static op_run_idc(out, outdir) {", "  auto rc;", "  rc = user_code(out, outdir);",
                  "  return rc;", "}", "", "static user_code(out, outdir) {", params.code, "  return 0;", "}"]  # type: ignore[attr-defined]
    else:
        if op.name == "analyze":
            lines.append(f'#include "{(idc_dir / "ops" / "info.idc").as_posix()}"')
        lines.append(f'#include "{(idc_dir / "ops" / (op.idc + ".idc")).as_posix()}"')
    lines += [
        "",
        "static main() {",
        "  auto out, rc;",
        f"  out = fopen({idc_str(result_path.as_posix())}, \"w\");",
        "  if (out == 0) Exit(3);",
        "  Wait();",
        f"  rc = op_{op.name}({args});",
    ]
    if save:
        lines.append('  if (rc == 0) { if (!SaveBase("", 0)) { err(out, "SaveBase failed"); rc = 1; } }')
    lines += ['  fprintf(out, "#END\\t%d\\n", rc);', "  fclose(out);", "  Exit(rc);", "}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- reading the result

def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append({"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_result(text: str, columns: list[str] | None) -> dict:
    result: dict = {"completed": False, "rc": None, "error": None, "info": {}, "total": None, "rows": []}
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        tag = parts[0]
        if tag == "#END":
            result["completed"] = True
            result["rc"] = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
        elif tag == "#ERROR":
            result["error"] = _unescape("\t".join(parts[1:]))
        elif tag == "#TOTAL":
            result["total"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        elif tag == "#INFO" and len(parts) >= 2:
            result["info"][parts[1]] = _unescape("\t".join(parts[2:]))
        elif columns:
            fields = [_unescape(p) for p in parts]
            row = dict(zip(columns, fields))
            if len(fields) > len(columns):
                row[columns[-1]] = "\t".join(fields[len(columns) - 1:])
            result["rows"].append(row)
        else:
            result["rows"].append(_unescape(line))
    return result

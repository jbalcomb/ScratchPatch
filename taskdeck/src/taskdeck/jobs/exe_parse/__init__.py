"""Parse Executable: split a DOS or Windows executable into small, labelled text files that an AI agent
doing reverse engineering can walk through or search one piece at a time.

The output is a folder named exactly like the executable (e.g. ``WIZARDS.EXE/``) with a summary and
layout, a metadata file, decoded headers and tables, hex-dump chunks per region and the strings, and
optionally a mirror of it in Google Drive. See docs/jobs/exe_parse.md.

    formats.py   reads the headers (MZ, PE, NE; LE/LX detected) into regions and tables
    layout.py    writes the folder
    drive.py     mirrors the folder to Google Drive
"""

from pathlib import Path

from taskdeck.core import WIFI_ONLY, Field, JobPlugin, Pipeline, Stage, StepError, step
from taskdeck.shared import files, gdrive, google_oauth, http, steps

from . import drive, formats, layout

CHUNKS = {"1 KB": 1024, "2 KB": 2048, "4 KB": 4096, "8 KB": 8192, "16 KB": 16384}
MAX_BYTES = 64 * 1024 * 1024
MAX_CHUNKS = 20_000
OUTPUT_FOLDER = ("TaskDeck", "Executables")  # in Downloads, and in My Drive


def default_parent() -> Path:
    return files.locations()[0].path.joinpath(*OUTPUT_FOLDER)


@step(Stage.VALIDATE, name="check file")
def check(ctx):
    exe = Path(ctx["exe"]).expanduser()
    if not exe.is_file():
        raise StepError(f"File not found: {exe}")
    size = exe.stat().st_size
    if size > MAX_BYTES:
        raise StepError(f"{exe.name} is {files.human_size(size)}; TaskDeck parses files up to "
                        f"{files.human_size(MAX_BYTES)}.")
    chunk = CHUNKS[ctx["chunk"]]
    if size / chunk > MAX_CHUNKS:
        raise StepError(f"That makes over {MAX_CHUNKS:,} chunk files. Choose a larger chunk size.")
    parent = Path(ctx["output"]).expanduser() if ctx.get("output") else default_parent()
    dest = parent / exe.name
    if dest.resolve() == exe.resolve():
        raise StepError("The folder can't go next to the executable: it has the same name. "
                        "Choose another folder to save into.")
    if ctx.get("drive") and "google_drive" not in ctx.services:
        raise StepError("Connect Google Drive first (Settings → Accounts), or switch off “Push to Google Drive”.")
    ctx["path"], ctx["dest"], ctx["chunk_bytes"] = exe, dest, chunk


@step(Stage.PREPARE, name="read headers")
def read(ctx):
    ctx["data"] = ctx["path"].read_bytes()
    ctx["parsed"] = formats.parse(ctx["data"])


@step(Stage.EXECUTE, name="write files")
def write(ctx):
    try:
        ctx["written"] = layout.write(ctx["parsed"], ctx["data"], ctx["path"], ctx["dest"], ctx["chunk_bytes"],
                                      min_string=max(2, int(ctx.get("min_string") or 4)), raw=bool(ctx.get("raw")),
                                      cancelled=lambda: ctx._cancelled)
    except layout.NotOurs as exc:
        raise StepError(str(exc)) from None
    except OSError as exc:
        raise StepError(f"Couldn't write {files.display_path(ctx['dest'])}: {exc.strerror or exc}") from None


@step(Stage.EXECUTE, name="push to Google Drive")
def push(ctx):
    if not ctx.get("drive"):
        return
    folder = ctx.settings.get("drive_folder") or "/".join(OUTPUT_FOLDER)
    try:
        ctx["drive_result"] = drive.push(ctx.services["google_drive"](), ctx["dest"], folder,
                                         cancelled=lambda: ctx._cancelled)
    except (gdrive.DriveError, http.HttpError, google_oauth.OAuthError) as exc:
        raise StepError(f"Saved on the phone, but the Google Drive upload stopped: {exc}") from None


@step(Stage.PRESENT, name="show result")
def present(ctx):
    exe, out = ctx["parsed"], ctx["written"]
    rows = {"Format": exe.description or exe.format,
            "Size": f"{exe.size:,} bytes",
            "Regions": len(exe.regions),
            "Chunk files": f"{out.chunks:,} of up to {files.human_size(ctx['chunk_bytes'])}",
            "Strings": out.strings,
            "Files written": f"{len(out.files):,} ({files.human_size(out.bytes)})",
            "Saved to": files.display_path(ctx["dest"])}
    if exe.entry:
        rows["Entry point"] = exe.entry[1]
    if ctx.get("drive_result"):
        location, sent, skipped, trashed = ctx["drive_result"]
        rows["Google Drive"] = location.display()
        rows["Uploaded"] = f"{sent:,} new or changed, {skipped:,} already there" + (
            f", {trashed:,} stale moved to trash" if trashed else "")
    ctx["result_rows"] = rows
    steps.present_rows("result_rows", title=f"Parsed {exe.format}")(ctx)
    if exe.warnings:
        ctx.result.warning = " ".join(exe.warnings)


class ExeParse(JobPlugin):
    id = "exe_parse"
    title = "Parse executable"
    description = ("Split a DOS or Windows .exe into small text files (summary, layout, metadata, headers, "
                   "hex chunks, strings) for an AI agent to reverse engineer, optionally in Google Drive.")
    category = "Tools"
    icon = "MEMORY"
    tags = ("exe", "dll", "binary", "mz", "pe", "ne", "dos", "windows", "hex", "dump", "disassemble",
            "reverse engineering", "strings", "header", "chunks", "agent")
    fields = (
        Field.file("exe", "Executable", required=True, help="A .exe, .dll, .com, .ovl ... file."),
        Field.text("output", "Save into (folder)",
                   help="The parsed files go in a folder named exactly like the executable, inside this one. "
                        "Leave empty for Downloads › TaskDeck › Executables."),
        Field.choice("chunk", "Chunk size", tuple(CHUNKS), default="2 KB",
                     help="Bytes per hex-dump file. 2 KB is 128 lines, roughly 4,000-5,000 tokens."),
        Field.number("min_string", "Shortest string", default=4, help="Characters a run of text needs to count."),
        Field.toggle("raw", "Also save raw .bin chunks", help="For agents that run a disassembler on the bytes."),
        Field.toggle("drive", "Push to Google Drive",
                     help="Mirror the folder to Google Drive (folder set in this job's settings)."),
    )
    settings = (
        Field.text("drive_folder", "Google Drive folder", default="/".join(OUTPUT_FOLDER),
                   help="Under My Drive; created if missing. The executable's folder goes inside it."),
        WIFI_ONLY,
    )
    nature = drive.NATURE  # pushing to Drive is a one-time copy of a fixed snapshot (taskdeck.core.copying)
    run_label = "Parse"

    def build_pipeline(self):
        return Pipeline([steps.require("exe"), check, read, write, push, present])


PLUGIN = ExeParse()

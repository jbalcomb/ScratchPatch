"""What Parse executable and Parse DLL share: their fields and settings, and the pipeline
check → read → write → push to Drive → show result. A job only says what kind of file it takes.

The output folder goes next to the file, named like it with "_" in front (``_WIZARDS.EXE``), the same
on the phone and in Google Drive. See docs/jobs/exe_parse.md and docs/jobs/dll_parse.md.
"""

from __future__ import annotations

from pathlib import Path

from taskdeck.core import GOOGLE_ACCOUNT, WIFI_ONLY, Field, JobPlugin, Pipeline, Stage, StepError, Task, step
from taskdeck.shared import files, steps

from . import drive, formats, layout

CHUNKS = {"1 KB": 1024, "2 KB": 2048, "4 KB": 4096, "8 KB": 8192, "16 KB": 16384}
MAX_BYTES = 64 * 1024 * 1024
MAX_CHUNKS = 20_000
DRIVE_FOLDER = "TaskDeck/Executables"  # default, under My Drive


def folder_name(file_name: str) -> str:
    """The output folder's name, the same on the phone and in Drive: "_WIZARDS.EXE" for WIZARDS.EXE (it
    goes next to the file, so it can't have exactly the same name)."""
    return f"_{file_name}"


def drive_folder(settings: dict) -> str:
    return (settings.get("drive_folder") or "").strip() or DRIVE_FOLDER


def fields(name: str, label: str, help: str, extensions: tuple[str, ...], example: str) -> tuple[Field, ...]:
    """The inputs: the file (input ``name``), where to save, chunk size, strings, Drive."""
    return (
        Field.file(name, label, required=True, help=help, extensions=extensions),
        Field.text("output", "Save into (folder)",
                   help=f"Leave empty to save next to the {label.lower()}, in a folder named like it with “_” in "
                        f"front (_{example}). A folder here gets that folder instead; a relative one is next to "
                        f"the {label.lower()}."),
        Field.choice("chunk", "Chunk size", tuple(CHUNKS), default="2 KB",
                     help="Bytes per hex-dump file. 2 KB is 128 lines, roughly 4,000-5,000 tokens."),
        Field.number("min_string", "Shortest string", default=4, help="Characters a run of text needs to count."),
        Field.toggle("drive", "Push to Google Drive",
                     help="Mirror the folder to Google Drive. Where it goes is shown below; the Drive folder is "
                          "in this job's settings."),
    )


def settings(example: str) -> tuple[Field, ...]:
    return (
        Field.text("drive_folder", "Google Drive folder", default=DRIVE_FOLDER,
                   help=f"Under My Drive; created if missing. The file's folder (_{example}) goes inside it."),
        WIFI_ONLY,
    )


class BinaryJob(JobPlugin):
    """Base of Parse executable and Parse DLL. Subclasses set the metadata and ``fields`` (from
    :func:`fields`), ``file_field`` (the name of the file input) and ``example``."""

    file_field = "exe"
    example = "WIZARDS.EXE"
    expects_dll: bool | None = None  # True / False: warn when the file turns out to be the other kind
    category = "Tools"
    accounts = ((GOOGLE_ACCOUNT, "drive"),)  # unlocked before a run that pushes to Google Drive
    nature = drive.NATURE  # pushing to Drive is a one-time copy of a fixed snapshot (taskdeck.core.copying)
    run_label = "Parse"

    def field_notes(self, raw, settings):
        """The Drive path under the Push to Google Drive toggle."""
        name = Path(str(raw.get(self.file_field) or "")).name or self.placeholder
        where = drive.target(drive_folder(settings), folder_name(name))
        return {"drive": ("Goes to " if raw.get("drive") else "Would go to ") + where}

    @property
    def placeholder(self) -> str:
        """What the note shows before a file is chosen: "EXECUTABLE.EXE", "LIBRARY.DLL"."""
        return "LIBRARY.DLL" if self.expects_dll else "EXECUTABLE.EXE"

    def build_pipeline(self):
        return Pipeline([steps.require(self.file_field), _check(self.file_field), read, write,
                         _push(self.id), _present(self.expects_dll)])


def _check(file_field: str):
    @step(Stage.VALIDATE, name="check file")
    def check(ctx):
        path = Path(ctx[file_field]).expanduser()
        if not path.is_file():
            raise StepError(f"File not found: {path}")
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise StepError(f"{path.name} is {files.human_size(size)}; TaskDeck parses files up to "
                            f"{files.human_size(MAX_BYTES)}.")
        chunk = CHUNKS[ctx["chunk"]]
        if size / chunk > MAX_CHUNKS:
            raise StepError(f"That makes over {MAX_CHUNKS:,} chunk files. Choose a larger chunk size.")
        parent = path.parent
        if ctx.get("output"):  # a relative folder is next to the file too
            parent = path.parent / Path(ctx["output"]).expanduser()
        if ctx.get("drive") and "google_drive" not in ctx.services:
            raise StepError("Connect Google Drive first (Settings → Accounts), or switch off “Push to Google Drive”.")
        ctx["path"], ctx["dest"], ctx["chunk_bytes"] = path, parent / folder_name(path.name), chunk

    return check


@step(Stage.PREPARE, name="read headers")
def read(ctx):
    ctx["data"] = ctx["path"].read_bytes()
    ctx["parsed"] = formats.parse(ctx["data"])


@step(Stage.EXECUTE, name="write files")
def write(ctx):
    try:
        ctx["written"] = layout.write(ctx["parsed"], ctx["data"], ctx["path"], ctx["dest"], ctx["chunk_bytes"],
                                      min_string=max(2, int(ctx.get("min_string") or 4)),
                                      cancelled=lambda: ctx._cancelled)
    except layout.NotOurs as exc:
        raise StepError(str(exc)) from None
    except OSError as exc:
        raise StepError(f"Couldn't write {files.display_path(ctx['dest'])}: {exc.strerror or exc}") from None


def _push(job_id: str):
    @step(Stage.EXECUTE, name="start the Google Drive upload")
    def push(ctx):
        if not ctx.get("drive"):
            return
        folder = drive_folder(ctx.settings)
        client, dest = ctx.services["google_drive"](), ctx["dest"]
        ctx["drive_target"] = drive.target(folder, dest.name)

        def work(task: Task) -> str:
            return drive.push(task, client, dest, folder)

        start = ctx.services.get("start_task")
        if start:  # in the app: a background task with its own progress (and notification), like Extract Archive
            ctx["upload_task"] = start(f"Uploading {dest.name} to {drive.target(folder, '')}", work)
            return
        try:  # no app (tests, scripts): upload now
            ctx["upload_message"] = work(Task(0, "upload", job_id))
        except RuntimeError as exc:
            raise StepError(f"Saved on the phone, but the Google Drive upload stopped: {exc}") from None

    return push


def _present(expects_dll: bool | None):
    @step(Stage.PRESENT, name="show result")
    def present(ctx):
        exe, out = ctx["parsed"], ctx["written"]
        rows = {"Format": exe.description or exe.format,
                "Size": f"{exe.size:,} bytes",
                "Regions": len(exe.regions),
                "Chunk files": f"{out.chunks:,} of up to {files.human_size(ctx['chunk_bytes'])}",
                "Strings": out.strings}
        if exe.exports or expects_dll:
            named = sum(1 for e in exe.exports if e.name)
            rows["Exports"] = f"{len(exe.exports):,} ({named:,} by name)" if exe.exports else "None"
        rows.update({"Files written": f"{len(out.files):,} ({files.human_size(out.bytes)})",
                     "Saved to": files.display_path(ctx["dest"]),
                     "Full path": str(ctx["dest"])})
        if exe.entry:
            rows["Entry point"] = exe.entry[1]
        if ctx.get("drive_target"):
            rows["Google Drive"] = ctx["drive_target"]
            rows["Upload"] = ctx.get("upload_message") or "In the background: progress below."
        ctx["result_rows"] = rows
        steps.present_rows("result_rows", title=f"Parsed {exe.format}")(ctx)
        warnings = list(exe.warnings)
        if expects_dll is True and exe.format != "unknown" and not exe.dll:
            warnings.insert(0, "This file isn't marked as a DLL (it may be a program): parsed anyway.")
        elif expects_dll is False and exe.dll:
            warnings.insert(0, "This file is a DLL: Parse DLL is made for those (the result is the same).")
        if expects_dll and exe.dll and not exe.exports:
            warnings.append("This DLL exports nothing.")
        if warnings:
            ctx.result.warning = " ".join(warnings)

    return present

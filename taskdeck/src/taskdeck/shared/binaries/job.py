"""What Parse executable and Parse DLL share: their fields and settings, and the pipeline
check → read → write → push to Drive → show result. A job only says what kind of file it takes, and
whether it takes supporting DLLs too.

Everything goes into one **base folder**, by default next to the file and named like it with "_" in front
and without the extension (``_Caster`` for Caster.exe), the same in Google Drive. Inside it, one folder per
parsed file (``Caster.exe/``, ``FOO.DLL/``) and the set's overview and cross-reference (bundle.py).
See docs/jobs/exe_parse.md and docs/jobs/dll_parse.md.
"""

from __future__ import annotations

import functools
from pathlib import Path

from taskdeck.core import GOOGLE_ACCOUNT, WIFI_ONLY, Field, JobPlugin, Pipeline, Stage, StepError, Task, step
from taskdeck.shared import files, steps

from . import bundle, drive, formats, layout

CHUNKS = {"1 KB": 1024, "2 KB": 2048, "4 KB": 4096, "8 KB": 8192, "16 KB": 16384}
MAX_BYTES = 64 * 1024 * 1024
MAX_CHUNKS = 20_000
DRIVE_FOLDER = "TaskDeck/Executables"  # default, under My Drive
DLL_EXTENSIONS = (".dll", ".drv", ".ocx", ".cpl", ".sys", ".vxd", ".386")


def base_name(file_name: str) -> str:
    """The base folder's default name: "_Caster" for Caster.exe (it goes next to the file)."""
    return f"_{Path(file_name).stem or file_name}"


def base_folder(path: Path, output: str) -> Path:
    """Where the results go: ``output`` (relative to the file's folder unless absolute), else the default."""
    if not output.strip():
        return path.parent / base_name(path.name)
    chosen = Path(output.strip()).expanduser()
    return chosen if chosen.is_absolute() else path.parent / chosen


def drive_folder(settings: dict) -> str:
    return (settings.get("drive_folder") or "").strip() or DRIVE_FOLDER


def fields(name: str, label: str, help: str, extensions: tuple[str, ...], example: str,
           supporting: bool = False) -> tuple[Field, ...]:
    """The inputs: the file (input ``name``), optionally its supporting DLLs, where to save, chunk size,
    strings, Drive."""
    base = base_name(example)
    out = [Field.file(name, label, required=True, help=help, extensions=extensions)]
    if supporting:
        out.append(Field.files("dlls", "Supporting DLLs", extensions=DLL_EXTENSIONS, lines=2,
                               help="Parsed along with it, each in its own folder next to it, and its imports "
                                    "matched to their exports. One per line; Add… to choose them."))
    out += [
        Field.text("output", "Save into (folder)",
                   help=f"The base folder for the results. Leave empty for “{base}” next to the {label.lower()}; "
                        f"a relative folder is next to it too."),
        Field.choice("chunk", "Chunk size", tuple(CHUNKS), default="2 KB",
                     help="Bytes per hex-dump file. 2 KB is 128 lines, roughly 4,000-5,000 tokens."),
        Field.number("min_string", "Shortest string", default=4, help="Characters a run of text needs to count."),
        Field.toggle("drive", "Push to Google Drive",
                     help="Mirror the base folder to Google Drive. Where it goes is shown below; the Drive folder "
                          "is in this job's settings."),
    ]
    return tuple(out)


def settings(example: str) -> tuple[Field, ...]:
    return (
        Field.text("drive_folder", "Google Drive folder", default=DRIVE_FOLDER,
                   help=f"Under My Drive; created if missing. The base folder ({base_name(example)}) goes inside it."),
        WIFI_ONLY,
    )


@functools.lru_cache(maxsize=8)
def _imported_dlls(path: str, size: int, mtime: float) -> tuple[str, ...]:
    """The DLLs a file imports from (cached: the note under the field asks on every keystroke)."""
    try:
        return tuple(dict.fromkeys(i.dll for i in formats.parse(Path(path).read_bytes()).imports))
    except OSError:
        return ()


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

    @property
    def placeholder(self) -> str:
        """What the notes show before a file is chosen: "EXECUTABLE.EXE", "LIBRARY.DLL"."""
        return "LIBRARY.DLL" if self.expects_dll else "EXECUTABLE.EXE"

    def field_notes(self, raw, settings):
        """Under the fields: where the base folder goes (phone, Drive), and which DLLs the file imports."""
        chosen = str(raw.get(self.file_field) or "").strip()
        path = Path(chosen).expanduser() if chosen else None
        output = str(raw.get("output") or "")
        base = base_folder(path, output) if path else Path(output.strip() or base_name(self.placeholder))
        notes = {"drive": ("Goes to " if raw.get("drive") else "Would go to ")
                 + drive.target(drive_folder(settings), base.name)}
        if path:
            notes["output"] = f"Saves into {base}"
        if "dlls" in {f.name for f in self.fields} and path and path.is_file():
            st = path.stat()
            imported = _imported_dlls(str(path), st.st_size, st.st_mtime)
            if imported:
                here = {p.name.lower() for p in path.parent.iterdir() if p.is_file()}
                found = [d for d in imported if d.lower() in here]
                notes["dlls"] = (f"{path.name} imports from {', '.join(imported)}."
                                 + (f" Next to it: {', '.join(found)}." if found else ""))
        return notes

    def build_pipeline(self):
        return Pipeline([steps.require(self.file_field), _check(self.file_field), read, write,
                         _push(self.id), _present(self.expects_dll)])


def _check(file_field: str):
    @step(Stage.VALIDATE, name="check files")
    def check(ctx):
        main = Path(ctx[file_field]).expanduser()
        paths = [main] + [Path(p).expanduser() for p in (ctx.get("dlls") or [])]
        names: dict[str, Path] = {}
        total = 0
        for path in paths:
            if not path.is_file():
                raise StepError(f"File not found: {path}")
            if path.name.lower() in names and names[path.name.lower()].resolve() != path.resolve():
                raise StepError(f"Two files are named {path.name}: each gets a folder by its name, so only one can.")
            names[path.name.lower()] = path
            size = path.stat().st_size
            if size > MAX_BYTES:
                raise StepError(f"{path.name} is {files.human_size(size)}; TaskDeck parses files up to "
                                f"{files.human_size(MAX_BYTES)}.")
            total += size
        chunk = CHUNKS[ctx["chunk"]]
        if total / chunk > MAX_CHUNKS:
            raise StepError(f"That makes over {MAX_CHUNKS:,} chunk files. Choose a larger chunk size.")
        if ctx.get("drive") and "google_drive" not in ctx.services:
            raise StepError("Connect Google Drive first (Settings → Accounts), or switch off “Push to Google Drive”.")
        ctx["paths"] = list(names.values())  # the main file first, duplicates once
        ctx["base"] = base_folder(main, str(ctx.get("output") or ""))
        ctx["chunk_bytes"] = chunk

    return check


@step(Stage.PREPARE, name="read headers")
def read(ctx):
    ctx["parsed"] = []
    for path in ctx["paths"]:
        data = path.read_bytes()
        ctx["parsed"].append((path, data, formats.parse(data)))


@step(Stage.EXECUTE, name="write files")
def write(ctx):
    base = ctx["base"]
    try:
        bundle.check(base)
        members = []
        for path, data, exe in ctx["parsed"]:
            ctx.check_cancelled()
            written = layout.write(exe, data, path, base / path.name, ctx["chunk_bytes"],
                                   min_string=max(2, int(ctx.get("min_string") or 4)),
                                   cancelled=lambda: ctx._cancelled)
            members.append(bundle.Member(path, exe, written))
        ctx["removed"] = bundle.remove_stale(base, {m.folder for m in members})
        ctx["xref"] = bundle.finish(base, members)
    except layout.NotOurs as exc:
        raise StepError(str(exc)) from None
    except OSError as exc:
        raise StepError(f"Couldn't write {files.display_path(base)}: {exc.strerror or exc}") from None
    ctx["members"] = members
    ctx["include"] = [m.folder for m in members] + [bundle.INDEX, bundle.OVERVIEW] + \
        ([bundle.XREF] if (base / bundle.XREF).is_dir() and ctx["xref"]["resolved"] + ctx["xref"]["not_exported"]
         else [])


def _push(job_id: str):
    @step(Stage.EXECUTE, name="start the Google Drive upload")
    def push(ctx):
        if not ctx.get("drive"):
            return
        folder = drive_folder(ctx.settings)
        client, base, include = ctx.services["google_drive"](), ctx["base"], ctx["include"]
        ctx["drive_target"] = drive.target(folder, base.name)

        def work(task: Task) -> str:
            return drive.push(task, client, base, folder, include=include)

        start = ctx.services.get("start_task")
        if start:  # in the app: a background task with its own progress (and notification), like Extract Archive
            ctx["upload_task"] = start(f"Uploading {base.name} to {drive.target(folder, '')}", work)
            return
        try:  # no app (tests, scripts): upload now
            ctx["upload_message"] = work(Task(0, "upload", job_id))
        except RuntimeError as exc:
            raise StepError(f"Saved on the phone, but the Google Drive upload stopped: {exc}") from None

    return push


def _present(expects_dll: bool | None):
    @step(Stage.PRESENT, name="show result")
    def present(ctx):
        members: list[bundle.Member] = ctx["members"]
        main, others = members[0], members[1:]
        exe = main.exe
        rows = {"Format": exe.description or exe.format,
                "Size": f"{exe.size:,} bytes",
                "Regions": len(exe.regions),
                "Chunk files": f"{sum(m.written.chunks for m in members):,} of up to "
                               f"{files.human_size(ctx['chunk_bytes'])}",
                "Strings": sum(m.written.strings for m in members)}
        if exe.exports or expects_dll:
            named = sum(1 for e in exe.exports if e.name)
            rows["Exports"] = f"{len(exe.exports):,} ({named:,} by name)" if exe.exports else "None"
        if others:
            rows["Supporting DLLs"] = ", ".join(f"{m.path.name} ({len(m.exe.exports):,} exports)" for m in others)
            x = ctx["xref"]
            rows["Imports resolved"] = (f"{x['resolved']:,} to the DLLs here"
                                        + (f", {x['not_exported']:,} not exported by them" if x["not_exported"] else ""))
        rows.update({"Files written": f"{sum(len(m.written.files) for m in members):,} "
                                      f"({files.human_size(sum(m.written.bytes for m in members))})",
                     "Saved to": files.display_path(ctx["base"]),
                     "Full path": str(ctx["base"])})
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
        for m in others:
            if not m.exe.dll:
                warnings.append(f"{m.path.name} isn't marked as a DLL.")
            warnings += [f"{m.path.name}: {w}" for w in m.exe.warnings]
        if ctx.get("removed"):
            warnings.append(f"Removed the results of {', '.join(ctx['removed'])}: not in this run.")
        if warnings:
            ctx.result.warning = " ".join(warnings)

    return present

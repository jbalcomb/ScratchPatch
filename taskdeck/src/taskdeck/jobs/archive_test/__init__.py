"""Archive engine check: open an archive with 7-Zip's library, list it, optionally extract it.

A test job for the 7-Zip engine (step 1 of docs/jobs/extract_archive_SPEC.md): proves the
native library loads on the phone. The real Extract Archive job replaces it.
"""

from pathlib import Path

from taskdeck.core import Field, JobPlugin, Pipeline, Problem, Result, Stage, StepError, default_data_dir, step
from taskdeck.shared import archive, steps


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


@step(Stage.EXECUTE, name="open archive")
def open_archive(ctx):
    try:
        with archive.Archive(ctx["path"], ctx.get("password") or None) as a:
            ctx["format"] = a.format
            ctx["entries"] = a.entries()
            if ctx.get("extract"):
                dest = default_data_dir() / "extract-test" / Path(ctx["path"]).name.split(".")[0]
                a.extract(dest)
                ctx["extracted_to"] = str(dest)
    except archive.ArchiveError as exc:
        raise StepError(str(exc)) from None


@step(Stage.PRESENT)
def report(ctx):
    entries = ctx["entries"]
    files = [e for e in entries if not e.is_dir]
    rows = [
        ("Engine", f"7-Zip {archive.version()}"),
        ("Format", ctx["format"]),
        ("Files", f"{len(files):,}" + (f" (+{len(entries) - len(files)} folders)" if len(entries) > len(files) else "")),
        ("Unpacked size", human_size(sum(e.size for e in files))),
        ("Encrypted", f"{sum(e.encrypted for e in files)} of {len(files)} files"),
    ]
    if ctx.get("extracted_to"):
        rows.append(("Extracted to", ctx["extracted_to"]))
    names = [e.name + ("/" if e.is_dir else "") for e in entries[:40]]
    more = len(entries) - len(names)
    ctx.result = Result(title=Path(ctx["path"]).name, rows=rows,
                        text="\n".join(names) + (f"\n… and {more} more" if more else ""))


class ArchiveTest(JobPlugin):
    id = "archive_test"
    title = "Archive engine check"
    description = "Test the built-in 7-Zip engine: open a zip, 7z or RAR file and list it."
    category = "Tools"
    icon = "INVENTORY_2_OUTLINED"
    tags = ("zip", "7z", "7-zip", "rar", "tar", "unzip", "extract", "archive", "test")
    fields = (
        Field.file("path", "Archive", required=True),
        Field.text("password", "Password (if encrypted)", secret=True),
        Field.toggle("extract", "Also extract it (into TaskDeck's own storage)"),
    )
    run_label = "Open"

    def check_ready(self, settings):
        if archive.available():
            return []
        return [Problem("The 7-Zip engine isn't in this app version. Install the latest APK.", fix="recheck")]

    def build_pipeline(self):
        return Pipeline([steps.require("path"), open_archive, report])


PLUGIN = ArchiveTest()

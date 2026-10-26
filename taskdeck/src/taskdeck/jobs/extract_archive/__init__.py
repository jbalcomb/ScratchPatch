"""Extract Archive: unpack zip, 7z, RAR, tar, gz... (with passwords) into a folder on the phone.

The app shows ``screen.ExtractScreen`` (browse, destination picker, background extraction).
The fields and pipeline below do the same thing in one go, for tests and the generic runner.
See docs/jobs/extract_archive_SPEC.md.
"""

from pathlib import Path

from taskdeck.core import Field, JobPlugin, Pipeline, Problem, Stage, StepError, Task, step
from taskdeck.shared import archive, steps

from . import logic


@step(Stage.VALIDATE, name="check archive")
def check(ctx):
    path = Path(ctx["archive"])
    if not path.is_file():
        raise StepError(f"File not found: {path}")
    s = logic.scan(path, ctx.get("password") or None)
    parent, name = logic.default_destination(path)
    if ctx.get("destination"):
        parent, name = Path(ctx["destination"]), None
    problems = logic.problems(s, parent, name, ctx.get("password") or "")
    if problems:
        raise StepError(" ".join(problems))
    ctx["dest"] = parent / name if name else parent


@step(Stage.EXECUTE, name="extract")
def extract(ctx):
    task = Task(0, "extract", "extract_archive")
    ctx["message"] = logic.extract(task, Path(ctx["archive"]), ctx["dest"], ctx.get("password") or None)


class ExtractArchive(JobPlugin):
    id = "extract_archive"
    title = "Extract archive"
    description = "Unpack zip, 7z, RAR, tar and gz files, password-protected too, into a folder on your phone."
    category = "Tools"
    icon = "UNARCHIVE_OUTLINED"
    tags = ("unzip", "zip", "7z", "7-zip", "rar", "unrar", "tar", "gz", "extract", "archive", "decompress")
    fields = (
        Field.file("archive", "Archive", required=True),
        Field.text("destination", "Extract into (folder)", help="Leave empty for a new folder next to the archive."),
        Field.text("password", "Password (if encrypted)", secret=True),
    )
    run_label = "Extract"

    def check_ready(self, settings):
        problem = archive.load_problem()
        return [Problem(problem, fix="recheck")] if problem else []

    def build_pipeline(self):
        return Pipeline([steps.require("archive"), check, extract, steps.present_text("message", title="Extracted")])

    def make_screen(self, app):
        from .screen import ExtractScreen  # the UI needs flet; the logic above doesn't

        return ExtractScreen(app, self)


PLUGIN = ExtractArchive()

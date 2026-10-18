"""Scaffold a new job plugin: ``python tools/new_job.py my_job "My Job" --category Text``."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

JOBS = Path(__file__).resolve().parents[1] / "src" / "taskdeck" / "jobs"

TEMPLATE = '''"""{title}: {description}"""

from taskdeck.core import Field, JobPlugin, Pipeline, Stage, step
from taskdeck.shared import steps


@step(Stage.EXECUTE)
def work(ctx):
    # Read inputs with ctx["name"], store intermediate values with ctx["key"] = value.
    ctx["output"] = {{"Input": ctx["text"], "Length": len(ctx["text"])}}


class {cls}(JobPlugin):
    id = "{id}"
    title = "{title}"
    description = "{description}"
    category = "{category}"
    icon = "WORK_OUTLINE"  # any name from flet.Icons
    tags = ()  # extra words the command palette should match
    fields = (Field.text("text", "Text", required=True),)

    def build_pipeline(self):
        # Prefer steps from taskdeck.shared; keep only job-specific logic here.
        return Pipeline([steps.require("text"), work, steps.present_rows("output")])


PLUGIN = {cls}()
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("id", help="snake_case package name, also the plugin id")
    ap.add_argument("title")
    ap.add_argument("--category", default="General")
    ap.add_argument("--description", default="Describe what this job does.")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.id):
        ap.error("id must be snake_case")
    target = JOBS / args.id
    if target.exists():
        ap.error(f"{target} already exists")
    target.mkdir()
    cls = "".join(p.capitalize() for p in args.id.split("_"))
    (target / "__init__.py").write_text(TEMPLATE.format(
        id=args.id, title=args.title, description=args.description, category=args.category, cls=cls))
    print(f"created {target / '__init__.py'}\nnext: fill in tags/icon, then run `python -m taskdeck.review`")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import textwrap

import pytest

from taskdeck.core import PluginRegistry
from taskdeck.review.__main__ import main as review_cli
from taskdeck.review.analyzer import find_step_runs, review, to_markdown

PLUGIN_TEMPLATE = '''
from taskdeck.core import Field, JobPlugin, Pipeline, Stage, step
from taskdeck.shared import steps


def {helper}({arg}):
    """Turn a list of words into a title, skipping blanks."""
    out = []
    for w in {arg}:
        w = w.strip()
        if not w:
            continue
        out.append(w[0].upper() + w[1:].lower())
    return " ".join(out)


@step(Stage.EXECUTE)
def work(ctx):
    ctx["out"] = {helper}(ctx["text"].split())


class P(JobPlugin):
    id = "{pid}"
    title = "{pid}"
    description = "fixture plugin {pid}"
    tags = ("x",)
    fields = (Field.text("text", "Text", required=True),)

    def build_pipeline(self):
        return Pipeline([steps.require("text"), steps.normalize_text("text"), work, steps.present_text("out")])


PLUGIN = P()
'''


@pytest.fixture
def fixture_registry(tmp_path, monkeypatch):
    pkg = tmp_path / "fixturejobs"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # Same logic, different names: the review must see through renaming.
    (pkg / "alpha.py").write_text(PLUGIN_TEMPLATE.format(pid="alpha", helper="titleize", arg="words"))
    (pkg / "beta.py").write_text(PLUGIN_TEMPLATE.format(pid="beta", helper="make_title", arg="parts"))
    (pkg / "gamma.py").write_text(textwrap.dedent('''
        from taskdeck.core import Field, JobPlugin, Pipeline, Stage, step

        @step(Stage.PRESENT)
        def show(ctx):
            pass

        @step(Stage.VALIDATE)
        def check(ctx):
            pass

        class G(JobPlugin):
            id = "gamma"
            title = "Gamma"
            description = ""
            fields = (Field.text("t", "T", required=True),)

            def build_pipeline(self):
                return Pipeline([show, check])

        PLUGIN = G()
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    return PluginRegistry().discover("fixturejobs", entry_point_group="none")


def test_detects_renamed_duplicate_helpers(fixture_registry):
    report = review(fixture_registry)
    pairs = {(d.a.split(":")[1].split()[0], d.b.split(":")[1].split()[0]) for d in report.duplicates}
    assert ("titleize", "make_title") in pairs
    dup = next(d for d in report.duplicates if "titleize" in d.a)
    assert dup.similarity == 1.0 and dup.kind == "extract"


def test_detects_repeated_step_runs_and_shapes(fixture_registry):
    report = review(fixture_registry)
    run = report.step_runs[0]
    assert run.plugins == ["alpha", "beta"]
    assert run.keys[:2] == ("shared.require", "shared.normalize_text")
    assert ("validate", "prepare", "execute", "present") in [g.shape for g in report.shapes]


def test_hygiene_flags_bad_plugin(fixture_registry):
    msgs = [i.message for i in review(fixture_registry).issues if i.plugin == "gamma"]
    assert any("description" in m for m in msgs)
    assert any("tags" in m for m in msgs)
    assert any("stage order" in m for m in msgs)


def test_markdown_report_mentions_findings(fixture_registry):
    md = to_markdown(review(fixture_registry))
    assert "extract to `taskdeck.shared`" in md and "gamma" in md


def test_step_runs_reports_only_maximal_sequences():
    runs = find_step_runs({"p": ["a", "b", "c", "x"], "q": ["a", "b", "c", "y"], "r": ["a", "b"]})
    assert [(r.keys, r.plugins) for r in runs] == [(("a", "b"), ["p", "q", "r"]), (("a", "b", "c"), ["p", "q"])]


def test_cli_on_builtin_plugins_is_clean(tmp_path):
    out = tmp_path / "report.md"
    assert review_cli(["--out", str(out), "--strict"]) == 0
    assert out.read_text().startswith("# Plugin commonality review")

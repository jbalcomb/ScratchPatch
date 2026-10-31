import pytest

from taskdeck.core import Field, JobPlugin, Pipeline, PluginRegistry, StepError
from taskdeck.review.analyzer import check_plugin

BUILTIN = {"text_stats", "case_convert", "hash_text", "json_format", "unit_convert", "tip_split", "daily_log", "extract_archive", "exe_parse", "dll_parse",
           "daily_log_reconcile", "receipts_capture", "receipts_review", "receipts_unmatched", "receipts_product_photo",
           "receipts_shopping", "receipts_reconcile"}


@pytest.fixture(scope="module")
def registry():
    return PluginRegistry().discover()


def test_discovers_all_builtin_jobs(registry):
    assert {p.id for p in registry} == BUILTIN
    assert registry.errors == {}


@pytest.mark.parametrize("job_id", sorted(BUILTIN))
def test_every_builtin_passes_plugin_hygiene(registry, job_id):
    # The same checks the plugin review runs; a new job must pass them to merge.
    assert check_plugin(registry.get(job_id)) == []


def test_field_coercion():
    assert Field.number("n", "N").coerce("2.5") == 2.5
    assert Field.number("n", "N", default=3).coerce("") == 3
    assert Field.toggle("t", "T").coerce(None) is False
    with pytest.raises(StepError, match="required"):
        Field.text("t", "Name", required=True).coerce("  ")
    with pytest.raises(StepError, match="number"):
        Field.number("n", "N").coerce("abc")
    with pytest.raises(StepError, match="one of"):
        Field.choice("c", "C", ["a", "b"]).coerce("z")


def test_duplicate_ids_rejected():
    class A(JobPlugin):
        id = "same"

        def build_pipeline(self):
            return Pipeline([])

    reg = PluginRegistry()
    reg.register(A())
    with pytest.raises(ValueError):
        reg.register(A())


def test_broken_plugin_is_recorded_not_fatal(tmp_path, monkeypatch):
    pkg = tmp_path / "brokenjobs"
    (pkg / "bad").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "bad" / "__init__.py").write_text("raise RuntimeError('kaboom')")
    monkeypatch.syspath_prepend(str(tmp_path))
    reg = PluginRegistry().discover("brokenjobs", entry_point_group="none")
    assert len(reg) == 0
    assert "kaboom" in reg.errors["brokenjobs.bad"]


CASES = [
    ("text_stats", {"text": "One two. Three!"}, True, "Words"),
    ("text_stats", {"text": "   "}, False, "required"),
    ("case_convert", {"text": "Hello World", "style": "snake_case"}, True, "hello_world"),
    ("case_convert", {"text": "parseHTTPResponse code", "style": "kebab-case"}, True, "parse-httpresponse-code"),
    ("hash_text", {"text": "abc", "algorithm": "sha256"}, True,
     "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
    ("hash_text", {"text": "abc", "algorithm": "crc32"}, True, "352441c2"),
    ("json_format", {"text": '{"b":1,"a":2}', "sort_keys": True, "minify": True}, True, '{"a":2,"b":1}'),
    ("json_format", {"text": "{bad"}, False, "Invalid JSON"),
    ("json_format", {"text": "{}", "indent": "20"}, False, "at most"),
    ("unit_convert", {"value": "1", "from_unit": "mi", "to_unit": "km"}, True, "1.60934 km"),
    ("unit_convert", {"value": "212", "from_unit": "°F", "to_unit": "°C"}, True, "100 °C"),
    ("unit_convert", {"value": "-500", "from_unit": "°C", "to_unit": "K"}, False, "absolute zero"),
    ("unit_convert", {"value": "1", "from_unit": "kg", "to_unit": "m"}, False, "Can't convert"),
    ("tip_split", {"bill": "100", "tip_percent": "20", "people": "4"}, True, "30.00"),
    ("tip_split", {"bill": "100", "people": "0"}, False, "at least"),
]


@pytest.mark.parametrize("job_id,inputs,ok,expect", CASES)
def test_builtin_jobs(registry, job_id, inputs, ok, expect):
    report = registry.get(job_id).run(inputs)
    assert report.ok is ok, report.error
    text = report.result.as_plain_text() if ok else report.error
    assert expect in text

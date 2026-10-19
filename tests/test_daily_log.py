"""Daily Log → Sheet: acceptance criteria from the PRD (§8, §14). No model or Google needed."""

from datetime import date

import pytest

from taskdeck.core import StepError
from taskdeck.jobs import daily_log
from taskdeck.jobs.daily_log import extract, sheets
from taskdeck.jobs.daily_log.record import (
    COLUMNS, build_record, build_row, header_problems, hours_between, miles_driven, missing_fields, norm_time, num,
    resolve_date,
)

TODAY = date(2026, 10, 18)
SAMPLE_LOG = """Friday October 16th
Starting mileage 52 mi
Start time 10:38
Online at 11:00
Got gas at 32 miles
New miles 540 miles
Offline at 2146
13 orders for $187.82
Arrival time 2209
Ending mileage? 303"""

# What a well-behaved model returns for SAMPLE_LOG (note the raw 2146-style time and "$").
SAMPLE_RAW = {
    "month": 10, "day": 16, "departure_time": "10:38", "departure_mileage": 52, "online_time": "11:00",
    "offline_time": "2146", "arrival_time": "22:09", "arrival_mileage": 303, "batches": None, "orders": 13,
    "earnings": "$187.82", "gas_fills": [{"at": 32, "after": 540}],
}
HEADERS = [c.header or "" for c in COLUMNS]


# ---- normalization ------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("10:38", "10:38"), ("2146", "21:46"), ("938", "09:38"), ("9:38 pm", "09:38"),
                                          ("2460", None), ("25:00", None), ("12", None), (None, None)])
def test_norm_time(raw, expected):
    assert norm_time(raw) == expected


@pytest.mark.parametrize("raw,expected", [("$1,234.50", 1234.5), ("13", 13), (13.0, 13), ("abc", None), (None, None),
                                          (True, None)])
def test_num(raw, expected):
    assert num(raw) == expected


def test_resolve_date_uses_previous_year_when_far_future():
    assert resolve_date(10, 16, TODAY) == "10/16/2026"
    assert resolve_date(10, 24, TODAY) == "10/24/2026"  # within 7 days ahead
    assert resolve_date(12, 30, TODAY) == "12/30/2025"
    assert resolve_date(2, 30, TODAY) is None
    assert resolve_date(None, 3, TODAY) is None


def test_hours_cross_midnight():  # acceptance 4
    assert hours_between("18:00", "01:30") == 7.5
    assert hours_between("11:00", "21:46") == 10.77
    assert hours_between(None, "21:46") is None


# ---- calculations / acceptance -----------------------------------------------------

def test_worked_example_record_and_row():  # acceptance 1 (§8)
    rec = build_record(SAMPLE_RAW, TODAY)
    assert rec == {
        "date": "10/16/2026", "departure_time": "10:38", "departure_mileage": 52, "online_time": "11:00",
        "offline_time": "21:46", "arrival_time": "22:09", "arrival_mileage": 303, "batches": None, "orders": 13,
        "earnings": 187.82, "online_hours": 10.77, "onclock_hours": 11.52, "gas_at": "32", "gas_after": "540",
        "miles_driven": 257,
    }
    assert build_row(rec) == ["10/16/2026", "10:38", 52, "11:00", "21:46", "22:09", 303, "", 13, 187.82, 10.77,
                              11.52, "32", "540", 257]


def test_no_gas_stop():  # acceptance 2
    rec = build_record({**SAMPLE_RAW, "gas_fills": []}, TODAY)
    assert rec["miles_driven"] == 52 - 303
    assert rec["gas_at"] is None and rec["gas_after"] is None
    row = build_row(rec)
    assert row[12] == "" and row[13] == ""


def test_two_gas_stops():  # acceptance 3
    raw = {**SAMPLE_RAW, "departure_mileage": 100, "arrival_mileage": 200,
           "gas_fills": [{"at": 32, "after": 540}, {"at": "40", "after": "$520"}]}
    rec = build_record(raw, TODAY)
    # (100-32) + (540-40) + (520-200) = 68 + 500 + 320
    assert rec["miles_driven"] == 888
    assert rec["gas_at"] == "32, 40" and rec["gas_after"] == "540, 520"
    assert miles_driven(None, 200, []) is None


def test_missing_fields_listed_but_gas_optional():  # acceptance 5
    rec = build_record({**SAMPLE_RAW, "gas_fills": None, "orders": None}, TODAY)
    assert missing_fields(rec) == ["Batch count", "Order count"]


def test_gas_fills_tolerates_junk():
    rec = build_record({**SAMPLE_RAW, "gas_fills": [{"at": 32}, "nonsense", {"at": 10, "after": 300}]}, TODAY)
    assert rec["gas_at"] == "10"


# ---- header validation ------------------------------------------------------------

def test_headers_ok():
    assert header_problems(HEADERS) == []
    assert header_problems(["anything"] + [f"  {h}  " for h in HEADERS[1:]]) == []  # A1 unchecked, trim


def test_headers_missing_and_swapped():  # acceptance 6
    only_b_to_l = HEADERS[:12]
    assert header_problems(only_b_to_l) == [
        "M1 should be “Gas At Mileage” but is “”",
        "N1 should be “Post-Gas Mileage” but is “”",
        "O1 should be “Miles Driven” but is “”",
    ]
    swapped = HEADERS.copy()
    swapped[1], swapped[2] = swapped[2], swapped[1]
    assert [p[:2] for p in header_problems(swapped)] == ["B1", "C1"]


class FakeBackend:
    def __init__(self, headers):
        self.headers = headers
        self.rows = []

    def header_row(self):
        return self.headers

    def append(self, row):
        self.rows.append(row)


def test_save_writes_nothing_when_headers_wrong():  # acceptance 6
    backend = FakeBackend(HEADERS[:12])
    with pytest.raises(StepError, match="M1 should be"):
        sheets.save_record(build_record(SAMPLE_RAW, TODAY), backend)
    assert backend.rows == []


def test_save_appends_one_row():
    backend = FakeBackend(HEADERS)
    sheets.save_record(build_record(SAMPLE_RAW, TODAY), backend)
    assert len(backend.rows) == 1 and backend.rows[0][14] == 257


# ---- model endpoints ----------------------------------------------------------------

def test_endpoints_from_settings():
    eps = extract.endpoints({"llm_url": "http://ws:8080/v1/chat/completions", "fallback_api_key": "k"})
    assert [e.label for e in eps] == ["AI Workstation", "Gemini", "Gemini (flash-lite)"]
    assert eps[2].model == "gemini-flash-lite-latest"
    assert eps[1].model == "gemini-flash-latest" and "generativelanguage.googleapis.com" in eps[1].url
    assert [e.label for e in extract.endpoints({"llm_url": "x"})] == ["AI Workstation"]  # no key -> no fallback


# ---- whole job through the plugin contract ------------------------------------------------

SETTINGS = {"llm_url": "http://ws:8080/v1/chat/completions", "service_account_json": "{}"}


def test_parse_is_side_effect_free_and_previews(monkeypatch):
    monkeypatch.setattr(extract, "ask_model", lambda text, settings: (dict(SAMPLE_RAW), "Gemini"))
    monkeypatch.setattr(daily_log, "build_record", lambda raw: build_record(raw, TODAY))
    monkeypatch.setattr(sheets, "make_backend", lambda s: pytest.fail("parse must not touch the sheet"))
    report = daily_log.PLUGIN.run({"log": SAMPLE_LOG}, settings=SETTINGS)
    assert report.ok, report.error
    rows = dict(report.result.rows)
    assert rows["Earnings"] == "$187.82" and rows["Batch count"] == "—" and rows["Miles driven"] == "257"
    assert report.result.warning == "Not found: Batch count."
    assert report.result.payload["earnings"] == 187.82  # sheet gets the plain number
    assert report.result.title == "Row for 10/16/2026 · parsed by Gemini"


def test_parse_requires_model_url():
    report = daily_log.PLUGIN.run({"log": SAMPLE_LOG}, settings={})
    assert not report.ok and "AI Workstation URL or a Gemini API key" in report.error


def test_parse_requires_log_text():
    assert "required" in daily_log.PLUGIN.run({"log": "  "}, settings=SETTINGS).error


def test_commit_validates_then_appends(monkeypatch):
    backend = FakeBackend(HEADERS)
    monkeypatch.setattr(sheets, "make_backend", lambda s: backend)
    ok, msg = daily_log.PLUGIN.try_commit(build_record(SAMPLE_RAW, TODAY), SETTINGS)
    assert ok and msg == "Saved row for 10/16/2026"
    assert len(backend.rows) == 1


def test_commit_without_key_explains():
    ok, msg = daily_log.PLUGIN.try_commit(build_record(SAMPLE_RAW, TODAY), {"llm_url": "x"})
    assert not ok and "service account key" in msg


def test_commit_with_bad_key_explains():
    ok, msg = daily_log.PLUGIN.try_commit(build_record(SAMPLE_RAW, TODAY), {"service_account_json": '{"type": "user"}'})
    assert not ok and "isn't a service account key" in msg


def test_settings_test_button_lists_each_model(monkeypatch):
    monkeypatch.setattr(extract, "ping", lambda e: f"✓ {e.label}: answered in 1.0 s")
    assert extract.test_models({"llm_url": "http://ws:8080/v1/chat/completions"}).splitlines() == [
        "✓ AI Workstation: answered in 1.0 s", "Gemini: not set up (no API key)"]
    assert daily_log.PLUGIN.settings_actions[0].label == "Test AI models"


# ---- readiness (checked when the job opens) ---------------------------------------------

import json as _json

GOOD_KEY = _json.dumps({"type": "service_account", "client_email": "bot@p.iam.gserviceaccount.com",
                        "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"})
READY = {"fallback_api_key": "k", "sheet_id": "S", "service_account_json": GOOD_KEY}


def messages(problems, blocking=True):
    return [p.message for p in problems if p.blocking == blocking]


def test_ready_needs_model_key_and_sheet(monkeypatch):
    monkeypatch.setattr("taskdeck.shared.google.ServiceAccountKey.check", lambda self: None)
    assert daily_log.PLUGIN.check_ready(READY) == []
    got = messages(daily_log.PLUGIN.check_ready({}))
    assert any("Sheet ID" in m for m in got)
    assert any("service account key" in m for m in got)
    assert any("AI Workstation URL or a Gemini API key" in m for m in got)


def test_ready_reports_unreadable_key():
    got = messages(daily_log.PLUGIN.check_ready({**READY, "service_account_json": GOOD_KEY[:50]}))
    assert any("isn't valid JSON" in m for m in got)
    got = messages(daily_log.PLUGIN.check_ready(READY))  # parses, but the private key is junk
    assert any("damaged or incomplete" in m for m in got)


def test_ready_flags_localhost_workstation():
    p = daily_log.PLUGIN.check_ready({**READY, "llm_url": "http://localhost:8080/v1/chat/completions"})
    assert any("phone itself" in m for m in messages(p, blocking=False))  # Gemini key set: only a warning


def test_ready_online_checks_sheet_headers(monkeypatch):
    monkeypatch.setattr(sheets, "make_backend", lambda s, timeout=30: FakeBackend(HEADERS[:12]))
    p = daily_log.PLUGIN.check_ready_online(READY)
    assert any("M1 should be" in m for m in messages(p))
    monkeypatch.setattr(sheets, "make_backend", lambda s, timeout=30: FakeBackend(HEADERS))
    assert daily_log.PLUGIN.check_ready_online(READY) == []


def test_ready_online_no_connection_is_not_blocking(monkeypatch):
    def offline(s, timeout=30):
        raise StepError("Can't reach sheets.googleapis.com: Network is unreachable")

    monkeypatch.setattr(sheets, "make_backend", offline)
    p = daily_log.PLUGIN.check_ready_online(READY)
    assert p and not p[0].blocking and "no connection" in p[0].message

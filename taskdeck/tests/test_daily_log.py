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
    """An in-memory tab: ``values`` is what the sheet holds (row 1 = headers)."""

    def __init__(self, headers, *rows):
        self.values = [list(headers), *[list(r) for r in rows]]
        self.writes, self.backups = [], []

    def rows(self):
        values = [list(r) for r in self.values]
        while values and all(str(v).strip() == "" for v in values[-1]):
            values.pop()  # the API leaves off trailing empty rows
        return values

    def write(self, first_row, rows):
        self.writes.append((first_row, len(rows)))
        while len(self.values) < first_row - 1 + len(rows):
            self.values.append([])
        for i, row in enumerate(rows):
            self.values[first_row - 1 + i] = list(row)

    def backup(self, title):
        self.backups.append(title)


ROW = build_row(build_record(SAMPLE_RAW, TODAY))


def test_save_writes_nothing_when_headers_wrong():  # acceptance 6
    backend = FakeBackend(HEADERS[:12])
    with pytest.raises(StepError, match="M1 should be"):
        sheets.save_record(build_record(SAMPLE_RAW, TODAY), backend)
    assert backend.writes == []


def test_save_writes_below_the_last_filled_row():
    backend = FakeBackend(HEADERS, ["10/15/2026", "11:13"], [], ["10/14/2026"])
    msg = sheets.save_record(build_record(SAMPLE_RAW, TODAY), backend)
    assert msg == "Saved row for 10/16/2026 (row 5)" and backend.writes == [(5, 1)]
    assert backend.values[4][14] == 257
    assert sheets.save_record(build_record(SAMPLE_RAW, TODAY), FakeBackend(HEADERS)).endswith("(row 2)")


def test_save_same_date_same_values_writes_nothing():
    sheet_row = ["2026-10-16"] + [str(v) for v in ROW[1:]]  # typed by hand, shown as text
    sheet_row[12] = "32"
    backend = FakeBackend(HEADERS, sheet_row)
    assert sheets.save_record(build_record(SAMPLE_RAW, TODAY), backend) == "Already in the sheet (row 2); nothing changed"
    assert backend.writes == []


def test_save_same_date_different_values_asks_then_replaces():
    from taskdeck.core import NeedsConfirmation

    old = list(ROW)
    old[9] = 190
    backend = FakeBackend(HEADERS, ["10/15/2026"], old)
    with pytest.raises(NeedsConfirmation) as info:
        sheets.save_record(build_record(SAMPLE_RAW, TODAY), backend)
    assert "Row 3 already has 10/16/2026" in str(info.value) and "Earnings: 190 → 187.82" in str(info.value)
    assert info.value.confirm_label == "Replace row 3" and backend.writes == []
    assert sheets.save_record(info.value.payload, backend) == "Replaced row 3 (10/16/2026)"
    assert backend.writes == [(3, 1)] and backend.values[2][9] == 187.82


def test_replace_refuses_if_the_row_moved():
    backend = FakeBackend(HEADERS, ["10/15/2026"])
    with pytest.raises(StepError, match="changed since"):
        sheets.save_record({**build_record(SAMPLE_RAW, TODAY), "_replace_row": 2}, backend)


def test_save_refuses_when_date_already_twice():
    backend = FakeBackend(HEADERS, ROW, ROW)
    with pytest.raises(StepError, match="Reconcile"):
        sheets.save_record(build_record(SAMPLE_RAW, TODAY), backend)
    assert backend.writes == []


# ---- reconciliation -------------------------------------------------------------------

from taskdeck.jobs.daily_log import reconcile, reconcile_job  # noqa: E402

DAY15 = ["10/15/2026", "11:13", 191, "11:29", "21:01", "21:28", 53, 6, 10, 115.34, 9.53, 10.25, "-", "-", 138]
MESSY = [HEADERS, DAY15] + [[]] * 39 + [["2026-10-18", "8:37", 303, "8:54"], DAY15[:12] + ["", "", 138], ROW]


def test_plan_for_the_messy_sheet():
    plan = reconcile.plan(MESSY)
    assert plan.changes == [
        "Remove 39 blank rows (rows 3–41).",
        "Remove row 43: a duplicate of row 2 (10/15/2026).",
        "Row 42: write the date “2026-10-18” as 10/18/2026.",
        "Put the rows in date order.",
    ]
    assert [r[0] for r in plan.rows] == ["10/15/2026", "10/16/2026", "10/18/2026"]
    assert plan.rows[0] == DAY15  # the hand-typed copy is kept
    assert plan.span == 44
    assert plan.remarks == [
        "Row 42 (10/18/2026): Offline Time, Arrival Time, Arrival Mileage, Batch Count, Order Count, Earnings empty.",
        "Row 44 (10/16/2026): Batch Count empty.",
    ]


def test_plan_keeps_the_more_complete_of_two_different_rows():
    partial = DAY15[:5]
    plan = reconcile.plan([HEADERS, partial, DAY15[:4] + ["21:05"] + DAY15[5:]])
    assert len(plan.rows) == 1 and plan.rows[0][4] == "21:05"
    assert "Remove row 2: another row for 10/15/2026" in plan.changes[0]


def test_plan_tidy_sheet_has_no_changes():
    plan = reconcile.plan([HEADERS, DAY15, ROW])
    assert not plan.changed and plan.rows == [DAY15, ROW]


def test_plan_keeps_rows_without_a_date_at_the_end():
    plan = reconcile.plan([HEADERS, ["note"], [], ROW, DAY15])
    assert [r[0] for r in plan.rows] == ["10/15/2026", "10/16/2026", "note"]
    assert any("no readable date" in r for r in plan.remarks)


def test_reconcile_job_previews_then_applies_with_backup(monkeypatch):
    backend = FakeBackend(*MESSY)
    monkeypatch.setattr(sheets, "make_backend", lambda s, timeout=30: backend)
    report = reconcile_job.PLUGIN.run({}, settings=SETTINGS)
    assert report.ok, report.error
    assert report.result.title == "4 fixes to apply" and backend.writes == []  # preview writes nothing
    ok, msg = reconcile_job.PLUGIN.try_commit(report.result.payload, SETTINGS)
    assert ok, msg
    assert len(backend.backups) == 1 and backend.backups[0] in msg
    assert backend.rows() == [HEADERS, DAY15, ROW, ["10/18/2026", "8:37", 303, "8:54"] + [""] * 11]
    assert not reconcile_job.PLUGIN.run({}, settings=SETTINGS).result.payload  # now tidy: nothing to apply


def test_reconcile_refuses_if_the_sheet_changed(monkeypatch):
    backend = FakeBackend(*MESSY)
    monkeypatch.setattr(sheets, "make_backend", lambda s, timeout=30: backend)
    payload = reconcile_job.PLUGIN.run({}, settings=SETTINGS).result.payload
    backend.values.append(["10/20/2026"])
    ok, msg = reconcile_job.PLUGIN.try_commit(payload, SETTINGS)
    assert not ok and "changed since" in msg and backend.backups == []


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
    assert ok and msg == "Saved row for 10/16/2026 (row 2)"
    assert backend.writes == [(2, 1)]


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
    monkeypatch.setattr(sheets, "make_backend", lambda s, timeout=30: FakeBackend(*MESSY))
    (p,) = daily_log.PLUGIN.check_ready_online(READY)
    assert not p.blocking and p.fix == "job:daily_log_reconcile" and "39 blank rows" in p.message


def test_ready_online_no_connection_is_not_blocking(monkeypatch):
    def offline(s, timeout=30):
        raise StepError("Can't reach sheets.googleapis.com: Network is unreachable")

    monkeypatch.setattr(sheets, "make_backend", offline)
    p = daily_log.PLUGIN.check_ready_online(READY)
    assert p and not p[0].blocking and "no connection" in p[0].message


class FakeClient:
    def __init__(self, values):
        self.values, self.writes = values, []

    def get_values(self, rng, raw=False):
        return self.values

    def write_rows(self, rng, rows):
        self.writes.append((rng, rows))


def test_service_account_backend_writes_exact_range():
    backend = sheets.ServiceAccountBackend.__new__(sheets.ServiceAccountBackend)
    backend.client, backend.tab = FakeClient([HEADERS]), "Sheet1"
    assert backend.rows() == [HEADERS]
    backend.write(5, [ROW, ["x"]])
    assert backend.client.writes == [("'Sheet1'!A5:O6", [ROW, ["x"] + [""] * 14])]


def test_workstation_api_key_is_sent():
    (ws,) = extract.endpoints({"llm_url": "http://100.83.88.59:8080/v1/chat/completions", "llm_api_key": "secret"})
    assert ws.api_key == "secret" and ws.label == "AI Workstation"


def test_plan_accepts_dates_without_leading_zeros():
    day3 = ["10/3/2026"] + DAY15[1:]
    plan = reconcile.plan([HEADERS, day3, DAY15])
    assert not plan.changed  # the sheet's own date format, not a problem
    assert reconcile.plan([HEADERS, ["2026-10-03"] + DAY15[1:]]).changes == [
        "Row 2: write the date “2026-10-03” as 10/3/2026."]


def row(**cells):
    """A sheet row from record fields, everything else empty."""
    out = [""] * 15
    for c in COLUMNS:
        if c.field in cells:
            out[c.number - 1] = cells[c.field]
    return out


FULL = dict(date="10/16/2026", departure_time="10:38", departure_mileage=52, online_time="11:00",
            offline_time="21:46", arrival_time="22:09", arrival_mileage=303, batches=6, orders=13, earnings=187.82)


def test_dash_means_known_missing_and_flows_into_calculated_columns():  # case 1
    plan = reconcile.plan([HEADERS, row(**{**FULL, "offline_time": "-", "arrival_time": "-"}, miles_driven=251)])
    assert plan.changes == ["Row 2 (10/16/2026): Online Hours → - (an input is “-”); "
                            "Onclock Hours → - (an input is “-”)."]
    assert plan.remarks == []  # "-" is fine
    assert plan.rows[0][10] == "-" and plan.rows[0][11] == "-"


def test_empty_input_is_reported_not_its_calculated_column():  # case 2
    plan = reconcile.plan([HEADERS, row(**{**FULL, "offline_time": ""}, onclock_hours=11.52, miles_driven=251)])
    assert plan.changes == [] and plan.remarks == ["Row 2 (10/16/2026): Offline Time empty."]


def test_calculated_columns_are_filled_when_inputs_are_there():  # case 3
    plan = reconcile.plan([HEADERS, row(**FULL, gas_at="32", gas_after="540")])
    assert plan.changes == ["Row 2 (10/16/2026): Online Hours → 10.77 (calculated); "
                            "Onclock Hours → 11.52 (calculated); Miles Driven → 257 (calculated)."]
    assert plan.rows[0][10:15] == [10.77, 11.52, "32", "540", 257]


def test_existing_calculated_values_and_dashes_are_left_alone():
    done = row(**FULL, online_hours="-", onclock_hours=11.52, miles_driven=251)
    plan = reconcile.plan([HEADERS, done])
    assert not plan.changed and plan.remarks == [] and plan.rows[0] == done


def test_gas_columns():
    half = reconcile.plan([HEADERS, row(**FULL, gas_at="32", online_hours=1, onclock_hours=1)])
    assert half.remarks == ["Row 2 (10/16/2026): Post-Gas Mileage empty."] and not half.changed
    dash = reconcile.plan([HEADERS, row(**FULL, gas_at="-", gas_after="-", online_hours=1, onclock_hours=1)])
    assert dash.changes == ["Row 2 (10/16/2026): Miles Driven → - (an input is “-”)."] and dash.remarks == []
    odd = reconcile.plan([HEADERS, row(**FULL, gas_at="32, 40", gas_after="540", online_hours=1, onclock_hours=1)])
    assert "different numbers" in odd.remarks[0] and not odd.changed


def test_day_off_row():
    plan = reconcile.plan([HEADERS, row(date="9/7/2026", departure_time="-", earnings="-")])
    assert plan.changes == ["Row 2 (9/7/2026): Onclock Hours → - (an input is “-”)."]
    assert plan.remarks == ["Row 2 (9/7/2026): Departure Mileage, Online Time, Offline Time, Arrival Time, "
                            "Arrival Mileage, Batch Count, Order Count empty."]

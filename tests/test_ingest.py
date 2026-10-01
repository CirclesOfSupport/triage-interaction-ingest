"""Run from the repo root:  python -m pytest -q tests

The parity test needs the manual builder script; point MANUAL_BUILDER at it to run it.
"""

import csv
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import staging  # noqa: E402
import main  # noqa: E402


def resp(rid, trid, submitted, status="Complete", **answers):
    sd = {"125": {"id": 125, "type": "HIDDEN", "answer": trid}} if trid is not None else {}
    for qid, val in answers.items():
        q = qid.lstrip("q")
        if isinstance(val, list):
            sd[q] = {"id": int(q), "type": "parent",
                     "options": {str(i): {"answer": v} for i, v in enumerate(val)}}
        else:
            sd[q] = {"id": int(q), "type": "TEXTBOX", "answer": val}
    return {"id": str(rid), "status": status, "date_submitted": submitted, "survey_data": sd}


SAMPLE = [
    resp(1, "T1", "2026-09-01 10:00:00 EDT", q136="Initial Contact", q119="9/1 4:27pm",
         q135="9/1/2026 5:00 pm", q126="9/1 5:30pm", q128=["Phone", "Text"], q134="Low",
         q131="No", q130="No"),
    # T1 resubmitted later: the later one must win
    resp(2, "T1", "2026-09-02 09:00:00 EDT", q136="Follow-Up", q119="1900",
         q135="did not respond", q126="9/2 8pm", q128=["Text"], q140="No"),
    # Earlier duplicate arriving after the later one in list order: must NOT win
    resp(3, "T2", "2026-09-03 12:00:00 EDT", q136="Initial Contact", q119="843",
         q135="", q120="10.11.2023 12:00am"),
    # Stale T2 answered 140; the later T2 submission (140 hidden) wins, so it lands null
    resp(4, "T2", "2026-09-03 11:00:00 EDT", q136="STALE", q119="1:00", q140="Yes"),
    resp(5, "testID", "2026-09-04 12:00:00 EDT", q119="1:00"),
    resp(6, None, "2026-09-04 12:00:00 EDT", q119="1:00"),
    resp(7, "  ", "2026-09-04 12:00:00 EDT", q119="1:00"),
    resp(8, " T3 ", "2026-09-05 08:00:00 EDT", status="Partial", q119="5:32 pm (EST)",
         q135="n/a"),
    resp(9, "T4", "2026-09-06 08:00:00 EDT", q119="garbage text", q135="maybe", q140="Yes"),
]


def test_collapse_and_rules():
    rows, stats = staging.build_staging(SAMPLE)
    by = {r["triage_request_id"]: r for r in rows}
    assert set(by) == {"T1", "T2", "T3", "T4"}
    assert stats["dup_trid_collapsed"] == 2
    assert stats["dropped_blank_or_test_trid"] == 3
    # latest submission wins
    assert by["T1"]["triage_interaction_type"] == "Follow-Up"
    assert by["T1"]["source_response_id"] == "2"
    assert by["T2"]["triage_interaction_type"] == "Initial Contact"
    # conclude nulled on no-response
    assert by["T1"]["triage_interaction_connected"] == "No"
    assert by["T1"]["triage_interaction_concluded_datetime"] is None
    # 135 blank falls back to deprecated 120
    assert by["T2"]["triage_interaction_connected"] == "Yes"
    assert by["T2"]["triage_interaction_connected_datetime"] == "2023-10-11T00:00:00"
    # trid is stripped; status carried
    assert by["T3"]["source_status"] == "Partial"
    assert by["T3"]["triage_interaction_initiated_datetime"] == "2026-09-05T17:32:00"
    assert stats["source_status_counts"] == {"Complete": 3, "Partial": 1}
    # question 140: taken from the winning submission; null when hidden
    assert by["T1"]["triage_interaction_vcl_warm_handoff"] == "No"
    assert by["T2"]["triage_interaction_vcl_warm_handoff"] is None
    assert by["T3"]["triage_interaction_vcl_warm_handoff"] is None
    assert by["T4"]["triage_interaction_vcl_warm_handoff"] == "Yes"
    assert (stats["vcl_warm_handoff_yes"], stats["vcl_warm_handoff_no"],
            stats["vcl_warm_handoff_null"]) == (1, 1, 2)


def test_merge_columns_include_140():
    assert staging.MERGE_COLUMNS[-1] == "triage_interaction_vcl_warm_handoff"
    assert len(staging.MERGE_COLUMNS) == 11


INVALID_SAMPLE = [
    resp(30, "T30", "2026-09-07 08:00:00 EDT", q119="2/30/26 4:27pm", q135="4/31 5pm", q126="9/7 6pm"),
    resp(31, "T31", "2026-09-07 08:00:00 EDT", q119="9/7 1pm", q135="no answer", q126="2/30 2pm"),
]


@pytest.mark.parametrize("sample", [SAMPLE, SAMPLE + INVALID_SAMPLE], ids=["sample", "with_invalid_dates"])
def test_parity_with_manual_builder(sample):
    builder = os.environ.get("MANUAL_BUILDER")
    if not builder:
        pytest.skip("MANUAL_BUILDER not set")
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "in.json"), os.path.join(d, "out.csv")
        json.dump(sample, open(inp, "w"))
        env = dict(os.environ, IN_PATH=inp, OUT_PATH=out,
                   PYTHONPATH=os.path.dirname(builder))
        subprocess.run([sys.executable, builder], check=True, env=env, capture_output=True)
        manual = {r["triage_request_id"]: r for r in csv.DictReader(open(out))}
    rows, _ = staging.build_staging(sample)
    ours = {r["triage_request_id"]: r for r in rows}
    assert set(manual) == set(ours)
    for trid, m in manual.items():
        for c in staging.MERGE_COLUMNS:
            assert (m[c] or None) == ours[trid][c], (trid, c)


class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.content = ("\ufeff" + json.dumps(body)).encode("utf-8") if body is not None else b""


def pages(total, per=500):
    n_pages = max(1, -(-total // per))
    out = {}
    for p in range(1, n_pages + 1):
        cnt = min(per, total - (p - 1) * per)
        out[p] = {"result_ok": True, "total_count": total, "total_pages": n_pages, "page": p,
                  "data": [{"id": f"{p}-{i}"} for i in range(cnt)]}
    return out


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("ALCHEMER_API_TOKEN", "TOKEN_SHOULD_NOT_LEAK")
    monkeypatch.setenv("ALCHEMER_API_SECRET", "SECRET_SHOULD_NOT_LEAK")
    monkeypatch.setattr(main.time, "sleep", lambda s: None)


def test_pull_pages_and_count(creds):
    pg = pages(1203)
    with mock.patch.object(main.requests, "get",
                           side_effect=lambda url, params, timeout: FakeResp(200, pg[params["page"]])):
        data, total = main.pull_all_responses()
    assert total == 1203 and len(data) == 1203


def test_pull_count_mismatch_fails(creds):
    pg = pages(600)
    pg[2]["data"] = pg[2]["data"][:-1]
    with mock.patch.object(main.requests, "get",
                           side_effect=lambda url, params, timeout: FakeResp(200, pg[params["page"]])):
        with pytest.raises(main.GateFailure) as e:
            main.pull_all_responses()
    assert e.value.gate == "alchemer_pull"


def test_errors_never_carry_credentials(creds):
    def boom(url, params, timeout):
        raise main.requests.ConnectionError(f"failed {url}?api_token={params['api_token']}")
    with mock.patch.object(main.requests, "get", side_effect=boom):
        with pytest.raises(main.GateFailure) as e:
            main.pull_all_responses()
    assert "LEAK" not in str(e.value)
    with mock.patch.object(main.requests, "get", return_value=FakeResp(401, {})):
        with pytest.raises(main.GateFailure) as e:
            main.pull_all_responses()
    assert "LEAK" not in str(e.value) and "401" in str(e.value)


def test_retry_then_success(creds):
    pg = pages(10)
    calls = {"n": 0}

    def flaky(url, params, timeout):
        calls["n"] += 1
        return FakeResp(503, {}) if calls["n"] < 3 else FakeResp(200, pg[params["page"]])
    with mock.patch.object(main.requests, "get", side_effect=flaky):
        data, _ = main.pull_all_responses()
    assert len(data) == 10 and calls["n"] == 3


def test_missing_credentials_reports_config(monkeypatch):
    monkeypatch.delenv("ALCHEMER_API_TOKEN", raising=False)
    monkeypatch.delenv("ALCHEMER_API_SECRET", raising=False)
    r = main.run_refresh()
    assert r["status"] == "error" and r["failed_check"] == "config"


def _patch_pipeline(monkeypatch, staging_row, match_row, target_row=None, types=None, invalid_rows=()):
    monkeypatch.setattr(main, "pull_all_responses", lambda: (SAMPLE, len(SAMPLE)))
    monkeypatch.setattr(main, "check_target_columns", lambda target=None: types)
    monkeypatch.setattr(main, "load_staging", lambda rows, ts: len(rows))
    merged = {"called": False, "validated": []}

    def fake_merge(types=None):
        merged["called"] = True
        return 5
    monkeypatch.setattr(main, "run_merge", fake_merge)

    def fake_validate(sql, gate):
        merged["validated"].append(sql)
        return True
    monkeypatch.setattr(main, "validate_sql", fake_validate)

    def fake_q(sql):
        if "no_with_conclude" in sql and "total_rows" in sql:
            return [staging_row]
        if "matched_trids" in sql:
            return [match_row]
        if "rows_with_interaction_data" in sql:
            return [target_row]
        if "invalid_date_columns IS NOT NULL" in sql:
            return list(invalid_rows)
        raise AssertionError(sql)
    monkeypatch.setattr(main, "q", fake_q)
    return merged


GOOD_STAGING = {"total_rows": 4, "distinct_trids": 4, "null_or_blank_trid": 0,
                "connected_unexpected": 0, "no_with_conclude": 0, "vcl_unexpected": 0,
                "wellness_unexpected": 0, "rescue_unexpected": 0, "initiated_not_datetime": 0,
                "connected_not_datetime": 0, "concluded_not_datetime": 0}
GOOD_MATCH = {"distinct_trids_in_staging": 4, "matched_trids": 3, "unmatched_trids": 1,
              "distinct_target_message_ids": 3}
GOOD_TARGET = {"rows_with_interaction_data": 5, "distinct_message_ids_with_data": 3,
               "no_with_conclude": 0, "latest_initiated_datetime": "2026-09-06T00:00:00"}


def test_full_run_ok(monkeypatch):
    merged = _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET)
    r = main.run_refresh()
    assert r["status"] == "ok" and merged["called"] and r["rows_modified"] == 5


def test_dry_run_skips_merge(monkeypatch):
    merged = _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET)
    r = main.run_refresh(dry_run=True)
    assert r["status"] == "ok" and not merged["called"] and r["rows_modified"] is None
    # the MERGE it would have run is compiled by BigQuery (dry run), not executed
    assert r["merge_validated"] is True and len(merged["validated"]) == 1
    assert merged["validated"][0].lstrip().startswith("MERGE")


@pytest.mark.parametrize("field", ["null_or_blank_trid", "connected_unexpected", "no_with_conclude",
                                   "vcl_unexpected"])
def test_staging_gate_blocks_merge(monkeypatch, field):
    bad = dict(GOOD_STAGING, **{field: 1})
    merged = _patch_pipeline(monkeypatch, bad, GOOD_MATCH, GOOD_TARGET)
    r = main.run_refresh()
    assert r["status"] == "error" and r["failed_check"] == "staging_check" and not merged["called"]


def test_collision_blocks_merge(monkeypatch):
    bad = dict(GOOD_MATCH, distinct_target_message_ids=2)
    merged = _patch_pipeline(monkeypatch, GOOD_STAGING, bad, GOOD_TARGET)
    r = main.run_refresh()
    assert r["failed_check"] == "match_check" and not merged["called"]


def test_target_check_after_merge(monkeypatch):
    _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, dict(GOOD_TARGET, no_with_conclude=2))
    r = main.run_refresh()
    assert r["status"] == "error" and r["failed_check"] == "target_check"


def test_http_codes(monkeypatch):
    _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET)
    c = main.app.test_client()
    assert c.get("/health").get_json() == {"status": "ok"}
    assert c.post("/run", json={"dry_run": True}).status_code == 200
    _patch_pipeline(monkeypatch, dict(GOOD_STAGING, no_with_conclude=1), GOOD_MATCH, GOOD_TARGET)
    resp = c.post("/run", json={})
    assert resp.status_code == 500 and resp.get_json()["failed_check"] == "staging_check"


# ---------------------------------------------------------------------------
# Both target shapes: the ten columns as STRING (until the type change) and with the
# three datetimes as DATETIME and the four Yes/No columns as BOOL (after it).
# ---------------------------------------------------------------------------

STRING_TYPES = {c: "STRING" for c in staging.MERGE_COLUMNS[1:]}
TYPED = dict(STRING_TYPES, **{c: "DATETIME" for c in main.DATETIME_COLUMNS},
             **{c: "BOOL" for c in main.BOOL_COLUMNS})


def _legacy_merge_sql():
    """The MERGE exactly as the service built it before the type change (62f4cc2)."""
    msg = main._MSG_EARLIEST.format(target=main.TARGET_TABLE)
    cols = staging.MERGE_COLUMNS[1:]
    select_cols = ",\n      ".join(f"s.{c}" for c in cols)
    set_cols = ",\n      ".join(f"T.{c} = U.{c}" for c in cols)
    return f"""
MERGE `{main.TARGET_TABLE}` AS T
USING (
  SELECT
      m.message_id,
      {select_cols}
  FROM `{main.STAGING_TABLE}` AS s
  JOIN ({msg}) AS m
    ON m.triage_request_id = s.triage_request_id
   AND m.rn = 1
) AS U
ON T.message_id = U.message_id
WHEN MATCHED THEN UPDATE SET
      {set_cols}
"""


def _legacy_verify_sql():
    return f"""
        SELECT
          COUNTIF(triage_interaction_initiated_datetime IS NOT NULL
                  OR triage_interaction_connected IS NOT NULL
                  OR triage_interaction_method IS NOT NULL) AS rows_with_interaction_data,
          COUNT(DISTINCT IF(triage_interaction_initiated_datetime IS NOT NULL
                  OR triage_interaction_connected IS NOT NULL
                  OR triage_interaction_method IS NOT NULL, message_id, NULL)) AS distinct_message_ids_with_data,
          COUNTIF(triage_interaction_connected = 'No'
                  AND triage_interaction_concluded_datetime IS NOT NULL) AS no_with_conclude,
          MAX(triage_interaction_initiated_datetime) AS latest_initiated_datetime
        FROM `{main.TARGET_TABLE}`
    """


def test_string_target_sql_unchanged():
    assert main.merge_sql(STRING_TYPES) == _legacy_merge_sql()
    assert main.merge_sql() == _legacy_merge_sql()
    assert main.verify_sql(STRING_TYPES) == _legacy_verify_sql()


def test_typed_target_merge_converts():
    sql = main.merge_sql(TYPED)
    for c in main.DATETIME_COLUMNS:
        assert f"CAST(s.{c} AS DATETIME) AS {c}" in sql
    for c in main.BOOL_COLUMNS:
        assert f"CASE s.{c} WHEN 'Yes' THEN TRUE WHEN 'No' THEN FALSE END AS {c}" in sql
    for c in ("triage_interaction_type", "triage_interaction_method",
              "triage_interaction_suicide_risk_assessment_result"):
        assert f"      s.{c}," in sql or f"      s.{c}\n" in sql
    for c in staging.MERGE_COLUMNS[1:]:
        assert f"T.{c} = U.{c}" in sql
    assert "CAST(s.triage_interaction_method" not in sql
    assert "AS BOOL" not in sql  # Yes/No is mapped explicitly, never cast


def test_mixed_target_merge_per_column():
    mixed = dict(STRING_TYPES, triage_interaction_initiated_datetime="DATETIME",
                 triage_interaction_connected="BOOL")
    sql = main.merge_sql(mixed)
    assert "CAST(s.triage_interaction_initiated_datetime AS DATETIME)" in sql
    assert "CASE s.triage_interaction_connected WHEN" in sql
    assert "CAST(s.triage_interaction_concluded_datetime" not in sql
    assert "CASE s.triage_interaction_vcl_warm_handoff" not in sql


def test_typed_verify_compares_bool():
    sql = main.verify_sql(TYPED)
    assert "triage_interaction_connected = FALSE" in sql and "'No'" not in sql


def _info_schema_rows(types, extra=None):
    rows = [{"column_name": c, "data_type": t} for c, t in types.items()]
    rows += [{"column_name": "message_id", "data_type": "STRING"},
             {"column_name": "message_time", "data_type": "TIMESTAMP"},
             {"column_name": "triage_request_id", "data_type": "STRING"}]
    return rows + (extra or [])


@pytest.mark.parametrize("types", [STRING_TYPES, TYPED])
def test_check_target_columns_accepts_both_shapes(monkeypatch, types):
    # a leftover column from the swap (e.g. *_string_old) is ignored
    extra = [{"column_name": "triage_interaction_connected_string_old", "data_type": "STRING"}]
    monkeypatch.setattr(main, "q", lambda sql: _info_schema_rows(types, extra))
    assert main.check_target_columns() == types


@pytest.mark.parametrize("column,bad", [
    ("triage_interaction_method", "BOOL"),
    ("triage_interaction_connected", "DATETIME"),
    ("triage_interaction_initiated_datetime", "TIMESTAMP"),
    ("triage_interaction_vcl_warm_handoff", "INT64"),
])
def test_check_target_columns_rejects_other_types(monkeypatch, column, bad):
    monkeypatch.setattr(main, "q", lambda sql: _info_schema_rows(dict(TYPED, **{column: bad})))
    with pytest.raises(main.GateFailure) as e:
        main.check_target_columns()
    assert e.value.gate == "target_columns" and column in e.value.detail


def test_check_target_columns_reads_the_named_target(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "q", lambda sql: seen.append(sql) or _info_schema_rows(TYPED))
    main.check_target_columns("early-alert-responses.DEV.triagetypes_copy_20261001")
    assert "`early-alert-responses.DEV.INFORMATION_SCHEMA.COLUMNS`" in seen[0]
    assert "table_name = 'triagetypes_copy_20261001'" in seen[0]


def test_check_target_columns_missing(monkeypatch):
    rows = [r for r in _info_schema_rows(TYPED) if r["column_name"] != "triage_interaction_connected"]
    monkeypatch.setattr(main, "q", lambda sql: rows)
    with pytest.raises(main.GateFailure) as e:
        main.check_target_columns()
    assert "missing on target: triage_interaction_connected" in e.value.detail


@pytest.mark.parametrize("field,column", [
    ("wellness_unexpected", "triage_interaction_wellness_check_initiated"),
    ("rescue_unexpected", "triage_interaction_active_rescue_initiated"),
    ("initiated_not_datetime", "triage_interaction_initiated_datetime"),
    ("connected_not_datetime", "triage_interaction_connected_datetime"),
    ("concluded_not_datetime", "triage_interaction_concluded_datetime"),
])
def test_typed_gates_enforced_only_on_typed_target(monkeypatch, field, column):
    bad = dict(GOOD_STAGING, **{field: 1})
    monkeypatch.setattr(main, "q", lambda sql: [bad])
    assert main.check_staging(STRING_TYPES)[field] == 1  # reported, not enforced
    with pytest.raises(main.GateFailure) as e:
        main.check_staging(dict(STRING_TYPES, **{column: TYPED[column]}))
    assert e.value.gate == "staging_check" and field in e.value.detail


def test_full_run_typed_target(monkeypatch):
    seen = {}
    merged = _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET, types=TYPED)
    monkeypatch.setattr(main, "run_merge", lambda types=None: seen.setdefault("types", types) and 5)
    r = main.run_refresh()
    assert r["status"] == "ok" and r["target_types"] == TYPED and seen["types"] == TYPED


def test_dry_run_against_dev_copy(monkeypatch):
    merged = _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET, types=TYPED)
    dev = "early-alert-responses.DEV.triagetypes_copy_20261001"
    r = main.run_refresh(dry_run=True, target_table=dev)
    assert r["status"] == "ok" and r["target_table"] == dev and not merged["called"]
    sql = merged["validated"][0]
    assert f"MERGE `{dev}` AS T" in sql and "CAST(s.triage_interaction_initiated_datetime" in sql
    assert main.TARGET_TABLE not in sql


@pytest.mark.parametrize("dry_run,target", [
    (False, "early-alert-responses.DEV.triagetypes_copy_20261001"),   # only with dry_run
    (True, "early-alert-responses.RESPONSES.response_data"),          # only DEV
    (True, "early-alert-responses.DEVX.t"),
])
def test_target_override_refused(monkeypatch, dry_run, target):
    merged = _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET, types=TYPED)
    r = main.run_refresh(dry_run=dry_run, target_table=target)
    assert r["status"] == "error" and r["failed_check"] == "config" and not merged["called"]


def test_http_target_override(monkeypatch):
    _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET, types=TYPED)
    c = main.app.test_client()
    ok = c.post("/run", json={"dry_run": True,
                              "target_table": "early-alert-responses.DEV.triagetypes_copy_20261001"})
    assert ok.status_code == 200
    refused = c.post("/run", json={"target_table": "early-alert-responses.DEV.x"})
    assert refused.status_code == 500 and refused.get_json()["failed_check"] == "config"


def test_validate_sql_reports_bigquery_rejection(monkeypatch):
    class Boom:
        def query(self, sql, job_config=None):
            assert job_config.dry_run is True
            raise main.gexc.BadRequest("Value of type STRING cannot be assigned to T.x, which has type BOOL")
    monkeypatch.setattr(main, "bq", lambda: Boom())
    with pytest.raises(main.GateFailure) as e:
        main.validate_sql("MERGE ...", "merge_validation")
    assert e.value.gate == "merge_validation" and "BOOL" in e.value.detail


# ---------------------------------------------------------------------------
# Typed dates that are not real calendar dates (2/30): null value, raw kept, night not halted
# ---------------------------------------------------------------------------

from normalizer import normalize  # noqa: E402


@pytest.mark.parametrize("raw", ["2/30/26 4:27pm", "4/31 10am", "2/29/25 1pm", "2/30"])
def test_normalizer_rejects_non_calendar_dates(raw):
    assert normalize(raw, "2026-09-01") == (None, "invalid_date")


@pytest.mark.parametrize("raw,iso", [("2/29/24 1:00pm", "2024-02-29T13:00:00"),
                                     ("9/1 4:27pm", "2026-09-01T16:27:00"),
                                     ("12/31/25 11:59 pm", "2025-12-31T23:59:00")])
def test_normalizer_keeps_real_dates(raw, iso):
    assert normalize(raw, "2026-09-01") == (iso, "full_parse")


def test_staging_invalid_date_row():
    rows, stats = staging.build_staging([
        resp(20, "T9", "2026-09-07 08:00:00 EDT", q119="2/30/26 4:27pm", q135="4/31 5pm",
             q126="9/7 6pm", q131="No", q130="No"),
        resp(21, "T10", "2026-09-07 08:00:00 EDT", q119="9/7 1pm", q135="did not respond",
             q126="2/30 2pm"),
    ])
    by = {r["triage_request_id"]: r for r in rows}
    t9 = by["T9"]
    assert t9["triage_interaction_initiated_datetime"] is None
    assert t9["triage_interaction_connected"] == "Yes" and t9["triage_interaction_connected_datetime"] is None
    assert t9["triage_interaction_concluded_datetime"] == "2026-09-07T18:00:00"
    assert t9["invalid_date_columns"] == ("triage_interaction_initiated_datetime, "
                                          "triage_interaction_connected_datetime")
    assert (t9["source_initiated_raw"], t9["source_connected_raw"]) == ("2/30/26 4:27pm", "4/31 5pm")
    # conclude is nulled by the no-response rule anyway, so it is not an invalid-date loss
    assert by["T10"]["invalid_date_columns"] is None and by["T10"]["source_concluded_raw"] == "2/30 2pm"
    assert stats["invalid_date_rows"] == 1
    assert all(c in staging.STAGING_COLUMNS for c in ("source_initiated_raw", "invalid_date_columns"))
    assert not any(c in staging.MERGE_COLUMNS for c in ("source_initiated_raw", "invalid_date_columns"))


INVALID_ROWS = [
    {"triage_request_id": "T9", "message_id": "m-1", "invalid_date_columns": "triage_interaction_initiated_datetime",
     "source_initiated_raw": "2/30/26 4:27pm", "source_connected_raw": None, "source_concluded_raw": None},
    {"triage_request_id": "T99", "message_id": None, "invalid_date_columns": "triage_interaction_initiated_datetime",
     "source_initiated_raw": "4/31", "source_connected_raw": None, "source_concluded_raw": None},
]


def test_invalid_date_merges_then_reports_error(monkeypatch):
    merged = _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET, invalid_rows=INVALID_ROWS)
    r = main.run_refresh()
    assert merged["called"] and r["rows_modified"] == 5          # everyone's data is written
    assert r["status"] == "error" and r["failed_check"] == "invalid_dates"
    assert "m-1 (triage_interaction_initiated_datetime)" in r["error"]
    assert r["invalid_dates"]["unmatched_trids"] == ["T99"]
    c = main.app.test_client()
    assert c.post("/run", json={}).status_code == 500                # the nightly alert fires


def test_invalid_date_unmatched_only_is_ok(monkeypatch):
    _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET, invalid_rows=INVALID_ROWS[1:])
    r = main.run_refresh()
    assert r["status"] == "ok" and r["invalid_dates"]["unmatched_trids"] == ["T99"]


def test_invalid_date_dry_run_reports_without_error(monkeypatch):
    merged = _patch_pipeline(monkeypatch, GOOD_STAGING, GOOD_MATCH, GOOD_TARGET, invalid_rows=INVALID_ROWS)
    r = main.run_refresh(dry_run=True)
    assert r["status"] == "ok" and not merged["called"]
    assert r["invalid_dates"]["messages"][0]["message_id"] == "m-1"

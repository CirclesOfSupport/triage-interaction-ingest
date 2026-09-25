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
         q135="did not respond", q126="9/2 8pm", q128=["Text"]),
    # Earlier duplicate arriving after the later one in list order: must NOT win
    resp(3, "T2", "2026-09-03 12:00:00 EDT", q136="Initial Contact", q119="843",
         q135="", q120="10.11.2023 12:00am"),
    resp(4, "T2", "2026-09-03 11:00:00 EDT", q136="STALE", q119="1:00"),
    resp(5, "testID", "2026-09-04 12:00:00 EDT", q119="1:00"),
    resp(6, None, "2026-09-04 12:00:00 EDT", q119="1:00"),
    resp(7, "  ", "2026-09-04 12:00:00 EDT", q119="1:00"),
    resp(8, " T3 ", "2026-09-05 08:00:00 EDT", status="Partial", q119="5:32 pm (EST)",
         q135="n/a"),
    resp(9, "T4", "2026-09-06 08:00:00 EDT", q119="garbage text", q135="maybe"),
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


def test_parity_with_manual_builder():
    builder = os.environ.get("MANUAL_BUILDER")
    if not builder:
        pytest.skip("MANUAL_BUILDER not set")
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "in.json"), os.path.join(d, "out.csv")
        json.dump(SAMPLE, open(inp, "w"))
        env = dict(os.environ, IN_PATH=inp, OUT_PATH=out,
                   PYTHONPATH=os.path.dirname(builder))
        subprocess.run([sys.executable, builder], check=True, env=env, capture_output=True)
        manual = {r["triage_request_id"]: r for r in csv.DictReader(open(out))}
    rows, _ = staging.build_staging(SAMPLE)
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


def _patch_pipeline(monkeypatch, staging_row, match_row, target_row=None):
    monkeypatch.setattr(main, "pull_all_responses", lambda: (SAMPLE, len(SAMPLE)))
    monkeypatch.setattr(main, "check_target_columns", lambda: None)
    monkeypatch.setattr(main, "load_staging", lambda rows, ts: len(rows))
    merged = {"called": False}

    def fake_merge():
        merged["called"] = True
        return 5
    monkeypatch.setattr(main, "run_merge", fake_merge)

    def fake_q(sql):
        if "no_with_conclude" in sql and "total_rows" in sql:
            return [staging_row]
        if "matched_trids" in sql:
            return [match_row]
        if "rows_with_interaction_data" in sql:
            return [target_row]
        raise AssertionError(sql)
    monkeypatch.setattr(main, "q", fake_q)
    return merged


GOOD_STAGING = {"total_rows": 4, "distinct_trids": 4, "null_or_blank_trid": 0,
                "connected_unexpected": 0, "no_with_conclude": 0}
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


@pytest.mark.parametrize("field", ["null_or_blank_trid", "connected_unexpected", "no_with_conclude"])
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

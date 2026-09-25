"""triage-interaction-ingest

Nightly refresh of the counselor interaction log into BigQuery.

Counselors record each triage interaction in the Alchemer Interaction Log survey. This
service pulls every response, builds one staging row per triage_request_id (see
staging.py), loads the rows into a staging table, checks them, and MERGEs the nine
triage_interaction_* columns onto the matching row of `triage-message-data`.

It replaces a four-step on-demand refresh (export, rebuild staging CSV, load, MERGE)
that had to be run by hand; the logic is unchanged.

Endpoints
  GET  /health  -> {"status": "ok"}
  POST /run     -> full refresh.       Body {}
  POST /run     -> checks, no MERGE.   Body {"dry_run": true}

Auth is the Cloud Run layer only (--no-allow-unauthenticated; callers need
roles/run.invoker). The body is not checked for a password.

Every run stops before the MERGE if any check fails, and returns status "error" with
HTTP 500 naming the failed check, so a caller (and the request log) sees the failure.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from staging import MERGE_COLUMNS, STAGING_COLUMNS, build_staging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("triage-interaction-ingest")

ALCHEMER_BASE = "https://api.alchemer.com/v5"
ALCHEMER_SURVEY_ID = os.environ.get("ALCHEMER_SURVEY_ID", "6902806")
RESULTS_PER_PAGE = 500          # Alchemer v5 maximum
ALCHEMER_MAX_RETRIES = 5
ALCHEMER_BACKOFF_BASE = 5       # seconds; 5, 10, 20, 40, 80
PAGE_CEILING = int(os.environ.get("PAGE_CEILING", "50"))

BQ_PROJECT = os.environ.get("BQ_PROJECT", "early-alert-responses")
STAGING_TABLE = os.environ.get(
    "STAGING_TABLE", "early-alert-responses.OPS.triage_interaction_staging")
TARGET_TABLE = os.environ.get(
    "TARGET_TABLE", "early-alert-responses.RESPONSES.triage-message-data")

MERGE_MAX_RETRIES = 3


class GateFailure(Exception):
    """A check failed; the run stops before (or after) the MERGE and reports it."""

    def __init__(self, gate, detail):
        super().__init__(f"{gate}: {detail}")
        self.gate = gate
        self.detail = detail


# ---------------------------------------------------------------------------
# Alchemer
# ---------------------------------------------------------------------------

def _alchemer_credentials():
    token = os.environ.get("ALCHEMER_API_TOKEN", "")
    secret = os.environ.get("ALCHEMER_API_SECRET", "")
    if not token or not secret:
        raise GateFailure("config", "ALCHEMER_API_TOKEN and ALCHEMER_API_SECRET must both be set")
    return token, secret


def _alchemer_get_page(page):
    """GET one surveyresponse page. Credentials travel in the query string, so no error
    message raised from here may include the request URL."""
    token, secret = _alchemer_credentials()
    url = f"{ALCHEMER_BASE}/survey/{ALCHEMER_SURVEY_ID}/surveyresponse"
    params = {
        "api_token": token,
        "api_token_secret": secret,
        "resultsperpage": RESULTS_PER_PAGE,
        "page": page,
    }
    last = None
    for attempt in range(ALCHEMER_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=120)
        except requests.RequestException as e:
            last = f"connection error ({type(e).__name__})"
        else:
            if resp.status_code == 200:
                try:
                    body = json.loads(resp.content.decode("utf-8-sig"))
                except ValueError:
                    raise GateFailure("alchemer_pull", f"page {page}: response was not JSON")
                if not body.get("result_ok", False):
                    msg = str(body.get("message", ""))[:200]
                    raise GateFailure("alchemer_pull", f"page {page}: result_ok false: {msg}")
                return body
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
            else:
                raise GateFailure("alchemer_pull", f"page {page}: HTTP {resp.status_code}")
        if attempt < ALCHEMER_MAX_RETRIES:
            wait = ALCHEMER_BACKOFF_BASE * (2 ** attempt)
            log.warning(f"alchemer page {page}: {last}; retry {attempt + 1} in {wait}s")
            time.sleep(wait)
    raise GateFailure("alchemer_pull", f"page {page}: gave up after retries ({last})")


def pull_all_responses():
    """All responses on the survey, oldest first. Checks the count pulled against the
    survey's total_count; one full re-pull if a response arrived mid-pull."""
    for pass_no in (1, 2):
        first = _alchemer_get_page(1)
        total_count = int(first.get("total_count", 0))
        total_pages = int(first.get("total_pages", 0))
        if total_pages > PAGE_CEILING:
            raise GateFailure("alchemer_pull",
                              f"total_pages {total_pages} exceeds PAGE_CEILING {PAGE_CEILING}")
        data = list(first.get("data", []))
        for page in range(2, total_pages + 1):
            data.extend(_alchemer_get_page(page).get("data", []))
        if len(data) == total_count:
            return data, total_count
        log.warning(f"pull pass {pass_no}: pulled {len(data)} != total_count {total_count}")
    raise GateFailure("alchemer_pull",
                      f"pulled {len(data)} responses but total_count is {total_count}")


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------

_bq = None


def bq():
    global _bq
    if _bq is None:
        _bq = bigquery.Client(project=BQ_PROJECT)
    return _bq


def q(sql):
    return list(bq().query(sql).result())


def load_staging(rows, run_ts):
    schema = [bigquery.SchemaField(c, "STRING") for c in STAGING_COLUMNS]
    schema.append(bigquery.SchemaField("loaded_at", "TIMESTAMP"))
    payload = [dict(r, loaded_at=run_ts) for r in rows]
    job = bq().load_table_from_json(
        payload,
        STAGING_TABLE,
        job_config=bigquery.LoadJobConfig(
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        ),
    )
    job.result()
    loaded = q(f"SELECT COUNT(*) AS n FROM `{STAGING_TABLE}`")[0]["n"]
    if loaded != len(rows):
        raise GateFailure("staging_load", f"built {len(rows)} rows, table holds {loaded}")
    return loaded


def check_target_columns():
    project, dataset, table = TARGET_TABLE.split(".")
    rows = q(f"""
        SELECT column_name, data_type
        FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = '{table}'
          AND (column_name LIKE 'triage_interaction%'
               OR column_name IN ('message_id', 'message_time', 'triage_request_id'))
    """)
    have = {r["column_name"]: r["data_type"] for r in rows}
    missing = [c for c in MERGE_COLUMNS[1:] + ["message_id", "message_time", "triage_request_id"]
               if c not in have]
    if missing:
        raise GateFailure("target_columns", f"missing on target: {', '.join(missing)}")
    not_string = [c for c in MERGE_COLUMNS[1:] if have[c] != "STRING"]
    if not_string:
        raise GateFailure("target_columns", f"not STRING on target: {', '.join(not_string)}")


def check_staging():
    r = q(f"""
        SELECT
          COUNT(*) AS total_rows,
          COUNT(DISTINCT triage_request_id) AS distinct_trids,
          COUNTIF(triage_request_id IS NULL OR TRIM(triage_request_id) = '') AS null_or_blank_trid,
          COUNTIF(triage_interaction_connected IS NOT NULL
                  AND triage_interaction_connected NOT IN ('Yes', 'No')) AS connected_unexpected,
          COUNTIF(triage_interaction_connected = 'No'
                  AND triage_interaction_concluded_datetime IS NOT NULL) AS no_with_conclude
        FROM `{STAGING_TABLE}`
    """)[0]
    out = dict(r.items())
    if out["total_rows"] != out["distinct_trids"]:
        raise GateFailure("staging_check", f"{out['total_rows']} rows but {out['distinct_trids']} distinct trids")
    for k in ("null_or_blank_trid", "connected_unexpected", "no_with_conclude"):
        if out[k] != 0:
            raise GateFailure("staging_check", f"{k} = {out[k]}")
    return out


_MSG_EARLIEST = """
    SELECT
      message_id,
      triage_request_id,
      ROW_NUMBER() OVER (
        PARTITION BY triage_request_id
        ORDER BY message_time ASC, message_id ASC
      ) AS rn
    FROM `{target}`
    WHERE triage_request_id IS NOT NULL
"""


def match_counts():
    msg = _MSG_EARLIEST.format(target=TARGET_TABLE)
    r = q(f"""
        WITH msg AS ({msg})
        SELECT
          COUNT(DISTINCT s.triage_request_id) AS distinct_trids_in_staging,
          COUNT(DISTINCT CASE WHEN m.message_id IS NOT NULL THEN s.triage_request_id END) AS matched_trids,
          COUNT(DISTINCT CASE WHEN m.message_id IS NULL THEN s.triage_request_id END) AS unmatched_trids,
          COUNT(DISTINCT m.message_id) AS distinct_target_message_ids
        FROM `{STAGING_TABLE}` AS s
        LEFT JOIN msg AS m
          ON m.triage_request_id = s.triage_request_id AND m.rn = 1
    """)[0]
    out = dict(r.items())
    if out["matched_trids"] != out["distinct_target_message_ids"]:
        raise GateFailure("match_check",
                          f"{out['matched_trids']} matched trids map to "
                          f"{out['distinct_target_message_ids']} message_ids (collision)")
    return out


def run_merge():
    msg = _MSG_EARLIEST.format(target=TARGET_TABLE)
    cols = MERGE_COLUMNS[1:]
    select_cols = ",\n      ".join(f"s.{c}" for c in cols)
    set_cols = ",\n      ".join(f"T.{c} = U.{c}" for c in cols)
    sql = f"""
MERGE `{TARGET_TABLE}` AS T
USING (
  SELECT
      m.message_id,
      {select_cols}
  FROM `{STAGING_TABLE}` AS s
  JOIN ({msg}) AS m
    ON m.triage_request_id = s.triage_request_id
   AND m.rn = 1
) AS U
ON T.message_id = U.message_id
WHEN MATCHED THEN UPDATE SET
      {set_cols}
"""
    for attempt in range(1, MERGE_MAX_RETRIES + 1):
        try:
            job = bq().query(sql)
            job.result()
            return job.num_dml_affected_rows
        except (gexc.BadRequest, gexc.Conflict) as e:
            # A concurrent write to the target can abort a mutating DML statement;
            # it is safe to re-run because the MERGE is UPDATE-only and idempotent.
            if "concurrent update" in str(e).lower() and attempt < MERGE_MAX_RETRIES:
                log.warning(f"MERGE concurrent-update conflict; retry {attempt} in {10 * attempt}s")
                time.sleep(10 * attempt)
                continue
            raise


def verify_target():
    r = q(f"""
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
        FROM `{TARGET_TABLE}`
    """)[0]
    out = dict(r.items())
    if out["no_with_conclude"] != 0:
        raise GateFailure("target_check", f"no_with_conclude = {out['no_with_conclude']}")
    return out


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_refresh(dry_run=False):
    run_ts = datetime.now(timezone.utc).isoformat()
    result = {"status": "ok", "mode": "dry_run" if dry_run else "merge", "run_at": run_ts}
    started = time.time()
    try:
        responses, total_count = pull_all_responses()
        result["alchemer_total_count"] = total_count
        rows, stats = build_staging(responses)
        result.update(stats)
        check_target_columns()
        result["staging_loaded"] = load_staging(rows, run_ts)
        result["staging_check"] = check_staging()
        result["match"] = match_counts()
        if dry_run:
            result["rows_modified"] = None
        else:
            result["rows_modified"] = run_merge()
            result["target"] = verify_target()
    except GateFailure as g:
        result["status"] = "error"
        result["failed_check"] = g.gate
        result["error"] = g.detail
    except Exception as e:  # noqa: BLE001 — any other failure must still report, not crash
        log.exception("refresh failed")
        result["status"] = "error"
        result["failed_check"] = "unexpected"
        result["error"] = f"{type(e).__name__}: {str(e)[:500]}"
    result["elapsed_sec"] = round(time.time() - started, 1)
    level = logging.INFO if result["status"] == "ok" else logging.ERROR
    log.log(level, "RESULT " + json.dumps(result, default=str))
    return result


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/run", methods=["POST"])
def run():
    body = request.get_json(force=True, silent=True) or {}
    result = run_refresh(dry_run=bool(body.get("dry_run", False)))
    code = 200 if result["status"] == "ok" else 500
    return app.response_class(json.dumps(result, default=str), status=code,
                              mimetype="application/json")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# triage-interaction-ingest

Cloud Run service that brings the counselor interaction log into BigQuery every night
(ITDO-397).

Our counselors record each triage interaction in the Alchemer **Interaction Log** survey
(6902806): who reached out, when, whether the individual responded, how, and the risk and
rescue outcomes. This service pulls every response, builds one row per
`triage_request_id`, and writes the nine `triage_interaction_*` columns onto the matching
message row in `RESPONSES.triage-message-data`.

It runs as a step in `nightly-pipeline`. It replaces a four-step refresh we used to run by
hand (export, rebuild a staging CSV, load it, MERGE); the rules are unchanged.

## What it does on `POST /run`

1. **Pull** every response from the survey, 500 per page, and check the number pulled
   against the survey's `total_count`. If a response arrives mid-pull the whole pull is
   repeated once; a second mismatch fails the run.
2. **Build staging rows** (`src/staging.py`), one per `triage_request_id`:
   - blank trids and the test ids `testID`, `demo`, `test` are dropped;
   - when a trid has more than one submission, the **latest** `date_submitted` wins;
   - the free-text datetimes are normalized to ISO by `src/normalizer.py` (handles
     `4:27pm`, military `1900`, `843`, bare hours, dot-dates, `(EST)` tags, time-only
     entries paired with the submission date);
   - "did the individual respond" comes from question 135, falling back to the deprecated
     question 120; a non-time answer ("did not respond") means `No`;
   - the concluded datetime is nulled whenever the individual did not respond — we decided
     on 2026-06-23 that conclude is only meaningful after a response.
3. **Load** the rows into `OPS.triage_interaction_staging` (replaced every run), plus
   three provenance columns (`source_response_id`, `source_date_submitted`,
   `source_status`) and `loaded_at`, and confirm the table holds exactly the rows built.
4. **Check** before writing anything:
   - the nine `triage_interaction_*` columns exist on the target as STRING;
   - staging has one row per trid, no blank trid, `connected` only `Yes`/`No`/null, and no
     `No` row carrying a conclude;
   - matched trids equal the distinct target `message_id`s they map to (no two trids land
     on one message).
5. **MERGE** onto `triage-message-data`, keyed on `message_id`. When a trid has several
   message rows, the data goes on the **earliest** (`message_time`, then `message_id`).
   UPDATE-only — it never inserts — and idempotent. Retried up to three times if BigQuery
   aborts it for a concurrent write to the table.
6. **Verify** the target: no `No` row carries a conclude; reports rows with interaction
   data, distinct message_ids with data, and the latest initiated datetime.

Trids with no message row (responses from before the BigQuery table's first data,
2023-01-19) are counted as `unmatched_trids` and skipped, never fabricated.

`rows_modified` can exceed `matched_trids`: some message_ids have two physical rows in
`triage-message-data`, and the MERGE writes both copies with identical values.

## Endpoints

```
GET  /health                          -> {"status":"ok"}
POST /run      body {}                -> full refresh
POST /run      body {"dry_run": true} -> steps 1-4 only; nothing written to triage-message-data
```

Any failed check stops the run before the MERGE (or, for step 6, after it) and returns
HTTP 500 with `"status":"error"`, `"failed_check"` naming the check, and `"error"`. Every
run logs one `RESULT {...}` line with the full response body, at ERROR level on failure.

## Auth

Cloud Run layer only: the service is deployed `--no-allow-unauthenticated`, and callers
need `roles/run.invoker` (the nightly-pipeline runtime service account and our own
accounts). The request body carries no password.

## Config (environment variables on the Cloud Run revision)

| Variable | Required | Default |
|---|---|---|
| `ALCHEMER_API_TOKEN` | yes | — |
| `ALCHEMER_API_SECRET` | yes | — |
| `ALCHEMER_SURVEY_ID` | no | `6902806` |
| `STAGING_TABLE` | no | `early-alert-responses.OPS.triage_interaction_staging` |
| `TARGET_TABLE` | no | `early-alert-responses.RESPONSES.triage-message-data` |
| `PAGE_CEILING` | no | `50` (runaway-pagination guard) |

Nothing credential-bearing is ever committed here. Alchemer credentials travel in the
request query string, so no error message this service raises includes a request URL.

The runtime service account needs BigQuery Data Editor and BigQuery Job User on
`early-alert-responses`.

## Tests

```
pip install -r requirements.txt pytest
python -m pytest -q tests
```

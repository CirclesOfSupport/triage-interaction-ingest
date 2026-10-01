# triage-interaction-ingest

Cloud Run service that brings the counselor interaction log into BigQuery every night
(ITDO-397).

Our counselors record each triage interaction in the Alchemer **Interaction Log** survey
(6902806): who reached out, when, whether the individual responded, how, the risk and
rescue outcomes, and whether a veteran was given a warm hand-off to the Veteran Crisis Line. This service pulls every response, builds one row per
`triage_request_id`, and writes the ten `triage_interaction_*` columns onto the matching
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
     entries paired with the submission date). A typed date that is not a real calendar
     date (`2/30`, `4/31`) gives no value — it is never guessed; the counselor's raw text
     stays in the staging row and the run reports it (step 7);
   - "did the individual respond" comes from question 135, falling back to the deprecated
     question 120; a non-time answer ("did not respond") means `No`; an answer whose date
     is not a real date still means `Yes`, with no connected datetime;
   - the concluded datetime is nulled whenever the individual did not respond — we decided
     on 2026-06-23 that conclude is only meaningful after a response;
   - the Veteran Crisis Line warm hand-off (`triage_interaction_vcl_warm_handoff`, `Yes`/`No`)
     comes from question 140, which the survey shows only for a veteran with a moderate or
     high determination (from 2026-08-29); it is null everywhere else.
3. **Load** the rows into `OPS.triage_interaction_staging` (replaced every run, all text
   columns), plus provenance columns (`source_response_id`, `source_date_submitted`,
   `source_status`, the raw initiated / connected / concluded answers, and
   `invalid_date_columns`) and `loaded_at`, and confirm the table holds exactly the rows built.
4. **Check** before writing anything:
   - the ten `triage_interaction_*` columns exist on the target with a type the MERGE can
     write: the three datetimes STRING or DATETIME; connected, wellness check, active rescue
     and the warm hand-off STRING or BOOL; type, method and the risk result STRING. Each
     column is read on its own, so a target part-way through a type change still works;
   - for each column that is typed, every staging value converts (Yes/No only for a BOOL,
     a real date-time for a DATETIME);
   - staging has one row per trid, no blank trid, `connected` and the warm hand-off only
     `Yes`/`No`/null, and no `No` row carrying a conclude;
   - matched trids equal the distinct target `message_id`s they map to (no two trids land
     on one message).
5. **MERGE** onto `triage-message-data`, keyed on `message_id`. When a trid has several
   message rows, the data goes on the **earliest** (`message_time`, then `message_id`).
   Text is converted to the target column's type: `CAST(... AS DATETIME)` for a DATETIME
   column, `Yes`→TRUE / `No`→FALSE for a BOOL column; a STRING column gets the text.
   UPDATE-only — it never inserts — and idempotent. Retried up to three times if BigQuery
   aborts it for a concurrent write to the table.
6. **Verify** the target: no `No` row carries a conclude; reports rows with interaction
   data, distinct message_ids with data, and the latest initiated datetime.
7. **Report invalid dates.** If any written row had a typed date that is not a real
   calendar date, the run returns `"status":"error"` with `"failed_check":"invalid_dates"`
   naming each `message_id` and column, so the nightly alert fires. Everyone else's data is
   already written by then; the night is not halted for one entry.

Trids with no message row (responses from before the BigQuery table's first data,
2023-01-19) are counted as `unmatched_trids` and skipped, never fabricated.

`rows_modified` can exceed `matched_trids`: some message_ids have two physical rows in
`triage-message-data`, and the MERGE writes both copies with identical values.

## Endpoints

```
GET  /health                          -> {"status":"ok"}
POST /run      body {}                -> full refresh
POST /run      body {"dry_run": true} -> steps 1-4, then BigQuery compiles the MERGE (a dry
                                         run, nothing written) and step 6's read runs
POST /run      body {"dry_run": true, "target_table": "early-alert-responses.DEV.<table>"}
                                      -> the same dry run against a DEV copy of the target
```

`target_table` is accepted only with `dry_run` and only in the `DEV` dataset.

Any failed check stops the run before the MERGE (or, for steps 6 and 7, after it) and returns
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

## Deploy

Continuous deployment from this repository: the service was created with the Cloud Run
"Connect repository" flow, which made a Cloud Build trigger on `main` that builds the
`Dockerfile`, pushes the image to Artifact Registry and deploys a new revision. That trigger
runs its own inline build config; `cloudbuild.yaml` here is not what it runs. Env vars live on
the Cloud Run revision (`gcloud run services update --update-env-vars`) and carry over from
revision to revision.

## Tests

```
pip install -r requirements.txt pytest
python -m pytest -q tests
```

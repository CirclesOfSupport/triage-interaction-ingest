"""Build one staging row per triage_request_id from Alchemer interaction-log responses.

The rules here are the same ones the on-demand refresh has used since June 2026; the
nightly service must produce the same rows the manual refresh produced.

Question IDs on the Interaction Log survey (6902806):
  125  triage_request_id (hidden value carried in from the triage email link)
  136  interaction type
  119  when outreach was initiated (free text)
  135  did the individual respond / when (radio + fill-in), coalesced with 120 (deprecated)
  126  when the interaction concluded (free text)
  128  how we communicated (checkbox, comma-joined)
  134  suicide risk assessment result
  131  wellness check initiated
  130  active rescue initiated
  140  warm hand-off to the Veteran Crisis Line (radio Yes/No; shown only for a veteran
       subscriber with a moderate or high determination, from 2026-08-29 — blank otherwise)

Collapse rule: one row per trid, keeping the LATEST submission (by date_submitted).
Conclude rule (team decision 2026-06-23): concluded_datetime is null whenever the
individual did not respond, because counselors historically filled it on no-response.

Invalid dates: a typed date that is not a real calendar date (2/30, 4/31) gives a null
datetime (normalizer class invalid_date). The row is still written; its staging row keeps
the counselor's raw text and names the column in invalid_date_columns, and the run reports
it as an error after the MERGE. A connected answer with an invalid date still means the
individual responded: connected stays "Yes" with a null connected datetime.
"""

import re
from collections import Counter

from normalizer import normalize

JUNK_TRIDS = ("testid", "demo", "test")

# The eleven columns the MERGE reads: the original staging CSV's ten, then 140.
MERGE_COLUMNS = [
    "triage_request_id",
    "triage_interaction_type",
    "triage_interaction_initiated_datetime",
    "triage_interaction_connected",
    "triage_interaction_connected_datetime",
    "triage_interaction_concluded_datetime",
    "triage_interaction_method",
    "triage_interaction_suicide_risk_assessment_result",
    "triage_interaction_wellness_check_initiated",
    "triage_interaction_active_rescue_initiated",
    "triage_interaction_vcl_warm_handoff",
]

# Provenance columns: which Alchemer response each row came from, the counselor's raw
# datetime answers, and which datetimes were not real dates. Not read by the MERGE; kept so
# a changed or surprising row can be traced to its submission.
SOURCE_COLUMNS = ["source_response_id", "source_date_submitted", "source_status",
                  "source_initiated_raw", "source_connected_raw", "source_concluded_raw",
                  "invalid_date_columns"]

STAGING_COLUMNS = MERGE_COLUMNS + SOURCE_COLUMNS


def ans(sd, qid):
    n = sd.get(qid)
    if not n:
        return None
    if n.get("type") == "parent":
        return "|".join([o["answer"] for o in n.get("options", {}).values() if o.get("answer")])
    return n.get("answer")


def checkbox_csv(sd, qid):
    n = sd.get(qid)
    if not n:
        return None
    if n.get("type") == "parent":
        picks = [o.get("answer") for o in n.get("options", {}).values() if o.get("answer")]
        picks = [p for p in picks if p]
        return ", ".join(picks) if picks else None
    return n.get("answer")


def resp_date(r):
    ds = r.get("date_submitted") or r.get("date_started") or ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", ds)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def connected_source(sd):
    """The connected answer: 135 (radio), coalesced with 120 (deprecated)."""
    src = ans(sd, "135")
    if src is None or str(src).strip() == "":
        src = ans(sd, "120")
    if src is None or str(src).strip() == "":
        return None
    return src


def connected_fields(sd, rd):
    """(connected_flag, connected_datetime, class)."""
    src = connected_source(sd)
    if src is None:
        return (None, None, "blank")             # neither present -> unknown
    iso, cls = normalize(src, rd)
    if cls == "non_time":                        # "did not respond to outreach"
        return ("No", None, cls)
    if iso:
        return ("Yes", iso, cls)
    if cls == "invalid_date":                    # a date was typed, so they responded
        return ("Yes", None, cls)
    return (None, None, cls)                     # present but unparseable -> leave blank


def build_staging(responses):
    """responses: list of Alchemer surveyresponse objects. Returns (rows, stats)."""
    latest = {}          # trid -> (date_submitted, response)
    dup_trids = 0
    dropped_no_trid = 0
    for r in responses:
        sd = r.get("survey_data", {}) or {}
        trid = ans(sd, "125")
        if not trid:
            dropped_no_trid += 1
            continue
        trid = trid.strip()
        if trid == "" or trid.lower() in JUNK_TRIDS:
            dropped_no_trid += 1
            continue
        sub = r.get("date_submitted") or ""
        if trid in latest:
            dup_trids += 1
            if sub <= latest[trid][0]:
                continue
        latest[trid] = (sub, r)

    rows = []
    for trid, (sub, r) in latest.items():
        sd = r.get("survey_data", {}) or {}
        rd = resp_date(r)
        init_iso, init_cls = normalize(ans(sd, "119"), rd)
        conn_flag, conn_iso, conn_cls = connected_fields(sd, rd)
        concl_iso, concl_cls = normalize(ans(sd, "126"), rd)
        if conn_flag == "No":
            concl_iso, concl_cls = None, "nulled_no_response"
        invalid = [c for c, cls in (("triage_interaction_initiated_datetime", init_cls),
                                    ("triage_interaction_connected_datetime", conn_cls),
                                    ("triage_interaction_concluded_datetime", concl_cls))
                   if cls == "invalid_date"]
        rows.append({
            "triage_request_id": trid,
            "triage_interaction_type": ans(sd, "136"),
            "triage_interaction_initiated_datetime": init_iso,
            "triage_interaction_connected": conn_flag,
            "triage_interaction_connected_datetime": conn_iso,
            "triage_interaction_concluded_datetime": concl_iso,
            "triage_interaction_method": checkbox_csv(sd, "128"),
            "triage_interaction_suicide_risk_assessment_result": ans(sd, "134"),
            "triage_interaction_wellness_check_initiated": ans(sd, "131"),
            "triage_interaction_active_rescue_initiated": ans(sd, "130"),
            "triage_interaction_vcl_warm_handoff": ans(sd, "140"),
            "source_response_id": str(r.get("id")) if r.get("id") is not None else None,
            "source_date_submitted": sub or None,
            "source_status": r.get("status"),
            "source_initiated_raw": ans(sd, "119"),
            "source_connected_raw": connected_source(sd),
            "source_concluded_raw": ans(sd, "126"),
            "invalid_date_columns": ", ".join(invalid) or None,
        })

    conn = Counter(row["triage_interaction_connected"] for row in rows)
    vcl = Counter(row["triage_interaction_vcl_warm_handoff"] for row in rows)
    stats = {
        "responses": len(responses),
        "staging_rows": len(rows),
        "dup_trid_collapsed": dup_trids,
        "dropped_blank_or_test_trid": dropped_no_trid,
        "connected_yes": conn.get("Yes", 0),
        "connected_no": conn.get("No", 0),
        "connected_null": conn.get(None, 0),
        "vcl_warm_handoff_yes": vcl.get("Yes", 0),
        "vcl_warm_handoff_no": vcl.get("No", 0),
        "vcl_warm_handoff_null": vcl.get(None, 0),
        "initiated_null": sum(1 for row in rows if not row["triage_interaction_initiated_datetime"]),
        "source_status_counts": dict(Counter(row["source_status"] for row in rows)),
        "invalid_date_rows": sum(1 for row in rows if row["invalid_date_columns"]),
    }
    return rows, stats

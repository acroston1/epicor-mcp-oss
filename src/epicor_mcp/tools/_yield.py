"""Production-yield trending for a part (engine helper for ``epicor_read``).

Yield calculations combine completed quantities from JobHead with scrap from
JobOper. This helper resolves the part, groups those quantities by month, and
computes the ratio without searching unrelated tables for scrap columns.

"Trend yield over N months" is phrased as a data read — the model reaches for
``epicor_read``, not ``epicor_help`` — so this lives as a RECOGNIZER inside
``epicor_read`` (dispatched before generic resolution), NOT as a dedicated tool
(contrast ``epicor_time_phase``, which models treat as an operation to run).

The metric, made explicit so it is never a silent guess:

    yield% = good / (good + scrapped) * 100
      good      = JobHead.QtyCompleted            (job-level completed, 1/job)
      scrapped  = Σ JobOper.ScrapQty over the job's operations
      bucket    = month of JobHead.JobCompletionDate

Good is taken at the JOB grain (one row per job) so summing never
double-counts the way an operation-level QtyCompleted would; scrap is only
recorded at the operation grain, so it is summed per job first.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

from epicor_mcp.tools._analytics import parse_time_window
from epicor_mcp.tools._engine import run_getrows
from epicor_mcp.tools._inline_schema import order_refusal, sort_records
from epicor_mcp.tools._resolve import error_envelope

if TYPE_CHECKING:
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_SERVICE = "Erp.BO.JobEntrySvc"
_DATE_FIELD = "JobCompletionDate"
_JOBNUM_CHUNK = 40  # OR-terms per JobOper scrap fetch
_DEFAULT_MONTHS = 3

# "yield" or "scrap" ...
_METRIC_RE = re.compile(r"\b(?:production\s+)?yield|\bscrap(?:ped|\s+rate)?\b", re.I)
# ... AND a trend / over-time signal.
_TREND_RE = re.compile(
    r"\btrend(?:ed|ing|s)?\b|\bover\s+time\b|\bby\s+month\b|\bmonth(?:ly|\s+over\s+month)\b"
    r"|\bper\s+month\b|\b(?:last|past)\s+\d+\s+month|\bhistor(?:y|ical)\b"
    r"|\bover\s+the\s+(?:last|past)\b",
    re.I,
)

# PartNum inside a `where` clause, or a part-number-style token in the phrase
# (>= 3 digits + a hyphen segment, so "3-months"/"over-view" never match).
_PART_IN_WHERE = re.compile(r"partnum\s*(?:=|eq|like)\s*'?([^'\s]+)'?", re.I)
_PART_TOKEN = re.compile(r"\b([0-9A-Za-z]{2,}(?:-[0-9A-Za-z]+)+)\b")


def detect_yield_trend(target: str) -> bool:
    """True when *target* asks to trend production yield / scrap over time."""
    t = target or ""
    return bool(_METRIC_RE.search(t) and _TREND_RE.search(t))


def _extract_part(target: str, where: str) -> str:
    """Pull a part number from `where` (PartNum=..) or a part-like token."""
    m = _PART_IN_WHERE.search(where or "")
    if m:
        return m.group(1).strip().strip("%")
    for m in _PART_TOKEN.finditer(target or ""):
        tok = m.group(1)
        if sum(c.isdigit() for c in tok) >= 3:
            return tok
    return ""


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _months_back(d: date, n: int) -> date:
    month = d.month - n
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, 1)


def _bucket_yield(job_rows: list, scrap_by_job: dict, date_field: str) -> list:
    """Pure roll-up: job rows + per-job scrap -> monthly yield buckets.

    Each job contributes its own good count once (no operation double-count);
    scrap is the per-job operation sum. Kept side-effect free so the ratio and
    bucketing are unit-testable without a live client.
    """
    buckets: dict[str, dict] = {}
    for r in job_rows:
        d = str(r.get(date_field) or "")[:7]  # YYYY-MM
        if not re.match(r"^\d{4}-\d{2}$", d):
            continue
        good = _num(r.get("QtyCompleted"))
        scrap = _num(scrap_by_job.get(r.get("JobNum"), 0.0))
        b = buckets.setdefault(
            d, {"month": d, "jobs": 0, "good_qty": 0.0, "scrap_qty": 0.0})
        b["jobs"] += 1
        b["good_qty"] += good
        b["scrap_qty"] += scrap
    out = []
    for d in sorted(buckets):
        b = buckets[d]
        total = b["good_qty"] + b["scrap_qty"]
        b["good_qty"] = round(b["good_qty"], 2)
        b["scrap_qty"] = round(b["scrap_qty"], 2)
        b["yield_pct"] = round(b["good_qty"] / total * 100, 2) if total else None
        out.append(b)
    return out


def _records(raw: str) -> list:
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    recs = payload.get("records") if isinstance(payload, dict) else None
    return recs if isinstance(recs, list) else []


async def yield_trend(
    client: "EpicorClient",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    session,
    *,
    target: str,
    where: str = "",
    limit: int = 25,
    order_by: str = "",
    soft: dict | None = None,
) -> str:
    """Trend a part's production yield by month. See module docstring."""
    part = _extract_part(target, where)
    if not part:
        # No part number in hand — the model usually has only a description or
        # customer ("Example Customer's part"). Tell it to resolve the part first,
        # with the exact call, instead of guessing scrap tables.
        return json.dumps(error_envelope(
            "need_part",
            "Recognised a production-yield trend request but no part number. "
            "If you only have a description or customer/brand, resolve the "
            "part FIRST, then re-call with that part number in `where`.",
            retry_with={
                "target": "Part",
                "where": "CommercialBrand like '%<brand>%' and "
                         "PartDescription like '%<desc>%'",
                "then": "epicor_read(target=\"trend production yield over 3 "
                        "months\", where=\"PartNum = '<PartNum>'\")",
            },
        ))

    allowed, msg = rbac.check_access(session.user_id, _SERVICE)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, _SERVICE).api_key or ""

    # Window: an explicit phrase ("last 3 months", "Q2 2025") wins; else the
    # 3-month default. Yield is bucketed on completion, so we filter completed
    # jobs by JobCompletionDate.
    window = parse_time_window(target)
    if window:
        start, end, label = window["start"], window["end"], window["label"]
    else:
        today = date.today()
        start = _months_back(today, _DEFAULT_MONTHS).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        label = f"last {_DEFAULT_MONTHS} months"

    part_esc = part.replace("'", "''")
    jh_filter = (
        f"PartNum eq '{part_esc}' and "
        f"{_DATE_FIELD} ge {start}T00:00:00 and "
        f"{_DATE_FIELD} le {end}T23:59:59"
    )
    try:
        jh_raw = await run_getrows(
            client, index, _SERVICE, "JobHead", api_key,
            filter=jh_filter,
            select=f"JobNum,{_DATE_FIELD},QtyCompleted,ProdQty",
            orderby="", top=1000, skip=0, count_only=False,
            group_by="", aggregate="", distinct="", format="json",
        )
    except Exception as exc:
        return json.dumps(error_envelope(
            "yield_failed",
            f"Could not read jobs for part '{part}': {exc}"))
    job_rows = _records(jh_raw)
    if not job_rows:
        return json.dumps({
            "summary": (f"No completed jobs for part '{part}' in {label} "
                        f"({start}..{end}) — no yield to trend."),
            "stop_hint": ("This is the complete answer: there is no production "
                          "in that window. Do NOT retry other tables; if the "
                          "user expected data, suggest widening the date range "
                          "or verifying the part number."),
            "resolved": {"service": _SERVICE, "entity_set": "JobHead",
                         "part": part, "window": f"{start}..{end}"},
            "records": [],
        }, default=str)

    # Per-job scrap from the operation table (the only place scrap is stored).
    job_nums = [str(r.get("JobNum")) for r in job_rows if r.get("JobNum") is not None]
    scrap_by_job: dict[str, float] = {}
    for i in range(0, len(job_nums), _JOBNUM_CHUNK):
        chunk = job_nums[i:i + _JOBNUM_CHUNK]
        ors = " or ".join(f"JobNum eq '{jn.replace(chr(39), chr(39) * 2)}'"
                          for jn in chunk)
        try:
            op_raw = await run_getrows(
                client, index, _SERVICE, "JobOper", api_key,
                filter=ors, select="JobNum,ScrapQty,ActScrapQty",
                orderby="", top=1000, skip=0, count_only=False,
                group_by="", aggregate="", distinct="", format="json",
            )
        except Exception:
            logger.exception("JobOper scrap fetch failed for chunk %d", i)
            continue
        for op in _records(op_raw):
            jn = str(op.get("JobNum"))
            scrap_by_job[jn] = scrap_by_job.get(jn, 0.0) + _num(op.get("ScrapQty"))

    months = _bucket_yield(job_rows, scrap_by_job, _DATE_FIELD)
    total_good = round(sum(m["good_qty"] for m in months), 2)
    total_scrap = round(sum(m["scrap_qty"] for m in months), 2)
    denom = total_good + total_scrap
    overall = round(total_good / denom * 100, 2) if denom else None

    # The records are monthly BUCKETS, not the caller's business rows. A
    # row-level key (JobNum, PartNum) is not something this result can be
    # sorted by at all — refuse and name the supported path, rather than
    # returning a plausible-but-unsorted series.
    notes = dict(soft or {})
    _BUCKET_COLS = ["month", "jobs", "good_qty", "scrap_qty", "yield_pct"]
    if (order_by or "").strip():
        months, err_kind, _v = sort_records(
            months, order_by, available=_BUCKET_COLS)
        if err_kind == "unknown_column":
            return json.dumps(order_refusal(
                "order_not_applicable", order_by, _BUCKET_COLS,
                reason=(f"This result is a MONTHLY yield series, not job rows, "
                        f"so it cannot be ordered by '{order_by}'. Sort by one "
                        "of valid.columns. For row-level ordering, read the "
                        "jobs directly: target='JobHead', "
                        f"where=\"PartNum = '{part}'\".")))
        if err_kind:
            return json.dumps(order_refusal(err_kind, order_by, _BUCKET_COLS))
        notes["order"] = f"{order_by} (over the monthly buckets)"

    return json.dumps({
        "summary": (f"Production yield for part '{part}', by month, {label} "
                    f"({start}..{end}): overall {overall}% "
                    f"({int(total_good)} good / {int(total_scrap)} scrapped "
                    f"across {len(job_rows)} job(s))."),
        "stop_hint": ("This monthly yield trend answers the question — present "
                      "it now. Do NOT re-query other tables for scrap; JobOper "
                      "is the only place it is recorded."),
        "metric": ("yield% = good / (good + scrapped) * 100; good = "
                   "JobHead.QtyCompleted (job-level), scrapped = "
                   "Σ JobOper.ScrapQty over the job's operations; bucketed by "
                   f"month of {_DATE_FIELD}. Jobs not completed in the window "
                   "are excluded."),
        "resolved": {"service": _SERVICE, "entity_set": "JobHead+JobOper",
                     "part": part, "window": f"{start}..{end}",
                     **({"assumptions": notes} if notes else {})},
        "row_count": len(months),
        "records": months,
    }, default=str)

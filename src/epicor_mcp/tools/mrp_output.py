"""Tool: epicor_mrp_output

Answer "what did MRP actually produce?" — counts of the planned jobs and the
purchasing/change suggestions MRP generated this calendar week, plus the open
backlog. This is the *output* layer, distinct from ``epicor_mrp_status`` which
reports the *run* (did MRP execute and finish).

Data sources:

- **Planned/unfirm jobs** — ``Erp.JobHead`` via ``Erp.BO.JobEntrySvc/GetList``.
  MRP jobs are numbered ``{plant}MRP{seq}`` (e.g. ``01MRP000000001``), so
  ``JobNum like '%MRP%'`` is the durable discriminator; ``JobFirm = false`` /
  ``JobClosed = false`` marks the still-open unfirm backlog. ``CreateDate``
  (date-only) is the "first-appeared" date.
- **New buy/make suggestions** — ``Erp.SugPoDtl`` via ``Erp.BO.POSuggSvc/GetList``
  (list ``SugPoDtlList``; ``SugType`` M=Material/S=Subcontract; ``CreatedOn``).
- **Change suggestions** (expedite / de-expedite / reschedule / cancel) —
  ``Erp.SugPOChg`` via ``Erp.BO.POSuggChgSvc/GetList`` (list ``SugPOChgList``).

Implementation notes:

- Uses the BAQ key from ``check_baq_access`` (granted to any authenticated user,
  not department-gated) and hardcodes read-only MRP-output queries — same narrow,
  contained pattern as ``mrp_status``.
- ``GetList`` with ``pageSize=0`` returns the full result set in one call, so we
  avoid OData ``$count`` (which silently saturates at 100 here) and ``$skip``
  paging. The big intermediate row lists are only counted/tallied, never returned.
- There is **no per-run attribution** in Epicor (no PlanGUID/SysTaskNum link on
  jobs or suggestions, and MRP runs several times a day), so these are
  date-window counts, not "what run X produced."
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP as Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_JOB_SERVICE = "Erp.BO.JobEntrySvc"
_POSUGG_SERVICE = "Erp.BO.POSuggSvc"
_POSUGGCHG_SERVICE = "Erp.BO.POSuggChgSvc"

# MRP-job discriminator (see module docstring). Note: GetList whereClause is
# SQL-ish (LIKE, plain 'YYYY-MM-DD' date literals), NOT OData.
_MRP_JOB_WHERE = "JobNum like '%MRP%'"


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_mrp_output`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_mrp_output() -> str:
        """Summarize what MRP suggested this week: job, buy, and change suggestions.

        Use this for "how many MRP jobs were created this week?", "what did MRP
        do?", or "what is MRP suggesting we make/buy/change?". For whether MRP
        *ran* and succeeded, use ``epicor_mrp_status`` instead.

        MRP's three suggestion outputs (counts for the current calendar week
        Sunday→now, plus open backlog):

        - ``job_make`` — new MRP **job/make suggestions**. Epicor has no separate
          job-suggestion table: make demand is materialized directly as unfirm,
          unreleased jobs (``JobNum like '%MRP%'``, ``JobReleased = false``), so
          those unfirm jobs ARE the make suggestions.
        - ``po_buy`` (+ ``po_buy_by_type`` M=Material/S=Subcontract) — new
          purchase suggestions (``Erp.SugPoDtl``).
        - ``po_change`` — PO change suggestions: expedite/de-expedite/reschedule/
          cancel (``Erp.SugPOChg``).

        Important interpretation notes (surface these to the user):
        - Job ``CreateDate`` is *first-appeared* (date-only) and PO ``CreatedOn``
          is regenerated each MRP run, so "this week" largely reflects the
          current outstanding suggestion set, not a clean week-over-week delta.
        - Counts are date-window based; Epicor has no per-run attribution, so
          this cannot say "run X created N suggestions."
        """
        try:
            session = get_current_session()

            baq_result = rbac.check_baq_access(session.user_id)
            if not baq_result.allowed:
                return format_response(
                    {"error": baq_result.message}, records_key=None
                )
            baq_key = baq_result.api_key or ""

            week_start = _week_start(datetime.now())
            ws = week_start.isoformat()  # 'YYYY-MM-DD' for SQL whereClause
            base = client._base_url

            # Unfirm + unreleased MRP jobs ARE the job/make suggestions; the
            # JobReleased=false guard drops any MRP job a planner later firms +
            # releases (it stops being a suggestion at that point).
            job_sugg_where = f"{_MRP_JOB_WHERE} and JobReleased = false"

            # Six independent one-shot GetList calls, run concurrently.
            queries = [
                (_JOB_SERVICE, f"{job_sugg_where} and CreateDate >= '{ws}'",
                 "JobHeadList"),                                       # 0 job/wk
                (_JOB_SERVICE, job_sugg_where, "JobHeadList"),         # 1 job/open
                (_POSUGG_SERVICE, f"CreatedOn >= '{ws}'",
                 "SugPoDtlList"),                                      # 2 buy/wk
                (_POSUGG_SERVICE, "", "SugPoDtlList"),                 # 3 buy/open
                (_POSUGGCHG_SERVICE, f"CreatedOn >= '{ws}'",
                 "SugPOChgList"),                                      # 4 chg/wk
                (_POSUGGCHG_SERVICE, "", "SugPOChgList"),              # 5 chg/open
            ]
            results = await asyncio.gather(
                *(_getlist_rows(client, base, baq_key, svc, where, list_key)
                  for svc, where, list_key in queries),
                return_exceptions=True,
            )

            result: dict[str, Any] = {
                "week_start": ws,
                "as_of": datetime.now().isoformat(timespec="seconds"),
                "suggestions_this_week": {
                    "job_make": _count(results[0]),
                    "po_buy": _count(results[2]),
                    "po_buy_by_type": _tally(results[2], "SugType"),
                    "po_change": _count(results[4]),
                },
                "open_backlog": {
                    "job_make": _count(results[1]),
                    "po_buy": _count(results[3]),
                    "po_change": _count(results[5]),
                },
                "notes": {
                    "job_make": (
                        "Unfirm, unreleased MRP jobs (JobNum like 'MRP', "
                        "JobFirm=false, JobReleased=false). Epicor has no "
                        "separate job-suggestion table — these unfirm jobs ARE "
                        "MRP's make suggestions."
                    ),
                    "po_buy": "SugType M=Material(buy), S=Subcontract.",
                    "general": (
                        "Suggestions are regenerated by MRP each run, so "
                        "'this week' largely reflects the current outstanding "
                        "set rather than a week-over-week delta. Date-window "
                        "counts only; Epicor has no per-run attribution. For "
                        "MRP run cadence and success/failure use "
                        "epicor_mrp_status."
                    ),
                },
            }
            return format_response(result, records_key=None)

        except Exception:
            logger.exception("epicor_mrp_output failed")
            return format_response(
                {"error": "Failed to fetch MRP output summary."},
                records_key=None,
            )


async def _getlist_rows(
    client: "EpicorClient",
    base_url: str,
    api_key: str,
    service: str,
    where: str,
    list_key: str,
) -> list[dict[str, Any]]:
    """Call ``{service}/GetList`` with ``pageSize=0`` (all rows) and return them.

    ``pageSize=0`` makes Epicor return the entire result set in one response, so
    no paging is needed. Returns the rows under *list_key* (``[]`` if missing).
    """
    resp = await client.call_method(
        base_url,
        service,
        "GetList",
        api_key,
        params={"whereClause": where, "pageSize": 0, "absolutePage": 1},
    )
    return resp.get("returnObj", {}).get(list_key, []) or []


def _count(rows: Any) -> Any:
    """Row count, or an ``{"error": ...}`` marker if that query failed."""
    if isinstance(rows, Exception):
        return {"error": f"query failed: {type(rows).__name__}"}
    return len(rows) if isinstance(rows, list) else 0


def _tally(rows: Any, field: str) -> dict[str, int]:
    """Group-count *rows* by *field* value. Empty dict if the query failed.

    (Per-day tallies aren't possible from these GetList "List" datasets — they
    accept ``CreateDate``/``CreatedOn`` in the whereClause but don't return the
    column — so we only group by columns the list actually carries, e.g.
    ``SugType``.)
    """
    if not isinstance(rows, list):
        return {}
    out: dict[str, int] = {}
    for row in rows:
        key = row.get(field) or "?"
        out[key] = out.get(key, 0) + 1
    return out


def _week_start(now: datetime) -> date:
    """Date of the most recent Sunday on or before *now* (week starts Sunday)."""
    # weekday(): Mon=0 .. Sun=6. Days since Sunday = (weekday + 1) % 7.
    return (now - timedelta(days=(now.weekday() + 1) % 7)).date()

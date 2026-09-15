"""Tool: epicor_mrp_status

Answer "what happened in the last MRP run?" for BAQ-capable users.

MRP run history lives in ``Ice.SysTask`` (one row per submitted task), with
per-run message/error text in the child ``Ice.SysTaskLog``. An MRP regen run
is identified by ``RunProcedure = 'Erp.Internal.MR.MrpExp.dll'`` and
``TaskDescription = 'Process MRP'`` (the latter excludes the related
``Job Load-Restoring`` step that shares the same RunProcedure).

``Ice.BO.SysTaskSvc`` is not assigned to any department in ``service_index.db``,
so the normal ``check_service_access`` path (used by ``epicor_query`` /
``epicor_run_method``) denies it — that is the "permissions" error seen in the
wild. This tool deliberately uses the BAQ key from ``check_baq_access`` (which
every authenticated user holds and which is *not* gated by department) and is
hardcoded to only ever read the MRP rows of SysTask/SysTaskLog, so the bypass
is narrow and read-only by construction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP as Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_SYSTASK_SERVICE = "Ice.BO.SysTaskSvc"
_SYSTASKLOG_SERVICE = "Ice.BO.SysTaskLogSvc"

# Isolates the MRP regen run in SysTask. RunProcedure is the durable engine
# discriminator; TaskDescription drops the related Job Load-Restoring step
# that shares the same RunProcedure. OData v4 syntax (eq, not =) — this is the
# service entity-set route, not the BAQ Data route.
_MRP_FILTER = (
    "RunProcedure eq 'Erp.Internal.MR.MrpExp.dll' "
    "and TaskDescription eq 'Process MRP'"
)
_SYSTASK_SELECT = (
    "SysTaskNum,TaskDescription,TaskStatus,StartedOn,EndedOn,"
    "SubmitUser,ProgressPercent,ActivityMsg"
)
_SYSTASKLOG_SELECT = "EnteredOn,MsgText,IsError,MsgType,EntryNum"


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_mrp_status`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_mrp_status(history: int = 1, date: str = "") -> str:
        """Summarize what happened in MRP run(s) — most recent, or on a date.

        Returns each MRP run's start/end time, duration, status, who submitted
        it, and progress — plus the log messages for the most recent run in the
        result so you can explain what happened or surface errors. MRP success
        logs are usually sparse (often a single "Done Processing Parts" line),
        so lead with status + duration and only quote log text when ``is_error``
        is true or the status is ERROR/CANCELLED.

        Examples
        --------
        - ``epicor_mrp_status()`` — the last MRP run + its log messages.
        - ``epicor_mrp_status(history=5)`` — the 5 most recent runs' headers.
        - ``epicor_mrp_status(date="2026-05-24")`` — every MRP run that started
          on that calendar day (plant-local). Use this for "what happened
          during MRP on <date>" questions.

        Parameters
        ----------
        history : int, optional
            How many recent MRP runs to return as headers, 1--25 (default 1).
            Ignored when ``date`` is supplied.
        date : str, optional
            A specific plant-local calendar day as ``YYYY-MM-DD``. When set,
            returns all MRP runs that started that day (newest first) instead
            of the most recent N. Resolve relative dates ("last Sunday",
            "May 24th") to ``YYYY-MM-DD`` before calling.
        """
        try:
            session = get_current_session()

            # BAQ key: granted to any authenticated user, not department-gated,
            # and able to read SysTask/SysTaskLog.
            baq_result = rbac.check_baq_access(session.user_id)
            if not baq_result.allowed:
                return format_response(
                    {"error": baq_result.message}, records_key=None
                )
            baq_key = baq_result.api_key or ""

            # Date-window mode vs recent-N mode.
            date = date.strip()
            if date:
                try:
                    query_filter = _day_window_filter(date)
                except ValueError:
                    return format_response(
                        {
                            "error": (
                                f"Invalid date '{date}'. Use YYYY-MM-DD "
                                "(e.g. 2026-05-24)."
                            )
                        },
                        records_key=None,
                    )
                # A single day rarely has >25 MRP runs; cap defensively.
                top = 25
            else:
                query_filter = _MRP_FILTER
                top = max(1, min(history, 25))

            # --- Step 1: MRP run headers, newest first --------------------
            headers_resp = await client.odata_query(
                client._base_url,
                _SYSTASK_SERVICE,
                "SysTasks",
                baq_key,
                odata_params={
                    "$filter": query_filter,
                    "$orderby": "StartedOn desc",
                    "$top": top,
                    "$select": _SYSTASK_SELECT,
                },
            )
            runs = headers_resp.get("value", [])
            if not runs:
                if date:
                    msg = (
                        f"No MRP runs started on {date} (plant-local). MRP "
                        "may not have been run that day."
                    )
                else:
                    msg = (
                        "No MRP runs found in SysTask "
                        "(RunProcedure 'Erp.Internal.MR.MrpExp.dll', "
                        "TaskDescription 'Process MRP')."
                    )
                return format_response({"message": msg}, records_key=None)

            latest = runs[0]
            sys_task_num = latest.get("SysTaskNum")

            # --- Step 2: log messages for the latest run ------------------
            log_messages: list[dict[str, Any]] = []
            if sys_task_num is not None:
                try:
                    log_resp = await client.odata_query(
                        client._base_url,
                        _SYSTASKLOG_SERVICE,
                        "SysTaskLogs",
                        baq_key,
                        odata_params={
                            "$filter": f"SysTaskNum eq {sys_task_num}",
                            "$orderby": "EntryNum",
                            "$select": _SYSTASKLOG_SELECT,
                        },
                    )
                    for row in log_resp.get("value", []):
                        log_messages.append(
                            {
                                "entered_on": row.get("EnteredOn"),
                                "msg_text": row.get("MsgText"),
                                "is_error": row.get("IsError"),
                                "msg_type": row.get("MsgType"),
                            }
                        )
                except Exception:
                    # Log text is supplementary — never fail the whole tool
                    # if only the child fetch breaks.
                    logger.warning(
                        "epicor_mrp_status: SysTaskLog fetch failed for "
                        "SysTaskNum=%s",
                        sys_task_num,
                        exc_info=True,
                    )

            result: dict[str, Any] = {
                "latest_run": _shape_run(latest),
                "log_messages": log_messages,
            }
            if date:
                result["date"] = date
                result["run_count"] = len(runs)
            if len(runs) > 1:
                result["recent_runs"] = [_shape_run(r) for r in runs]

            return format_response(result, records_key=None)

        except Exception:
            logger.exception("epicor_mrp_status failed")
            return format_response(
                {"error": "Failed to fetch MRP run status."},
                records_key=None,
            )


def _day_window_filter(date_str: str) -> str:
    """OData ``$filter`` clause for MRP runs that started on local day *date_str*.

    *date_str* is ``YYYY-MM-DD`` in plant-local time. Returns ``_MRP_FILTER``
    ANDed with a ``StartedOn ge <day> and StartedOn lt <next-day>`` window.

    Epicor's OData compares the stored ``StartedOn`` (a DateTimeOffset carrying
    the plant-local offset) against the literal's **wall-clock components and
    ignores the offset** — a ``...T05:00:00Z`` literal matches
    rows at 5 AM *local*, not 5 AM UTC. So we deliberately do NOT timezone-shift
    the bounds; we pass the plain local-day midnight boundaries and tack on a
    ``Z`` only because the bare and explicit-offset literal forms are rejected (400/500)
    and ``Z`` is the one accepted DateTimeOffset format.

    Raises ``ValueError`` if *date_str* is not a valid ``YYYY-MM-DD`` date.
    """
    day = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    next_day = day + timedelta(days=1)
    start_z = day.strftime("%Y-%m-%dT00:00:00Z")
    end_z = next_day.strftime("%Y-%m-%dT00:00:00Z")
    return (
        f"{_MRP_FILTER} and StartedOn ge {start_z} and StartedOn lt {end_z}"
    )


def _shape_run(row: dict[str, Any]) -> dict[str, Any]:
    """Project a raw SysTask row into the tool's run summary shape."""
    started = row.get("StartedOn")
    ended = row.get("EndedOn")
    duration_min = _duration_minutes(started, ended)
    shaped: dict[str, Any] = {
        "sys_task_num": row.get("SysTaskNum"),
        "status": row.get("TaskStatus"),
        "started": started,
        "ended": ended,
        "duration_min": duration_min,
        "submit_user": row.get("SubmitUser"),
        "progress_pct": row.get("ProgressPercent"),
        "activity_msg": row.get("ActivityMsg"),
    }
    if not ended:
        shaped["note"] = "still running (no EndedOn yet)"
    return shaped


def _duration_minutes(started: Any, ended: Any) -> float | None:
    """Minutes between two Epicor DateTimeOffset strings, or None.

    Returns None when either bound is missing (e.g. an ACTIVE/PENDING run with
    no EndedOn) or when the timestamps can't be parsed.
    """
    if not started or not ended:
        return None
    try:
        start_dt = _parse_dt(started)
        end_dt = _parse_dt(ended)
    except (ValueError, TypeError):
        return None
    if start_dt is None or end_dt is None:
        return None
    return round((end_dt - start_dt).total_seconds() / 60.0, 1)


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 / DateTimeOffset string (``Z`` or ``±hh:mm``)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)

"""Tool: epicor_baq_create

Create a BAQ in Epicor from SQL and (by default) run it in the same call.

Requires WRITE access.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.epicor_client.error_handler import EpicorError, ErrorHandler
from epicor_mcp.response import format_response
from epicor_mcp.tools._aggregate import aggregate_records
from epicor_mcp.tools._baq_helpers import (
    create_baq,
    run_baq,
    strip_outer_where,
)

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)


def _cleanup_hint(query_id: str) -> str:
    """One-off cleanup nudge appended to successful create responses.

    The BAQ is already saved in Epicor; if the query was a throwaway, the
    caller should delete it now rather than leaving ``AUTO-*`` BAQs to
    accumulate in the data dictionary.
    """
    return (
        f"This BAQ is now saved in Epicor as '{query_id}'. If this was a "
        f"one-off query (not something the user asked to save, schedule, or "
        f"re-run later), delete it now with "
        f"epicor_baq_delete(baq_id='{query_id}') so AUTO- BAQs don't pile up. "
        f"Keep it only when the user wants a reusable/saved query."
    )


def _retry_hint(query_id: str, baq_name: str) -> str:
    """Failure nudge: fix-in-place rather than spawning a new BAQ name.

    Steers the caller to re-call with the *same* ``baq_name`` (``replace=True``
    overwrites in place) instead of inventing ``AUTO-foo_v2`` etc., and to
    delete the broken BAQ if abandoning the query.
    """
    return (
        f"The BAQ '{query_id}' was saved but does not run. To iterate, fix "
        f"the SQL and re-call epicor_baq_create with the SAME baq_name "
        f"('{baq_name}') — replace=True overwrites it in place, so you won't "
        f"pile up duplicates. Do NOT invent a new name. If you're abandoning "
        f"this query, delete it with epicor_baq_delete(baq_id='{query_id}')."
    )


async def _diagnose_run_failure(
    *,
    session,
    rbac,
    client,
    baq_index,
    sql: str,
    base_name: str,
) -> dict:
    """Probe whether a failed BAQ run is a SQL problem or a filter-value
    problem by spinning up a temp twin BAQ with the outer WHERE removed,
    running it, then deleting it.

    Returns a small dict describing the verdict, suitable for inlining into
    the response so Claude knows whether to fix SQL or fix filter values.

    Best-effort: any exception inside the probe collapses to
    ``{"verdict": "inconclusive", ...}``.
    """
    stripped_sql, did_strip = strip_outer_where(sql)
    if not did_strip:
        return {
            "verdict": "no_where_to_strip",
            "explanation": (
                "The SQL has no outer WHERE clause, so the failure is in "
                "the SELECT / JOIN structure itself rather than a filter "
                "value. Re-check field aliases, schema prefixes, and join "
                "conditions."
            ),
        }

    diag_name = (base_name + "__diag")[:25]
    try:
        create_result = await create_baq(
            session=session,
            rbac=rbac,
            client=client,
            baq_name=diag_name,
            description=f"Diagnostic twin of {base_name} (WHERE stripped)",
            sql=stripped_sql,
            replace=True,
            baq_index=baq_index,
        )
        if "error" in create_result:
            return {
                "verdict": "inconclusive",
                "reason": "diagnostic_create_failed",
                "detail": create_result.get("error"),
            }
        diag_baq_id = create_result["baq_id"]

        run_failed = False
        run_detail = ""
        try:
            run_result = await run_baq(
                session=session,
                rbac=rbac,
                client=client,
                baq_id=diag_baq_id,
                top=1,
            )
        except EpicorError as exc:
            run_failed = True
            run_detail = ErrorHandler.format_user_message(exc)
            run_result = {}

        # Always clean up — we don't want diagnostic twins littering Epicor.
        try:
            api_key = (
                rbac._user_map.get_write_key()
                if session.access_level == "read_write"
                else rbac._user_map.get_baq_key()
            )
            await client.post(
                "Ice.BO.BAQDesignerSvc/DeleteByID",
                api_key or "",
                json_body={"queryID": diag_baq_id},
            )
        except Exception:
            logger.debug("diagnostic BAQ cleanup failed", exc_info=True)

        if run_failed:
            return {
                "verdict": "sql_likely_bad",
                "diagnostic_baq_run": "also_failed_without_filter",
                "diagnostic_error": run_detail,
                "explanation": (
                    "Even with the WHERE clause removed, the BAQ failed. "
                    "The issue is in the SELECT / JOIN / aggregation, not "
                    "the filter values. Fix the SQL structure — check "
                    "field aliases (every selected column needs "
                    "`as [Alias]`), schema prefixes (Erp./Ice.), that "
                    "calculated/aggregate fields use `[Calculated_*]` "
                    "aliases, and that any `_c` field is selected from "
                    "the `_UD` extension table rather than the base "
                    "table."
                ),
            }
        return {
            "verdict": "filter_likely_bad",
            "diagnostic_baq_run": "succeeded_without_filter",
            "rows_returned_without_filter": run_result.get("record_count"),
            "explanation": (
                "The BAQ executes fine when the WHERE clause is removed. "
                "Your SQL is structurally OK — the failure is caused by "
                "the filter values you used. Verify each filtered value "
                "(codes, IDs, names) actually exists in Epicor before "
                "retrying. Don't recreate the BAQ with tweaked SQL — "
                "fix the filter values."
            ),
        }
    except Exception:
        logger.exception("diagnostic run failed")
        return {"verdict": "inconclusive", "reason": "diagnostic_exception"}


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    baq_index: "BAQSchemaIndex",
) -> None:
    """Bind the ``epicor_baq_create`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_baq_create(
        baq_name: str,
        description: str,
        sql: str,
        run: bool = True,
        top: int = 25,
        filter: str = "",
        orderby: str = "",
        group_by: str = "",
        aggregate: str = "",
        distinct: str = "",
        format: str = "json",
        replace: bool = True,
    ) -> str:
        """Create (or replace) a BAQ from SQL and optionally run it.

        Requires WRITE access. The ``AUTO-`` prefix is prepended to
        ``baq_name`` automatically — pass ``"my-baq"`` and the saved
        BAQ id is ``"AUTO-my-baq"``.

        Workflow rules (Epicor's SQL parser is strict):
          - Prefix every table: ``Erp.PartCost as [PartCost]``
          - Alias every selected field:
              ``[POHeader].[PONum] as [POHeader_PONum]``
          - Calculated fields: ``(expr) as [Calculated_Name]``
          - User-defined ``_c`` fields come from the ``_UD`` extension
            table, never the base table — ``[OrderRel].[Note_c]``
            saves fine and then fails at run time. Instead::

                LEFT OUTER JOIN Erp.OrderRel_UD as [OrderRel_UD]
                  ON OrderRel.SysRowID = OrderRel_UD.ForeignSysRowID

            then select ``[OrderRel_UD].[Note_c] as [...]``. (This is
            BAQ-only — ``epicor_query`` reads ``_c`` off the base table.)
          - Include ``Company`` first in every join condition.
          - Put ``GROUP BY`` inside a subquery, never in the outer query.
          - Use normal newlines; HTML-entity-encoded operators (``&lt;``,
            ``&gt;``) are un-escaped automatically.

        Args:
            baq_name: Short name without AUTO- prefix (max 25 chars,
                alphanumeric + ``-_``).
            description: One-line description.
            sql: Epicor-dialect SQL.
            run: When ``True`` (default), execute the BAQ immediately and
                return its first ``top`` rows alongside the save metadata.
                Set ``False`` to save without running.
            top: Row cap for the post-save run (1-1000, default 25).
            filter: Optional OData ``$filter`` applied to the run.
            orderby: Optional OData ``$orderby`` applied to the run.
            group_by, aggregate, distinct: Optional in-process aggregation
                of the returned rows. See ``epicor_query`` for syntax.
            format: ``"json"`` (default), ``"csv"``, or ``"tsv"``. When
                ``top >= 200`` and the user didn't set this, switches to
                CSV automatically.
            replace: When ``True`` (default), delete any existing
                ``AUTO-<baq_name>`` first so iterating just means
                re-calling with the same name.

        Response shape:
            ``{"success": true, "baq_id": "AUTO-...", "replaced_existing":
            bool, "tables_used": N, "fields_selected": N, ...}``
            When ``run=True`` the BAQ rows are merged in under
            ``records`` / ``record_count`` (or aggregated form).

        Lifecycle / cleanup:
            Every successful response carries a ``cleanup_hint``. The BAQ
            is persisted in Epicor — if the query was a one-off, delete it
            with ``epicor_baq_delete(baq_id=...)`` once you've returned the
            answer, so ``AUTO-*`` BAQs don't accumulate. Keep it only when
            the user wants a reusable/saved query. On a run failure the
            response carries a ``next_step``: fix the SQL and re-call with
            the SAME ``baq_name`` (``replace=True`` overwrites in place) —
            don't spawn a new name — or delete the broken BAQ if abandoning.
        """
        try:
            session = get_current_session()

            create_result = await create_baq(
                session=session,
                rbac=rbac,
                client=client,
                baq_name=baq_name,
                description=description,
                sql=sql,
                replace=replace,
                baq_index=baq_index,
            )
            if "error" in create_result:
                return json.dumps(
                    {"success": False, **create_result, "baq_name": baq_name},
                    indent=2,
                )

            query_id = create_result["baq_id"]

            if not run:
                return json.dumps(
                    {
                        "success": True,
                        **create_result,
                        "cleanup_hint": _cleanup_hint(query_id),
                    },
                    indent=2,
                )

            # Auto-CSV at scale — only when not aggregating (agg is small).
            if (
                format == "json"
                and top >= 200
                and not (group_by or aggregate or distinct)
            ):
                format = "csv"

            try:
                run_result = await run_baq(
                    session=session,
                    rbac=rbac,
                    client=client,
                    baq_id=query_id,
                    filter=filter,
                    top=top,
                    orderby=orderby,
                )
            except EpicorError as exc:
                diagnostic = await _diagnose_run_failure(
                    session=session,
                    rbac=rbac,
                    client=client,
                    baq_index=baq_index,
                    sql=sql,
                    base_name=baq_name,
                )
                return json.dumps({
                    "success": False,
                    **create_result,
                    "run_error": (
                        f"BAQ '{query_id}' saved, but the immediate run "
                        f"failed: {ErrorHandler.format_user_message(exc)}"
                    ),
                    "status_code": exc.status_code,
                    "epicor_message": exc.message,
                    "diagnostic": diagnostic,
                    "next_step": _retry_hint(query_id, baq_name),
                }, indent=2)

            records = run_result.get("records")
            if (group_by or aggregate or distinct) and isinstance(records, list):
                try:
                    agg = aggregate_records(
                        records,
                        group_by=group_by,
                        aggregate=aggregate,
                        distinct=distinct,
                    )
                except ValueError as ve:
                    return json.dumps({
                        "success": False,
                        **create_result,
                        "run_error": str(ve),
                    }, indent=2)
                run_result = {
                    **{k: v for k, v in run_result.items()
                       if k not in ("records", "record_count")},
                    **agg,
                }

            merged = {
                "success": True,
                **{k: v for k, v in create_result.items() if k != "baq_id"},
                "baq_id": query_id,
                **{k: v for k, v in run_result.items() if k != "baq_id"},
                "cleanup_hint": _cleanup_hint(query_id),
            }
            return format_response(merged, records_key="records", format=format)

        except Exception as exc:
            logger.exception("epicor_baq_create failed")
            error_msg = str(exc)
            hint = ""
            if "500" in error_msg or "Internal Server Error" in error_msg:
                hint = (
                    " This usually means the SQL has syntax errors. "
                    "Check table prefixes (Erp., Ice.), field aliases, "
                    "and bracket formatting."
                )
            elif "401" in error_msg or "403" in error_msg:
                hint = (
                    " Authentication or authorization failed. The write "
                    "API key may be expired or invalid."
                )
            return json.dumps({
                "success": False,
                "error": f"BAQ creation failed: {error_msg}{hint}",
                "baq_name": baq_name,
            })

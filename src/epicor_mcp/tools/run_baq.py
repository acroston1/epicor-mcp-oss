"""Tool: epicor_run_baq

Execute a BAQ (Business Activity Query) by its ID.  BAQs are pre-defined
server-side queries that join multiple tables and expose a flat result set
via the ``BaqSvc`` OData endpoint.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.epicor_client.error_handler import EpicorError, ErrorHandler
from epicor_mcp.response import format_response
from epicor_mcp.tools._aggregate import aggregate_records
from epicor_mcp.tools._baq_helpers import run_baq as run_baq_impl
from epicor_mcp.tools._tenant import PLANTS, match_plant

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_run_baq`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_run_baq(
        baq_id: str,
        filter: str = "",
        top: int = 10,
        orderby: str = "",
        group_by: str = "",
        aggregate: str = "",
        distinct: str = "",
        format: str = "json",
    ) -> str:
        """Execute a BAQ (Business Activity Query) by its ID.

        BAQs are pre-defined queries that join multiple Epicor tables and
        return a flat result set.  Use this when a BAQ already exists for
        the data you need, rather than building complex OData queries
        yourself.

        Examples
        --------
        - ``epicor_run_baq(baq_id="zCustom_OpenPOs")``
        - ``epicor_run_baq(baq_id="zCustom_OpenPOs",
              filter="POHeader_VendorNum eq 1234", top=50)``
        - ``epicor_run_baq(baq_id="zCustom_APAging",
              orderby="InvoiceAmt desc", top=200)``
        - ``epicor_run_baq(baq_id="zCustom_APAging", top=500,
              format="csv")`` — CSV fits ~2-3× more rows in the
              tool-result byte budget.

        Parameters
        ----------
        baq_id : str
            The BAQ identifier (e.g. ``"zCustom_OpenPOs"``).
        filter : str, optional
            OData ``$filter`` expression applied to the BAQ results.
            Column names in BAQ results are typically prefixed with
            the table alias (e.g. ``"POHeader_VendorNum"``).
            String literals must be single-quoted
            (e.g. ``"PartClass eq 'ABC'"``, ``"Year ge '2025'"``).
            If unsure of a column's type, check ``epicor_table_lookup``
            first or inspect a sample row with ``top=1``.
        top : int, optional
            Maximum records to return, 1--1000 (default ``10``).  Bump
            up when you need more and pair with ``format="csv"`` for
            large result sets.
        orderby : str, optional
            OData ``$orderby`` expression
            (e.g. ``"InvoiceAmt desc"``).
        format : str, optional
            Wire format for the rows array.  ``"json"`` (default) returns
            rows as a JSON list.  ``"csv"`` or ``"tsv"`` replaces the
            ``records`` array with a ``rows_csv`` string.
        """
        try:
            session = get_current_session()

            # Auto-CSV at scale: same heuristic as epicor_query.
            if (
                format == "json"
                and top >= 200
                and not (group_by or aggregate or distinct)
            ):
                format = "csv"

            result = await run_baq_impl(
                session=session,
                rbac=rbac,
                client=client,
                baq_id=baq_id,
                filter=filter,
                top=top,
                orderby=orderby,
            )
            records = result.get("records")
            if (group_by or aggregate or distinct) and isinstance(records, list):
                try:
                    agg = aggregate_records(
                        records,
                        group_by=group_by,
                        aggregate=aggregate,
                        distinct=distinct,
                    )
                except ValueError as ve:
                    return json.dumps({"error": str(ve)})
                merged = {
                    **{k: v for k, v in result.items()
                       if k not in ("records", "record_count")},
                    **agg,
                }
                if result.get("record_count") == top:
                    merged["note"] = (
                        f"Aggregated over the first {top} rows. Raise 'top' "
                        "to widen the scan."
                    )
                return format_response(merged, records_key="records", format=format)
            return format_response(result, records_key="records", format=format)

        except EpicorError as exc:
            logger.exception("epicor_run_baq failed (Epicor API error)")
            payload: dict = {
                "error": (
                    f"BAQ '{baq_id}' execution failed: "
                    f"{ErrorHandler.format_user_message(exc)}"
                ),
                "status_code": exc.status_code,
                "epicor_message": exc.message,
            }
            # The model often reaches for run_baq with a place name when the
            # user says "report from <site>" — but a site is a Plant filter,
            # not a saved BAQ. Redirect immediately instead of letting it hunt.
            plant = match_plant(baq_id)
            if plant is not None:
                payload["not_a_baq_hint"] = (
                    f"'{baq_id}' looks like the site '{PLANTS[plant]}', which is "
                    f"a PLANT (code '{plant}'), not a BAQ. For a report 'from "
                    f"{PLANTS[plant]}', query the data directly with a plant "
                    f"filter — e.g. epicor_query(service=\"Erp.BO.SalesOrderSvc\", "
                    f"entity_set=\"OrderHed\", filter=\"OpenOrder eq true and "
                    f"Plant eq '{plant}'\"), or epicor_query_with_children for a "
                    f"by-customer/month/part rollup."
                )
            return json.dumps(payload)
        except Exception as exc:
            logger.exception("epicor_run_baq failed")
            return json.dumps(
                {
                    "error": (
                        f"BAQ '{baq_id}' execution failed: {exc}. "
                        "Verify the BAQ ID exists and the filter syntax is correct."
                    )
                }
            )

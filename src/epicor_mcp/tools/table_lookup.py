'Tool: epicor_table_lookup'

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

# A "full name" looks like ``Schema.Table`` — letters/digits, dot, letters/digits.
# Used to decide whether the caller is naming tables (schema mode) or
# describing what they want (search mode).
_FULL_NAME_RE = re.compile(r"^[A-Za-z_][\w]*\.[A-Za-z_][\w]*$")


def _looks_like_full_names(query: str) -> list[str] | None:
    """If *query* is a comma- or whitespace-separated list of ``Schema.Table``
    names, return the cleaned list. Otherwise return ``None``."""
    parts = [p.strip() for p in re.split(r"[,\s]+", query) if p.strip()]
    if not parts:
        return None
    if all(_FULL_NAME_RE.match(p) for p in parts):
        return parts
    return None


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    baq_index: "BAQSchemaIndex",
) -> None:
    """Bind the ``epicor_table_lookup`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_table_lookup(
        query: str,
        limit: int = 10,
        full_schema_for_top: int = 2,
    ) -> str:
        """Find Epicor tables by keyword OR fetch full schema for named tables.

        Use this as the entry point for BAQ authoring. It dispatches
        automatically:

        - **Schema mode** — when ``query`` is one or more comma-separated
          ``Schema.Table`` names (e.g. ``"Erp.OrderHed,Erp.OrderDtl"``),
          returns the full field list (name, type, label, mandatory,
          format, description) for each. Skip ahead to this when you
          already know the table names.

        - **Search mode** — for any other input (keywords, natural
          language, bare table names like ``"OrderHed"``), returns the
          best-ranked matching tables. Each match has its key fields
          inline; the top ``full_schema_for_top`` matches include the
          complete field list so you can usually go straight to
          ``epicor_baq_create`` without a follow-up call.

        Search ranking (in order):
          1. Exact case-insensitive match on table name
             (``OrderHed`` → ``Erp.OrderHed`` first).
          2. Prefix match on table name (catches ``OrderHedXxx``).
          3. FTS5 over name + description, terms OR-joined so verbose
             natural-language queries still return results.

        NOTE: This tool searches the data dictionary (Epicor table
        catalog). It does NOT list saved BAQ definitions — there is no
        index for those. Use ``epicor_run_baq`` to execute a known BAQ.

        Args:
            query: Either ``"Schema.Table[, Schema.Table...]"`` (schema
                mode) or free-text keywords (search mode).
            limit: Max search results to return (1-50, default 10).
                Ignored in schema mode.
            full_schema_for_top: In search mode, how many top matches get
                the full field list inline (default 2). Pass 0 to keep
                every match brief.
        """
        try:
            session = get_current_session()
            get_current_session()  # ensure auth

            limit = max(1, min(limit, 50))
            full_schema_for_top = max(0, min(full_schema_for_top, limit))

            # --- Schema mode ---------------------------------------------
            named = _looks_like_full_names(query)
            if named:
                if len(named) > 10:
                    return json.dumps({
                        "error": (
                            f"Too many tables requested ({len(named)}). "
                            "Max 10 per call; split into multiple calls."
                        )
                    })
                return _render_schema_mode(baq_index, named)

            # --- Search mode ---------------------------------------------
            tables = baq_index.search_tables(query, limit=limit)
            if not tables:
                return json.dumps({
                    "mode": "search",
                    "query": query,
                    "results": [],
                    "hint": (
                        "No tables matched. If you already know the "
                        "full table names, call this tool with "
                        "Schema.Table form (e.g. \"Erp.OrderHed,"
                        "Erp.OrderDtl\") to get their schemas directly."
                    ),
                })

            output: list[dict] = []
            for idx, tbl in enumerate(tables):
                full_name = tbl.get("full_name", "")
                fields = baq_index.get_fields(full_name)
                show_full = idx < full_schema_for_top
                visible = fields if show_full else fields[:10]
                rendered = [
                    {
                        "name": f.get("field_name", ""),
                        "type": f.get("data_type", ""),
                    }
                    for f in visible
                ]
                output.append({
                    "full_name": full_name,
                    "table_name": tbl.get("table_name", ""),
                    "description": tbl.get("description", ""),
                    "fields" if show_full else "key_fields": rendered,
                    "total_fields": len(fields),
                    "full_schema_inline": show_full,
                })

            return json.dumps({
                "mode": "search",
                "query": query,
                "result_count": len(output),
                "results": output,
                "note": (
                    f"Top {full_schema_for_top} match(es) include the full "
                    "schema — usually enough to write the BAQ SQL directly. "
                    "For others, re-call with their Schema.Table name to "
                    "get full fields."
                ) if full_schema_for_top else None,
            }, indent=2)

        except Exception:
            logger.exception("epicor_table_lookup failed")
            return json.dumps(
                {"error": "Table lookup failed. Try different keywords or named tables."}
            )


def _render_schema_mode(
    baq_index: "BAQSchemaIndex",
    table_names: list[str],
) -> str:
    """Return full schema for each named table (or ``not_found``)."""
    tables_out: list[dict] = []
    not_found: list[str] = []
    for name in table_names:
        info = baq_index.get_table(name)
        if info is None:
            not_found.append(name)
            continue
        fields = baq_index.get_fields(name)
        tables_out.append({
            "full_name": info.get("full_name", ""),
            "table_name": info.get("table_name", ""),
            "description": info.get("description", ""),
            "field_count": len(fields),
            "fields": [
                {
                    "name": f.get("field_name", ""),
                    "type": f.get("data_type", ""),
                }
                for f in fields
            ],
        })
    result: dict = {
        "mode": "schema",
        "tables": tables_out,
        "baq_sql_rules": (
            "Prefix tables with schema (Erp.X / Ice.X), alias every table "
            "and every selected field ([T].[F] as [T_F]), include Company "
            "first in joins, and put GROUP BY in a subquery."
        ),
    }
    if not_found:
        result["not_found"] = not_found
        result["not_found_hint"] = (
            "Call this tool with a search query (free-text keywords) "
            "to find the correct table names."
        )
    return format_response(result, records_key=None)

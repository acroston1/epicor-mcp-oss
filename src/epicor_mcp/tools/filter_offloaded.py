"""Tool: epicor_filter_offloaded

Structured, field-aware row filter over an offloaded tool-response file.

``epicor_grep_offloaded`` searches lines with a regex — fine for free-text
scans.  For "find the row where field X equals value Y" questions, use
this tool instead: it parses the JSON server-side and returns full rows
that match, without line-number bookkeeping or max-matches cutoffs.
"""

from __future__ import annotations

import json
import logging
import re as _re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response
from epicor_mcp.response.formatter import _collect_rows_lists

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_FILENAME_RE = _re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{32}\.json$")


def _extract_filename(url_or_name: str) -> str:
    return url_or_name.strip().rstrip("/").split("/")[-1]


def _row_matches(row: dict, where: dict) -> bool:
    """All key/value pairs in ``where`` must equal the row's values.

    String comparison is case-insensitive; numeric comparisons are exact.
    ``None`` in ``where`` matches missing or null fields.
    """
    for field, expected in where.items():
        actual = row.get(field)
        if expected is None:
            if actual is not None and actual != "":
                return False
            continue
        if actual is None:
            return False
        if isinstance(expected, str) and isinstance(actual, str):
            if expected.casefold() != actual.casefold():
                return False
            continue
        if actual != expected:
            return False
    return True


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_filter_offloaded`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_filter_offloaded(
        file: str,
        where: str,
        table: str = "",
        limit: int = 20,
        select: str = "",
    ) -> str:
        """Return rows from an offloaded file that match a ``{field: value}`` filter.

        **When to use this over ``epicor_grep_offloaded``:** you know the
        exact field(s) and value(s) you want — e.g. "which QuoteDtl row
        has ``QuoteLine`` 51 and ``PartNum`` PART-001?". This parses the
        JSON on the server and returns full rows, avoiding the
        line-number gymnastics grep forces when a field repeats many
        times in the file.

        **Even better option before you use this:** if you haven't
        already offloaded the record, skip ``epicor_get_record`` with
        ``include_children=True`` and just run::

            epicor_query(service="Erp.BO.QuoteSvc", entity_set="QuoteDtl",
                         filter="QuoteNum eq 10001 and PartNum eq 'PART-001'")

        That returns the single matching row directly — no offload, no
        follow-up call.

        Parameters
        ----------
        file : str
            URL or filename returned by a prior tool call's offload
            pointer (``offloaded: true`` payload).
        where : str
            JSON object of ``{field: value}`` pairs — e.g.
            ``'{"QuoteLine": 51, "PartNum": "PART-001"}'``.  All pairs
            must match (AND).  String comparisons are
            case-insensitive; numerics are exact.
        table : str, optional
            Name of the top-level table to scan (``"QuoteDtl"``,
            ``"POHeader"``, etc.).  If empty, every list-of-dicts in the
            payload is searched and results are grouped by table.
        limit : int, optional
            Max rows to return across all tables (default 20, capped 500).
        select : str, optional
            Comma-separated field names to project (default: all fields).

        Examples
        --------
        - ``epicor_filter_offloaded(file="<url>", table="QuoteDtl",
              where='{"QuoteLine": 51}')``
        - ``epicor_filter_offloaded(file="<url>", table="JobOper",
              where='{"OpCode": "ASSM"}',
              select="OprSeq,ResourceGrpID,EstProdHours")``
        """
        try:
            _ = get_current_session()

            from epicor_mcp.config import get_settings

            settings = get_settings()
            offload_dir = settings.response_offload_dir
            if not offload_dir or not str(offload_dir).strip():
                return json.dumps({
                    "error": "Offloading is disabled on this server."
                })

            filename = _extract_filename(file)
            if not _FILENAME_RE.match(filename):
                return json.dumps({
                    "error": (
                        f"Invalid offload filename {filename!r}. Pass the "
                        "URL or filename returned by a prior tool's "
                        "offload pointer."
                    )
                })

            file_path = Path(offload_dir) / filename
            try:
                resolved = file_path.resolve(strict=True)
                resolved.relative_to(Path(offload_dir).resolve())
            except (OSError, ValueError):
                return json.dumps({
                    "error": (
                        "File not found (possibly purged by retention). "
                        "Re-run the original query for a fresh pointer."
                    )
                })

            try:
                where_dict = json.loads(where or "{}")
            except json.JSONDecodeError as exc:
                return json.dumps({
                    "error": f"Invalid JSON in 'where': {exc}",
                })
            if not isinstance(where_dict, dict) or not where_dict:
                return json.dumps({
                    "error": (
                        "'where' must be a non-empty JSON object, e.g. "
                        '\'{"QuoteLine": 51, "PartNum": "PART-001"}\''
                    )
                })

            limit = max(1, min(limit, 500))
            select_fields = [
                s.strip() for s in select.split(",") if s.strip()
            ] if select else []

            try:
                with resolved.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.exception("Reading offloaded file failed")
                return json.dumps({
                    "error": f"Could not parse offloaded file: {exc}",
                })

            # Which table(s) to scan
            target_lists: list[tuple[str, list[dict]]] = []
            if table:
                rows = data.get(table) if isinstance(data, dict) else None
                if isinstance(rows, list):
                    target_lists = [(table, rows)]
                else:
                    available = (
                        [k for k, v in data.items()
                         if isinstance(v, list)] if isinstance(data, dict)
                        else []
                    )
                    return json.dumps({
                        "error": (
                            f"Table {table!r} not found or is not a list. "
                            f"Available list keys: {available}"
                        )
                    })
            else:
                for _, key, rows in _collect_rows_lists(data):
                    # Only include top-level containers — skip nested row lists
                    if isinstance(key, str):
                        target_lists.append((key, rows))

            # Apply the where filter, optional projection
            matches_by_table: dict[str, list[dict]] = {}
            total_matched = 0
            total_scanned = 0
            for tbl_name, rows in target_lists:
                tbl_matches: list[dict] = []
                for row in rows:
                    total_scanned += 1
                    if not isinstance(row, dict):
                        continue
                    if not _row_matches(row, where_dict):
                        continue
                    if select_fields:
                        tbl_matches.append(
                            {k: row.get(k) for k in select_fields}
                        )
                    else:
                        tbl_matches.append(row)
                    total_matched += 1
                    if total_matched >= limit:
                        break
                if tbl_matches:
                    matches_by_table[tbl_name] = tbl_matches
                if total_matched >= limit:
                    break

            result: dict[str, Any] = {
                "file": filename,
                "where": where_dict,
                "total_matched": total_matched,
                "total_scanned": total_scanned,
                "matches": matches_by_table,
            }
            if total_matched >= limit:
                result["note"] = (
                    f"Reached limit={limit}. Tighten the 'where' clause "
                    "or raise 'limit' if you need more matches."
                )
            if not matches_by_table:
                result["note"] = (
                    f"No rows matched {where_dict!r} in {filename}. "
                    "Check field names and values, or try "
                    "epicor_grep_offloaded for a broader text search."
                )

            return format_response(result, records_key=None)

        except Exception:
            logger.exception("epicor_filter_offloaded failed")
            return json.dumps({
                "error": (
                    "Filter failed. Verify the file URL/name and the "
                    "where clause."
                )
            })

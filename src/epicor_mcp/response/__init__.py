"""Response formatting layer for Epicor MCP tools.

Every tool routes its final return value through :func:`format_response` so
that bloat stripping, optional CSV encoding, and the oversize-truncation
guard all live in one place.
"""

from epicor_mcp.response.formatter import (
    BLOAT_KEYS,
    BLOAT_PREFIXES,
    compute_stats,
    format_response,
    offload_response,
    purge_stale_offloads,
    rows_to_csv,
    strip_bloat,
    truncate_and_summarize,
)

__all__ = [
    "BLOAT_KEYS",
    "BLOAT_PREFIXES",
    "compute_stats",
    "format_response",
    "offload_response",
    "purge_stale_offloads",
    "rows_to_csv",
    "strip_bloat",
    "truncate_and_summarize",
]

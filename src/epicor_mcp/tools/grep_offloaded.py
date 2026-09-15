"""Tool: epicor_grep_offloaded

Search an offloaded tool-response file by regex, server-side.

When a read tool's response is too large for the chat context it gets
offloaded to a file on the MCP server and only a small pointer (URL,
row counts, preview, stats) is returned.  Fetching that file from a
bash sandbox often fails (host allowlists, missing tools, etc.), so
this tool runs the grep server-side and returns only the matching
lines — cheaper in context than the whole file and not blocked by
client sandbox policies.
"""

from __future__ import annotations

import json
import logging
import re as _re
from pathlib import Path
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

# Same regex the /response-files/ HTTP endpoint uses to prevent traversal.
_FILENAME_RE = _re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{32}\.json$")


def _extract_filename(url_or_name: str) -> str:
    """Return just the basename from either a bare filename or a full URL."""
    raw = url_or_name.strip()
    # Strip trailing slash, then take the last path component
    return raw.rstrip("/").split("/")[-1]


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_grep_offloaded`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_grep_offloaded(
        file: str,
        pattern: str,
        max_matches: int = 50,
        case_insensitive: bool = True,
        context_lines: int = 0,
    ) -> str:
        'Grep an offloaded tool-response file and return matching lines.'
        try:
            # Validate session (any authenticated user can grep their own
            # offloaded results).
            _ = get_current_session()

            from epicor_mcp.config import get_settings

            settings = get_settings()
            offload_dir = settings.response_offload_dir
            if not offload_dir or not str(offload_dir).strip():
                return json.dumps({
                    "error": (
                        "Offloading is disabled on this server "
                        "(EPICOR_MCP_RESPONSE_OFFLOAD_DIR is empty)."
                    )
                })

            filename = _extract_filename(file)
            if not _FILENAME_RE.match(filename):
                return json.dumps({
                    "error": (
                        f"Invalid offload filename {filename!r}. Expected "
                        "the URL or filename returned by a previous tool's "
                        "offload pointer (pattern "
                        "YYYYMMDDTHHMMSSZ-<32hex>.json)."
                    )
                })

            file_path = Path(offload_dir) / filename
            try:
                resolved = file_path.resolve(strict=True)
                resolved.relative_to(Path(offload_dir).resolve())
            except (OSError, ValueError):
                return json.dumps({
                    "error": (
                        f"File not found (may have been purged by the "
                        f"retention policy). Re-run the original query to "
                        f"get a fresh offload pointer."
                    )
                })

            flags = _re.IGNORECASE if case_insensitive else 0
            try:
                rx = _re.compile(pattern, flags)
            except _re.error as exc:
                return json.dumps({
                    "error": f"Invalid regex pattern: {exc}",
                })

            max_matches = max(1, min(max_matches, 500))
            context_lines = max(0, min(context_lines, 10))

            matches: list[dict] = []
            hits = 0
            with resolved.open("r", encoding="utf-8") as f:
                lines = f.readlines()

            for lineno, line in enumerate(lines, start=1):
                if not rx.search(line):
                    continue
                hits += 1
                entry: dict = {
                    "line": lineno,
                    "text": line.rstrip("\n"),
                }
                if context_lines > 0:
                    start = max(0, lineno - 1 - context_lines)
                    end = min(len(lines), lineno + context_lines)
                    entry["context"] = [
                        {"line": i + 1, "text": lines[i].rstrip("\n")}
                        for i in range(start, end)
                        if i != lineno - 1
                    ]
                matches.append(entry)
                if len(matches) >= max_matches:
                    break

            result: dict = {
                "file": filename,
                "pattern": pattern,
                "match_count": len(matches),
                "matches": matches,
                "total_lines_scanned": len(lines),
            }
            if hits >= max_matches:
                result["note"] = (
                    f"Reached max_matches={max_matches}; there may be more "
                    "matches further down. Tighten the pattern or raise "
                    "max_matches if you need them all."
                )
            if not matches:
                result["note"] = (
                    f"No matches for {pattern!r} in {filename}. "
                    "Check the pattern and try again, or fall back to a "
                    "targeted epicor_query with a $filter."
                )

            return format_response(result, records_key="matches")

        except Exception:
            logger.exception("epicor_grep_offloaded failed")
            return json.dumps({
                "error": (
                    "Grep failed. Verify the file URL/name from a recent "
                    "offload pointer is still valid (24h retention)."
                )
            })

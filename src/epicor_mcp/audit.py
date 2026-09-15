"""Audit logging for MCP tool calls.

Every tool invocation is recorded to an append-only SQLite database with:
who (user email, department), what (tool name, arguments), when (ISO-8601
timestamp), outcome (success/error), and duration.

Usage::

    audit = AuditLogger(Path("data/audit.db"))
    install_audit_hook(mcp_server, audit)   # wraps FastMCP.call_tool()
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from epicor_mcp.context import (
    clear_authz_decision,
    get_authz_decision,
    get_current_session_or_none,
)

# --- Circuit breaker ---------------------------------------------------------
# When the same (user, tool) emits ``_CIRCUIT_THRESHOLD`` errors within
# ``_CIRCUIT_WINDOW_SECONDS``, short-circuit the next call with a guidance
# message instead of forwarding it to Epicor. Cleared on the next success.
# Anti-pattern this catches: Claude looping on baq_create with empty 400s
# from the BAQ runtime.
#
# The window doubles as the lockout duration: once tripped, the breaker clears
# only after the failures age out of this window (tripped calls don't re-arm
# it). Keep this short — a long window leaves the model locked out with no way
# to retry until it elapses.
_CIRCUIT_THRESHOLD = 5
_CIRCUIT_WINDOW_SECONDS = 5.0
_circuit_failures: dict[tuple[str, str], deque[float]] = {}

logger = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    user_email   TEXT    NOT NULL,
    department   TEXT    NOT NULL DEFAULT '',
    tool_name    TEXT    NOT NULL,
    arguments    TEXT    NOT NULL DEFAULT '{}',
    status       TEXT    NOT NULL,
    duration_ms  REAL,
    error        TEXT    NOT NULL DEFAULT '',
    authz_source TEXT    NOT NULL DEFAULT '',
    authz_reason TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp  ON audit_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_user       ON audit_log (user_email);
CREATE INDEX IF NOT EXISTS idx_audit_tool       ON audit_log (tool_name);

-- Menu-derived RBAC shadow report: every disagreement between
-- the legacy department decision and the menu decision, in shadow mode.
CREATE TABLE IF NOT EXISTS authz_shadow_divergence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    user          TEXT    NOT NULL,
    service       TEXT    NOT NULL,
    tool          TEXT    NOT NULL DEFAULT '',
    dept_decision INTEGER NOT NULL,
    menu_decision INTEGER NOT NULL,
    reason        TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_divergence_ts   ON authz_shadow_divergence (timestamp);
CREATE INDEX IF NOT EXISTS idx_divergence_user ON authz_shadow_divergence (user);
"""

# Columns added to an existing (old-schema) audit_log by in-place migration.
_AUTHZ_COLUMNS: tuple[str, ...] = ("authz_source", "authz_reason")


class AuditLogger:
    """Append-only SQLite audit log for tool calls."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # Migrate an existing (pre-authz) audit_log in place. ``executescript``
        # above only creates the table when absent, so an old db keeps its
        # original column set until we ALTER it here.
        self._migrate_authz_columns()
        logger.info("Audit logger initialized at %s", db_path)

    def _migrate_authz_columns(self) -> None:
        """Idempotently add authz_source / authz_reason to an old audit_log."""
        cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(audit_log)")
        }
        if not cols:
            # Table did not exist before executescript — it now has the columns.
            return
        added = False
        for col in _AUTHZ_COLUMNS:
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE audit_log ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
                added = True
        if added:
            self._conn.commit()
            logger.info("Migrated audit_log: added authz_source / authz_reason columns")

    def log(
        self,
        *,
        user_email: str,
        department: str,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        duration_ms: float | None = None,
        error: str = "",
        authz_source: str = "",
        authz_reason: str = "",
    ) -> None:
        """Write a single audit record."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                "INSERT INTO audit_log "
                "(timestamp, user_email, department, tool_name, arguments, status, "
                "duration_ms, error, authz_source, authz_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    user_email,
                    department,
                    tool_name,
                    json.dumps(arguments, default=str),
                    status,
                    duration_ms,
                    error,
                    authz_source,
                    authz_reason,
                ),
            )
            self._conn.commit()
        except Exception:
            logger.exception("Failed to write audit log entry")

    def log_shadow_divergence(
        self,
        *,
        user: str,
        service: str,
        tool: str = "",
        dept_decision: bool,
        menu_decision: bool,
        reason: str = "",
    ) -> None:
        """Record a shadow-mode legacy-vs-menu divergence for the shadow report."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                "INSERT INTO authz_shadow_divergence "
                "(timestamp, user, service, tool, dept_decision, menu_decision, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    user,
                    service,
                    tool,
                    int(bool(dept_decision)),
                    int(bool(menu_decision)),
                    reason,
                ),
            )
            self._conn.commit()
        except Exception:
            logger.exception("Failed to write shadow-divergence entry")

    def shadow_report(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return the most recent shadow divergences (newest first)."""
        try:
            rows = self._conn.execute(
                "SELECT timestamp, user, service, tool, dept_decision, "
                "menu_decision, reason FROM authz_shadow_divergence "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except Exception:
            logger.exception("Failed to read shadow-divergence report")
            return []
        return [
            {
                "timestamp": r[0],
                "user": r[1],
                "service": r[2],
                "tool": r[3],
                "dept_decision": bool(r[4]),
                "menu_decision": bool(r[5]),
                "reason": r[6],
            }
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()


def _result_to_text(result: Any) -> str | None:
    """Best-effort: pull the JSON/text payload out of a tool result.

    FastMCP wraps tool returns in ``TextContent`` (or a list of them). The
    underlying tool function returns ``str`` (a JSON-serialised dict). This
    walks the common shapes and returns the first text-like body it finds.

    Handles the post-``convert_result=True`` shape (``list[ContentBlock]``)
    as well as ``(unstructured, structured)`` tuples and bare strings.
    """
    if result is None:
        return None
    if isinstance(result, str):
        return result
    # FastMCP ``convert_result=True`` with output_schema returns
    # ``(unstructured_content, structured_content)``. Walk the unstructured
    # half (first element) for the text body.
    if isinstance(result, tuple) and len(result) == 2:
        return _result_to_text(result[0])
    if isinstance(result, (list, tuple)):
        for item in result:
            text = _result_to_text(item)
            if text:
                return text
        return None
    # TextContent and friends carry the payload on ``.text``.
    text_attr = getattr(result, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    # Some shapes are dicts already (newer FastMCP versions)
    if isinstance(result, dict):
        if "text" in result and isinstance(result["text"], str):
            return result["text"]
        try:
            return json.dumps(result)
        except Exception:
            return None
    return None


def _build_breaker_response(
    json_text: str,
    convert_result: bool,
) -> Any:
    """Return the circuit-breaker payload in whatever shape the caller of
    ``call_tool`` expects.

    The FastMCP server calls the tool manager with ``convert_result=True``
    (see ``server.py``), which means the wrapped function must return
    a ``list[ContentBlock]`` already. When the breaker fires we have to
    match that shape — returning a bare string makes pydantic try to
    validate each character as a Content variant, generating thousands of
    bogus errors.

    When ``convert_result`` is False (rare: direct ToolManager calls in
    tests or dev tooling), the framework expects the raw return value,
    so we pass the JSON string through unchanged.
    """
    if convert_result:
        return [TextContent(type="text", text=json_text)]
    return json_text


def _classify_response(result: Any) -> tuple[str, str]:
    """Return ``(status, error_message)`` for an MCP tool result.

    A tool returning ``{"error": "..."}`` or ``{"success": false, ...}``
    counts as an error even though no exception escaped. Anything else is
    success.
    """
    text = _result_to_text(result)
    if not text:
        return ("success", "")
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return ("success", "")
    if not isinstance(payload, dict):
        return ("success", "")
    if payload.get("success") is False:
        msg = (
            payload.get("error")
            or payload.get("run_error")
            or payload.get("message")
            or "tool returned success=false"
        )
        return ("error", str(msg))
    if "error" in payload and payload.get("error"):
        return ("error", str(payload["error"]))
    return ("success", "")


def _circuit_state(user_email: str, tool_name: str) -> deque[float]:
    """Return the (auto-pruned) failure deque for a (user, tool) pair."""
    key = (user_email, tool_name)
    q = _circuit_failures.get(key)
    if q is None:
        q = deque()
        _circuit_failures[key] = q
    cutoff = time.monotonic() - _CIRCUIT_WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    return q


def _circuit_breaker_response(tool_name: str, recent_failures: int) -> str:
    """Build the short-circuit JSON returned in place of a 6th+ failure."""
    return json.dumps({
        "success": False,
        "error": "circuit_breaker_tripped",
        "tool": tool_name,
        "recent_failures": recent_failures,
        "window_seconds": int(_CIRCUIT_WINDOW_SECONDS),
        "guidance": (
            f"This tool has failed {recent_failures} times in the last "
            f"{int(_CIRCUIT_WINDOW_SECONDS)} seconds for this user. "
            "The pattern suggests the issue is not the SQL/syntax but the "
            "data values being filtered on (e.g. a code, ID, or name that "
            "doesn't exist in Epicor). Stop iterating on the query and "
            "verify the filter values first: run a simple epicor_query "
            "against the lookup table (SalesRep, Customer, Vendor, Part, "
            "etc.) with no value filter to confirm the value you're "
            "searching for actually exists. After one successful call this "
            "breaker resets automatically."
        ),
    }, indent=2)


def install_audit_hook(mcp: Any, audit: AuditLogger) -> None:
    """Wrap ``ToolManager.call_tool`` to log every invocation.

    The low-level MCP server captures a reference to ``FastMCP.call_tool``
    at registration time, so patching the FastMCP instance attribute has no
    effect.  Instead we wrap ``_tool_manager.call_tool`` which is the actual
    execution path for all tool calls.
    """
    tool_manager = mcp._tool_manager
    original_call_tool = tool_manager.call_tool

    def _read_authz_decision() -> tuple[str, str]:
        """Pop the enforcer's per-request decision (source, reason) contextvar."""
        decision = get_authz_decision()
        clear_authz_decision()
        if decision:
            return decision.get("source", ""), decision.get("reason", "")
        return "", ""

    async def audited_call_tool(
        name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> Any:
        session = get_current_session_or_none()
        user_email = session.user_id if session else "unknown"
        department = session.department if session else ""

        # Circuit breaker — short-circuit before invoking the tool when the
        # user is in a confirmed loop. Audits the trip as a failure so the
        # log keeps an accurate count.
        failures = _circuit_state(user_email, name)
        if len(failures) >= _CIRCUIT_THRESHOLD:
            payload_json = _circuit_breaker_response(name, len(failures))
            audit.log(
                user_email=user_email,
                department=department,
                tool_name=name,
                arguments=arguments,
                status="error",
                duration_ms=0.0,
                error="circuit_breaker_tripped",
            )
            return _build_breaker_response(
                payload_json,
                convert_result=bool(kwargs.get("convert_result")),
            )

        start = time.perf_counter()
        try:
            result = await original_call_tool(name, arguments, **kwargs)
            duration_ms = (time.perf_counter() - start) * 1000
            status, error_msg = _classify_response(result)
            authz_source, authz_reason = _read_authz_decision()
            audit.log(
                user_email=user_email,
                department=department,
                tool_name=name,
                arguments=arguments,
                status=status,
                duration_ms=duration_ms,
                error=error_msg,
                authz_source=authz_source,
                authz_reason=authz_reason,
            )
            if status == "error":
                failures.append(time.monotonic())
            else:
                failures.clear()
            return result
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            authz_source, authz_reason = _read_authz_decision()
            audit.log(
                user_email=user_email,
                department=department,
                tool_name=name,
                arguments=arguments,
                status="error",
                duration_ms=duration_ms,
                error=str(exc),
                authz_source=authz_source,
                authz_reason=authz_reason,
            )
            failures.append(time.monotonic())
            raise

    tool_manager.call_tool = audited_call_tool
    logger.info("Audit hook installed on ToolManager.call_tool")

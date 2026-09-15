"""Request-scoped context for the Epicor MCP Server.

Provides a ``contextvars``-based mechanism for passing the current user
session to tool handlers without threading it through the MCP SDK's
internal call chain.

Usage:

    # In middleware / auth layer (once per request):
    from epicor_mcp.context import set_current_session, clear_current_session
    set_current_session(session)
    ...
    clear_current_session()

    # In tool handlers:
    from epicor_mcp.context import get_current_session
    session = get_current_session()  # raises RuntimeError if not set
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from epicor_mcp.auth.session import MCPSession


_current_session: contextvars.ContextVar["MCPSession | None"] = contextvars.ContextVar(
    "_current_session", default=None
)

# Request-scoped authorization-decision reason, set by ``RBACEnforcer`` on every
# service check and read+cleared by the audit hook into the audit_log's
# ``authz_source`` / ``authz_reason`` columns. Shape:
# ``{"source": "department" | "menu" | "unregistered", "reason": <str>}``.
_authz_decision: contextvars.ContextVar["dict[str, str] | None"] = contextvars.ContextVar(
    "_authz_decision", default=None
)

# Request-scoped record of what ``_argguard`` did to the raw arguments (aliased
# / ignored), read by the tool bodies into ``resolved.assumptions``. See
# ``set_arg_notes``.
_arg_notes: contextvars.ContextVar["dict | None"] = contextvars.ContextVar(
    "_arg_notes", default=None
)


def set_current_session(session: "MCPSession") -> contextvars.Token:
    """Store the current user session in the context variable.

    Returns a token that can be used with ``clear_current_session`` to
    restore the previous value.
    """
    return _current_session.set(session)


def get_current_session() -> "MCPSession":
    """Retrieve the current user session from the context variable.

    Raises
    ------
    RuntimeError
        If no session has been set for the current context (i.e., the
        request was not authenticated or the middleware did not run).
    """
    session = _current_session.get()
    if session is None:
        raise RuntimeError(
            "No authenticated session in the current request context. "
            "Ensure the authentication middleware is active."
        )
    return session


def get_current_session_or_none() -> "MCPSession | None":
    """Retrieve the current user session, or ``None`` if not set."""
    return _current_session.get()


def clear_current_session(token: contextvars.Token | None = None) -> None:
    """Clear the current session from the context variable.

    If *token* is provided (from a prior ``set_current_session`` call),
    the context variable is reset to its previous value.  Otherwise it
    is set to ``None``.
    """
    if token is not None:
        _current_session.reset(token)
    else:
        _current_session.set(None)


# ---------------------------------------------------------------------------
# Authorization decision (feeds audit authz_source / authz_reason)
# ---------------------------------------------------------------------------

def set_arg_notes(notes: "dict") -> contextvars.Token:
    """Record what the argument guard did to this call's arguments.

    The guard rewrites/ignores arguments BEFORE FastMCP's arg model discards
    them; the tool bodies then fold these notes into ``resolved.assumptions``
    so an alias is never invisible. Returns a token — ALWAYS reset it in a
    ``finally``, or a leaked note attaches a fabricated assumption to an
    unrelated later call.
    """
    return _arg_notes.set(notes)


def get_arg_notes() -> "dict":
    """Return this call's argument-guard notes (``{}`` when the guard didn't run)."""
    return _arg_notes.get() or {}


def clear_arg_notes(token: contextvars.Token | None = None) -> None:
    """Reset the argument-guard notes (pass the token from ``set_arg_notes``)."""
    if token is not None:
        _arg_notes.reset(token)
    else:
        _arg_notes.set(None)


def set_authz_decision(source: str, reason: str) -> None:
    """Record the authorization source + reason for the current request.

    Called by ``RBACEnforcer.check_service_access`` on every code path so the
    audit hook can attribute each tool call to the decision that authorized it.
    """
    _authz_decision.set({"source": source, "reason": reason})


def get_authz_decision() -> "dict[str, str] | None":
    """Return the current request's authorization decision, or ``None``."""
    return _authz_decision.get()


def clear_authz_decision() -> None:
    """Clear the authorization decision for the current context."""
    _authz_decision.set(None)

"""MCP session management for authenticated Epicor users.

Each MCP connection gets a session that holds the user's identity, department,
access level, and environment. Sessions are stored in memory with expiry-based
cleanup.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from epicor_mcp.auth.oauth import ValidatedToken

logger = logging.getLogger(__name__)

# Sessions expire after 8 hours of inactivity
_SESSION_TTL_SECONDS = 8 * 60 * 60

# Run cleanup at most every 5 minutes
_CLEANUP_INTERVAL_SECONDS = 300


@dataclass
class MCPSession:
    """Holds authenticated context for a single MCP connection.

    Attributes:
        session_id: Unique session identifier (typically the MCP session ID).
        user_id: User's email / UPN from Azure AD.
        department: The Epicor department the user belongs to.
        access_level: Whether the user can write or is read-only.
        environment: Which Epicor environment (pilot or live) to target.
        created_at: Unix timestamp of session creation.
        last_active: Unix timestamp of last activity.
        claims: Full JWT claims from the OAuth token.
    """

    session_id: str
    user_id: str
    department: str
    access_level: Literal["read_only", "read_write"] = "read_only"
    environment: Literal["pilot", "live"] = "pilot"
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Check if the session has expired due to inactivity."""
        return (time.time() - self.last_active) > _SESSION_TTL_SECONDS

    def touch(self) -> None:
        """Update the last-active timestamp (call on each request)."""
        self.last_active = time.time()


class SessionStore:
    """In-memory store for active MCP sessions with automatic cleanup.

    Thread-safe for single-threaded async usage (which is the standard
    asyncio model). For multi-worker deployments, replace with Redis
    or a shared store.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, MCPSession] = {}
        self._last_cleanup: float = 0.0

    def _maybe_cleanup(self) -> None:
        """Remove expired sessions if enough time has passed since last cleanup."""
        now = time.time()
        if (now - self._last_cleanup) < _CLEANUP_INTERVAL_SECONDS:
            return

        expired_ids = [
            sid for sid, session in self._sessions.items() if session.is_expired
        ]
        for sid in expired_ids:
            logger.info(
                "Cleaning up expired session %s (user=%s)",
                sid,
                self._sessions[sid].user_id,
            )
            del self._sessions[sid]

        if expired_ids:
            logger.info("Cleaned up %d expired sessions", len(expired_ids))
        self._last_cleanup = now

    def create_session(
        self,
        session_id: str,
        validated_token: ValidatedToken,
        user_config: dict[str, Any],
        environment: Literal["pilot", "live"] = "pilot",
    ) -> MCPSession:
        """Create a new session from a validated OAuth token and user config.

        Args:
            session_id: Unique identifier for this session.
            validated_token: The validated Azure AD token.
            user_config: User configuration dict containing at minimum
                         'department' and optionally 'access_level'.
            environment: Which Epicor environment to use.

        Returns:
            The newly created MCPSession.
        """
        self._maybe_cleanup()

        department = user_config.get("department", "")
        access_level = user_config.get("access_level", "read_only")
        if access_level not in ("read_only", "read_write"):
            access_level = "read_only"

        session = MCPSession(
            session_id=session_id,
            user_id=validated_token.user_id,
            department=department,
            access_level=access_level,
            environment=environment,
            claims=validated_token.claims,
        )
        self._sessions[session_id] = session
        logger.info(
            "Created session %s for user=%s dept=%s access=%s env=%s",
            session_id,
            session.user_id,
            session.department,
            session.access_level,
            session.environment,
        )
        return session

    def get_session(self, session_id: str) -> MCPSession | None:
        """Retrieve a session by ID, returning None if not found or expired.

        If the session exists and is not expired, its last-active timestamp
        is updated.
        """
        self._maybe_cleanup()
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.is_expired:
            logger.info("Session %s expired, removing", session_id)
            del self._sessions[session_id]
            return None
        session.touch()
        return session

    def remove_session(self, session_id: str) -> None:
        """Remove a session by ID (e.g., on explicit logout)."""
        if session_id in self._sessions:
            logger.info("Removing session %s", session_id)
            del self._sessions[session_id]

    @property
    def active_count(self) -> int:
        """Return the number of currently active (non-expired) sessions."""
        self._maybe_cleanup()
        return len(self._sessions)

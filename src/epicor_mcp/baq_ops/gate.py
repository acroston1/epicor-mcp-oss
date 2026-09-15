"""Per-request authorization for optional BAQ saving.

``make_can_save`` returns a closure that resolves the current session and its
user profile on every call. Registration must not call it: session identity is
request-scoped, so a startup-time decision would be stale or unauthenticated.

The right is ``access_level == "read_write" OR UserProfile.can_write_baqs``.
Both limbs matter: a read-only profile can carry an explicit BAQ-save grant.
Unknown sessions fail closed. The profile flag can come from operator user
configuration or the established BAQ security-group claims; department mapping
alone does not grant it.

The callback uses the enforcer's user-map interface without importing its full
stack, avoiding a cycle with server registration. The SQL runtime evaluates the
right before executing a request that asks to run and save. A denial supplies a
rows-only retry; a missing callback describes unavailable server capability.
See ``tests/test_baq_save.py`` for the deterministic permission-path coverage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from epicor_mcp.context import get_current_session_or_none
from epicor_mcp.sql.envelope import error_envelope

logger = logging.getLogger(__name__)

__all__ = [
    "SaveRight",
    "make_can_save",
    "save_gate_envelope",
    "save_unavailable_envelope",
    "EPICOR_BAQ_GROUPS",
]

#: The Epicor security groups ``server.py`` turns into ``can_write_baqs`` for an
#: auto-provisioned user. Named in the denial so the caller knows what to ask an
#: administrator for, rather than being told only that they may not.
EPICOR_BAQ_GROUPS = ["ExtBAQDesigner", "BAQ", "BAMP", "BAMS"]


@dataclass(frozen=True)
class SaveRight:
    """One resolved answer to *"may this caller write a BAQ?"*, with its reasons.

    ``right_source`` records WHERE ``can_write_baqs`` came from — ``users.json``
    for a configured user, the Epicor group claims for an auto-provisioned one.
    That distinction is what stops the denial message from asserting Epicor group
    membership as fact: for a configured user the flag can be false while the
    groups are fine, so a message that blames the groups either way
    overreaches.
    """

    allowed: bool
    reason: str = ""
    user_id: str = ""
    access_level: str = ""
    can_write_baqs: bool = False
    epicor_username: str = ""
    right_source: str = "unknown"  # 'users_json' | 'epicor_groups' | 'unknown'

    def __bool__(self) -> bool:
        return self.allowed


def make_can_save(rbac: Any) -> Callable[[], SaveRight]:
    """Return a zero-argument closure resolving the caller's BAQ-write right.

    *rbac* is duck-typed: the only thing read is ``rbac._user_map.get_user``.
    """

    def _right() -> SaveRight:
        session = get_current_session_or_none()
        if session is None:
            # No session means no identity, and an unidentified caller cannot be
            # proven to hold a right. Fail closed, and say which of the two
            # things is missing so the operator is not left guessing.
            return SaveRight(False, reason="no_session")
        profile = None
        try:
            profile = rbac._user_map.get_user(session.user_id)
        except Exception:  # noqa: BLE001 - a lookup fault must not fail OPEN
            logger.warning(
                "BAQ-save right lookup failed for %s; failing closed",
                getattr(session, "user_id", "?"),
                exc_info=True,
            )
        flag = bool(profile and getattr(profile, "can_write_baqs", False))
        access_level = str(getattr(session, "access_level", "") or "")
        allowed = access_level == "read_write" or flag
        return SaveRight(
            allowed=allowed,
            reason="ok" if allowed else "no_baq_right",
            user_id=str(getattr(session, "user_id", "") or ""),
            access_level=access_level,
            can_write_baqs=flag,
            # The legacy BAQ workflow's author rule, verbatim.
            epicor_username=(
                str(getattr(profile, "epicor_username", "") or "")
                if profile
                else str(getattr(session, "user_id", "") or "").split("@")[0]
            ),
            right_source="users_json" if profile else "unknown",
        )

    return _right


def save_gate_envelope(right: SaveRight, sql: str, page_size: int = 200) -> dict[str, Any]:
    """The terminal INV-1 refusal a caller without the right gets.

    ``terminal`` because retrying cannot grant a right, and ``retry_with`` is the
    RUNNABLE rows-only call rather than a template — the caller asked a data
    question and a save; the data half is still available in one clean hop.

    A bare ``{"error": <prose>}`` is not enough here; the envelope shape means
    the caller does not have to parse English to find the recovery.
    """
    if right.reason == "no_session":
        why = (
            "this request carries no authenticated session, so the server cannot "
            "establish who you are, let alone what you may write"
        )
    else:
        why = (
            f"your session is {right.access_level or 'read-only'} and the server's user "
            "record does not carry the can_write_baqs right"
        )
    return error_envelope(
        "baq_save_not_authorized",
        (
            f"Saving a query as a BAQ needs BAQ write access, and this account does not "
            f"have it: {why}. The BAQ was not created — and the statement was not run, "
            "because you asked for both. That right comes from membership of one of the "
            f"Epicor security groups {', '.join(EPICOR_BAQ_GROUPS)}, or from an explicit "
            "entry in this server's user record; ask your Epicor administrator which "
            "applies to you. To just get the rows, re-send the same SQL without save_as."
        ),
        evidence="The save gate requires a session and the two-limb check "
        "(access_level == 'read_write' OR UserProfile.can_write_baqs)",
        valid={"right": "can_write_baqs", "epicor_groups": list(EPICOR_BAQ_GROUPS)},
        retry_with={"sql": sql, "page_size": page_size},
        detail={
            "stage": "baq_save_gate",
            "user": right.user_id,
            "access_level": right.access_level,
            "can_write_baqs": right.can_write_baqs,
            "right_source": right.right_source,
        },
        terminal=True,
    )


def save_unavailable_envelope(sql: str, page_size: int = 200) -> dict[str, Any]:
    """No ``can_save`` was injected at all — the bare wedge entry point.

    ``wedge_server.create_mcp_server`` builds a runtime with no user map, so
    there is nothing that could prove a right. That is a property of THIS
    deployment, not a fact about the caller, and the message says so: blaming
    the caller for the server's own missing wiring is the ``_declared_types``
    dead end the legacy tools document.
    """
    return error_envelope(
        "baq_save_unavailable",
        (
            "This server instance cannot save a BAQ: it was started without a user map, "
            "so there is nothing here that can establish BAQ write access. This is a "
            "property of the deployment, not of your account or your SQL. Re-send the "
            "same SQL without save_as to get the rows, and save from the main server "
            "(the one with OAuth and users.json) if you need the BAQ."
        ),
        valid={"right": "can_write_baqs"},
        retry_with={"sql": sql, "page_size": page_size},
        detail={"stage": "baq_save_gate", "reason": "no_user_map"},
        terminal=True,
    )

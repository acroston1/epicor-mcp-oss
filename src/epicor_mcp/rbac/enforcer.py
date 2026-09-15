"""RBAC enforcement logic for the Epicor MCP Server.

Performs three categories of access checks -- all against local, in-memory
data (no network calls).  This keeps latency negligible for the hot path.

1. **Service access** -- can the user's department reach a given service?
2. **Method access** -- does the user's access level (read_only / read_write)
   permit the requested method on that service?
3. **BAQ access** -- can the user execute BAQ queries?  (All authenticated
   users can; the department BAQ key is selected automatically.)

The enforcer also exposes convenience helpers consumed by tool modules:

- ``check_access``  -- combined service check used by most tools.
- ``check_write_access`` -- write-method guard used by ``run_method``.
- ``get_available_tools`` -- returns the tool names available to a user.
- ``is_read_method``  -- static classifier for Epicor method names.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from epicor_mcp.context import set_authz_decision

if TYPE_CHECKING:
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.user_map import UserMap, UserProfile

logger = logging.getLogger(__name__)


# ======================================================================
# Enums & data classes
# ======================================================================

class AccessLevel(str, Enum):
    """Permission tier assigned to each user."""

    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


@dataclass
class AccessCheckResult:
    """Outcome of a single RBAC check.

    Attributes:
        allowed:  ``True`` if the action is permitted.
        message:  Human-readable explanation (useful for both allow and deny).
        api_key:  The department API key to use when *allowed* is ``True``.
    """

    allowed: bool
    message: str
    api_key: str | None = None


# ======================================================================
# Tools available per access level
# ======================================================================

_READ_ONLY_TOOLS: list[str] = [
    "discover",
    "describe",
    "query",
    "get_record",
    "run_method",
    "run_baq",
]

_READ_WRITE_TOOLS: list[str] = _READ_ONLY_TOOLS + [
    "workflow",
]

# Method prefixes that look like reads but actually mutate state.
_WRITE_LIKE_PREFIXES: tuple[str, ...] = (
    "GetNew",
    "Change",
    "Update",
    "Delete",
)


# ======================================================================
# Enforcer
# ======================================================================

class RBACEnforcer:
    """RBAC enforcer with a menu-derived decision source.

    All checks run purely against in-memory data -- no network I/O is
    performed on the hot path.  The Epicor fetching that populates the
    per-user ``UserAuthzSnapshot`` happens in the auth middleware
    (``await authorizer.ensure_snapshot(email)``); ``check_service_access``
    reads the cached snapshot synchronously via ``authorizer.get_snapshot``.

    Three modes select the *service* decision source:

    - ``"off"``     -- legacy department behavior; the authorizer is ignored.
    - ``"shadow"``  -- the legacy department decision ships, but the menu
      decision is also computed and every disagreement (including hits on the
      empty-department allow-all fallback) is recorded via *record_divergence*.
    - ``"enforce"`` -- the menu decision is authoritative; the empty-department
      allow-all fallback is dead.

    The method / write / BAQ tiers are orthogonal axes and behave identically
    across modes.

    Parameters
    ----------
    index:
        ``ServiceIndex`` instance for the legacy department checks and the
        service-name existence guidance.
    user_map:
        ``UserMap`` for resolving user -> department -> API key.
    authorizer:
        ``MenuAuthorizer`` (or a duck-typed stand-in) exposing the sync
        ``get_snapshot(email)`` hot path. Required for shadow/enforce.
    mode:
        One of ``"off"`` / ``"shadow"`` / ``"enforce"``.
    record_divergence:
        Callable invoked in shadow mode with a divergence dict whenever the
        legacy and menu decisions disagree.
    """

    def __init__(
        self,
        index: "ServiceIndex",
        user_map: "UserMap",
        *,
        authorizer: Any | None = None,
        mode: str = "off",
        record_divergence: Callable[[dict], None] | None = None,
    ) -> None:
        self._index = index
        self._user_map = user_map
        self._authorizer = authorizer
        self._mode = mode
        self._record_divergence = record_divergence

    # ------------------------------------------------------------------
    # Core access checks
    # ------------------------------------------------------------------

    def check_service_access(self, user_id: str, service_id: str) -> AccessCheckResult:
        """Check whether *user_id* can access *service_id*.

        The decision source depends on the configured mode (off / shadow /
        enforce). On every path a decision-reason contextvar is set so the
        audit hook can attribute the call. Service-name normalization and the
        wrong-service-name guidance are preserved across all modes.

        Returns
        -------
        AccessCheckResult
        """
        # Normalize: the index stores "Erp.BO.LaborSvc" but callers may
        # pass "Erp.BO.Labor" — append "Svc" when missing.
        if (service_id.startswith(("Erp.BO.", "Ice.BO."))
                and not service_id.endswith("Svc")):
            service_id = service_id + "Svc"

        user = self._user_map.get_user(user_id)
        if user is None:
            set_authz_decision("unregistered", f"user '{user_id}' not registered")
            return AccessCheckResult(
                allowed=False,
                message=(
                    f"User '{user_id}' is not registered in the MCP server. "
                    "Contact your administrator to request access."
                ),
            )

        if self._mode == "enforce":
            return self._menu_decision(user, service_id)

        if self._mode == "shadow":
            dept_result = self._department_decision(user, service_id)
            menu_allowed = self._menu_allows(user, service_id)
            if dept_result.allowed != menu_allowed:
                self._emit_divergence(user, service_id, dept_result.allowed, menu_allowed)
            return dept_result

        # mode == "off" (and any unrecognized value): legacy department path.
        return self._department_decision(user, service_id)

    # ------------------------------------------------------------------
    # Decision sources
    # ------------------------------------------------------------------

    def _department_decision(
        self, user: "UserProfile", service_id: str, *, set_ctx: bool = True
    ) -> AccessCheckResult:
        """Legacy department-based service check (byte-compatible with the original check).

        Supports multi-department users: iterates over all of the user's
        departments to find one that owns the requested service (or whose list
        is empty — the allow-all fallback), then returns that department's
        API key.
        """
        # Check if the service exists in the index at all.
        # This distinguishes "wrong service name" from "no department access".
        service_exists = self._index.service_exists(service_id)

        # Get all departments this user belongs to.
        # UserProfile.department is the primary; extra_departments holds others.
        departments = [user.department]
        if hasattr(user, "extra_departments"):
            departments.extend(user.extra_departments)

        # Try each department to find one that owns this service.
        for dept in departments:
            dept_services = self._index.get_department_services(dept)
            if not dept_services or service_id in dept_services:
                api_key = self._user_map.get_department_key(dept)
                if api_key is not None:
                    if set_ctx:
                        set_authz_decision("department", f"granted via {dept} department")
                    return AccessCheckResult(
                        allowed=True,
                        message=f"Access granted via {dept} department.",
                        api_key=api_key,
                    )

        if not service_exists:
            if set_ctx:
                set_authz_decision("department", f"service '{service_id}' does not exist")
            return AccessCheckResult(
                allowed=False,
                message=(
                    f"Service '{service_id}' does not exist in Epicor. "
                    "You may have the wrong service name. Use "
                    "epicor_discover_services to search for the correct "
                    "service. For example, RMA services are "
                    "'Erp.BO.RMAProcSvc' and 'Erp.BO.RMADispSvc', not "
                    "'Erp.BO.RMASvc'."
                ),
            )

        dept_names = ", ".join(departments)
        if set_ctx:
            set_authz_decision(
                "department", f"no department ({dept_names}) grants '{service_id}'"
            )
        return AccessCheckResult(
            allowed=False,
            message=(
                f"None of your departments ({dept_names}) have access to "
                f"service '{service_id}'. Use epicor_discover_services "
                "to find services available to you, or contact your "
                "administrator if you believe this is an error."
            ),
        )

    def _menu_decision(self, user: "UserProfile", service_id: str) -> AccessCheckResult:
        """Menu-security service check (authoritative in enforce mode).

        Fails closed when the snapshot is missing or an error snapshot.
        SecurityMgr bypasses everything. Preserves the wrong-service-name
        guidance for services no menu grants that also do not exist.
        """
        snap = self._get_snapshot(user)

        if snap is None or getattr(snap, "is_error", False):
            reason = (
                "no authorization snapshot (fail closed)"
                if snap is None
                else "error snapshot (fail closed)"
            )
            set_authz_decision("menu", reason)
            return AccessCheckResult(
                allowed=False,
                message=(
                    f"Access denied by menu security: {reason}. Your Epicor "
                    "authorization could not be established. Contact your "
                    "administrator if you believe this is an error."
                ),
            )

        # SecurityMgr / allow-all sentinel bypasses menu evaluation entirely.
        if getattr(snap, "security_mgr", False) or getattr(snap, "allow_all", False):
            set_authz_decision("menu", "Epicor security manager — allow all")
            return AccessCheckResult(
                allowed=True,
                message="Access granted: you are an Epicor security manager (menu security bypassed).",
                api_key=self._user_map.get_read_key(),
            )

        if snap.allows(service_id):
            reason = self._grant_reason(snap, service_id)
            set_authz_decision("menu", reason)
            return AccessCheckResult(
                allowed=True,
                message=f"Access granted via menu security ({reason}).",
                api_key=self._user_map.get_read_key(),
            )

        # Not granted by any menu. Distinguish a wrong service name from a
        # real lack of access, mirroring the legacy guidance.
        if not self._index.service_exists(service_id):
            set_authz_decision("menu", f"service '{service_id}' does not exist")
            return AccessCheckResult(
                allowed=False,
                message=(
                    f"Service '{service_id}' does not exist in Epicor. "
                    "You may have the wrong service name. Use "
                    "epicor_discover_services to search for the correct "
                    "service. For example, RMA services are "
                    "'Erp.BO.RMAProcSvc' and 'Erp.BO.RMADispSvc', not "
                    "'Erp.BO.RMASvc'."
                ),
            )

        set_authz_decision("menu", f"no launchable menu grants '{service_id}'")
        return AccessCheckResult(
            allowed=False,
            message=(
                f"Access denied by menu security: no Epicor menu you can launch "
                f"uses service '{service_id}'. Your access derives from the "
                "Kinetic applications behind your menu items. Contact your "
                "administrator if you believe this is an error."
            ),
        )

    # ------------------------------------------------------------------
    # Menu-decision helpers
    # ------------------------------------------------------------------

    def _get_snapshot(self, user: "UserProfile") -> Any | None:
        """Fetch the cached snapshot for *user* (keyed by user_id == email)."""
        if self._authorizer is None:
            return None
        return self._authorizer.get_snapshot(user.user_id)

    def _menu_allows(self, user: "UserProfile", service_id: str) -> bool:
        """Boolean menu decision used by shadow mode (no message, no contextvar)."""
        snap = self._get_snapshot(user)
        if snap is None or getattr(snap, "is_error", False):
            return False
        if getattr(snap, "security_mgr", False) or getattr(snap, "allow_all", False):
            return True
        return bool(snap.allows(service_id))

    @staticmethod
    def _grant_reason(snap: Any, service_id: str) -> str:
        """Build a short human reason naming the granting menu + SecCode.

        Reads the snapshot's ``grants`` chain when present (real
        ``UserAuthzSnapshot``); falls back to a generic reason for duck-typed
        snapshots that expose only ``allows()``.
        """
        grants = getattr(snap, "grants", None)
        grant = grants.get(service_id) if isinstance(grants, dict) else None
        menus = getattr(grant, "menus", None) if grant is not None else None
        if menus:
            parts = []
            for m in menus:
                menu_id = getattr(m, "menu_id", "")
                sec_code = getattr(m, "sec_code", "")
                parts.append(f"menu {menu_id} (SecCode {sec_code})" if sec_code else f"menu {menu_id}")
            return "granted by " + ", ".join(parts)
        return f"granted by an allowed menu using {service_id}"

    def _emit_divergence(
        self, user: "UserProfile", service_id: str, dept_allowed: bool, menu_allowed: bool
    ) -> None:
        """Record a shadow-mode divergence between legacy and menu decisions."""
        if self._record_divergence is None:
            return
        reason = (
            "legacy allowed but menu denied"
            if dept_allowed and not menu_allowed
            else "legacy denied but menu allowed"
        )
        try:
            self._record_divergence(
                {
                    "user_id": user.user_id,
                    "service_id": service_id,
                    "dept_allowed": dept_allowed,
                    "menu_allowed": menu_allowed,
                    "reason": reason,
                }
            )
        except Exception:  # pragma: no cover — a broken hook must never break auth
            logger.exception("record_divergence hook raised")

    def check_method_access(
        self, user_id: str, service_id: str, method_name: str
    ) -> AccessCheckResult:
        """Check whether *user_id* can invoke *method_name* on *service_id*.

        After verifying service-level access, this applies method-level
        rules based on the user's ``AccessLevel``:

        - **read_only**: only ``Get*`` methods are allowed, **excluding**
          ``GetNew*`` (which mutates the dataset by adding a blank row).
        - **read_write**: all methods are allowed.

        Returns
        -------
        AccessCheckResult
        """
        # Step 1: service-level check.
        svc_result = self.check_service_access(user_id, service_id)
        if not svc_result.allowed:
            return svc_result

        # Step 2: method-level check.
        user = self._user_map.get_user(user_id)
        # user cannot be None here because check_service_access passed.
        assert user is not None

        if user.access_level == AccessLevel.READ_ONLY:
            if not self.is_read_method(method_name):
                return AccessCheckResult(
                    allowed=False,
                    message=(
                        f"Method '{method_name}' is a write/mutation method. "
                        f"Your access level ({user.access_level.value}) only "
                        "permits read methods (Get*, excluding GetNew*)."
                    ),
                )

        # For write methods by read_write users, use the admin write key
        if not self.is_read_method(method_name):
            write_key = self._user_map.get_write_key()
            if write_key:
                return AccessCheckResult(
                    allowed=True,
                    message=f"Write access granted: {method_name} on {service_id}.",
                    api_key=write_key,
                )

        return AccessCheckResult(
            allowed=True,
            message=f"Method access granted: {method_name} on {service_id}.",
            api_key=svc_result.api_key,
        )

    def check_baq_access(self, user_id: str) -> AccessCheckResult:
        """Check whether *user_id* can execute BAQ queries.

        All authenticated users may run BAQs. Executing BAQs requires
        Ice.BO.DynamicQuery access, so we always prefer the BAQ key
        or write key over the read key.

        Returns
        -------
        AccessCheckResult
        """
        # BAQ execution needs DynamicQuery — use BAQ key first, then write key.
        # The read key does NOT have DynamicQuery access.
        baq_key = self._user_map.get_baq_key()
        if baq_key:
            return AccessCheckResult(
                allowed=True,
                message="BAQ access granted.",
                api_key=baq_key,
            )

        write_key = self._user_map.get_write_key()
        if write_key:
            return AccessCheckResult(
                allowed=True,
                message="BAQ access granted.",
                api_key=write_key,
            )

        return AccessCheckResult(
            allowed=False,
            message="No BAQ-capable API key configured. Contact your administrator.",
        )

    # ------------------------------------------------------------------
    # Convenience wrappers used by tool modules
    # ------------------------------------------------------------------

    def check_access(self, user_id: str, service_id: str) -> tuple[bool, str]:
        """Simplified service access check returning ``(allowed, message)``.

        This is the signature consumed by the existing tool modules
        (``discover``, ``describe``, ``query``, ``get_record``).
        """
        result = self.check_service_access(user_id, service_id)
        return result.allowed, result.message

    def check_write_access(self, user_id: str, service_id: str) -> tuple[bool, str]:
        """Simplified write-access check returning ``(allowed, message)``.

        Used by ``run_method`` to gate mutation methods.  This checks that
        the user has ``READ_WRITE`` access level on the given service.
        """
        # Service-level access first.
        svc_result = self.check_service_access(user_id, service_id)
        if not svc_result.allowed:
            return False, svc_result.message

        user = self._user_map.get_user(user_id)
        assert user is not None

        if user.access_level != AccessLevel.READ_WRITE:
            return False, (
                f"Write access denied. Your access level "
                f"({user.access_level.value}) does not permit write "
                f"operations on '{service_id}'."
            )

        return True, "Write access granted."

    # ------------------------------------------------------------------
    # Tool enumeration
    # ------------------------------------------------------------------

    def get_available_tools(self, user_id: str) -> list[str]:
        """Return the list of MCP tool names available to *user_id*.

        - ``read_only`` users get discovery, describe, query, get_record,
          run_method (read methods only), and run_baq.
        - ``read_write`` users additionally get the ``workflow`` tool.
        - Unregistered users get an empty list.
        """
        user = self._user_map.get_user(user_id)
        if user is None:
            return []

        if user.access_level == AccessLevel.READ_WRITE:
            return list(_READ_WRITE_TOOLS)
        return list(_READ_ONLY_TOOLS)

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_read_method(method_name: str) -> bool:
        """Determine whether *method_name* is a read-only accessor.

        Rules:
        - Starts with ``Get`` **and** does not start with ``GetNew`` -> read.
        - Everything else (``Update``, ``Delete``, ``Change*``, ``GetNew*``,
          custom methods) -> write / mutation.
        """
        return method_name.startswith("Get") and not method_name.startswith("GetNew")

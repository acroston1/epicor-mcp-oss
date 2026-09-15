"""RBAC enforcement for department-level access control.

Maps authenticated users to departments, resolves department API keys,
and checks service/method access before any Epicor API call is dispatched.
"""

from epicor_mcp.rbac.enforcer import AccessCheckResult, AccessLevel, RBACEnforcer
from epicor_mcp.rbac.epicor_authz import (
    EpicorAuthzClient,
    EpicorUserIdentity,
    MenuRow,
    SecurityRow,
)
from epicor_mcp.rbac.menu_authz import (
    GrantMenu,
    MenuAuthorizer,
    ServiceGrant,
    UserAuthzSnapshot,
)
from epicor_mcp.rbac.menu_map_store import MenuMapStore
from epicor_mcp.rbac.user_map import UserMap, UserProfile

__all__ = [
    "AccessCheckResult",
    "AccessLevel",
    "RBACEnforcer",
    "UserMap",
    "UserProfile",
    "EpicorAuthzClient",
    "EpicorUserIdentity",
    "MenuRow",
    "SecurityRow",
    "MenuAuthorizer",
    "UserAuthzSnapshot",
    "ServiceGrant",
    "GrantMenu",
    "MenuMapStore",
]

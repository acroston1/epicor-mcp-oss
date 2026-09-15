"""Resolve user departments from Epicor security groups.

Queries the Epicor UserFile to find a user by email, then maps their
Epicor security groups to operator-configured MCP departments. Users can belong
to multiple departments. These labels do not replace menu-derived table
authorization or the independent BAQ-save permission check.

The mapping from Epicor security groups to MCP departments is configured
in ``data/users.json`` under ``epicor_group_to_department``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Compatibility name only: installations must supply their own mapping.
DEFAULT_GROUP_DEPARTMENT_MAP: dict[str, list[str]] = {}


def validate_group_map(value: Any) -> dict[str, list[str]]:
    """Validate and copy the operator's exact Epicor group-to-department map.

    Keys and department names are non-empty strings without surrounding
    whitespace. Values are lists; an empty list grants no department labels.
    Group matching is case-sensitive, matching Epicor's returned group codes.
    """
    setting = "epicor_group_to_department"
    if not isinstance(value, dict):
        raise ValueError(f"{setting} must be an object mapping group codes to lists of department names")
    result: dict[str, list[str]] = {}
    for group, departments in value.items():
        if not isinstance(group, str) or not group.strip() or group != group.strip():
            raise ValueError(f"{setting} group codes must be non-empty strings without surrounding whitespace")
        if not isinstance(departments, list):
            raise ValueError(f"{setting}[{group!r}] must be a list of department names")
        if any(not isinstance(name, str) or not name.strip() or name != name.strip()
               for name in departments):
            raise ValueError(f"{setting}[{group!r}] department names must be non-empty strings without surrounding whitespace")
        result[group] = list(departments)
    return result


class EpicorUserResolver:
    """Resolve a user's MCP departments by querying Epicor's UserFile.

    Uses the service_account service account to look up any user by email address,
    then maps their Epicor security groups to MCP departments.

    Parameters
    ----------
    base_url:
        Epicor OData base URL (e.g., ``https://.../api/v2/odata/YOUR_COMPANY/``).
    username:
        Service account username (e.g., ``service_account``).
    password:
        Service account password.
    api_key:
        Admin-level API key that can read UserFile.
    company_id:
        Epicor company ID supplied by the operator.
    group_map:
        Custom mapping of Epicor group codes to department lists.
        If omitted or empty, no department is inferred. The mapping is copied.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        api_key: str,
        company_id: str = "",
        group_map: dict[str, list[str]] | None = None,
    ) -> None:
        import base64
        self._base_url = base_url.rstrip("/") + "/"
        self._auth_token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._api_key = api_key
        self._company_id = company_id
        self._group_map = validate_group_map({} if group_map is None else group_map)
        self._client = httpx.AsyncClient(timeout=30)

        # Cache: email -> {user_id, name, departments, groups}
        self._cache: dict[str, dict[str, Any]] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Basic {self._auth_token}",
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
            "Company": self._company_id,
        }

    async def resolve_user(self, email: str) -> dict[str, Any] | None:
        """Look up a user by email and return their departments.

        Returns a dict with:
            - user_id: Epicor UserID
            - name: Display name
            - email: Email address
            - departments: set of MCP department names
            - epicor_groups: list of raw Epicor security group codes

        Returns None if the email is not found in Epicor.
        Results are cached in memory.
        """
        email_lower = email.lower()
        if email_lower in self._cache:
            return self._cache[email_lower]

        try:
            r = await self._client.get(
                f"{self._base_url}Ice.BO.UserFileSvc/UserFiles",
                headers=self._headers(),
                params={
                    "$filter": f"tolower(EMailAddress) eq '{email_lower}'",
                    "$select": "UserID,Name,EMailAddress,GroupList",
                    "$top": 5,
                },
            )
            r.raise_for_status()
        except Exception as exc:
            logger.error("Failed to query Epicor UserFile for %s: %s", email, exc)
            return None

        users = r.json().get("value", [])
        if not users:
            logger.warning("No Epicor user found with email %s", email)
            return None

        # If multiple UserIDs share the same email, pick the one with
        # the most security groups (likely the "real" user, not a dashboard).
        best = max(users, key=lambda u: len(u.get("GroupList", "")))

        group_list_str = best.get("GroupList", "")
        epicor_groups = [g.strip() for g in group_list_str.split("~") if g.strip()]

        # Map Epicor groups to MCP departments
        departments: set[str] = set()
        for group_code in epicor_groups:
            mapped_depts = self._group_map.get(group_code, [])
            departments.update(mapped_depts)

        # Check if user has BAQ design permissions
        baq_groups = {"ExtBAQDesigner", "BAQ", "BAMP", "BAMS"}
        can_write_baqs = bool(baq_groups & set(epicor_groups))

        result = {
            "user_id": best["UserID"],
            "name": best.get("Name", best["UserID"]),
            "email": best.get("EMailAddress", email),
            "departments": departments,
            "epicor_groups": epicor_groups,
            "can_write_baqs": can_write_baqs,
        }

        self._cache[email_lower] = result
        logger.info(
            "Resolved %s -> UserID=%s, departments=%s, baq_write=%s (from %d Epicor groups)",
            email, result["user_id"], sorted(departments), can_write_baqs, len(epicor_groups),
        )
        return result

    def set_group_map(self, group_map: dict[str, list[str]]) -> None:
        """Replace the validated mapping and discard cached department resolutions."""
        self._group_map = validate_group_map(group_map)
        self.clear_cache()

    def clear_cache(self) -> None:
        """Clear the user resolution cache."""
        self._cache.clear()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

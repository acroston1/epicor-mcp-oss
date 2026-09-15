"""User-to-department mapping and department API key resolution.

Loads user profiles and department API keys from JSON configuration files.
Provides case-insensitive user lookup, department key selection (regular
and BAQ keys), and environment URL resolution.

Configuration files
-------------------
- ``data/users.json`` -- user-email-keyed dict with department defaults.
- ``data/department_keys.json`` -- departments, keys, scopes, and environment URLs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from epicor_mcp.rbac.enforcer import AccessLevel
from epicor_mcp.rbac.epicor_user_resolver import validate_group_map

logger = logging.getLogger(__name__)

# Fallback environment URLs used only when department_keys.json is missing
# the ``environments`` section.
_FALLBACK_ENVIRONMENT_URLS: dict[str, str] = {
    "pilot": "",
    "live": "",
}


@dataclass
class UserProfile:
    """Resolved profile for an authenticated MCP user.

    Attributes:
        user_id:          Email / UPN (always stored lowercase).
        epicor_username:  The Epicor login name for this person.
        department:       Department key (e.g. ``"Finance"``, ``"Engineering"``).
        access_level:     ``AccessLevel.READ_ONLY`` or ``AccessLevel.READ_WRITE``.
        environment:      ``"pilot"`` or ``"live"``.
        display_name:     Human-readable name shown in logs and error messages.
    """

    user_id: str
    epicor_username: str
    department: str
    access_level: AccessLevel
    environment: str
    display_name: str
    extra_departments: list[str] = None
    can_write_baqs: bool = False

    def __post_init__(self):
        if self.extra_departments is None:
            self.extra_departments = []


class UserMap:
    """Resolve users to departments and departments to API keys.

    All data is loaded from disk at construction time and held in memory.
    Call :meth:`reload` to pick up changes without restarting the server.

    Parameters
    ----------
    users_config_path:
        Path to the users JSON file (``data/users.json``).
    department_keys_path:
        Path to the department keys JSON file (``data/department_keys.json``).
    """

    def __init__(self, users_config_path: str, department_keys_path: str) -> None:
        self._users_path = Path(users_config_path)
        self._keys_path = Path(department_keys_path)

        # Internal lookup tables -- populated by _load().
        self._users: dict[str, UserProfile] = {}
        self._admin_read_key: str = ""
        self._admin_write_key: str = ""
        self._admin_baq_key: str = ""
        # Legacy per-department keys (kept for backward compat)
        self._department_keys: dict[str, str] = {}
        self._department_baq_keys: dict[str, str] = {}
        self._environment_urls: dict[str, str] = dict(_FALLBACK_ENVIRONMENT_URLS)
        self._department_defaults: dict[str, dict[str, str]] = {}
        self._group_department_map: dict[str, str] = {}
        self._epicor_group_department_map: dict[str, list[str]] = {}

        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def epicor_group_to_department(self) -> dict[str, list[str]]:
        """Return a copy of the configured Epicor group-to-department mapping."""
        return {group: list(departments)
                for group, departments in self._epicor_group_department_map.items()}

    def get_user(self, user_id: str) -> UserProfile | None:
        """Look up a user by email / UPN.  Case-insensitive.

        Returns ``None`` if the user is not found in the configuration.
        """
        return self._users.get(user_id.lower())

    def get_or_create_user_from_claims(self, user_id: str, claims: dict) -> UserProfile | None:
        """Look up a user, or auto-create from Azure AD token claims.

        If the user is not in users.json, attempt to resolve their department
        from Azure AD group claims or the 'department' claim, and create a
        profile using department_defaults.

        The ``groups`` claim requires the Azure AD app registration to have
        "groupMembershipClaims": "SecurityGroup" in its manifest, and the
        groups to be named like "Epicor-Finance", "Epicor-Engineering", etc.

        The ``department`` claim requires adding an optional claim in the
        Azure AD app registration's token configuration.
        """
        existing = self.get_user(user_id)
        if existing:
            return existing

        # Resolve departments from claims (supports multiple)
        all_departments = self._resolve_departments_from_claims(claims)
        if not all_departments:
            logger.warning(
                "Cannot auto-assign department for %s — no matching group "
                "or department claim. Add them to users.json manually.",
                user_id,
            )
            return None

        primary_dept = all_departments[0]
        extra_depts = all_departments[1:]
        defaults = self._department_defaults.get(primary_dept, {})
        # Check for BAQ write permission from Epicor resolution
        can_write_baqs = claims.get("_can_write_baqs", False)

        profile = UserProfile(
            user_id=user_id.lower(),
            epicor_username=claims.get("preferred_username", user_id).split("@")[0],
            department=primary_dept,
            access_level=AccessLevel(defaults.get("access_level", "read_only")),
            environment=defaults.get("environment", "pilot"),
            display_name=claims.get("name", user_id.split("@")[0]),
            extra_departments=extra_depts,
            can_write_baqs=can_write_baqs,
        )

        # Cache in memory so subsequent requests don't re-resolve
        self._users[user_id.lower()] = profile
        logger.info(
            "Auto-created profile for %s: dept=%s access=%s",
            user_id, primary_dept, profile.access_level.value,
        )
        return profile

    def _resolve_departments_from_claims(self, claims: dict) -> list[str]:
        """Try to determine departments from Azure AD token claims.

        Returns a list of department names (may be multiple for multi-dept users).
        """
        departments: set[str] = set()

        # Strategy 1: Azure AD group names (e.g., "Epicor-Finance")
        group_names = claims.get("groups", [])
        for group in group_names:
            if isinstance(group, str):
                for dept in self._department_defaults:
                    if group.lower() in (
                        f"epicor-{dept.lower()}",
                        f"epicor_{dept.lower()}",
                        dept.lower(),
                    ):
                        departments.add(dept)

        # Strategy 2: Azure AD 'department' claim
        ad_department = claims.get("department", "")
        if ad_department:
            for dept in self._department_defaults:
                if ad_department.lower() == dept.lower():
                    departments.add(dept)

        # Strategy 3: Azure AD group-to-department mapping from config
        group_map = self._group_department_map
        for group in group_names:
            if group in group_map:
                departments.add(group_map[group])

        # Strategy 4: Epicor-resolved departments (set by EpicorUserResolver)
        epicor_depts = claims.get("_epicor_departments")
        if epicor_depts:
            departments.update(epicor_depts)

        return sorted(departments)

    def set_user_departments(self, user_id: str, departments: set[str]) -> None:
        """Update an existing user's departments from Epicor resolution."""
        user = self.get_user(user_id)
        if user and departments:
            dept_list = sorted(departments)
            user.department = dept_list[0]
            user.extra_departments = dept_list[1:]
            logger.info("Updated %s departments: %s", user_id, dept_list)

    def update_user_access(
        self,
        user_id: str,
        *,
        access_level: AccessLevel | None = None,
        can_write_baqs: bool | None = None,
    ) -> UserProfile | None:
        """Hot-swap a user's access level and/or BAQ write permission in memory.

        Returns the updated profile, or ``None`` if the user isn't cached.
        Changes are in-memory only — they survive until the server restarts
        or ``reload()`` is called.
        """
        user = self.get_user(user_id)
        if user is None:
            return None

        old_level = user.access_level
        old_baq = user.can_write_baqs

        if access_level is not None:
            user.access_level = access_level
        if can_write_baqs is not None:
            user.can_write_baqs = can_write_baqs

        logger.info(
            "Hot-swapped %s: access_level %s→%s, can_write_baqs %s→%s",
            user_id,
            old_level.value, user.access_level.value,
            old_baq, user.can_write_baqs,
        )
        return user

    def list_cached_users(self) -> list[UserProfile]:
        """Return all currently cached user profiles."""
        return list(self._users.values())

    def get_first_user(self) -> UserProfile | None:
        """Return the first user profile (for dev mode fallback)."""
        if self._users:
            return next(iter(self._users.values()))
        return None

    def get_department_key(self, department: str, baq: bool = False) -> str | None:
        """Return the API key for a department.

        With the 2-key model, always returns the admin read key.
        For BAQ access, also returns the admin read key (it has BAQ access).
        Falls back to legacy per-department keys if admin keys aren't set.
        """
        if self._admin_read_key:
            return self._admin_read_key
        store = self._department_baq_keys if baq else self._department_keys
        return store.get(department)

    def get_write_key(self) -> str | None:
        """Return the admin write API key."""
        return self._admin_write_key or None

    def get_read_key(self) -> str | None:
        """Return the admin read API key."""
        return self._admin_read_key or None

    def get_baq_key(self) -> str | None:
        """Return the BAQ write API key (read-all + BAQ write)."""
        return self._admin_baq_key or None

    def get_environment_url(self, environment: str) -> str:
        """Return the OData base URL for the given environment.

        Reads each environment's ``odata_base`` from ``department_keys.json``.
        The known ``pilot`` and ``live`` entries remain empty until configured;
        no installation URL is bundled.

        Parameters
        ----------
        environment:
            ``"pilot"`` or ``"live"``.

        Returns
        -------
        str
            The configured OData base URL, or an empty string when unset.

        Raises
        ------
        ValueError
            If *environment* is neither configured nor a known default name.
        """
        url = self._environment_urls.get(environment)
        if url is None:
            raise ValueError(
                f"Unknown environment '{environment}'. "
                f"Valid values: {sorted(self._environment_urls)}"
            )
        return url

    def reload(self) -> None:
        """Reload both configuration files from disk.

        This allows live updates to user mappings and department keys
        without restarting the MCP server process.
        """
        logger.info("Reloading user map from %s and %s", self._users_path, self._keys_path)
        # Load and validate before replacing active state. A rejected edit must
        # not discard valid profiles or leave the two group maps out of sync.
        replacement = type(self)(str(self._users_path), str(self._keys_path))
        self.__dict__.update(replacement.__dict__)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load users and department keys from their JSON files."""
        self._load_department_keys()
        self._load_users()

    def _load_users(self) -> None:
        """Parse ``data/users.json`` into :class:`UserProfile` instances.

        Expected JSON structure::

            {
              "users": {
                "reader@example.org": {
                  "epicor_username": "reader",
                  "department": "Finance",
                  "access_level": "read_write",
                  "environment": "pilot",
                  "display_name": "Example Reader"
                }
              },
              "department_defaults": {
                "Finance": { "access_level": "read_only", "environment": "pilot" },
                ...
              }
            }
        """
        if not self._users_path.exists():
            logger.warning("Users config file not found: %s", self._users_path)
            return

        try:
            raw: dict[str, Any] = json.loads(
                self._users_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Failed to load users configuration {self._users_path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError(f"{self._users_path}: users configuration must be a JSON object")
        try:
            self._epicor_group_department_map = validate_group_map(
                raw.get("epicor_group_to_department", {})
            )
        except ValueError as exc:
            raise ValueError(f"{self._users_path}: {exc}") from exc

        # Load department defaults (used for users that omit access_level / environment).
        self._department_defaults = raw.get("department_defaults", {})

        # Load Azure AD group -> department mapping (for auto-assignment)
        self._group_department_map = raw.get("azure_ad_group_map", {})

        users_section = raw.get("users", {})
        if not isinstance(users_section, dict):
            logger.error(
                "Expected 'users' to be a dict in %s, got %s",
                self._users_path,
                type(users_section).__name__,
            )
            return

        for email, info in users_section.items():
            try:
                uid = email.lower()
                department = info["department"]

                # Merge department defaults for any missing fields.
                dept_defaults = self._department_defaults.get(department, {})

                access_level_str = info.get(
                    "access_level",
                    dept_defaults.get("access_level", "read_only"),
                )
                environment = info.get(
                    "environment",
                    dept_defaults.get("environment", "pilot"),
                )

                profile = UserProfile(
                    user_id=uid,
                    epicor_username=info["epicor_username"],
                    department=department,
                    access_level=AccessLevel(access_level_str),
                    environment=environment,
                    display_name=info.get("display_name", info["epicor_username"]),
                    extra_departments=info.get("extra_departments", []),
                    can_write_baqs=info.get("can_write_baqs", False),
                )
                self._users[uid] = profile
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping invalid user entry '%s': %s", email, exc)

        logger.info("Loaded %d user profiles", len(self._users))

    def _load_department_keys(self) -> None:
        """Parse ``data/department_keys.json`` into key lookup dicts.

        Expected JSON structure::

            {
              "departments": {
                "Finance": {
                  "read_key": "YOUR_READ_API_KEY",
                  "read_scope": "YOUR_READ_SCOPE",
                  "baq_key": "YOUR_BAQ_API_KEY",
                  "baq_scope": "YOUR_BAQ_SCOPE"
                },
                ...
              },
              "environments": {
                "pilot": { "odata_base": "https://..." },
                "live": { "odata_base": "https://..." }
              },
              "company_id": "YOUR_COMPANY"
            }
        """
        if not self._keys_path.exists():
            logger.warning("Department keys file not found: %s", self._keys_path)
            return

        try:
            raw: dict[str, Any] = json.loads(
                self._keys_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Failed to load department keys from %s: %s",
                self._keys_path,
                exc,
            )
            return

        # --- Admin keys from the configured credentials INI ---------------
        self._load_admin_keys_from_credentials()

        # --- Admin keys from JSON (fallback) --------------------------------
        if not self._admin_read_key:
            admin_keys = raw.get("admin_keys", {})
            if admin_keys:
                self._admin_read_key = admin_keys.get("read_key", "")
                self._admin_write_key = admin_keys.get("write_key", "")

        # --- Environments section -----------------------------------------
        environments = raw.get("environments", {})
        for env_name, env_cfg in environments.items():
            if isinstance(env_cfg, dict):
                odata_base = env_cfg.get("odata_base")
                if odata_base:
                    self._environment_urls[env_name] = odata_base

        logger.info(
            "Loaded API keys: read=%s write=%s, %d environment URLs",
            "set" if self._admin_read_key else "MISSING",
            "set" if self._admin_write_key else "MISSING",
            len(self._environment_urls),
        )

    def _load_admin_keys_from_credentials(self) -> None:
        """Load admin API keys from the operator-configured credentials INI.

        Only ``EPICOR_MCP_CREDENTIALS_PATH`` is consulted; no file in a user's
        home directory is opened automatically. Keys mirror auth/credentials.py::

            [epicor]
            api_key = <read API key>
            baq_api_key = <optional BAQ key>
            write_api_key = <optional write key for SSO-mode grants>
        """
        import configparser
        try:
            from epicor_mcp.config import get_settings
            configured = get_settings().credentials_path
        except Exception:  # settings unavailable in some offline harnesses
            configured = ""
        if not configured:
            return
        cred_path = Path(configured).expanduser()
        if not cred_path.exists():
            return

        config = configparser.ConfigParser(interpolation=None)
        config.read(cred_path, encoding="utf-8")

        if "epicor" not in config:
            return

        epicor = config["epicor"]
        read_key = epicor.get("api_key", "")
        write_key = epicor.get("write_api_key", "")
        baq_key = epicor.get("baq_api_key", "")

        if read_key:
            self._admin_read_key = read_key
        if write_key:
            self._admin_write_key = write_key
        if baq_key:
            self._admin_baq_key = baq_key

        logger.info(
            "Loaded API keys from %s: read=%s baq=%s write=%s",
            cred_path,
            "set" if read_key else "missing",
            "set" if baq_key else "missing",
            "set" if write_key else "missing",
        )

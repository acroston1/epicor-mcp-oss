"""new menu-authz settings on the pydantic Settings model.

These gate how the middleware/authorizer behave, so their defaults are part of
the contract: shadow mode by default (safe roll-in), 300s TTL, 3600s stale grace,
a local menu_security.db path.
"""

from __future__ import annotations

from epicor_mcp.config import Settings


def _settings() -> Settings:
    # Construct directly; the new fields are absent from .env so defaults apply.
    return Settings()


def test_menu_authz_mode_defaults_off_without_sso():
    assert _settings().menu_authz_mode == "off"


def test_ttl_and_grace_defaults():
    s = _settings()
    assert s.menu_authz_ttl_seconds == 300
    assert s.menu_authz_stale_grace_seconds == 3600


def test_menu_map_db_path_default_is_menu_security_db():
    s = _settings()
    assert str(s.menu_map_db_path).endswith("menu_security.db")


def test_menu_map_overrides_path_present():
    # Exists as a setting (value may be a path or empty); just must be addressable.
    assert hasattr(_settings(), "menu_map_overrides_path")


def test_menu_authz_live_url_defaults_empty_meaning_epicor_live():
    # Empty => fall back to epicor_live_url (resolved server-side).
    assert _settings().menu_authz_live_url == ""

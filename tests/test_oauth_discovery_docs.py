"""Regression coverage: test oauth discovery docs."""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient  # noqa: E402

import epicor_mcp.server as server  # noqa: E402
from epicor_mcp.config import Settings  # noqa: E402
from epicor_mcp.discovery import DiscoveryIndex  # noqa: E402

import fixtures.authz as fa  # noqa: E402

_DOCS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
)


class _FakeAuthzClient:
    """Matches create_app's constructor call; serves the tenant, never networks."""

    def __init__(self, **kwargs) -> None:
        pass

    async def fetch_user(self, email):
        return fa.ident(email.split("@")[0], groups=("APP",), email=email)

    async def fetch_menus(self):
        return []

    async def fetch_security_rows(self):
        return []

    async def aclose(self):
        pass


def _client(tmp_path, monkeypatch, **overrides) -> TestClient:
    monkeypatch.setattr(server, "EpicorAuthzClient", _FakeAuthzClient)
    monkeypatch.setattr(DiscoveryIndex, "load", classmethod(lambda cls, root: None))
    db = fa.build_menu_security_db(tmp_path / "menu_security.db")
    from tests.fixtures.oss_server import server_settings
    settings = server_settings(tmp_path, menu_map_db_path=db, **overrides)
    return TestClient(server.create_app(settings))


def test_dev_mode_with_blank_azure_ids_still_404s_the_docs(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, auth_mode="none", azure_tenant_id="", azure_client_id="")
    for path in _DOCS:
        r = c.get(path)
        assert r.status_code == 404, path
        # The 404 must be READABLE by a browser/Electron client, which is the
        # whole reason it comes from the app: a JSON body, not a bare status.
        assert r.json()["detail"].casefold() == "not found", path


def test_dev_mode_with_populated_azure_ids_serves_the_docs(tmp_path, monkeypatch):
    c = _client(
        tmp_path,
        monkeypatch,
        azure_tenant_id="00000000-0000-0000-0000-000000000000",
        azure_client_id="11111111-1111-1111-1111-111111111111",
    )
    r = c.get(_DOCS[0])
    assert r.status_code == 200
    assert r.json()["authorization_servers"] == [
        "https://mcp.example.org"
    ]
    r = c.get(_DOCS[1])
    assert r.status_code == 200
    meta = r.json()
    # The advertised flow runs against the ROOT authorization server (the
    # /oauth/* proxy) — that is what makes it completable, and it is the
    # contract the bridge's discover_oauth() walks.
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert meta[key].startswith("https://mcp.example.org/oauth/"), key
    # The jwks_uri must not carry the blank-tenant double slash — the exact
    
    assert "//discovery" not in meta["jwks_uri"].replace("https://", "", 1)


def test_one_blank_id_is_still_unusable_config(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='Azure|azure|SSO|tenant|client'):
        _client(tmp_path, monkeypatch, azure_client_id='')

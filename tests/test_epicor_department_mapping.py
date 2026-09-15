"""Operator-owned department configuration and both real SSO resolution paths."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import epicor_mcp.server as server
import epicor_mcp.rbac.epicor_user_resolver as resolver_module
from epicor_mcp.auth.oauth import ValidatedToken
from epicor_mcp.rbac.user_map import UserMap
from tests.fixtures.oss_server import server_settings
from fixtures.authz import build_menu_security_db
from fixtures.authz.fakes import ident, mrow, srow


MAPPING = {"EXAMPLE-OPS": ["Operations", "Planning"], "EXAMPLE-OTHER": []}
MALFORMED = [None, [], "Operations", 7, {"": ["Operations"]},
             {" ": ["Operations"]}, {"EXAMPLE-OPS": "Operations"},
             {"EXAMPLE-OPS": [""]}, {"EXAMPLE-OPS": [" "]},
             {"EXAMPLE-OPS": [3]}, {"EXAMPLE-OPS": None}]


def _user_map(tmp_path, config):
    users = tmp_path / "users.json"
    users.write_text(json.dumps(config))
    keys = tmp_path / "department_keys.json"
    keys.write_text("{}")
    return UserMap(str(users), str(keys)), users


@pytest.mark.parametrize("config", [{}, {"epicor_group_to_department": {}}])
def test_absent_and_empty_group_maps_have_no_inherited_departments(tmp_path, config):
    user_map, _ = _user_map(tmp_path, config)
    assert user_map.epicor_group_to_department == {}
    assert resolver_module.DEFAULT_GROUP_DEPARTMENT_MAP == {}
    assert server._departments_from_groups(["APP", "ENG", "QA"]) == []


def test_mapping_is_exact_and_defensively_copied(tmp_path):
    user_map, _ = _user_map(tmp_path, {"epicor_group_to_department": MAPPING})
    mapping = user_map.epicor_group_to_department
    assert mapping == MAPPING
    assert server._departments_from_groups(["EXAMPLE-OPS", "UNMAPPED"], mapping) == [
        "Operations", "Planning",
    ]
    assert server._departments_from_groups(["example-ops"], mapping) == []
    mapping["EXAMPLE-OPS"].append("Injected")
    assert user_map.epicor_group_to_department == MAPPING


@pytest.mark.parametrize("value", MALFORMED)
def test_malformed_mapping_is_rejected_at_configuration_load(tmp_path, value):
    with pytest.raises(ValueError, match="epicor_group_to_department"):
        _user_map(tmp_path, {"epicor_group_to_department": value})


@pytest.mark.parametrize("value", [{1: ["Operations"]}, {"EXAMPLE": ("Operations",)}])
def test_programmatic_mapping_rejects_non_json_shape(value):
    with pytest.raises(ValueError):
        resolver_module.validate_group_map(value)


def test_reload_removes_old_map_and_preserves_state_on_malformed_map(tmp_path):
    profile = {"department": "Explicit", "epicor_username": "reader", "access_level": "read_only",
               "can_write_baqs": True, "environment": "pilot"}
    config = {"users": {"explicit@example.org": profile},
              "epicor_group_to_department": MAPPING}
    user_map, users = _user_map(tmp_path, config)
    original = user_map.get_user("explicit@example.org")
    users.write_text(json.dumps({"epicor_group_to_department": None}))
    with pytest.raises(ValueError, match="epicor_group_to_department"):
        user_map.reload()
    assert user_map.epicor_group_to_department == MAPPING
    assert user_map.get_user("explicit@example.org") == original
    users.write_text(json.dumps({"users": config["users"]}))
    user_map.reload()
    assert user_map.epicor_group_to_department == {}
    assert user_map.get_user("explicit@example.org").can_write_baqs is True


def _mock_legacy_http(monkeypatch, groups):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def get(self, url, *, headers, params):
            calls.append((url, params))
            return httpx.Response(200, request=httpx.Request("GET", url), json={
                "value": [{"UserID": "reader", "Name": "Example Reader",
                           "EMailAddress": "reader@example.org",
                           "GroupList": "~".join(groups)}],
            })

        async def aclose(self):
            pass

    monkeypatch.setattr(resolver_module, "httpx", SimpleNamespace(AsyncClient=Client))
    return calls


@pytest.mark.parametrize("mapping,expected", [(None, set()), ({}, set()),
    (MAPPING, {"Operations", "Planning"})])
async def test_legacy_resolver_uses_only_operator_mapping(monkeypatch, mapping, expected):
    calls = _mock_legacy_http(monkeypatch, ["EXAMPLE-OPS", "APP", "UNMAPPED"])
    resolver = resolver_module.EpicorUserResolver(
        "https://erp.example.org/api/v2/odata/DEMO/", "service", "test", "test",
        group_map=mapping,
    )
    try:
        result = await resolver.resolve_user("reader@example.org")
        assert result["departments"] == expected
        assert result["can_write_baqs"] is False
        assert len(calls) == 1
        assert calls[0][0].endswith("Ice.BO.UserFileSvc/UserFiles")
        assert calls[0][1]["$filter"] == "tolower(EMailAddress) eq 'reader@example.org'"
        assert calls[0][1]["$select"] == "UserID,Name,EMailAddress,GroupList"
        assert calls[0][1]["$top"] == 5
    finally:
        await resolver.close()


async def test_legacy_map_reload_clears_cached_departments(monkeypatch):
    calls = _mock_legacy_http(monkeypatch, ["EXAMPLE-OPS"])
    resolver = resolver_module.EpicorUserResolver(
        "https://erp.example.org/api/v2/odata/DEMO/", "service", "test", "test",
        group_map=MAPPING,
    )
    try:
        assert (await resolver.resolve_user("reader@example.org"))["departments"]
        resolver.set_group_map({})
        assert (await resolver.resolve_user("reader@example.org"))["departments"] == set()
        assert len(calls) == 2
    finally:
        await resolver.close()


def _app(tmp_path, monkeypatch, config, *, source="menu", groups=("EXAMPLE-OPS",)):
    legacy_calls = _mock_legacy_http(monkeypatch, groups)

    class AuthzClient:
        def __init__(self, **kwargs):
            pass

        async def fetch_user(self, email):
            return ident("reader", email=email, groups=groups) if source == "menu" else None

        async def fetch_menus(self):
            return [mrow("EXAMPLE-AP", sec_code="EXAMPLE-SEC", program="Erp.UI.APInvoiceEntry")]

        async def fetch_security_rows(self):
            return [srow("EXAMPLE-SEC", entry_list="EXAMPLE-ACCESS")]

        async def aclose(self):
            pass

    monkeypatch.setattr(server, "EpicorAuthzClient", AuthzClient)
    db = build_menu_security_db(tmp_path / "menu_security.db")
    settings = server_settings(tmp_path, menu_map_db_path=db,
                               menu_authz_mode="enforce", admin_secret="test-secret")
    settings.users_config_path.write_text(json.dumps(config))
    app = server.create_app(settings)

    async def validate(token):
        return ValidatedToken(user_id="reader@example.org", claims={})

    app.state.token_validator.validate_token = validate
    return app, settings.users_config_path, legacy_calls


@pytest.mark.parametrize("source", ["menu", "legacy"])
def test_server_wires_operator_mapping_into_both_resolution_paths(tmp_path, monkeypatch, source):
    app, _, legacy_calls = _app(tmp_path, monkeypatch,
        {"epicor_group_to_department": MAPPING}, source=source)
    with TestClient(app) as client:
        response = client.get("/mapping-probe", headers={"Authorization": "Bearer synthetic"})
        assert response.status_code == 404
        profile = app.state.user_map.get_user("reader@example.org")
        assert profile.department == "Operations"
        assert profile.extra_departments == ["Planning"]
        assert profile.can_write_baqs is False
        assert profile.access_level.value == "read_only"
        if source == "menu":
            explanation = client.get("/admin/authz/reader@example.org",
                                     headers={"X-Admin-Secret": "test-secret"}).json()
            assert "Erp.BO.APInvoiceSvc" not in {
                item["service_id"] for item in explanation["services"]
            }, "Department labels must not grant a menu's service access"
    assert bool(legacy_calls) is (source == "legacy")


@pytest.mark.parametrize("config", [{}, {"epicor_group_to_department": {}},
                                    {"epicor_group_to_department": MAPPING}])
def test_unmapped_user_does_not_acquire_an_implicit_profile(tmp_path, monkeypatch, config):
    app, _, _ = _app(tmp_path, monkeypatch, config, groups=("APP",))
    with TestClient(app) as client:
        assert client.get("/mapping-probe", headers={"Authorization": "Bearer synthetic"}).status_code == 403
    assert app.state.user_map.get_user("reader@example.org") is None


@pytest.mark.parametrize("source", ["menu", "legacy"])
def test_admin_reload_applies_mapping_removal_to_both_paths(tmp_path, monkeypatch, source):
    app, users, _ = _app(tmp_path, monkeypatch,
                        {"epicor_group_to_department": MAPPING}, source=source)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer synthetic"}
        assert client.get("/mapping-probe", headers=headers).status_code == 404
        users.write_text("{}")
        assert client.post("/admin/reload", headers={"X-Admin-Secret": "test-secret"}).status_code == 200
        assert client.get("/mapping-probe", headers=headers).status_code == 403


def test_explicit_profile_and_baq_permission_override_department_inference(tmp_path, monkeypatch):
    profile = {"department": "Explicit", "epicor_username": "reader", "access_level": "read_only",
               "can_write_baqs": True, "environment": "pilot"}
    app, _, legacy_calls = _app(tmp_path, monkeypatch, {
        "users": {"reader@example.org": profile}, "epicor_group_to_department": MAPPING,
    })
    with TestClient(app) as client:
        assert client.get("/mapping-probe", headers={"Authorization": "Bearer synthetic"}).status_code == 404
    actual = app.state.user_map.get_user("reader@example.org")
    assert actual.department == "Explicit"
    assert actual.extra_departments == []
    assert actual.can_write_baqs is True
    assert actual.environment == "pilot"
    assert legacy_calls == []


@pytest.mark.parametrize("source", ["menu", "legacy"])
def test_existing_baq_group_right_is_separate_from_department_map(tmp_path, monkeypatch, source):
    app, _, _ = _app(tmp_path, monkeypatch, {"epicor_group_to_department": MAPPING},
                    source=source, groups=("EXAMPLE-OPS", "BAQ"))
    with TestClient(app) as client:
        assert client.get("/mapping-probe", headers={"Authorization": "Bearer synthetic"}).status_code == 404
    assert app.state.user_map.get_user("reader@example.org").can_write_baqs is True


def test_invalid_admin_reload_preserves_working_configuration(tmp_path, monkeypatch):
    app, users, _ = _app(tmp_path, monkeypatch, {"epicor_group_to_department": MAPPING})
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer synthetic"}
        assert client.get("/mapping-probe", headers=headers).status_code == 404
        users.write_text(json.dumps({"epicor_group_to_department": None}))
        response = client.post("/admin/reload", headers={"X-Admin-Secret": "test-secret"})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_configuration"
        assert "epicor_group_to_department" in response.json()["message"]
        assert client.get("/mapping-probe", headers=headers).status_code == 404
    assert app.state.user_map.epicor_group_to_department == MAPPING

"""admin /admin/authz/* endpoints + dev-mode middleware priming.

Mode-B extension of the suite (the Mode-A report flagged these as
no-deterministic-coverage). Runs the REAL FastAPI app via Starlette TestClient,
hermetically:

  * ``server.EpicorAuthzClient`` is monkeypatched to a fake (no Epicor network);
  * menu_security.db + audit.db are synthetic temporary files;
  * vector search + live forum are disabled so nothing reaches out.

The app still constructs the real MenuMapStore + MenuAuthorizer + enforcer +
audit logger, and the admin routes / auth middleware are the real closures — so
this exercises the actual wiring with synthetic identities and local databases.
These tests never call live Epicor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fastapi.testclient import TestClient  # noqa: E402

import epicor_mcp.server as server  # noqa: E402
from epicor_mcp.config import Settings  # noqa: E402

import fixtures.authz as fa  # noqa: E402
from fixtures.authz.fakes import ident, mrow, srow  # noqa: E402

# The server fixture creates the service index required by the menu authorizer.

ADMIN = {"X-Admin-Secret": "test-secret"}
APUSER = "apuser@example.org"

# Tenant that matches the tmp menu_security.db built by build_menu_security_db.
_MENUS = [
    mrow("AP0100", sec_code="APSEC", program="Erp.UI.APInvoiceEntry"),
    mrow("AP0200", sec_code="APSEC", program="Erp.UI.APAdjustmentEntry"),
    mrow("PO0100", sec_code="POSEC", program="Erp.UI.POEntry"),
]
_SECURITY = [srow("APSEC", entry_list="APP"), srow("POSEC", entry_list="PURCH")]
_KNOWN = {
    APUSER: ident("apuser", groups=("APP",), email=APUSER),
    "enguser@example.org": ident("enguser", groups=("ENG",)),
}


class _FakeAuthzClient:
    """Matches create_app's constructor call; serves the tenant, never networks.

    Resolves ANY email to an APP-group identity so whoever the dev user happens
    to be (the first synthetic configured profile) still primes a real snapshot.
    """

    def __init__(self, **kwargs) -> None:
        pass

    async def fetch_user(self, email):
        return _KNOWN.get(email.lower()) or ident(
            email.split("@")[0], groups=("APP",), email=email
        )

    async def fetch_menus(self):
        return list(_MENUS)

    async def fetch_security_rows(self):
        return list(_SECURITY)

    async def aclose(self):
        pass


def _build_app(tmp_path, monkeypatch, *, mode: str = "shadow"):
    monkeypatch.setattr(server, "EpicorAuthzClient", _FakeAuthzClient)
    db = fa.build_menu_security_db(tmp_path / "menu_security.db")
    from tests.fixtures.oss_server import server_settings
    settings = server_settings(tmp_path,
        menu_authz_mode=mode,
        admin_secret="test-secret",
        vector_search_enabled=False,
        forum_live_enabled=False,
        audit_log_path=tmp_path / "audit.db",
        menu_map_db_path=db,
    )
    return server.create_app(settings)


@pytest.fixture
def app(tmp_path, monkeypatch):
    return _build_app(tmp_path, monkeypatch)


# ====================================================================== #
# Admin auth gate
# ====================================================================== #

def test_admin_authz_inspect_requires_admin(app):
    with TestClient(app) as c:
        r = c.get(f"/admin/authz/{APUSER}")  # no X-Admin-Secret
    assert r.status_code == 403


def test_admin_authz_evict_requires_admin(app):
    with TestClient(app) as c:
        r = c.post(f"/admin/authz/evict/{APUSER}")
    assert r.status_code == 403


# ====================================================================== #
# GET /admin/authz/{email} — inspect (primes + explain)
# ====================================================================== #

def test_inspect_returns_menu_derived_reasoning_chain(app):
    with TestClient(app) as c:
        r = c.get(f"/admin/authz/{APUSER}", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["epicor_user_id"] == "apuser"
    assert body["security_mgr"] is False
    # APInvoiceSvc + APAdjustmentSvc + VendorSvc + baseline CompanySvc.
    assert body["allowed_service_count"] == 4

    services = {s["service_id"]: s for s in body["services"]}
    granted_by = services["Erp.BO.APInvoiceSvc"]["granted_by"]
    assert any(
        g["menu_id"] == "AP0100" and g["sec_code"] == "APSEC"
        and "APP" in g["matched_principals"]
        for g in granted_by
    )


# ====================================================================== #
# POST /admin/authz/evict/{email} — really drops the cached snapshot
# ====================================================================== #

def test_evict_drops_cached_snapshot(app):
    authorizer = app.state.menu_authorizer
    with TestClient(app) as c:
        c.get(f"/admin/authz/{APUSER}", headers=ADMIN)      # primes
        assert authorizer.get_snapshot(APUSER) is not None
        r = c.post(f"/admin/authz/evict/{APUSER}", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "evicted"
    assert authorizer.get_snapshot(APUSER) is None


# ====================================================================== #
# POST /admin/authz/refresh — tenant refetch + evict-all
# ====================================================================== #

def test_refresh_evicts_all_snapshots(app):
    authorizer = app.state.menu_authorizer
    with TestClient(app) as c:
        c.get(f"/admin/authz/{APUSER}", headers=ADMIN)      # primes
        assert authorizer.get_snapshot(APUSER) is not None
        r = c.post("/admin/authz/refresh", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "refreshed"
    assert authorizer.get_snapshot(APUSER) is None


# ====================================================================== #
# GET /admin/authz/shadow-report — reads audit divergences
# ====================================================================== #

def test_shadow_report_returns_mode_and_divergences(app):
    with TestClient(app) as c:
        r = c.get("/admin/authz/shadow-report", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "shadow"
    assert isinstance(body["divergences"], list)
    assert body["count"] == len(body["divergences"])


# ====================================================================== #
# Anonymous requests must not prime authenticated user snapshots.
# ====================================================================== #

def test_anonymous_request_does_not_prime_a_microsoft_user(app):
    authorizer = app.state.menu_authorizer
    user = app.state.user_map.get_first_user().user_id
    with TestClient(app) as client:
        assert authorizer.get_snapshot(user) is None
        response = client.get("/mcp")
        assert response.status_code == 401
        assert authorizer.get_snapshot(user) is None

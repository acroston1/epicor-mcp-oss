"""Table-authz WIRING — the table gate reaching the running server.

Builder-scope: server.py + config.py only. The scope/authorizer SEMANTICS are
pinned in ``tests/test_table_authz_core.py``; this file pins the seams that no
unit test there can see, because each lives between modules:

1. The ``TableAuthorizer`` exists WITHOUT the discovery index. It used to be
   constructed inside the discovery try-block behind ``if _discovery_index is
   not None:``, so deleting ``data/discovery_index/`` silently removed the
   query gate while every tool kept answering.
2. Every /admin endpoint that evicts or refreshes menu snapshots also evicts
   the pinned table scope(s). Scopes are SESSION-PINNED with **no TTL** — these
   endpoints and a restart are the ONLY refresh paths, so a missed eviction
   site means a revoked Epicor grant lives until the next deploy.
3. ``/health`` reports the (normalised) mode and ``/admin/authz/{email}``
   explains the table scope beside the menu chain that feeds it.
4. The config DEFAULT is ``gate``. Asserted on the
   FIELD, not on ``Settings()`` — a local ``.env`` can pin the deployed value and
   would mask a reverted default.

Hermetic like ``test_admin_authz_endpoints.py``: fake authz client, tmp
menu_security.db + audit.db, no live Epicor. ``DiscoveryIndex.load`` is patched
to ``None`` in every app build — that IS seam 1 under test, and it keeps
operator-generated indexes out of the suite.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from fastapi.testclient import TestClient  # noqa: E402

import epicor_mcp.server as server  # noqa: E402
from epicor_mcp.config import Settings  # noqa: E402
from epicor_mcp.discovery import DiscoveryIndex  # noqa: E402
from epicor_mcp.wedge_server import WedgeRuntime  # noqa: E402

import fixtures.authz as fa  # noqa: E402
from fixtures.authz.fakes import ident, mrow, srow  # noqa: E402

# The app fixtures build their own synthetic metadata and configuration.

ADMIN = {"X-Admin-Secret": "test-secret"}
APUSER = "apuser@example.org"

# Same tenant shape as test_admin_authz_endpoints.py: APP group -> two AP menus.
_MENUS = [
    mrow("AP0100", sec_code="APSEC", program="Erp.UI.APInvoiceEntry"),
    mrow("AP0200", sec_code="APSEC", program="Erp.UI.APAdjustmentEntry"),
    mrow("PO0100", sec_code="POSEC", program="Erp.UI.POEntry"),
]
_SECURITY = [srow("APSEC", entry_list="APP"), srow("POSEC", entry_list="PURCH")]


class _FakeAuthzClient:
    """Matches create_app's constructor call; serves the tenant, never networks."""

    def __init__(self, **kwargs) -> None:
        pass

    async def fetch_user(self, email):
        return ident(email.split("@")[0], groups=("APP",), email=email)

    async def fetch_menus(self):
        return list(_MENUS)

    async def fetch_security_rows(self):
        return list(_SECURITY)

    async def aclose(self):
        pass


def _build_app(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EpicorAuthzClient", _FakeAuthzClient)
    # Seam 1: NO discovery index, ever, in these builds. The gate must not
    # depend on it — and patching `load` (not the manifest probe) exercises the
    # exact branch that used to skip the TableAuthorizer construction.
    monkeypatch.setattr(
        DiscoveryIndex, "load", classmethod(lambda cls, root: None)
    )
    db = fa.build_menu_security_db(tmp_path / "menu_security.db")
    from tests.fixtures.oss_server import server_settings
    settings = server_settings(tmp_path,
        menu_authz_mode="shadow",
        # Explicit: the repo .env pins EPICOR_MCP_TABLE_AUTHZ_MODE=boost, and
        # this suite is about the GATE wiring, not about what the deployed box
        # happens to have configured today.
        table_authz_mode="gate",
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


class _Recorder:
    """Stands in on app.state — the endpoints must read it at REQUEST time,
    which is exactly what makes it substitutable here."""

    mode = "gate"

    def __init__(self) -> None:
        self.evicted: list[str] = []
        self.evict_all_calls = 0

    def evict(self, email: str) -> bool:
        self.evicted.append(email)
        return True

    def evict_all(self) -> int:
        self.evict_all_calls += 1
        return 1


# ====================================================================== #
# 4 — the config default really flipped
# ====================================================================== #

def test_config_default_table_authz_mode_is_gate():
    """On the FIELD, not on Settings(): Settings() reads .env, which pins the
    deployed rollback value and would mask a quietly-reverted default."""
    assert Settings.model_fields["table_authz_mode"].default == "gate"


# ====================================================================== #
# 1 — the authorizer exists without the discovery index
# ====================================================================== #

def test_table_authorizer_exists_without_the_discovery_index(app):
    ta = app.state.table_authorizer
    assert ta is not None, (
        "TableAuthorizer was not constructed — with DiscoveryIndex.load "
        "returning None, this is the old inside-the-try-block wiring back"
    )
    assert ta.mode == "gate"


_RUNTIME_TAKES_GATE = (
    "table_authorizer" in inspect.signature(WedgeRuntime.__init__).parameters
)


def test_the_runtime_receives_the_same_authorizer_app_state_holds(tmp_path, monkeypatch):
    """The gate must reach the object that executes SQL, and it must be the
    SAME instance the admin endpoints evict — two instances would mean two
    session-pinned caches, and eviction reaching only the one nobody queries."""
    import epicor_mcp.wedge_server as wedge

    captured: dict = {}
    original = wedge.WedgeRuntime

    class _Capturing(original):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured["runtime"] = self

    monkeypatch.setattr(wedge, "WedgeRuntime", _Capturing)
    app = _build_app(tmp_path, monkeypatch)
    runtime = captured["runtime"]
    assert app.state.table_authorizer is not None
    assert runtime.table_authorizer is app.state.table_authorizer


# ====================================================================== #
# 2 — every menu-snapshot eviction takes the table scopes with it
# ====================================================================== #

def test_admin_authz_evict_also_evicts_the_pinned_table_scope(app):
    rec = _Recorder()
    app.state.table_authorizer = rec
    with TestClient(app) as c:
        r = c.post(f"/admin/authz/evict/{APUSER}", headers=ADMIN)
    assert r.status_code == 200
    assert rec.evicted == [APUSER]


def test_admin_authz_refresh_also_evicts_all_table_scopes(app):
    rec = _Recorder()
    app.state.table_authorizer = rec
    with TestClient(app) as c:
        r = c.post("/admin/authz/refresh", headers=ADMIN)
    assert r.status_code == 200
    assert rec.evict_all_calls == 1


def test_admin_reload_also_evicts_all_table_scopes(app):
    rec = _Recorder()
    app.state.table_authorizer = rec
    with TestClient(app) as c:
        r = c.post("/admin/reload", headers=ADMIN)
    assert r.status_code == 200
    assert rec.evict_all_calls == 1


def test_admin_user_evict_also_evicts_that_users_table_scope(app):
    rec = _Recorder()
    app.state.table_authorizer = rec
    first = app.state.user_map.get_first_user()
    if first is None:
        pytest.fail("synthetic user fixture was not loaded")
    with TestClient(app) as c:
        r = c.post(f"/admin/users/{first.user_id}/evict", headers=ADMIN)
    assert r.status_code == 200
    assert rec.evicted == [first.user_id.lower()]


def test_eviction_endpoints_survive_an_unwired_authorizer(app):
    """The fallback FastMCP branch (no service index) leaves
    app.state.table_authorizer as None; menu eviction must keep working."""
    app.state.table_authorizer = None
    with TestClient(app) as c:
        r = c.post(f"/admin/authz/evict/{APUSER}", headers=ADMIN)
    assert r.status_code == 200


# ====================================================================== #
# 3 — /health mode + /admin/authz/{email} table_scope block
# ====================================================================== #

def test_health_reports_the_table_authz_mode(app):
    with TestClient(app) as c:
        body = c.get("/health").json()
    assert body["table_authz_mode"] == "gate"


def test_health_reports_unwired_when_no_authorizer_was_built(app):
    app.state.table_authorizer = None
    with TestClient(app) as c:
        body = c.get("/health").json()
    assert body["table_authz_mode"] == "unwired"


def test_explain_includes_the_table_scope_block(app):
    """The APP-group tenant projects the AP services through the REAL service
    index, so the scope comes back SCOPED (exactly the menu tables — default deny) and — being
    a successful computation — PINNED for the process lifetime.

    The scope is computed HERE, in the test's own thread, before the request:
    ``ServiceIndex`` holds a ``check_same_thread`` sqlite connection bound to
    the thread that built the app, and ``TestClient`` dispatches on a portal
    thread, where the projection degrades to UNAVAILABLE (fail closed — the
    right behaviour, but a harness artifact: uvicorn runs the loop on the
    constructing thread). Pre-pinning is also the production shape this block
    reports: a session-pinned scope serving later requests from the cache.
    """
    import asyncio

    scope = asyncio.run(app.state.table_authorizer.scope_for(APUSER))
    assert scope.state.value == "scoped", scope.reason
    with TestClient(app) as c:
        r = c.get(f"/admin/authz/{APUSER}", headers=ADMIN)
    assert r.status_code == 200
    ts = r.json()["table_scope"]
    assert ts["mode"] == "gate"
    assert ts["state"] == "scoped"
    assert ts["tables"] > 0
    assert ts["pinned"] is True
    assert ts["reason"]


def test_explain_reports_not_computed_when_unwired(app):
    app.state.table_authorizer = None
    with TestClient(app) as c:
        r = c.get(f"/admin/authz/{APUSER}", headers=ADMIN)
    assert r.status_code == 200
    ts = r.json()["table_scope"]
    assert ts["state"] == "not_computed"
    assert ts["pinned"] is False

"""Identity comes from the SESSION, never from an argument.

The gate cold-start/identity contract: ``for_email`` is
REMOVED from the LLM-facing surface. In gate mode it OUTRANKED the session, so
any tool caller could pick whose table authorization applied to them — an
identity-spoofing vector. Separately, the moment a bearer token becomes a
session identity, the server fires a fire-and-forget ``scope_for`` prime so the
model's first tool call finds the scope pinned (admin) or warming (scoped user)
instead of waiting for the initial metadata crawl inline.

Pins, in order:

1. the REGISTERED discovery tool schemas carry no ``for_email``;
2. an explicit ``for_email`` (or an old alias spelling) draws the standard
   ``unknown_arguments`` envelope naming session-derived identity and the
   ``/admin/authz/{email}`` explain endpoint — never honored, never silently
   dropped, and it never reaches the tool body or the authorizer;
3. the SESSION email drives the scope: two different session emails produce
   two different scopes through the same registered tool;
4. ``EPICOR_MCP_DEV_IDENTITY`` still works headerless, inside the REAL
   ``TableAuthorizer.resolve_identity`` (whose signature has no ``for_email``);
5. the connect-time prime fires exactly on token validation with mode=gate and
   an authorizer present, never under another mode, and a prime failure never
   fails the request.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mcp.server.fastmcp import FastMCP

from epicor_mcp.discovery.authz import AuthzScope, TableAuthorizer
from epicor_mcp.discovery.tools import register_discovery_tools
from epicor_mcp.tools._argguard import (
    _declared_types,
    _screen_arguments,
    install_validation_guard,
)

# --------------------------------------------------------------------------- #
# Duck-typed index + fakes (the test_discovery_gate pattern)
# --------------------------------------------------------------------------- #
_TABLES = {"JobHead": "Erp.JobHead", "POHeader": "Erp.POHeader"}
_FIELDS = {
    "JobHead": [("JobNum", "nvarchar")],
    "POHeader": [("PONum", "int")],
}


class _Hit:
    def __init__(self, table: str, full: str) -> None:
        self.table, self.full_name = table, full
        self.description, self.field_count, self.score = "", 1, 0.9


class _FieldHit:
    def __init__(self, table: str, field: str, sql_type: str) -> None:
        self.table, self.field, self.sql_type = table, field, sql_type
        self.label, self.description, self.required = "", "", False


class _Index:
    manifest = {"table_count": 2, "field_count": 2}

    def search_tables(self, vec, q, limit=5, allowed=None):
        out = []
        for name, full in _TABLES.items():
            if allowed is not None and name.lower() not in allowed:
                continue
            out.append(_Hit(name, full))
        return out[:limit]

    def search_fields(self, table, vec, q, limit=6):
        return [_FieldHit(table, n, t) for n, t in _FIELDS.get(table, [])][:limit]

    def fields_of(self, table):
        return [{"name": n} for n, _ in _FIELDS.get(table, [])]

    def resolve_table(self, name):
        return {k.lower(): k for k in _TABLES}.get(str(name).strip().lower())

    def table_info(self, canon):
        return {"full_name": _TABLES[canon], "field_count": 1, "description": ""}

    def find_column_elsewhere(self, core):
        return [], 0

    def name_matches_elsewhere(self, q, here):
        return []


class _SessionAuthorizer:
    """Fake with the current surface: resolve_identity takes ONLY the
    session email, scope_for records every fetch and answers per-identity."""

    mode = "gate"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve_identity(self, session_email: str = "") -> str:
        return (session_email or "").strip()

    async def scope_for(self, email: str) -> AuthzScope:
        self.calls.append(email)
        table = {"job@example.org": "JobHead", "po@example.org": "POHeader"}.get(email)
        if table is None:
            return AuthzScope.unavailable(email, "no identity supplied")
        return AuthzScope.scoped(email, [table], "test scope")


async def _embed(text, prefix):
    return None


def _register_real(authorizer, session_email=None):
    """Register the REAL tools on a REAL FastMCP — the schemas under test are
    the ones a client would list, not a mirror."""
    mcp = FastMCP(name="identity-test")
    ok = register_discovery_tools(
        mcp,
        _Index(),
        embed_query=_embed,
        authorizer=authorizer,
        session_email=session_email,
    )
    assert ok
    return mcp


# --------------------------------------------------------------------------- #
# 1 — the registered schemas carry NO for_email
# --------------------------------------------------------------------------- #
def test_the_registered_schemas_carry_no_for_email():
    """``for_email`` is gone from the surface.
    Driven off the REGISTERED tools so a re-added parameter cannot hide behind
    a stale mirror."""
    mcp = _register_real(_SessionAuthorizer())
    for name in ("epicor_tables", "epicor_fields"):
        props = mcp._tool_manager.get_tool(name).parameters["properties"]
        assert "for_email" not in props, (
            f"{name} re-grew for_email — in gate mode it outranked the "
            "session, an identity-spoofing vector"
        )


# --------------------------------------------------------------------------- #
# 2 — an explicit for_email is unknown_arguments: not honored, not dropped
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool", ["epicor_tables", "epicor_fields"])
@pytest.mark.parametrize("spelling", ["for_email", "email", "user_email"])
def test_a_supplied_identity_argument_draws_unknown_arguments(tool, spelling):
    mcp = _register_real(_SessionAuthorizer())
    declared = _declared_types(mcp, tool)
    assert declared is not None and "for_email" not in declared
    args = {"query": "jobs", spelling: "victim@example.org"}
    if tool == "epicor_fields":
        args["table"] = "JobHead"
    _out, _notes, env = _screen_arguments(tool, args, declared)
    assert env is not None and env["error"] == "unknown_arguments"
    # The hint says where identity COMES from and how to inspect someone
    # else's access — a bare "no such parameter" makes the model invent the
    # next spelling.
    assert "session" in env["message"]
    assert "/admin/authz/{email}" in env["message"]
    # The arguments that DID map survive; the identity never does.
    assert env["retry_with"].get("query") == "jobs"
    assert "for_email" not in json.dumps(env.get("retry_with", {}))


async def test_for_email_is_rejected_before_the_tool_or_authorizer_runs():
    """Through the REAL guard on the REAL ToolManager: the reject
    short-circuits ahead of the tool body, so a spoofed identity never
    reaches the authorizer at all — not honored, not silently dropped."""
    auth = _SessionAuthorizer()
    mcp = _register_real(auth, session_email=lambda: "job@example.org")
    install_validation_guard(mcp)
    raw = await mcp._tool_manager.call_tool(
        "epicor_tables",
        {"query": "jobs", "for_email": "victim@example.org"},
    )
    env = json.loads(raw)
    assert env["error"] == "unknown_arguments"
    assert auth.calls == [], "the spoofed identity must never fetch a scope"


# --------------------------------------------------------------------------- #
# 3 — the session email is what drives the scope
# --------------------------------------------------------------------------- #
async def test_two_session_emails_get_two_different_scopes():
    auth = _SessionAuthorizer()
    cell = {"email": "job@example.org"}
    mcp = _register_real(auth, session_email=lambda: cell["email"])
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}

    first = await tools["epicor_tables"](query="work")
    cell["email"] = "po@example.org"
    second = await tools["epicor_tables"](query="work")

    assert auth.calls == ["job@example.org", "po@example.org"]
    assert [t["name"] for t in first["tables"]] == ["JobHead"]
    assert [t["name"] for t in second["tables"]] == ["POHeader"]


# --------------------------------------------------------------------------- #
# 4 — the dev-mode env override still works headerless
# --------------------------------------------------------------------------- #
def test_real_resolve_identity_has_no_for_email_and_dev_outranks_session():
    import inspect

    auth = TableAuthorizer(None, None, dev_identity="dev@example.org")
    assert "for_email" not in inspect.signature(auth.resolve_identity).parameters
    assert auth.resolve_identity() == "dev@example.org"
    assert auth.resolve_identity(session_email="sess@example.net") == "dev@example.org"
    no_dev = TableAuthorizer(None, None)
    assert no_dev.resolve_identity(session_email="sess@example.net") == "sess@example.net"
    assert no_dev.resolve_identity() == ""


async def test_dev_identity_drives_the_tools_with_no_session_at_all():
    """End-to-end through the REAL TableAuthorizer: a headerless (dev-mode)
    call resolves the configured dev identity, and the envelope's
    detail.email proves WHOSE authorization was consulted."""
    auth = TableAuthorizer(
        None, None, mode="gate", dev_identity="dev@example.org"
    )
    mcp = _register_real(auth, session_email=lambda: "")
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    resp = await tools["epicor_tables"](query="jobs")
    # No menu authorizer/index behind it -> UNAVAILABLE, which fails closed;
    # what this pins is the IDENTITY the failure is attributed to.
    assert resp["error"] == "authorization_unavailable"
    assert resp["detail"]["email"] == "dev@example.org"
    assert resp["terminal"] is False


# --------------------------------------------------------------------------- #
# 6 (message half) — the unavailable wording has ONE source
# --------------------------------------------------------------------------- #
def test_the_unavailable_guidance_is_shared_not_forked():
    """The cold-user honesty rewording must reach BOTH surfaces from one
    constant: the discovery envelope and epicor_query's scope-gate envelope."""
    from epicor_mcp.sql.scope_gate import (
        AUTHZ_UNAVAILABLE_GUIDANCE,
        unavailable_envelope,
    )
    from epicor_mcp.discovery.tools import _authz_unavailable

    assert "minute" in AUTHZ_UNAVAILABLE_GUIDANCE
    assert "background" in AUTHZ_UNAVAILABLE_GUIDANCE
    assert "usually transient" not in AUTHZ_UNAVAILABLE_GUIDANCE, (
        "the old claim was false for minutes on a cold scoped user"
    )
    scope = AuthzScope.unavailable("u@x", "snapshot failed: TimeoutError")
    disco = _authz_unavailable(scope, retry_with={"query": "jobs"})
    query = unavailable_envelope("snapshot failed: TimeoutError", sql="select 1")
    for env in (disco, query):
        assert AUTHZ_UNAVAILABLE_GUIDANCE in env["message"]
        assert env["terminal"] is False


# --------------------------------------------------------------------------- #
# 5 — the connect-time prime
# --------------------------------------------------------------------------- #
# create_app harness — same shape as tests/test_authz_wiring.py: fake authz
# client, tmp menu db, no discovery index, real users.json via the data tree.
from fastapi.testclient import TestClient  # noqa: E402

import epicor_mcp.server as server  # noqa: E402
from epicor_mcp.auth.oauth import ValidatedToken  # noqa: E402
from epicor_mcp.config import Settings  # noqa: E402
from epicor_mcp.discovery import DiscoveryIndex  # noqa: E402

import fixtures.authz as fa  # noqa: E402
from fixtures.authz.fakes import ident, mrow, srow  # noqa: E402

_MENUS = [mrow("AP0100", sec_code="APSEC", program="Erp.UI.APInvoiceEntry")]
_SECURITY = [srow("APSEC", entry_list="APP")]


class _FakeAuthzClient:
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
    monkeypatch.setattr(DiscoveryIndex, "load", classmethod(lambda cls, root: None))
    db = fa.build_menu_security_db(tmp_path / "menu_security.db")
    from tests.fixtures.oss_server import server_settings
    settings = server_settings(tmp_path,
        menu_authz_mode="shadow",
        table_authz_mode="gate",
        admin_secret="test-secret",
        vector_search_enabled=False,
        forum_live_enabled=False,
        audit_log_path=tmp_path / "audit.db",
        menu_map_db_path=db,
    )
    return server.create_app(settings)


class _PrimeRecorder:
    """Stands in on app.state.table_authorizer — the prime must read it at
    REQUEST time (the /admin seam), which is what makes this substitutable."""

    def __init__(self, mode: str = "gate") -> None:
        self.mode = mode
        self.calls: list[str] = []

    def resolve_identity(self, session_email: str = "") -> str:
        return (session_email or "").strip()

    async def scope_for(self, email: str) -> AuthzScope:
        self.calls.append(email)
        return AuthzScope.unlimited(email, "test")


def _wire_token(app, email: str):
    """Make the middleware's validator accept any bearer as *email*. The
    middleware closes over the SAME object app.state holds, so an instance
    patch reaches it."""

    async def _fake_validate(token: str) -> ValidatedToken:
        return ValidatedToken(user_id=email, claims={"preferred_username": email})

    app.state.token_validator.validate_token = _fake_validate


def _settle(client, rec, timeout_s: float = 3.0) -> None:
    """The prime is fire-and-forget on the portal loop; give it cycles."""
    deadline = time.monotonic() + timeout_s
    while not rec.calls and time.monotonic() < deadline:
        time.sleep(0.01)


class TestConnectTimePrime:
    def _first_user_email(self, app):
        first = app.state.user_map.get_first_user()
        if first is None:
            pytest.fail("synthetic user fixture was not loaded")
        return first.user_id

    def test_prime_fires_on_token_validation_with_the_session_email(
        self, tmp_path, monkeypatch
    ):
        app = _build_app(tmp_path, monkeypatch)
        email = self._first_user_email(app)
        _wire_token(app, email)
        rec = _PrimeRecorder(mode="gate")
        app.state.table_authorizer = rec
        with TestClient(app) as c:
            r = c.get("/prime-probe", headers={"Authorization": "Bearer x"})
            assert r.status_code == 404  # unknown path; the middleware ran
            _settle(c, rec)
        assert rec.calls == [email], (
            "a validated bearer token must fire exactly one background "
            "scope_for prime for the session identity"
        )

    def test_prime_does_not_fire_outside_gate_mode(self, tmp_path, monkeypatch):
        app = _build_app(tmp_path, monkeypatch)
        email = self._first_user_email(app)
        _wire_token(app, email)
        rec = _PrimeRecorder(mode="boost")
        app.state.table_authorizer = rec
        with TestClient(app) as c:
            r = c.get("/prime-probe", headers={"Authorization": "Bearer x"})
            assert r.status_code == 404
            time.sleep(0.2)  # give a wrongly-scheduled task time to surface
        assert rec.calls == [], (
            "boost/off never refuse on a cold scope — a background crawl "
            "there is pure load, so the prime is gate-only"
        )

    def test_a_prime_failure_never_touches_the_request(self, tmp_path, monkeypatch):
        app = _build_app(tmp_path, monkeypatch)
        email = self._first_user_email(app)
        _wire_token(app, email)

        class _Exploding(_PrimeRecorder):
            def resolve_identity(self, session_email: str = "") -> str:
                raise RuntimeError("scheduling boom")

        app.state.table_authorizer = _Exploding(mode="gate")
        with TestClient(app) as c:
            r = c.get("/prime-probe", headers={"Authorization": "Bearer x"})
        assert r.status_code == 404, "a prime failure must be invisible to the request"

    def test_an_async_prime_failure_is_swallowed_too(self, tmp_path, monkeypatch):
        app = _build_app(tmp_path, monkeypatch)
        email = self._first_user_email(app)
        _wire_token(app, email)

        class _AsyncExploding(_PrimeRecorder):
            async def scope_for(self, email: str) -> AuthzScope:
                self.calls.append(email)
                raise RuntimeError("prime boom")

        rec = _AsyncExploding(mode="gate")
        app.state.table_authorizer = rec
        with TestClient(app) as c:
            r = c.get("/prime-probe", headers={"Authorization": "Bearer x"})
            assert r.status_code == 404
            _settle(c, rec)
            # A follow-up request must be unaffected by the failed task.
            assert c.get("/health").status_code == 200
        assert rec.calls == [email]


# --------------------------------------------------------------------------- #
# 2b — the SESSION itself must not be caller-selectable
# --------------------------------------------------------------------------- #
def test_a_foreign_mcp_session_id_cannot_borrow_another_identity(
    tmp_path, monkeypatch
):
    """Removing the `for_email` ARGUMENT closed one hole; `mcp-session-id` was
    the same hole through the front door.

    The header is caller-supplied and the store's default key is the bare
    e-mail, so `get_session(header)` would hand a validated bearer whoever's
    session id it guessed — and every session-scoped decision, the table-authz
    gate included (`WedgeRuntime.run` resolves `session.user_id`), would then
    run as that person. The middleware must fall back to the CALLER's own key
    and leave the foreign session untouched.
    """
    app = _build_app(tmp_path, monkeypatch)
    first = app.state.user_map.get_first_user()
    if first is None:
        pytest.fail("synthetic user fixture was not loaded")
    caller = first.user_id
    victim = "victim@example.org"

    from epicor_mcp.auth.session import MCPSession

    app.state.session_store._sessions[victim] = MCPSession(
        session_id=victim,
        user_id=victim,
        department="payroll",
        access_level="read_write",
        environment="live",
    )
    _wire_token(app, caller)
    app.state.table_authorizer = _PrimeRecorder(mode="gate")

    with TestClient(app) as c:
        assert c.get(
            "/prime-probe",
            headers={"Authorization": "Bearer x", "mcp-session-id": victim},
        ).status_code == 404

    borrowed = app.state.session_store._sessions[victim]
    assert borrowed.user_id == victim, "the foreign session was re-pointed"
    assert borrowed.access_level == "read_write", (
        "the caller's profile overwrote the victim's session in place"
    )
    own = app.state.session_store.get_session(caller)
    assert own is not None and own.user_id == caller, (
        "the caller must be served their OWN session, keyed by their own id"
    )

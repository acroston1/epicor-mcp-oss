"""Regression coverage: test wedge session attribution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from epicor_mcp.context import (
    clear_current_session,
    get_current_session_or_none,
    set_current_session,
)
from epicor_mcp.sql.governor import CostGovernor, GovernorPolicy


@dataclass
class _Session:
    """Duck-typed MCPSession: `run` reads `.user_id` and nothing else."""

    user_id: str


class _Runtime:
    """The identity half of ``WedgeRuntime.run``, isolated from HTTP + Epicor."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def run(self) -> str:
        session = get_current_session_or_none()
        session_id = getattr(session, "user_id", None) or "anonymous"
        self.seen.append(session_id)
        return session_id


def test_the_runtime_charges_the_authenticated_user_not_anonymous():
    rt = _Runtime()
    token = set_current_session(_Session("adminuser@example.org"))  # type: ignore[arg-type]
    try:
        assert asyncio.run(rt.run()) == "adminuser@example.org"
    finally:
        clear_current_session(token)
    # ...and with no session it is still `anonymous`, which is what an
    # unauthenticated path SHOULD look like — the wedge just must not use it for
    # authenticated callers.
    assert asyncio.run(rt.run()) == "anonymous"


def test_the_auth_middleware_sets_and_clears_the_session(tmp_path, monkeypatch):
    """The real middleware, driven with a stub validator — no network, no token.

    Asserted on the recorded set/clear calls rather than on MCP internals: the
    property that matters is that the identity reaches the context and does NOT
    leak past the response, because a leaked contextvar attributes the NEXT
    caller's query to this user.
    """
    from fastapi.testclient import TestClient

    from epicor_mcp import server as wedge_server
    from epicor_mcp.auth import oauth as oauth_mod
    from epicor_mcp.auth.oauth import ValidatedToken
    from epicor_mcp.config import Settings

    class _Validator:
        def __init__(self, settings):  # noqa: D107
            self.settings = settings

        async def validate_token(self, token: str) -> ValidatedToken:
            if token != "good":
                from jose import JWTError

                raise JWTError("bad token")
            return ValidatedToken(
                user_id="apuser@example.org",
                claims={"preferred_username": "apuser@example.org"},
            )

    from tests.fixtures.oss_server import server_settings
    from tests.test_admin_authz_endpoints import _FakeAuthzClient
    monkeypatch.setattr(wedge_server, "EpicorAuthzClient", _FakeAuthzClient)
    settings = server_settings(tmp_path, table_authz_mode="off")
    real_cls = wedge_server.AzureADTokenValidator
    real_set, real_clear = wedge_server.set_current_session, wedge_server.clear_current_session
    events: list[tuple[str, str]] = []

    def _spy_set(session):
        events.append(("set", session.user_id))
        return real_set(session)

    def _spy_clear(tok=None):
        events.append(("clear", ""))
        return real_clear(tok)

    wedge_server.AzureADTokenValidator = _Validator  # type: ignore[assignment]
    wedge_server.set_current_session = _spy_set  # type: ignore[assignment]
    wedge_server.clear_current_session = _spy_clear  # type: ignore[assignment]
    try:
        app = wedge_server.create_app(settings)
        with TestClient(app) as client:
            # No token: 401, and no session is ever created.
            assert client.get("/mcp").status_code == 401
            assert events == []
            # Bad token: 401, still no session.
            client.get("/mcp", headers={"Authorization": "Bearer nope"})
            assert events == []
            # Good token: the session is set for the request and cleared after.
            client.get("/mcp", headers={"Authorization": "Bearer good"})
            assert events[0] == ("set", "apuser@example.org")
            assert events[-1] == ("clear", "")
        # ...and nothing leaked into this test's own context.
        assert get_current_session_or_none() is None
    finally:
        wedge_server.AzureADTokenValidator = real_cls  # type: ignore[assignment]
        wedge_server.set_current_session = real_set  # type: ignore[assignment]
        wedge_server.clear_current_session = real_clear  # type: ignore[assignment]


def test_two_users_do_not_share_one_budget_bucket():
    """Why A5 is a cost finding and not only an audit one."""
    gov = CostGovernor(GovernorPolicy(session_budget_s=10.0, session_budget_window_s=60.0))
    now = 1000.0
    gov.record("alice@example.org", 12.0, now=now)
    assert gov.check_budget("alice@example.org", now=now) is not None
    assert gov.check_budget("bob@example.org", now=now) is None
    # ...which is exactly the isolation `session_id="anonymous"` destroyed.
    gov2 = CostGovernor(GovernorPolicy(session_budget_s=10.0, session_budget_window_s=60.0))
    gov2.record("anonymous", 12.0, now=now)
    assert gov2.check_budget("anonymous", now=now) is not None

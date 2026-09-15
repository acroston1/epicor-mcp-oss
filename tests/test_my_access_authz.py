"""epicor_my_access surfaces the menu-derived reasoning chain.

The tool is a closure registered on the MCP server, so we capture it with a fake
server and drive it directly: fake rbac exposing ``_authorizer`` + ``_mode``, a
fake authorizer whose ``explain()`` returns a canned chain, a session in the
request contextvar. Asserts my_access nests the chain under
``menu_authorization``, sets ``authorization_source`` per mode, and demotes
departments to informational — without booting a server or touching Epicor.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from epicor_mcp.context import clear_current_session, set_current_session
from epicor_mcp.tools import my_access

from fixtures.authz.fakes import FakeIndex, FakeUserMap, profile

EMAIL = "apuser@example.org"

_CHAIN = {
    "epicor_user_id": "apuser",
    "groups": ["APP"],
    "security_mgr": False,
    "stale": False,
    "allowed_service_count": 4,
    "allowed_menu_count": 2,
    "services": [
        {
            "service_id": "Erp.BO.APInvoiceSvc",
            "granted_by": [
                {
                    "menu_id": "AP0100",
                    "menu_desc": "AP Invoice Entry",
                    "sec_code": "APSEC",
                    "matched_principals": ["APP"],
                }
            ],
        }
    ],
}


class _FakeAuthorizer:
    def __init__(self, chain):
        self._chain = chain
        self._snap: dict = {}

    def get_snapshot(self, uid):
        return self._snap.get(uid)

    async def ensure_snapshot(self, uid):
        self._snap[uid] = object()
        return self._snap[uid]

    def explain(self, uid):
        return self._chain


class _CaptureServer:
    """Captures the @server.tool()-decorated function and fakes the tool list."""

    def __init__(self):
        self.fn = None
        self._tool_manager = SimpleNamespace(
            list_tools=lambda: [SimpleNamespace(name="epicor_my_access")]
        )

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.fn = fn
            return fn
        return deco


def _make_tool(mode: str, *, authorizer=None):
    srv = _CaptureServer()
    user_map = FakeUserMap({EMAIL: profile("apuser", department="Finance")})
    rbac = SimpleNamespace(_user_map=user_map, _authorizer=authorizer, _mode=mode)
    index = FakeIndex(dept_services={"Finance": set()})
    my_access.register(srv, index, rbac, client=None)
    return srv.fn


async def _call(fn):
    token = set_current_session(SimpleNamespace(user_id=EMAIL))
    try:
        raw = await fn()
    finally:
        clear_current_session(token)
    return json.loads(raw)


async def test_shadow_mode_nests_menu_chain_and_marks_departments_informational():
    fn = _make_tool("shadow", authorizer=_FakeAuthorizer(_CHAIN))
    result = await _call(fn)
    assert result["menu_authorization"]["epicor_user_id"] == "apuser"
    assert result["menu_authorization"]["allowed_service_count"] == 4
    assert result["authorization_source"].startswith("shadow")
    assert "informational" in result["departments_note"].lower()


async def test_enforce_mode_authorization_source_is_menu():
    fn = _make_tool("enforce", authorizer=_FakeAuthorizer(_CHAIN))
    result = await _call(fn)
    assert result["authorization_source"] == "menu"
    assert result["menu_authorization"]["services"][0]["service_id"] == "Erp.BO.APInvoiceSvc"


async def test_off_mode_has_no_menu_chain():
    fn = _make_tool("off", authorizer=None)
    result = await _call(fn)
    assert "menu_authorization" not in result
    assert result["authorization_source"] == "department"


async def test_shadow_mode_primes_snapshot_when_absent():
    authz = _FakeAuthorizer(_CHAIN)
    fn = _make_tool("shadow", authorizer=authz)
    await _call(fn)
    # explain() was reachable only because ensure_snapshot primed the cache.
    assert authz.get_snapshot(EMAIL) is not None

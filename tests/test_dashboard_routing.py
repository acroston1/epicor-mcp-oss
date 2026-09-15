"""Unit tests for dashboard intent-routing in ``epicor_baq`` + the engine.

Covers dashboard-routing regressions with mocked Epicor calls:

* ``_do_run`` AUTO-ROUTES a dashboard-shaped ``baq`` (has whitespace, or
  contains the word "dashboard") to the dashboard engine instead of returning
  ``baq_not_found`` — for example
  ``run baq='Sample Sales Overview dashboard'``.
* a normal single-token BAQ id is NOT re-routed.
* ``_do_dashboard`` no longer errors on an empty ``baq`` — it passes through so
  the engine can LIST dashboards (discovery); ``dashboard_fn=None`` is still a
  terminal ``dashboard_unavailable``.
* ``_is_list_request`` / ``_list_dashboards`` discovery helpers.
* the engine's Step-0 list branch returns ``mode='dashboard_list'``.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.context import clear_current_session, set_current_session
from epicor_mcp.tools import dashboard_baq as _dash
from epicor_mcp.tools.baq import (
    _capture_dashboard_fn,
    _do_dashboard,
    _do_find,
    _do_run,
)
from epicor_mcp.tools.dashboard_baq import _is_list_request, _list_dashboards

_SESSION = types.SimpleNamespace(user_id="tester")


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# A recording stub for the captured dashboard engine (dashboard_fn).
# --------------------------------------------------------------------------- #

class _DashSpy:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, *, dashboard, filter="", top=25, execute=True):
        self.calls.append({"dashboard": dashboard, "filter": filter,
                           "top": top, "execute": execute})
        return json.dumps({"mode": "dashboard", "dashboard": dashboard})


# --------------------------------------------------------------------------- #
# _do_run auto-route
# --------------------------------------------------------------------------- #

class _RunClient:
    """Minimal BaqSvc fake so a NON-routed run has somewhere to land."""

    def __init__(self):
        self.gets: list[str] = []

    async def get(self, url, api_key, params=None):
        self.gets.append(url)
        return {"value": [{"OrderDtl_PartNum": "WIDGET-100"}]}


class _RunRBAC:
    def check_baq_access(self, user_id):
        return types.SimpleNamespace(allowed=True, api_key="BAQKEY", message="")


def _run_kwargs(**over):
    kw = dict(session=_SESSION, rbac=_RunRBAC(), client=_RunClient(),
              baq="DEMO-SalesForecast", where="", limit=50, cursor="", params=None,
              dashboard_fn=_DashSpy())
    kw.update(over)
    return kw


@pytest.mark.parametrize("name,expected_dash", [
    ("Sample Sales Overview dashboard", "Sample Sales Overview"),
    ("Sample Sales Overview", "Sample Sales Overview"),  # spaced only
    ("Open Backlog Dashboard", "Open Backlog"),
    ("dashboard", ""),                                           # word only -> list
])
def test_run_autoroutes_dashboard_shaped_name(name, expected_dash):
    spy = _DashSpy()
    out = _run(_do_run(**_run_kwargs(baq=name, dashboard_fn=spy)))
    assert spy.calls, "dashboard engine should have been invoked"
    assert spy.calls[0]["dashboard"] == expected_dash
    assert json.loads(out)["mode"] == "dashboard"


def test_run_does_not_route_a_real_baq_id():
    spy = _DashSpy()
    client = _RunClient()
    out = _run(_do_run(**_run_kwargs(baq="DEMO-SalesForecast", client=client,
                                     dashboard_fn=spy)))
    assert not spy.calls, "single-token BAQ id must NOT route to dashboard"
    assert client.gets and "BaqSvc/DEMO-SalesForecast/Data" in client.gets[0]
    assert "records" in out or "record_count" in out


def test_run_without_dashboard_fn_does_not_route():
    # Guarded: no engine available -> a spaced name falls through to the run
    # path (which will fail as a BAQ), never a crash on the routing branch.
    client = _RunClient()
    _run(_do_run(**_run_kwargs(baq="Some Dashboard Name", client=client,
                               dashboard_fn=None)))
    assert client.gets, "should have attempted the normal run path"


# --------------------------------------------------------------------------- #
# _do_dashboard pass-through / discovery
# --------------------------------------------------------------------------- #

def test_dashboard_empty_name_passes_through_to_list():
    spy = _DashSpy()
    out = _run(_do_dashboard(dashboard_fn=spy, baq="", where="", limit=25))
    assert spy.calls and spy.calls[0]["dashboard"] == ""
    assert json.loads(out)["mode"] == "dashboard"


def test_dashboard_unavailable_is_terminal():
    out = _run(_do_dashboard(dashboard_fn=None, baq="OpenBacklog",
                             where="", limit=25))
    env = json.loads(out)
    assert env["error"] == "dashboard_unavailable"
    assert env["terminal"] is True


# --------------------------------------------------------------------------- #
# discovery helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,expected", [
    ("", True),
    ("   ", True),
    ("a dashboard", True),
    ("which dashboards can I see", True),
    ("go to a dashboard", True),
    ("list", True),
    ("Sample Sales Overview", False),
    ("OpenBacklog", False),
    ("the sales dashboard", False),   # 'sales' is a real token
])
def test_is_list_request(name, expected):
    assert _is_list_request(name) is expected


# --------------------------------------------------------------------------- #
# find -> dashboard redirect (mis-routed dashboard ask)
# --------------------------------------------------------------------------- #

class _EmptyIndex:
    def search_tables(self, *a, **k):
        return []


@pytest.mark.parametrize("query,expected_dash", [
    ("dashboard", ""),
    ("Sample Sales Overview dashboard", "Sample Sales Overview"),
    ("Open backlog dashboards", "Open backlog"),
])
def test_find_redirects_dashboard_intent(query, expected_dash):
    out = _do_find(baq_index=_EmptyIndex(), baq=query, limit=10)  # sync
    env = json.loads(out)
    assert env["error"] == "use_dashboard_action"
    assert env["retry_with"] == {"action": "dashboard", "baq": expected_dash}


def test_find_without_dashboard_word_is_not_redirected():
    # A normal table search must NOT be hijacked by the dashboard redirect —
    # whether authoring is gated (baq_find_disabled) or runs (empty results),
    # the error is never use_dashboard_action.
    out = _do_find(baq_index=_EmptyIndex(), baq="LaborDtl", limit=10)
    assert json.loads(out).get("error") != "use_dashboard_action"


class _ListClient:
    """Fake DashBoardSvc/GetList."""

    def __init__(self, rows):
        self._rows = rows
        self._base_url = "https://host/"
        self.calls: list[dict] = []

    async def call_method(self, base_url, service, method, api_key, params=None):
        self.calls.append({"service": service, "method": method,
                           "params": params or {}})
        return {"returnObj": {"DashBdDefList": self._rows}}


_DEF_ROWS = [
    {"DefinitionID": "ZReport", "Description": "Z Report"},
    {"DefinitionID": "OpenBacklog", "Description": "Open Backlog"},
    {"DefinitionID": "OpenBacklog", "Description": "Open Backlog"},   # dup
    {"DefinitionID": "", "Description": "nameless"},          # dropped
]


def test_list_dashboards_dedups_and_sorts():
    client = _ListClient(_DEF_ROWS)
    items = _run(_list_dashboards(client, "KEY"))
    assert [d["id"] for d in items] == ["OpenBacklog", "ZReport"]  # sorted by desc
    # GetList called once with an empty whereClause (pull all definitions).
    assert client.calls[0]["method"] == "GetList"
    assert client.calls[0]["params"].get("whereClause") == ""


# --------------------------------------------------------------------------- #
# engine Step-0 list branch (captured real coroutine)
# --------------------------------------------------------------------------- #

class _EngineRBAC:
    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(allowed=True, api_key="SVCKEY", message="")

    def check_baq_access(self, user_id):
        return types.SimpleNamespace(allowed=True, api_key="BAQKEY", message="")


def test_engine_lists_dashboards_on_generic_name():
    client = _ListClient(_DEF_ROWS)
    fn = _capture_dashboard_fn(index=None, rbac=_EngineRBAC(), client=client)
    assert fn is not None
    token = set_current_session(_SESSION)
    try:
        out = _run(fn(dashboard="which dashboards", filter="", top=25,
                      execute=True))
    finally:
        clear_current_session(token)
    env = json.loads(out)
    assert env["mode"] == "dashboard_list"
    assert env["count"] == 2
    assert {d["id"] for d in env["dashboards"]} == {"OpenBacklog", "ZReport"}

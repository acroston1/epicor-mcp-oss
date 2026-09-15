"""Regressions for the adversarial-review findings on the never-silently-drop
change set.

Every test here pins a case where the tool produced (or was one edit away from
producing) an answer that LOOKED correct: a sort the engine refused but the
response claimed, a threshold that was ignored while every group was returned,
an INV-1 envelope whose ``valid.columns`` was empty. Calls-to-answer is the
metric — a silent wrong result makes the model hunt, a precise error converges
it in one hop.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.tools._aggregate import aggregate_records, resolve_sort_terms
from epicor_mcp.tools._resolve import column_help


# --------------------------------------------------------------------------- #
# Findings 7 / 9 — rollup order_by silently discarded, default rank presented
# --------------------------------------------------------------------------- #

_RECS = [{"Cust": "A", "Qty": 1}, {"Cust": "B", "Qty": 9}]


@pytest.mark.parametrize("clause", ["TotalQty asc", "totalqty asc", "[TotalQty] asc"])
def test_rollup_sort_matches_alias_case_and_bracket_insensitively(clause):
    """`totalqty asc` used to return the exact INVERSE of the request.

    The membership test was case-sensitive with no else-branch, so an alias
    typed in a different case fell through to the historic
    first-aggregate-DESC default, silently.
    """
    out = aggregate_records(
        _RECS, group_by="Cust", aggregate="sum(Qty) as TotalQty", order_by=clause)
    assert [r["Cust"] for r in out["records"]] == ["A", "B"]
    assert out["order_applied"] is True
    assert "order_warning" not in out


def test_rollup_sort_on_unknown_key_warns_loudly_instead_of_silently_ranking():
    out = aggregate_records(
        _RECS, group_by="Cust", aggregate="sum(Qty) as TotalQty",
        order_by="ScrapQty desc")
    assert out["order_applied"] is False
    warn = out["order_warning"]
    # Must name the offending key AND the keys that WOULD work.
    assert "ScrapQty" in warn and "TotalQty" in warn and "Cust" in warn
    assert "NOT the ordering you asked for" in warn


def test_one_bad_term_does_not_silently_kill_the_whole_clause():
    out = aggregate_records(
        _RECS, group_by="Cust", aggregate="sum(Qty) as q", order_by="Cust asc, Bogus desc")
    assert out["order_applied"] is False and "Bogus" in out["order_warning"]


def test_resolve_sort_terms_reports_unresolvable_names():
    ok, bad = resolve_sort_terms([("totalqty", "asc"), ("Nope", "desc")], ["TotalQty"])
    assert ok == [("TotalQty", "asc")] and bad == ["Nope"]


# --------------------------------------------------------------------------- #
# Finding 6 — column_help called with the wrong positional order
# --------------------------------------------------------------------------- #

def test_column_help_envelope_actually_carries_valid_columns():
    """The order_by path passed (index, service, entity_set, bad), binding a
    STRING to valid_columns. It was iterated as characters, so valid.columns
    came back EMPTY under a message saying 'use a name from valid.columns' —
    leaving the caller unable to use the suggested recovery."""
    help_ = column_help("Erp.BO.ReqSvc", "ReqHead", ["UnitCost", "PartNum"], ["UnitCst"])
    assert help_["columns"] == ["UnitCost"]
    assert help_["did_you_mean"]["UnitCst"] == ["UnitCost"]
    assert help_["total_columns"] == 2


# --------------------------------------------------------------------------- #
# Findings 1 / 2 — order_by and having dropped on the parent/child join branch,
# which is the MANDATORY route for every table in _CHILD_TO_PARENT_PIVOT.
# --------------------------------------------------------------------------- #

def test_join_engine_accepts_order_by_and_having():
    """Signature-level pin: the engine used to have NO order/having parameter
    at all, so read.py could not have passed them even if it wanted to."""
    import inspect

    from epicor_mcp.tools import query_with_children as qwc

    captured: dict = {}

    class _Shim:
        def tool(self, *a, **k):
            def deco(fn):
                captured["fn"] = fn
                return fn
            return deco

    class _Stub:
        def get_entity_sets(self, s): return []
        def get_fields(self, s, e): return []

    qwc.register(_Shim(), _Stub(), object(), object())
    params = inspect.signature(captured["fn"]).parameters
    assert "order_by" in params and "having" in params


def test_read_join_branch_forwards_order_by_and_having():
    """read.py's join branch returns long before its own order block, so the
    only way a caller sort survives is being handed to the join engine."""
    import inspect

    src = inspect.getsource(
        __import__("epicor_mcp.tools.read", fromlist=["read"]))
    call = src[src.index("raw = await _children_join("):]
    call = call[:call.index(")\n")]
    assert "having=having" in call
    assert "order_by=order_by" in call
    # A caller sort must also disable the early stop, or it ranks an arbitrary
    # prefix and calls it a top-N.
    assert "order_by.strip()" in call


def test_aggregated_join_applies_having_not_just_reports_it():
    """The join called aggregate_records WITHOUT having=, so every group came
    back as if it were the thresholded answer."""
    out = aggregate_records(
        [{"P": "X", "Q": 200}, {"P": "Y", "Q": 5}],
        group_by="P", aggregate="sum(Q) as q", having="q > 100")
    assert [r["P"] for r in out["records"]] == ["X"]
    assert "having_warning" not in out


# --------------------------------------------------------------------------- #
# Finding 10 — nothing asserted the guard is actually wired into the server
# --------------------------------------------------------------------------- #

def test_validation_guard_is_installed_by_the_real_server_factory():
    """Deleting install_validation_guard(mcp) from server.py must fail a test:
    without the guard every unknown argument is silently dropped again."""
    import inspect

    from epicor_mcp import server as server_mod

    src = inspect.getsource(server_mod._create_mcp_server)
    assert "install_validation_guard(mcp)" in src
    # Install ORDER is load-bearing: the guard must precede the audit hook so
    # the audit wrapper sits OUTSIDE it and these rejections are classified,
    # logged and counted by the circuit breaker.
    assert src.index("install_validation_guard(mcp)") < src.index("install_audit_hook")


def test_guard_replaces_tool_manager_call_tool():
    from mcp.server.fastmcp import FastMCP

    from epicor_mcp.tools._argguard import install_validation_guard

    mcp = FastMCP("t")
    before = mcp._tool_manager.call_tool
    install_validation_guard(mcp)
    assert mcp._tool_manager.call_tool is not before
    assert mcp._tool_manager.call_tool.__name__ == "guarded_call_tool"


# --------------------------------------------------------------------------- #
# Findings 4 / 5 / 8 — an alias applied with no trace is a silent REWRITE
# --------------------------------------------------------------------------- #

def test_arg_notes_are_injected_into_every_tool_response():
    """get_arg_notes() had exactly ONE consumer (read.py), so epicor_baq,
    epicor_act, epicor_time_phase, epicor_help and epicor_find_person rewrote
    arguments invisibly — including 'table'->'tables' under action='create',
    which changes what is persisted to LIVE Epicor."""
    from epicor_mcp.tools._argguard import _inject_notes

    notes = {"arg_aliased": {"select": "fields"}}
    folded = json.loads(_inject_notes(json.dumps({"records": []}), notes))
    assert folded["arg_notes"]["arg_aliased"] == {"select": "fields"}


def test_inject_notes_never_mangles_a_non_json_or_non_object_payload():
    from epicor_mcp.tools._argguard import _inject_notes

    notes = {"arg_aliased": {"a": "b"}}
    assert _inject_notes("not json at all", notes) == "not json at all"
    assert _inject_notes("[1, 2, 3]", notes) == "[1, 2, 3]"
    assert _inject_notes("x", {}) == "x"


def test_typo_correction_on_the_write_tool_is_reported():
    """Regression coverage: test typo correction on the write tool is reported."""
    from epicor_mcp.tools._argguard import _inject_notes, _screen_arguments

    decl = {"action": "string", "target": "string", "environment": "string"}
    args, notes, env = _screen_arguments(
        "epicor_act",
        {"action": "update", "target": "Part", "environmnet": "live"}, decl)
    assert env is None
    assert args["environment"] == "live"
    assert notes["arg_typo_fixed"] == {"environmnet": "environment"}
    # ...and it must now reach the model.
    folded = json.loads(_inject_notes(json.dumps({"ok": True}), notes))
    assert folded["arg_notes"]["arg_typo_fixed"] == {"environmnet": "environment"}


# --------------------------------------------------------------------------- #
# Common aliases models send, each pinned by a test
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool,decl,given,want", [
    ("epicor_time_phase", {"part": "string", "plant": "string", "limit": "integer"},
     {"part_num": "A1"}, {"part": "A1"}),
    ("epicor_time_phase", {"part": "string", "plant": "string", "limit": "integer"},
     {"part": "A1", "top": 5}, {"part": "A1", "limit": 5}),
    ("epicor_help", {"query": "string", "source": "string", "limit": "integer"},
     {"query": "q", "max_results": 3}, {"query": "q", "limit": 3}),
])
def test_common_aliases_are_applied_and_recorded(tool, decl, given, want):
    from epicor_mcp.tools._argguard import _screen_arguments

    args, notes, env = _screen_arguments(tool, given, decl)
    assert env is None
    assert args == want
    assert notes["arg_aliased"]


def test_alias_targets_match_the_REAL_registered_tool_schemas():
    """The guard's alias tables were only ever checked against hand-copied
    parameter dicts in the tests. Rename a real parameter and every alias
    pointing at it degrades to a hard `unknown_arguments` rejection — the
    silent-drop fix quietly becoming a wall — with a green suite.

    _screen_arguments only aliases when `target in declared`, so this asserts
    against the schema FastMCP actually advertises.
    """
    from mcp.server.fastmcp import FastMCP

    from epicor_mcp.tools import baq as baq_mod
    from epicor_mcp.tools import read as read_mod
    from epicor_mcp.tools._argguard import _ARG_ALIASES, _declared_types
    from tests.test_read_routing_e2e import _Client, _Idx, _RBAC

    class _BaqIdx:
        def search(self, *a, **k): return []
        def get_baq(self, *a, **k): return None

    mcp = FastMCP("t")
    read_mod.register(mcp, _Idx({}), _RBAC(), _Client())
    baq_mod.register(mcp, _Idx({}), _RBAC(), _Client(), _BaqIdx())

    for tool in ("epicor_read", "epicor_baq"):
        declared = _declared_types(mcp, tool)
        assert declared, f"{tool} advertised no parameters"
        missing = {a: t for a, t in _ARG_aliases_for(tool).items()
                   if t not in declared}
        assert not missing, (
            f"{tool}: alias target(s) {missing} are no longer parameters "
            f"(declared: {sorted(declared)})")
        # An alias must never shadow a real parameter, or the caller's own
        # value would be overwritten by the alias.
        shadow = [a for a in _ARG_aliases_for(tool) if a in declared]
        assert not shadow, f"{tool}: alias name(s) {shadow} are real parameters"
    assert set(_ARG_ALIASES) >= {"epicor_read", "epicor_baq"}


def _ARG_aliases_for(tool: str) -> dict:
    from epicor_mcp.tools._argguard import _ARG_ALIASES

    return _ARG_ALIASES.get(tool, {})


def test_raw_sql_redirect_is_not_gated_on_create():
    """`query='SELECT ...'` on run/find is the same misconception; falling
    through to the generic unknown_arguments bucket taught the model nothing."""
    from epicor_mcp.tools._argguard import _baq_rejects

    for action in ("run", "find", "create", ""):
        env = _baq_rejects({"action": action, "query": "SELECT 1"})
        assert env["error"] == "raw_sql_unsupported"
        assert env["retry_with"]["action"] == (action or "create")


# --------------------------------------------------------------------------- #
# The unsupported raw-SQL guard must execute through the real FastMCP seam.
# --------------------------------------------------------------------------- #

def test_baq_raw_sql_is_short_circuited_through_the_installed_guard():
    """Proves the rejection is REACHABLE for epicor_baq at runtime: the real
    registered schema, the real name key, the real ToolManager.call_tool.

    An unsupported query argument must not be dropped before composing a
    different BAQ from the remaining arguments.
    """
    from mcp.server.fastmcp import FastMCP

    from epicor_mcp.tools import baq as baq_mod
    from epicor_mcp.tools._argguard import install_validation_guard

    class _Idx:
        def get_entity_sets(self, s): return []
        def get_fields(self, s, e): return []
        def search_services(self, *a, **k): return []

    class _RBAC:
        def check_access(self, *a, **k): return True, ""
        def check_service_access(self, *a, **k):
            return types.SimpleNamespace(allowed=True, api_key="k")

    class _BaqIdx:
        def search(self, *a, **k): return []
        def get_baq(self, *a, **k): return None

    calls: list = []

    class _Client:
        async def get(self, *a, **k):
            calls.append(("get", a, k))
            return {"value": []}

        async def post(self, *a, **k):
            calls.append(("post", a, k))
            return {}

    mcp = FastMCP("t")
    baq_mod.register(mcp, _Idx(), _RBAC(), _Client(), _BaqIdx())
    install_validation_guard(mcp)

    orig = baq_mod.get_current_session
    baq_mod.get_current_session = lambda: types.SimpleNamespace(user_id="t")
    try:
        out = asyncio.run(mcp._tool_manager.call_tool(
            "epicor_baq",
            {"action": "create", "baq": "Example_Inventory_Query",
             "description": "stale inventory",
             "query": "SELECT p.PartNum, (pw.OnHandQty * p.AvgCost) as V"},
            context=None))
    finally:
        baq_mod.get_current_session = orig

    env = json.loads(str(out) if isinstance(out, str) else out[0].text)
    assert env["error"] == "raw_sql_unsupported"
    # Names the SUPPORTED path, not merely "unknown argument" (INV-1).
    assert "tables" in env["message"] and "fields" in env["message"]
    assert env["retry_with"]["baq"] == "Example_Inventory_Query"
    # The decisive assertion: the create path was never entered.
    assert not [c for c in calls if c[0] == "post"]

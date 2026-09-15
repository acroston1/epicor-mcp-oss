"""The silent-argument-drop fix.

FastMCP builds its arg model with pydantic's default ``extra='ignore'`` and
``model_dump_one_level`` iterates DECLARED fields only, so an argument that is
not a parameter is parsed and thrown away with no error and no trace. A
dropped ``select`` discards the model's own column pruning, so wide entities
blow the whole inline budget; a dropped ``query='SELECT ...'`` on
``epicor_baq`` create loses the entire analytical intent while the tool
composes a DIFFERENT BAQ, persists it to Epicor, and reports success.

Rule under test: an unknown argument is either ALIASED (with the coercion
recorded) or REJECTED with an INV-1 envelope naming the supported path.
Silence is never an outcome.
"""

from __future__ import annotations

import json

import pytest

from epicor_mcp.tools._argguard import _ARG_ALIASES, _screen_arguments

READ_DECL = {
    "target": "string", "fields": "string", "where": "string",
    "children": "string", "group_by": "string", "aggregate": "string",
    "having": "string", "order_by": "string", "limit": "integer",
    "top": "integer", "count_only": "boolean", "cursor": "string",
}
BAQ_DECL = {
    "action": "string", "baq": "string", "tables": "string", "fields": "string",
    "where": "string", "description": "string", "order_by": "string",
    "limit": "integer", "cursor": "string", "params": "object",
}
FIND_DECL = {"search_name": "string", "top": "integer"}
HELP_DECL = {"query": "string", "source": "string", "limit": "integer"}


# --------------------------------------------------------------------------- #
# The mechanism this whole change exists for
# --------------------------------------------------------------------------- #

def test_fastmcp_arg_model_really_discards_extras():
    """Locks in WHY the guard must sit ABOVE pydantic validation.

    If a future `mcp` upgrade flips the arg model to extra='forbid'/'allow',
    this fails loudly instead of the behavior drifting silently.
    """
    from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
    from pydantic import Field, create_model

    model = create_model(
        "T", __base__=ArgModelBase,
        target=(str, Field(default="")), fields=(str, Field(default="")),
    )
    inst = model.model_validate({"target": "Part", "select": "PartNum"})
    assert inst.model_extra in (None, {})
    assert "select" not in inst.model_dump_one_level()


# --------------------------------------------------------------------------- #
# Aliasing
# --------------------------------------------------------------------------- #

def test_read_select_aliases_to_fields():
    """`select` IS the OData projection — a rename, not a guess."""
    out, notes, env = _screen_arguments(
        "epicor_read", {"target": "Part", "select": "PartNum"}, READ_DECL)
    assert env is None
    assert out == {"target": "Part", "fields": "PartNum"}
    assert notes["arg_aliased"] == {"select": "fields"}


def test_read_empty_fields_is_absent_not_a_choice():
    """The common case: `fields=""` must not beat `select`."""
    out, notes, env = _screen_arguments(
        "epicor_read", {"target": "Part", "fields": "", "select": "PartNum"},
        READ_DECL)
    assert env is None
    assert out["fields"] == "PartNum"


def test_declared_parameter_wins_over_its_alias():
    """The model already supplied the right parameter — erroring costs a hop
    for zero information. But the drop is RECORDED."""
    out, notes, env = _screen_arguments(
        "epicor_read",
        {"target": "Part", "fields": "PartNum", "select": "PartDescription"},
        READ_DECL)
    assert env is None
    assert out["fields"] == "PartNum"
    assert "select" in notes["arg_ignored"]


@pytest.mark.parametrize("src,dst", [
    ("top", "limit"), ("select", "fields"), ("table", "tables"),
    ("baq_id", "baq"), ("baq_name", "baq"), ("query_name", "baq"),
    ("name", "baq"), ("id", "baq"), ("dashboard_id", "baq"),
])
def test_baq_aliases(src, dst):
    out, notes, env = _screen_arguments(
        "epicor_baq", {"action": "run", src: "X"}, BAQ_DECL)
    assert env is None, env
    assert out[dst] == "X"
    assert notes["arg_aliased"][src] == dst


def test_find_person_query_aliases_but_baq_query_is_rejected():
    """One test, three tools: proves the table is PER-TOOL, not global.

    `query` is a search term on find_person, the DECLARED parameter on
    epicor_help, and a hard reject on epicor_baq create.
    """
    out, _n, env = _screen_arguments(
        "epicor_find_person", {"query": "morgan"}, FIND_DECL)
    assert env is None and out["search_name"] == "morgan"

    out, notes, env = _screen_arguments(
        "epicor_help", {"query": "how do I"}, HELP_DECL)
    assert env is None and notes == {} and out["query"] == "how do I"

    _o, _n, env = _screen_arguments(
        "epicor_baq", {"action": "create", "baq": "X", "query": "SELECT 1"},
        BAQ_DECL)
    assert env["error"] == "raw_sql_unsupported"


# --------------------------------------------------------------------------- #
# Rejections — INV-1, naming the supported path
# --------------------------------------------------------------------------- #

def test_baq_create_raw_sql_short_circuits():
    """Unsupported raw SQL must be rejected before tool execution."""
    args = {
        "action": "create", "baq": "Example_Inventory_Query",
        "tables": "Erp.PartWhse, Erp.Part, Erp.PartTran",
        "description": "stale inventory",
        "query": ("SELECT p.PartNum, (pw.OnHandQty * p.AvgCost) as "
                  "InventoryValue, MAX(pt.TranDate) FROM ..."),
    }
    _o, _n, env = _screen_arguments("epicor_baq", args, BAQ_DECL)
    assert env["error"] == "raw_sql_unsupported"
    # Names the SUPPORTED path, not just the absence.
    assert "tables" in env["message"] and "fields" in env["message"]
    assert env["retry_with"]["tables"] == "Erp.PartWhse, Erp.Part, Erp.PartTran"


def test_baq_group_by_and_aggregate_reject_together_with_a_merged_fields():
    """Aliasing one half while rejecting the other yields a HALF-APPLIED query
    — a silent wrong artifact, the exact failure being fixed."""
    _o, _n, env = _screen_arguments("epicor_baq", {
        "action": "create", "baq": "X", "tables": "Erp.Part",
        "group_by": "ClassID", "aggregate": "count(PartNum)",
    }, BAQ_DECL)
    assert env["error"] == "group_by_not_a_baq_param"
    merged = env["retry_with"]["fields"]
    assert "ClassID" in merged and "count(PartNum)" in merged


def test_baq_date_bucket_group_by_steers_to_epicor_read():
    """The BAQ composer groups by raw columns only."""
    _o, _n, env = _screen_arguments("epicor_baq", {
        "action": "create", "baq": "X", "tables": "Erp.Part",
        "group_by": "month(TranDate), PartNum",
    }, BAQ_DECL)
    assert env["retry_with"]["tool"] == "epicor_read"


def test_baq_timeout_ms_is_dropped_but_noted():
    """Acknowledged-and-discarded: no control surface, so erroring costs a hop
    for a cosmetic key. Still never silent."""
    out, notes, env = _screen_arguments(
        "epicor_baq", {"action": "run", "baq": "X", "timeout_ms": 5000},
        BAQ_DECL)
    assert env is None
    assert "timeout_ms" not in out
    assert "timeout_ms" in notes["arg_ignored"]


def test_find_person_ambiguous_hints_are_rejected():
    _o, _n, env = _screen_arguments(
        "epicor_find_person", {"search_name": "Mike", "role_hint": "planner"},
        FIND_DECL)
    assert env["error"] == "unknown_arguments"
    assert "search_name" in env["valid"]["arguments"]


def test_two_sources_for_one_target_never_pick_silently():
    _o, _n, env = _screen_arguments(
        "epicor_baq", {"action": "run", "baq_id": "A", "name": "B"}, BAQ_DECL)
    assert env["error"] == "ambiguous_argument"


# --------------------------------------------------------------------------- #
# Unknown-unknowns — the general case, beyond the common aliases above
# --------------------------------------------------------------------------- #

def test_confident_typo_is_aliased():
    out, notes, env = _screen_arguments(
        "epicor_read", {"targt": "Part"}, READ_DECL)
    assert env is None
    assert out["target"] == "Part"
    assert notes["arg_typo_fixed"] == {"targt": "target"}


def test_novel_argument_is_a_precise_error_listing_the_real_parameters():
    _o, _n, env = _screen_arguments(
        "epicor_read", {"target": "Part", "frobnicate": 1}, READ_DECL)
    assert env["error"] == "unknown_arguments"
    assert set(env["valid"]["arguments"]) == set(READ_DECL)
    # retry_with must not replay the rejected key.
    assert "frobnicate" not in env["retry_with"]


# --------------------------------------------------------------------------- #
# Table hygiene
# --------------------------------------------------------------------------- #

def test_no_alias_source_shadows_a_declared_parameter():
    """If a real parameter is later added with an alias's name, the alias would
    silently shadow it. Fail loudly here instead."""
    declared = {"epicor_read": READ_DECL, "epicor_baq": BAQ_DECL,
                "epicor_find_person": FIND_DECL, "epicor_help": HELP_DECL}
    for tool, aliases in _ARG_ALIASES.items():
        for src in aliases:
            assert src not in declared.get(tool, {}), (tool, src)


def test_no_arguments_means_no_notes_and_no_copy():
    args = {"target": "Part"}
    out, notes, env = _screen_arguments("epicor_read", args, READ_DECL)
    assert env is None and notes == {} and out is args


# --------------------------------------------------------------------------- #
# End to end: the alias must actually PRUNE, not merely be renamed
# --------------------------------------------------------------------------- #

def test_select_alias_reaches_the_wire_through_the_installed_guard():
    """The whole point of a `select` argument is column pruning. Asserting the
    table entry alone would pass while the projection stayed default."""
    import asyncio
    import types

    from mcp.server.fastmcp import FastMCP

    from epicor_mcp.tools import read as read_mod
    from epicor_mcp.tools._argguard import install_validation_guard
    from tests.test_read_routing_e2e import _clear_caches, _Client, _Idx, _RBAC

    _clear_caches()
    orig = read_mod.get_current_session
    read_mod.get_current_session = lambda: types.SimpleNamespace(user_id="t")
    try:
        cols = ["PartNum", "RequestDate", "DueDate", "VendorNum"]
        idx = _Idx(
            {("Erp.BO.ReqSvc", "ReqHead"): [
                {"field_name": c, "field_type": "Edm.String"} for c in cols]},
            entity_sets={"Erp.BO.ReqSvc": ["ReqHead"]},
            hosts={"reqhead": [{"service_id": "Erp.BO.ReqSvc",
                                "entity_set_name": "ReqHead"}]})
        client = _Client(get_result={"value": [{"PartNum": "A"}]})
        mcp = FastMCP("t")
        read_mod.register(mcp, idx, _RBAC(), client)
        install_validation_guard(mcp)

        out = asyncio.run(mcp._tool_manager.call_tool(
            "epicor_read",
            {"target": "Erp.BO.ReqSvc/ReqHead", "select": "PartNum",
             "order_by": "DueDate asc"},
            context=None))
        assert client.gets[-1][1]["$select"] == "PartNum,DueDate"
        assert client.gets[-1][1]["$orderby"] == "DueDate asc"
        assert "'select': 'fields'" in json.loads(out)["summary"]

        rej = asyncio.run(mcp._tool_manager.call_tool(
            "epicor_read", {"target": "X", "frobnicate": 1}, context=None))
        assert json.loads(str(rej))["error"] == "unknown_arguments"
    finally:
        read_mod.get_current_session = orig

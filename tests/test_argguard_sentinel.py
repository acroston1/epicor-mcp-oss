"""The `_declared_types` None/{} sentinel split and the post-alias retry_with.

`{}` used to be ONE sentinel for two opposite outcomes: "this tool
could not be resolved" and "this tool resolves and takes no arguments".
`_screen_arguments` stood down for both, so a registered zero-arg tool
(`epicor_mrp_output`) accepted `history=3` and discarded it in silence — the
unknown-argument guard voided for exactly the tool where the confusion is likeliest,
because `history`/`date` are real parameters of its sibling
`epicor_mrp_status`.

The hard-reject branch built `retry_with` from the RAW arguments, so
every alias resolved earlier in the same loop was thrown away under a message
promising "the arguments that DID map are in retry_with".
"""

from __future__ import annotations

import logging

import pytest

from epicor_mcp.tools._argguard import _declared_types, _screen_arguments

FIND_DECL = {"search_name": "string", "top": "integer"}
BAQ_DECL = {
    "action": "string", "baq": "string", "tables": "string", "fields": "string",
    "where": "string", "description": "string", "order_by": "string",
    "limit": "integer", "cursor": "string", "params": "object",
}


# --------------------------------------------------------------------------- #
# The sentinel split itself
# --------------------------------------------------------------------------- #

class _Tool:
    def __init__(self, params):
        self.parameters = params


class _Mgr:
    def __init__(self, tools):
        self._t = tools

    def get_tool(self, name):
        return self._t.get(name)


class _MCP:
    def __init__(self, tools):
        self._tool_manager = _Mgr(tools)


def test_unresolvable_tool_is_none_not_empty_dict():
    mcp = _MCP({})
    assert _declared_types(mcp, "epicor_nope") is None


def test_zero_arg_tool_is_empty_dict_not_none():
    """The pin that distinguishes the two branches. A real FastMCP zero-arg
    tool advertises {'properties': {}, ...} — `properties` IS present."""
    mcp = _MCP({"epicor_mrp_output": _Tool(
        {"properties": {}, "type": "object"})})
    declared = _declared_types(mcp, "epicor_mrp_output")
    assert declared == {}
    assert declared is not None


def test_declared_types_reads_a_real_schema():
    mcp = _MCP({"t": _Tool({"properties": {
        "query": {"type": "string"},
        "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    }})})
    assert _declared_types(mcp, "t") == {"query": "string", "limit": "integer"}


def test_unresolvable_tool_is_announced_not_silent(caplog):
    """Passing through in SILENCE is what made this branch invisible for the
    entire life of the guard."""
    mcp = _MCP({})
    with caplog.at_level(logging.ERROR):
        assert _declared_types(mcp, "gone") is None
    assert any("could not resolve tool schema" in r.message for r in caplog.records)

    args = {"anything": 1}
    out, notes, env = _screen_arguments("gone", args, None)
    assert env is None
    assert out is args, "arguments must pass through unmutated"
    assert "arg_guard_unavailable" in notes


# --------------------------------------------------------------------------- #
# Zero-arg tools are now SCREENED, not waved through
# --------------------------------------------------------------------------- #

def test_zero_arg_tool_rejects_unknown_arguments():
    """The live-defect regression pin: this returned the tool's success payload
    with both arguments silently discarded."""
    out, notes, env = _screen_arguments(
        "epicor_mrp_output", {"history": 3}, {})
    assert env is not None
    assert env["error"] == "unknown_arguments"
    assert "takes no arguments" in env["message"]


def test_zero_arg_tool_names_the_tool_that_owns_the_argument():
    """`valid.arguments: {}` alone names no supported path and dead-ends the
    model — `history` belongs to the sibling epicor_mrp_status."""
    _, _, env = _screen_arguments("epicor_mrp_output", {"history": 3}, {})
    assert "epicor_mrp_status" in env["message"]
    assert env["retry_with"]["tool"] == "epicor_mrp_status"


def test_zero_arg_tool_with_no_arguments_is_untouched():
    args: dict = {}
    out, notes, env = _screen_arguments("epicor_mrp_output", args, {})
    assert env is None and notes == {} and out is args


# --------------------------------------------------------------------------- #
# Item 4 — hard-reject retry_with must carry what DID map
# --------------------------------------------------------------------------- #

def test_hard_reject_preserves_the_aliases_resolved_alongside_it():
    """{"query":"ExampleUser","resolve_to":"employee"} handed back {} and left the
    model with nothing to re-call, under a message promising otherwise."""
    _, _, env = _screen_arguments(
        "epicor_find_person",
        {"query": "ExampleUser", "resolve_to": "employee"}, FIND_DECL)
    assert env["error"] == "unknown_arguments"
    assert env["retry_with"]["search_name"] == "ExampleUser"


def test_hard_reject_explains_why_the_argument_was_reached_for():
    """A bare "no such parameter" is what makes the model invent the NEXT one."""
    _, _, env = _screen_arguments(
        "epicor_find_person",
        {"role_hint": "planner", "query": "Taylor M"}, FIND_DECL)
    assert env["retry_with"]["search_name"] == "Taylor M"
    assert "role_hint" in env["detail"]["why"]


def test_reference_is_aliased_not_rejected():
    """`reference` is lifted verbatim from the tool's OWN docstring; rejecting
    the word the tool taught the model is a self-inflicted round trip."""
    out, notes, env = _screen_arguments(
        "epicor_find_person", {"reference": "jdoe"}, FIND_DECL)
    assert env is None
    assert out == {"search_name": "jdoe"}
    assert notes["arg_aliased"] == {"reference": "search_name"}


def test_baq_reject_template_keeps_the_name_and_description():
    """_baq_rejects used to fire on the RAW dict, so a name that arrived as
    `baq_name` vanished from the recovery template."""
    _, _, env = _screen_arguments("epicor_baq", {
        "action": "create", "baq_name": "Example_Inventory_Query",
        "description": "d", "query": "SELECT 1"}, BAQ_DECL)
    assert env["error"] == "raw_sql_unsupported"
    assert env["retry_with"]["baq"] == "Example_Inventory_Query"
    assert env["retry_with"]["description"] == "d"


def test_dashboard_id_coerces_action_away_from_run():
    """A tidy id like EXAMPLE-DASH under action='run' would be looked up as a saved
    BAQ and 404 — _do_run's auto-route only fires on a spaced/worded value."""
    out, notes, env = _screen_arguments(
        "epicor_baq", {"action": "run", "dashboard_id": "EXAMPLE-DASH"}, BAQ_DECL)
    assert env is None
    assert out["action"] == "dashboard"
    assert out["baq"] == "EXAMPLE-DASH"
    assert "arg_coerced" in notes


def test_dashboard_action_is_not_disturbed():
    out, _, env = _screen_arguments("epicor_baq", {
        "action": "dashboard", "dashboard_id": "Sample Sales Overview",
        "top": 5000}, BAQ_DECL)
    assert env is None
    assert out["action"] == "dashboard"


@pytest.mark.parametrize("tool,decl,args,aliased_target", [
    ("epicor_find_person", FIND_DECL,
     {"query": "X", "resolve_to": "employee"}, "search_name"),
])
def test_mixed_aliasable_and_unmappable_always_echoes_the_mapped_value(
        tool, decl, args, aliased_target):
    """Whole-class guard: the raw-arguments bug hit every tool that mixes an
    aliasable argument with a rejectable one."""
    _, _, env = _screen_arguments(tool, args, decl)
    assert env is not None
    assert aliased_target in env["retry_with"]


def test_real_registered_zero_arg_tool_still_advertises_empty_properties():
    """Pin the sentinel against REALITY, not just the hand-built stub.

    Every case above asserts on `_Tool({'properties': {}, ...})` — our own
    belief about FastMCP's schema shape. If the installed FastMCP ever emitted
    a zero-arg schema WITHOUT a `properties` key, `_declared_types` would fall
    back to None, `_screen_arguments` would stand down, and the whole zero-arg
    guard would go quiet with every stubbed test still green.
    """
    import types as _types

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pin")

    @mcp.tool(name="epicor_mrp_output_pin")
    async def _zero_arg() -> str:  # noqa: ANN202 — schema shape is the subject
        return "{}"

    schema = mcp._tool_manager.get_tool("epicor_mrp_output_pin").parameters
    assert "properties" in schema, schema
    assert schema["properties"] == {}
    # And therefore the guard sees {} (guard active), never None (stood down).
    declared = _declared_types(mcp, "epicor_mrp_output_pin")
    assert declared == {} and declared is not None

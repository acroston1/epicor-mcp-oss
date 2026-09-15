"""Argument coercion + the ToolError->INV-1 validation guard.

Two distinct bugs, which is why both layers ship together:

1. `tables=['Erp.Part','Erp.PartTran']` (a real list) fails pydantic validation
   BEFORE the tool body runs, and FastMCP stringifies the ValidationError into
   the ToolError text — leaking the `https://errors.pydantic.dev/...` URL and
   the internal `epicor_baqArguments` model name to the model.
2. `tables='["Erp.Part","Erp.PartTran"]'` (a JSON STRING) PASSES validation and
   is comma-split into the garbage terms `'["Erp.Part"'` / `'"Erp.PartTran"]'`
   -> a misleading `unknown_tables`. A silent wrong guess wearing an unrelated
   error code, invisible in any error-mix tally.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from epicor_mcp.tools._resolve import coerce_csv
# Reuse the mock-client harness rather than a second copy of it — these tests
# must exercise the REGISTERED tools, not grep their source.
from tests.test_read_routing_e2e import _Client, _Idx, _make, _RBAC, _Server


# --------------------------------------------------------------------------- #
# coerce_csv
# --------------------------------------------------------------------------- #

def test_coerce_csv_list():
    assert (coerce_csv(["Erp.PartWhse", "Erp.Part", "Erp.PartTran"])
            == "Erp.PartWhse, Erp.Part, Erp.PartTran")


def test_coerce_csv_json_string():
    """The silent-wrong-guess bug: this used to comma-split into garbage."""
    assert (coerce_csv('["Erp.Part","Erp.PartTran"]')
            == "Erp.Part, Erp.PartTran")


@pytest.mark.parametrize("value", [
    "count(PONum)",
    "sum(OrderDtl.OrderQty) as TotalQty",
    "[Alias].[Col]",
    "month(OrderDate)",
    "PartNum, QtyOnHand",
    "sum(OnHandQty * AvgCost) as InventoryValue",
])
def test_coerce_csv_preserves_sql_shaped_strings(value):
    """Regression fence for the BAQ aggregate grammar and rollup buckets."""
    assert coerce_csv(value) == value


def test_coerce_csv_none_and_scalars():
    assert coerce_csv(None) == ""
    assert coerce_csv(5) == "5"
    assert coerce_csv([]) == ""


def test_coerce_csv_malformed_json_array_passes_through():
    assert coerce_csv('[not json') == '[not json'


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #

class _FakeTool:
    def __init__(self, props):
        self.parameters = {"properties": props}


class _FakeManager:
    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    async def call_tool(self, name, arguments, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return "ok"

    def get_tool(self, name):
        return _FakeTool({"tables": {"type": "string"},
                          "limit": {"type": "integer"}})


class _FakeMCP:
    def __init__(self, exc=None):
        self._tool_manager = _FakeManager(exc)


def _install(exc):
    from epicor_mcp.tools._argguard import install_validation_guard
    mcp = _FakeMCP(exc)
    install_validation_guard(mcp)
    return mcp


def _validation_error():
    from pydantic import BaseModel

    class M(BaseModel):
        tables: str

    try:
        M(tables=["a", "b"])
    except ValidationError as ve:
        return ve
    raise AssertionError("expected a ValidationError")


def _run(mcp, args, convert_result=False):
    return asyncio.run(mcp._tool_manager.call_tool(
        "epicor_baq", args, convert_result=convert_result))


def test_validation_error_returns_inv1_envelope():
    cause = _validation_error()
    exc = ToolError(f"Error executing tool epicor_baq: {cause}")
    exc.__cause__ = cause
    mcp = _install(exc)

    out = _run(mcp, {"tables": ["Erp.Part", "Erp.PartTran"]})
    env = json.loads(out)
    assert env["error"] == "invalid_argument_type"
    assert env["valid"]["arguments"]["tables"] == "string"
    assert env["retry_with"]["tables"] == "Erp.Part, Erp.PartTran"
    # The leak, gone.
    assert "errors.pydantic.dev" not in out
    assert "Arguments" not in out


def test_guard_reraises_non_validation_toolerror():
    """A genuine crash must propagate — swallowing it is a silent wrong guess."""
    exc = ToolError("Error executing tool epicor_baq: boom")
    exc.__cause__ = ValueError("boom")
    mcp = _install(exc)
    with pytest.raises(ToolError):
        _run(mcp, {"tables": "Erp.Part"})


def test_guard_passes_success_through():
    mcp = _install(None)
    assert _run(mcp, {"tables": "Erp.Part"}) == "ok"


def test_guard_response_shape():
    """Wrong shape makes pydantic validate each CHARACTER as a Content variant."""
    cause = _validation_error()
    exc = ToolError("x")
    exc.__cause__ = cause
    mcp = _install(exc)

    assert isinstance(_run(mcp, {"tables": ["a"]}, convert_result=False), str)
    blocks = _run(mcp, {"tables": ["a"]}, convert_result=True)
    assert isinstance(blocks, list)
    assert blocks[0].type == "text"
    assert json.loads(blocks[0].text)["error"] == "invalid_argument_type"


# --------------------------------------------------------------------------- #
# Widening — the tool bodies normalise before anything splits on ","
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fields_arg", [
    ["PartNum", "PartDescription"],          # a real list
    '["PartNum","PartDescription"]',         # a JSON string
    "PartNum, PartDescription",              # the already-correct form
])
def test_read_list_valued_fields_reach_the_wire_intact(monkeypatch, fields_arg):
    """BEHAVIOUR, not `inspect.getsource`.

    A source grep for the widened annotation passes even if the body forgets
    to call `coerce_csv` — and then `fields=["PartNum","PartDescription"]` is
    comma-split from its repr into the garbage terms `["PartNum"` /
    `"PartDescription"]` and comes back as a misleading `unknown_columns`:
    the silent-wrong-guess-wearing-an-unrelated-error-code failure this file
    exists to prevent. So assert what actually reaches the wire.
    """
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "Part"): [
            {"field_name": "PartNum", "field_type": "Edm.String"},
            {"field_name": "PartDescription", "field_type": "Edm.String"},
        ]},
        entity_sets={"Erp.BO.PartSvc": ["Part", "Parts"]},
        hosts={"part": [{"service_id": "Erp.BO.PartSvc",
                         "entity_set_name": "Part"}]},
    )
    client = _Client(get_result={"value": [{"PartNum": "X"}]})
    fn = _make(idx, client, monkeypatch)

    out = json.loads(asyncio.run(fn(target="Part", fields=fields_arg)))

    # `resolved.fields` is the projection the engine actually built, on either
    # route (PartSvc is heavy, so this one lands on GetRows, not $select).
    assert "error" not in out, out
    assert out["resolved"]["fields"] == ["PartNum", "PartDescription"], out
    blob = json.dumps(out["resolved"]["fields"])
    assert "[" not in blob.strip("[]") and '\\"' not in blob


class _WDH:
    """Records what the write layer was actually handed."""

    def __init__(self):
        self.created: list[dict] = []

    async def create_record(self, *, base_url, service, entity, api_key, changes):
        self.created.append(changes)
        return {"ok": True}

    def trim_dataset(self, result, entity):
        return result


def _act_tool(monkeypatch, wdh=None):
    from epicor_mcp.tools import act as act_mod

    monkeypatch.setattr(act_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="tester"))
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "Part"): [
            {"field_name": "PartNum", "field_type": "Edm.String"},
        ]},
        entity_sets={"Erp.BO.PartSvc": ["Part", "Parts"]},
        hosts={"part": [{"service_id": "Erp.BO.PartSvc",
                         "entity_set_name": "Part"}]},
    )
    srv = _Server()
    act_mod.register(srv, idx, _ActRBAC(), _Client(),
                     dataset_handler=wdh or _WDH())
    return srv.fn


class _ActRBAC(_RBAC):
    def check_write_access(self, user_id, service_id):
        return (True, "")

    @property
    def _user_map(self):
        return types.SimpleNamespace(get_write_key=lambda: "WK")


def test_act_single_dict_record_is_batched(monkeypatch):
    """A model batching one record as a bare dict must not be dropped.

    Asserts the record REACHES the write layer, not that a particular
    expression appears in act.py's source.
    """
    wdh = _WDH()
    fn = _act_tool(monkeypatch, wdh)
    asyncio.run(fn(action="create", target="Erp.BO.PartSvc/Part",
                   records={"PartNum": "X"}, environment="live"))
    assert wdh.created == [{"PartNum": "X"}]


def test_act_multi_record_changes_list_is_never_silently_truncated(monkeypatch):
    """`changes` applies ONE change set; a list of N is a batch, not changes[0].

    Keeping only the first element silently discarded the rest on a WRITE
    path — the worst place for a partial result. The tool must refuse and
    hand back the corrected call.
    """
    wdh = _WDH()
    fn = _act_tool(monkeypatch, wdh)
    out = json.loads(asyncio.run(fn(
        action="create", target="Erp.BO.PartSvc/Part",
        changes=[{"PartNum": "A"}, {"PartNum": "B"}], environment="pilot")))

    assert out["error"] == "changes_is_a_batch"
    # INV-1: the fix is handed back COMPLETE — both records, not just the one
    # that would have survived.
    assert out["retry_with"]["records"] == [{"PartNum": "A"}, {"PartNum": "B"}]
    assert "changes" not in out["retry_with"]
    assert wdh.created == [], "nothing may be written when the call is rejected"


def test_act_single_element_changes_list_is_unwrapped(monkeypatch):
    """One change set wrapped in a list is unambiguous — accept it."""
    wdh = _WDH()
    fn = _act_tool(monkeypatch, wdh)
    out = asyncio.run(fn(action="create", target="Erp.BO.PartSvc/Part",
                         changes=[{"PartNum": "A"}], environment="live"))
    assert "changes_is_a_batch" not in out
    assert wdh.created == [{"PartNum": "A"}]


# --------------------------------------------------------------------------- #
# retry_with must never be a copy of the call that just failed
# --------------------------------------------------------------------------- #

def _object_validation_error():
    from pydantic import BaseModel

    class M(BaseModel):
        params: dict | None = None

    try:
        M(params='{"partNum": "X"}')
    except ValidationError as ve:
        return ve
    raise AssertionError("expected a ValidationError")


def test_json_object_string_is_parsed_into_retry_with():
    """A JSON object STRING for a dict param is the commonest weak-model slip.

    `coerce_csv` returns a plain string untouched, so retry_with came back
    byte-identical to the rejected call while the message said "re-call with
    the values in retry_with" — a guaranteed identical-retry loop.
    """
    from epicor_mcp.tools._argguard import _validation_envelope

    env = _validation_envelope(
        _object_validation_error(),
        {"action": "GetByID", "target": "Erp.BO.PartSvc",
         "params": '{"partNum": "X"}'},
        {"action": "string", "target": "string", "params": "object"},
    )
    assert env["retry_with"]["params"] == {"partNum": "X"}
    assert env["retry_with"]["params"] != '{"partNum": "X"}'


def test_uncorrectable_value_is_dropped_from_retry_with():
    """If nothing can be fixed, do NOT hand back the failing value."""
    from epicor_mcp.tools._argguard import _validation_envelope

    args = {"action": "GetByID", "target": "Erp.BO.PartSvc",
            "params": "not json at all"}
    env = _validation_envelope(
        _object_validation_error(), args,
        {"action": "string", "target": "string", "params": "object"},
    )
    assert "params" not in env["retry_with"], (
        "echoing the rejected value guarantees an identical retry")
    assert env["retry_with"] != args
    # ...and the model must be TOLD why it is missing, not left to wonder.
    assert "OMITTED" in env["message"]
    assert "params" in env["message"]

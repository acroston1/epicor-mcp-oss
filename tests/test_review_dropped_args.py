"""Regressions for the review findings on the 19c41ee follow-up work.

Every case here is a SILENT WRONG ANSWER or a silently-dropped argument — the
class the whole workstream exists to remove. Grouped by the defect, not by the
function, because several share one root cause: a parameter was added to a
signature and never referenced in the body, so the plumbing looked done.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.tools import baq as baq_mod
from epicor_mcp.tools import read as read_mod
from epicor_mcp.tools._aggregate import _sort_key
from epicor_mcp.tools._inline_schema import sort_records
from epicor_mcp.tools.baq import _sort_records_by


# --------------------------------------------------------------------------- #
# BAQ client-side ordering was LEXICOGRAPHIC -> every numeric ranking inverted
# --------------------------------------------------------------------------- #

def test_baq_client_sort_compares_numbers_numerically():
    """'sum(POAmt) as Total desc' returned the SMALLEST buyers first.

    str(95) > str(1000), so the order_note asserted a ranking that was the
    exact inverse of the truth — a confidently wrong answer, not an error.
    """
    recs = [{"Total": 95}, {"Total": 1000}, {"Total": 900}, {"Total": None}]
    desc = [r["Total"] for r in _sort_records_by(recs, [("[Total]", "desc")])]
    asc = [r["Total"] for r in _sort_records_by(recs, [("[Total]", "asc")])]
    assert desc == [1000, 900, 95, None]
    assert asc == [95, 900, 1000, None]
    # Blanks stay LAST in both directions (a mixed key would raise TypeError).
    assert desc[-1] is None and asc[-1] is None


def test_baq_client_sort_still_orders_strings():
    recs = [{"Buyer": "carol"}, {"Buyer": "alice"}, {"Buyer": ""}]
    out = [r["Buyer"] for r in _sort_records_by(recs, [("[Buyer]", "asc")])]
    assert out == ["alice", "carol", ""]


# --------------------------------------------------------------------------- #
# The JOIN path floated blanks to the TOP on desc, then trimmed to `limit`
# --------------------------------------------------------------------------- #

def test_join_desc_sort_keeps_blanks_last():
    """A 'top 3 by measure desc' join returned 3 EMPTY rows.

    _sort_key puts blanks in key group 1; under reverse=True that group LEADS,
    and the trim to `limit` then kept exactly those. Drives the real join tool
    (one stubbed GetRows page) rather than re-deriving the comparator here --
    a test that reimplements the logic proves nothing about the code path.
    """
    from epicor_mcp.tools import query_with_children as qwc
    from tests.test_read_routing_e2e import _Server

    PARENT = [{"OrderNum": i} for i in (1, 2, 3, 4, 5)]
    CHILD = [
        {"OrderNum": 1, "DocExtPrice": 10},
        {"OrderNum": 2, "DocExtPrice": None},
        {"OrderNum": 3, "DocExtPrice": 50},
        {"OrderNum": 4, "DocExtPrice": ""},
        {"OrderNum": 5, "DocExtPrice": 30},
    ]

    class _Idx:
        def get_fields(self, svc, es):
            return [{"field_name": "OrderNum", "field_type": "Edm.Int32"}]

        def get_entity_sets(self, svc):
            return ["OrderHed", "OrderDtl"]

        def get_field_types(self, svc, es):
            return {"OrderNum": "Edm.Int32", "DocExtPrice": "Edm.Decimal"}

    class _RBAC:
        def check_access(self, *a, **k): return True, ""
        def check_service_access(self, *a, **k):
            return types.SimpleNamespace(api_key="K")

    class _Client:
        def __init__(self):
            self.n = 0

        async def post(self, *a, **k):
            self.n += 1
            if self.n > 1:
                return {"returnObj": {"OrderHed": [], "OrderDtl": []}}
            return {"returnObj": {"OrderHed": PARENT, "OrderDtl": CHILD}}

    srv = _Server()
    qwc.register(srv, _Idx(), _RBAC(), _Client())
    monkey = getattr(qwc, "get_current_session", None)
    assert monkey is not None
    qwc.get_current_session = lambda: types.SimpleNamespace(user_id="t")
    try:
        out = json.loads(asyncio.run(srv.fn(
            service="Erp.BO.SalesOrderSvc", parent_entity="OrderHed",
            child_entity="OrderDtl", order_by="DocExtPrice desc")))
    finally:
        qwc.get_current_session = monkey

    vals = [r.get("DocExtPrice") for r in out["records"]]
    # The populated rows lead; blanks are pushed to the tail in BOTH directions.
    assert vals[:3] == [50, 30, 10], vals
    assert all(v in (None, "") for v in vals[3:]), vals


# --------------------------------------------------------------------------- #
# _run_analytic / _rank_result: order_by was accepted and discarded
# --------------------------------------------------------------------------- #

def _rank(order_by="", recs=None, soft=None):
    recipe = {
        "label": "parts by sales volume", "scope": "last 12 months",
        "service": "Erp.BO.PartSvc", "entity": "Part",
        "group_by": "PartNum", "aggregate": "sum(Qty) as TotalQty",
        "measure_alias": "TotalQty",
    }
    payload = {"records": recs if recs is not None else [
        {"PartNum": "FAST", "TotalQty": 900},
        {"PartNum": "MID", "TotalQty": 90},
        {"PartNum": "SLOW", "TotalQty": 9},
    ]}
    return json.loads(read_mod._rank_result(
        json.dumps(payload), recipe, 2, "", order_by=order_by, soft=soft))


def test_analytic_default_is_still_top_n_descending():
    out = _rank()
    assert [r["PartNum"] for r in out["records"]] == ["FAST", "MID"]
    assert out["summary"].startswith("Top 2 ")


def test_analytic_ascending_order_by_returns_the_bottom_n():
    """'order_by=TotalQty asc' means the SLOWEST movers.

    It used to be dropped: the caller got the highest sellers under a 'Top N'
    label with nothing in the payload to contradict it.
    """
    out = _rank(order_by="TotalQty asc")
    assert [r["PartNum"] for r in out["records"]] == ["SLOW", "MID"]
    # The label must not still claim "Top".
    assert not out["summary"].startswith("Top ")
    assert out["summary"].startswith("Bottom 2 ")
    assert "TotalQty asc" in out["summary"]
    assert "TotalQty asc" in out["resolved"]["assumptions"]["order"]


def test_analytic_order_by_non_measure_column_is_applied_and_labelled():
    out = _rank(order_by="PartNum asc")
    assert [r["PartNum"] for r in out["records"]] == ["FAST", "MID"]
    # Not a measure ranking, so neither "Top" nor "Bottom" would be honest.
    assert out["summary"].startswith("First 2 ")


def test_analytic_unknown_order_column_refuses_with_inv1():
    out = _rank(order_by="Nonexistent desc")
    assert out.get("error"), out
    assert "valid" in out


def test_analytic_carries_pre_dispatch_assumptions():
    """`soft` (site_resolved, arg aliasing) was dropped on this route entirely."""
    out = _rank(soft={"site_resolved": "Plant='Oakridge' -> Plant='10'"})
    assert out["resolved"]["assumptions"]["site_resolved"].endswith("'10'")


# --------------------------------------------------------------------------- #
# read_bom: the trim destroyed the TRUE material total
# --------------------------------------------------------------------------- #

def test_bom_limit_trim_states_the_real_total():
    """A 240-material job reported '100 material(s)' as the recipe size.

    `materials` was reassigned before the note formatted len(materials), so the
    denominator was lost and summary/row_count both repeated the trimmed count.
    A PARTIAL bill of materials presented with a specific, wrong, confident
    number is exactly the trap the BOM recognizer exists to prevent.
    """
    from epicor_mcp.tools._partviews import read_bom

    mtls = [{"PartNum": f"M{i}", "MtlSeq": i, "AssemblySeq": 0}
            for i in range(240)]

    class _C:
        async def post(self, *a, **k):
            return {"returnObj": {
                "JobMtl": mtls,
                "JobOper": [{"OprSeq": 10, "AssemblySeq": 0}],
                "JobHead": [{"JobNum": "J1", "PartNum": "P1"}]}}

    class _RB:
        def check_access(self, *a, **k):
            return True, ""

        def check_service_access(self, *a, **k):
            return types.SimpleNamespace(api_key="K")

    out = json.loads(asyncio.run(read_bom(
        _C(), None, _RB(), types.SimpleNamespace(user_id="t"),
        where="JobNum = 'J1'", target="BOM for part P1", fields="",
        limit=100, order_by="", soft={})))

    assert out["row_count"] == 100
    # The TRUE size of the recipe survives the trim.
    assert out["total_rows"] == 240
    note = out["resolved"]["assumptions"]["limit_trim"]
    assert "240" in note and "PARTIAL" in note
    # The summary must not present 100 as the recipe size.
    assert "100 of 240" in out["summary"]


def test_bom_untrimmed_summary_is_unchanged():
    from epicor_mcp.tools._partviews import read_bom

    mtls = [{"PartNum": "M1", "MtlSeq": 1, "AssemblySeq": 0}]

    class _C:
        async def post(self, *a, **k):
            return {"returnObj": {
                "JobMtl": mtls, "JobOper": [],
                "JobHead": [{"JobNum": "J1", "PartNum": "P1"}]}}

    class _RB:
        def check_access(self, *a, **k):
            return True, ""

        def check_service_access(self, *a, **k):
            return types.SimpleNamespace(api_key="K")

    out = json.loads(asyncio.run(read_bom(
        _C(), None, _RB(), types.SimpleNamespace(user_id="t"),
        where="JobNum = 'J1'", target="BOM for part P1", fields="",
        limit=100, order_by="", soft={})))
    assert out["row_count"] == 1 and out["total_rows"] == 1
    assert "PARTIAL" not in out["summary"]
    assert "limit_trim" not in (out["resolved"].get("assumptions") or {})


# --------------------------------------------------------------------------- #
# _read_contacts: order_by accepted and discarded
# --------------------------------------------------------------------------- #

def test_contacts_sort_helper_is_the_shared_one():
    """The contacts route must give the SAME refusal as every other route."""
    rows = [{"Name": "b"}, {"Name": "a"}]
    out, kind, _ = sort_records(rows, "Name desc", available=["Name"])
    assert not kind and [r["Name"] for r in out] == ["b", "a"]
    _out, kind2, valid = sort_records(rows, "Nope", available=["Name"])
    assert kind2 == "unknown_column" and valid == ["Name"]


# --------------------------------------------------------------------------- #
# epicor_baq action='run': `fields` projection wiring (helper was tested, the
# CALL SITE was not — micro-reverting both call sites left the suite green)
# --------------------------------------------------------------------------- #

def _baq_srv(monkeypatch, run_result):
    from mcp.server.fastmcp import FastMCP

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

    class _Client:
        async def get(self, *a, **k): return {"value": []}
        async def post(self, *a, **k): return {}

    async def _run_impl(**kw):
        return run_result

    monkeypatch.setattr(baq_mod, "run_baq_impl", _run_impl)
    monkeypatch.setattr(baq_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="t"))
    mcp = FastMCP("t")
    baq_mod.register(mcp, _Idx(), _RBAC(), _Client(), _BaqIdx())
    return mcp


def _call(mcp, args):
    out = asyncio.run(mcp._tool_manager.call_tool("epicor_baq", args, context=None))
    return json.loads(str(out) if isinstance(out, str) else out[0].text)


def test_baq_run_actually_prunes_to_fields(monkeypatch):
    """`select` was aliased to `fields`, ANNOUNCED as applied, then discarded.

    The eight existing projection tests all call the helper directly, so both
    call sites could be reverted to identity pass-throughs with the whole suite
    still green. This drives the REGISTERED tool.
    """
    mcp = _baq_srv(monkeypatch, {"records": [
        {"PONum": 1, "BuyerID": "AC", "Junk": "x"},
        {"PONum": 2, "BuyerID": "BD", "Junk": "y"},
    ]})
    out = _call(mcp, {"action": "run", "baq": "EXAMPLE-DASH", "fields": "PONum"})
    recs = out.get("records") or []
    assert recs, out
    # The decisive assertion: the extra columns are GONE from the payload.
    assert all(set(r) == {"PONum"} for r in recs), recs


def test_baq_run_without_fields_keeps_every_column(monkeypatch):
    mcp = _baq_srv(monkeypatch, {"records": [{"PONum": 1, "BuyerID": "AC"}]})
    out = _call(mcp, {"action": "run", "baq": "EXAMPLE-DASH"})
    assert set(out["records"][0]) == {"PONum", "BuyerID"}


# --------------------------------------------------------------------------- #
# The run -> dashboard auto-reroute silently voided `order_by`
# --------------------------------------------------------------------------- #

def test_dashboard_reroute_applies_order_by(monkeypatch):
    """The tool description promises "order_by also applies to action='run'".

    A spaced/dashboard-worded `baq` re-routes internally to _do_dashboard,
    which had no order_by parameter at all — an INV-2 internal reroute voiding
    a documented argument with no announcement.
    """
    seen = {}

    async def _dash(**kw):
        seen.update(kw)
        return json.dumps({"records": [
            {"Part": "A", "Qty": 5}, {"Part": "B", "Qty": 50},
            {"Part": "C", "Qty": 30}]})

    out = json.loads(asyncio.run(baq_mod._do_dashboard(
        dashboard_fn=_dash, baq="Sample Sales Overview",
        where="", limit=10, order_by="Qty desc")))
    # Numeric, not lexicographic (50 before 30 before 5).
    assert [r["Qty"] for r in out["records"]] == [50, 30, 5]
    assert "Qty desc" in out["order"]


def test_dashboard_order_by_unknown_column_is_announced_not_asserted(monkeypatch):
    async def _dash(**kw):
        return json.dumps({"records": [{"Part": "A", "Qty": 5}]})

    out = json.loads(asyncio.run(baq_mod._do_dashboard(
        dashboard_fn=_dash, baq="X", where="", limit=10,
        order_by="Nonexistent desc")))
    # Never claim an ordering that did not happen.
    assert "NOT applied" in out["order"]
    assert "Qty" in out["order"] and "Part" in out["order"]


# --------------------------------------------------------------------------- #
# The PLAIN read path had no Edm.Boolean veto on order_by auto-correct
# --------------------------------------------------------------------------- #

def test_plain_read_order_by_is_never_corrected_to_a_boolean(monkeypatch):
    """The join path vetoed this; the plain path — the far more common one —
    accepted _correct_column unconditionally, so 'Part order_by=OnHandQty desc'
    silently ranked by the BOOLEAN HasOnHandQty. A boolean ranking returns
    plausible rows, so nothing downstream catches it.
    """
    from tests.test_read_routing_e2e import _Client, _Idx, _RBAC, _Server

    monkeypatch.setattr(read_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="t"))
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "Part"): [
            {"field_name": "PartNum", "field_type": "Edm.String"},
            {"field_name": "HasOnHandQty", "field_type": "Edm.Boolean"},
        ]},
        entity_sets={"Erp.BO.PartSvc": ["Part", "Parts"]},
    )
    client = _Client()
    srv = _Server()
    read_mod.register(srv, idx, _RBAC(), client)
    out = json.loads(asyncio.run(srv.fn(
        target="Erp.BO.PartSvc/Part", order_by="OnHandQty desc")))

    # Refused, not silently re-pointed at the boolean.
    assert out.get("error") == "unknown_columns", out
    # And the wrong sort never reached the wire.
    sent = json.dumps(getattr(client, "gets", []) + getattr(client, "posts", []))
    assert "HasOnHandQty" not in sent

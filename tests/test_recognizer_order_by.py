"""Caller `order_by` and the announcement channel on the recognizer routes.

Item 1. The eleven recognizer routes return BEFORE `epicor_read`'s
post-resolution argument pipeline, so `order_by` (and `having`, `count_only`,
`cursor`, and `fields` on six of them) were dropped in silence. Worse, the
fail-soft assumptions bag `soft` — which carries the argument guard's own
alias notes — was consumed only after every recognizer return and passed to
none of them, so the "announce what you coerced" contract had no channel at
all on these routes.

Each route decides APPLY / PUSH / REFUSE according to how its result set is
produced; what none of them may do is ignore the clause silently.
"""

from __future__ import annotations

import json
import types

import pytest

from epicor_mcp.tools import _planner, _posugg, _yield
from epicor_mcp.tools._attachments import read_attachments
from epicor_mcp.tools._partviews import read_bom, read_timephase
from epicor_mcp.tools._planner import planner_jobs
from epicor_mcp.tools._posugg import po_suggestions
from epicor_mcp.tools._whereused import where_used
from epicor_mcp.tools._yield import yield_trend

from tests.test_attachments import (
    _Client as _AttClient,
    _Idx as _AttIdx,
    _dataset as _att_dataset,
)
from tests.test_partviews import _FakeClient as _PVClient, configured_plants
from tests.test_planner import _PEOPLE, _run
from tests.test_whereused import _FakeClient as _WUClient, _FakeRBAC, _row

_SESSION = types.SimpleNamespace(user_id="tester")
_INDEX = None


# --------------------------------------------------------------------------- #
# where-used — APPLY client-side, and sort BEFORE the trim
# --------------------------------------------------------------------------- #

def test_where_used_sorts_the_full_set_before_trimming():
    """The whole page is in memory, so ordering it makes a REAL top-N. Sorting
    after the trim would rank only the first `limit` rows and present it as
    global."""
    rows = [_row(parent=f"P{i:03d}", mtlseq=i) for i in range(60)]
    out = json.loads(_run(where_used(
        _WUClient(rows=rows), _FakeRBAC(), _SESSION,
        target="what is part PART-100 used to make",
        limit=5, order_by="PartNum desc")))

    got = [r["PartNum"] for r in out["records"]]
    assert got == ["P059", "P058", "P057", "P056", "P055"]
    assert "PartNum desc" in out["resolved"]["assumptions"]["order"]


def test_where_used_unknown_order_column_is_an_inv1_envelope():
    out = json.loads(_run(where_used(
        _WUClient(rows=[_row()]), _FakeRBAC(), _SESSION,
        target="what is part PART-100 used to make",
        order_by="NoSuchCol desc")))

    assert out["error"] == "unknown_order_column"
    # INV-1: hand back the CORRECT names, never just the rejected one.
    assert "PartNum" in out["valid"]["columns"]


def test_where_used_announces_the_truncation():
    rows = [_row(parent=f"P{i:03d}", mtlseq=i) for i in range(30)]
    out = json.loads(_run(where_used(
        _WUClient(rows=rows), _FakeRBAC(), _SESSION,
        target="what is part PART-100 used to make", limit=5)))
    assert "limit_trim" in out["resolved"]["assumptions"]
    assert out["row_count"] == len(out["records"]) == 5


def test_where_used_full_page_never_implies_completeness():
    """The fetch is a fixed single pageSize=500 page with no next-page check."""
    rows = [_row(parent=f"P{i:04d}", mtlseq=i) for i in range(500)]
    out = json.loads(_run(where_used(
        _WUClient(rows=rows), _FakeRBAC(), _SESSION,
        target="what is part PART-100 used to make", limit=500)))
    assert "incomplete" in out["resolved"]["assumptions"]


# --------------------------------------------------------------------------- #
# planner jobs — PUSH server-side (a post-sort here would fake a top-N)
# --------------------------------------------------------------------------- #

def _patch_planner(monkeypatch, jobs=None, capture=None):
    jobs = jobs if jobs is not None else []

    async def _fake_odata(client, service, entity, api_key, **kw):
        return json.dumps({"records": _PEOPLE})

    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        if capture is not None:
            capture.update(kw)
        return json.dumps({"records": jobs})

    monkeypatch.setattr(_planner, "run_odata", _fake_odata)
    monkeypatch.setattr(_planner, "run_getrows", _fake_getrows)


def test_planner_jobs_pushes_the_caller_clause_server_side(monkeypatch):
    """The fetch is `top=limit`, so the page is server-trimmed — a client-side
    post-sort would rank only the DueDate-earliest `limit` rows."""
    cap = {}
    _patch_planner(monkeypatch, jobs=[{"JobNum": "J1"}], capture=cap)
    out = json.loads(_run(planner_jobs(
        None, _INDEX, _FakeRBAC(), _SESSION,
        target="jobs for planner Plan2", where="", limit=10,
        order_by="JobNum desc")))

    assert cap["orderby"] == "JobNum desc"
    assert "server-side" in out["resolved"]["assumptions"]["order"]


def test_planner_jobs_bad_order_column_makes_no_http_call(monkeypatch):
    """Validate against _JOB_FIELDS BEFORE the call, or Epicor's generic
    GetRows 500 reaches the model instead of a usable envelope."""
    cap = {}

    async def _boom(*a, **k):
        raise AssertionError("JobHead must not be fetched for a bad sort key")

    _patch_planner(monkeypatch, jobs=[], capture=cap)
    monkeypatch.setattr(_planner, "run_getrows", _boom)
    out = json.loads(_run(planner_jobs(
        None, _INDEX, _FakeRBAC(), _SESSION,
        target="jobs for planner Plan2", where="", limit=10,
        order_by="NotAField")))
    assert out["error"] == "unknown_order_column"


def test_planner_jobs_default_order_is_unchanged(monkeypatch):
    cap = {}
    _patch_planner(monkeypatch, jobs=[{"JobNum": "J1"}], capture=cap)
    _run(planner_jobs(None, _INDEX, _FakeRBAC(), _SESSION,
                      target="jobs for planner Plan2", where="", limit=10))
    assert cap["orderby"] == "DueDate"


# --------------------------------------------------------------------------- #
# PO suggestions — APPLY client-side; server-side ordering 500s on this BO
# --------------------------------------------------------------------------- #

def _patch_posugg(monkeypatch, rows, capture=None):
    async def _fake_odata(client, service, entity, api_key, **kw):
        if capture is not None:
            capture.update(kw)
        return json.dumps({"records": rows})

    monkeypatch.setattr(_posugg, "run_odata", _fake_odata)


def test_po_suggestions_sorts_client_side_and_sends_no_server_order(monkeypatch):
    """Regression coverage: test po suggestions sorts client side and sends no server order."""
    cap = {}
    rows = [{"PONum": 3, "VendorName": "C", "DueDate": "2026-09-01"},
            {"PONum": 1, "VendorName": "A", "DueDate": "2026-07-15"},
            {"PONum": 2, "VendorName": "B", "DueDate": "2026-08-10"}]
    _patch_posugg(monkeypatch, rows, cap)
    out = json.loads(_run(po_suggestions(
        None, _INDEX, _FakeRBAC(), _SESSION, kind="change",
        target="PO change suggestions", where="", limit=10,
        order_by="VendorName asc")))

    assert cap["orderby"] in ("", None), "server-side ordering 500s here"
    assert [r["VendorName"] for r in out["records"]] == ["A", "B", "C"]
    assert "client-side" in out["resolved"]["assumptions"]["order"]


def test_po_suggestions_unknown_order_column_is_refused(monkeypatch):
    _patch_posugg(monkeypatch, [{"PONum": 1, "DueDate": "2026-07-15"}])
    out = json.loads(_run(po_suggestions(
        None, _INDEX, _FakeRBAC(), _SESSION, kind="change",
        target="PO change suggestions", where="", limit=10,
        order_by="Nonsense")))
    assert out["error"] == "unknown_order_column"


# --------------------------------------------------------------------------- #
# yield trend — the records are monthly BUCKETS, so a row key is REFUSED
# --------------------------------------------------------------------------- #

_JH = [
    {"JobNum": "J1", "JobCompletionDate": "2026-05-10T00:00:00",
     "QtyCompleted": 90, "ProdQty": 100},
    {"JobNum": "J2", "JobCompletionDate": "2026-06-05T00:00:00",
     "QtyCompleted": 50, "ProdQty": 100},
]
_JO = [{"JobNum": "J1", "ScrapQty": 10}, {"JobNum": "J2", "ScrapQty": 50}]


def _patch_yield(monkeypatch):
    from tests.test_yield import _fake_getrows_factory
    monkeypatch.setattr(_yield, "run_getrows",
                        _fake_getrows_factory(_JH, _JO))


def test_yield_trend_orders_the_monthly_buckets(monkeypatch):
    _patch_yield(monkeypatch)
    out = json.loads(_run(yield_trend(
        None, _INDEX, _FakeRBAC(), _SESSION,
        target="trend production yield over the last 3 months",
        where="PartNum = 'P'", limit=25, order_by="yield_pct desc")))

    pcts = [m["yield_pct"] for m in out["records"]]
    assert pcts == sorted(pcts, reverse=True)
    assert len(pcts) > 1, "need >1 bucket for the ordering to mean anything"


def test_yield_trend_refuses_a_row_level_sort_key(monkeypatch):
    """Returning a plausible-but-unsorted series would be the silent wrong
    answer; name the path that CAN order job rows instead."""
    _patch_yield(monkeypatch)
    out = json.loads(_run(yield_trend(
        None, _INDEX, _FakeRBAC(), _SESSION,
        target="trend production yield over the last 3 months",
        where="PartNum = 'P'", limit=25, order_by="JobNum")))

    assert out["error"] == "order_not_applicable"
    assert "JobHead" in out["message"]
    assert "yield_pct" in out["valid"]["columns"]


# --------------------------------------------------------------------------- #
# BOM — TWO result sets, so which one was sorted must be ANNOUNCED
# --------------------------------------------------------------------------- #

_MTL = {"JobMtl": [{"AssemblySeq": 0, "MtlSeq": 10, "PartNum": "M1", "QtyPer": 1},
                   {"AssemblySeq": 0, "MtlSeq": 20, "PartNum": "M2", "QtyPer": 2}],
        "JobOper": [{"AssemblySeq": 0, "OprSeq": 10, "OpCode": "CUT"},
                    {"AssemblySeq": 0, "OprSeq": 20, "OpCode": "WELD"}]}


def _bom_client():
    return _PVClient(job_methods={"J1": _MTL})


def _patch_bom(monkeypatch):
    from epicor_mcp.tools import _partviews

    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        return json.dumps({"records": [
            {"JobNum": "J1", "PartNum": "P", "JobReleased": True,
             "JobClosed": True, "StartDate": "2026-01-01"}]})

    monkeypatch.setattr(_partviews, "run_getrows", _fake_getrows)


def test_bom_order_by_announces_which_set_it_sorted(monkeypatch):
    _patch_bom(monkeypatch)
    out = json.loads(_run(read_bom(
        _bom_client(), _INDEX, _FakeRBAC(), _SESSION,
        target="BOM for part P", where="", fields="", limit=0, order_by="MtlSeq desc")))

    assert [m["MtlSeq"] for m in out["materials"]] == [20, 10]
    assert "materials" in out["resolved"]["assumptions"]["order"]


def test_bom_order_by_on_an_operations_column_sorts_operations(monkeypatch):
    _patch_bom(monkeypatch)
    out = json.loads(_run(read_bom(
        _bom_client(), _INDEX, _FakeRBAC(), _SESSION,
        target="BOM for part P", where="", fields="", limit=0, order_by="OprSeq desc")))

    assert [o["OprSeq"] for o in out["operations"]] == [20, 10]
    assert "operations" in out["resolved"]["assumptions"]["order"]


def test_bom_order_by_on_neither_set_returns_both_column_lists(monkeypatch):
    _patch_bom(monkeypatch)
    out = json.loads(_run(read_bom(
        _bom_client(), _INDEX, _FakeRBAC(), _SESSION,
        target="BOM for part P", where="", fields="", limit=0, order_by="Bogus")))

    assert out["error"] in ("unknown_order_column", "order_not_applicable")
    assert "materials_columns" in out["valid"]
    assert "operations_columns" in out["valid"]


def test_bom_limit_trims_materials_with_an_explicit_partial_warning(monkeypatch):
    """A silently truncated BOM reads as the full recipe — the worst possible
    silent wrong answer on this route."""
    _patch_bom(monkeypatch)
    out = json.loads(_run(read_bom(
        _bom_client(), _INDEX, _FakeRBAC(), _SESSION,
        target="BOM for part P", where="", fields="", limit=1)))

    assert len(out["materials"]) == 1
    assert "PARTIAL" in out["resolved"]["assumptions"]["limit_trim"]


# --------------------------------------------------------------------------- #
# time phase — sort the MERGED plants, and never over-report the row count
# --------------------------------------------------------------------------- #

def _tp_rows(n, plant):
    return [{"PartNum": "P", "Plant": plant, "DueDate": f"2026-{m:02d}-01",
             "Quantity": m} for m in range(1, n + 1)]


def test_timephase_row_count_matches_the_records_it_shipped(configured_plants):
    """row_count used to report the FULL merged count beside a trimmed
    `records` — 200 claimed, 25 shipped, unannounced."""
    client = _PVClient(timephase_by_plant={
        p: _tp_rows(12, p) for p in configured_plants})
    out = json.loads(_run(read_timephase(
        client, _FakeRBAC(), _SESSION, where="PartNum = 'P'",
        target="time phase for part P", fields="", limit=25)))

    assert out["row_count"] == len(out["records"])
    assert "limit_trim" in out["resolved"]["assumptions"]


def test_timephase_sorts_the_merged_set_not_each_plant(configured_plants):
    """The configured-plant gather EXTENDS a flat list per plant, so the merged set had
    no coherent order at all — it was plant-grouped-then-arbitrary."""
    client = _PVClient(timephase_by_plant={
        "101": [{"PartNum": "P", "Plant": "101", "DueDate": "2026-12-01"}],
        "202": [{"PartNum": "P", "Plant": "202", "DueDate": "2026-01-01"}],
    })
    out = json.loads(_run(read_timephase(
        client, _FakeRBAC(), _SESSION, where="PartNum = 'P'",
        target="time phase for part P", fields="", limit=25,
        order_by="DueDate asc")))

    dates = [r["DueDate"] for r in out["records"]]
    assert dates == sorted(dates)
    # Plant 202's row must precede plant 101's — proof it is not concatenation.
    assert out["records"][0]["Plant"] == "202"


# --------------------------------------------------------------------------- #
# THE announcement-channel guard — `soft` must reach resolved.assumptions
# --------------------------------------------------------------------------- #

def test_where_used_surfaces_the_guards_alias_note():
    """Batch 3's "alias what maps and ANNOUNCE it" contract produced the alias
    side-effect with no announcement on these routes: `soft` was seeded with
    get_arg_notes() and then handed to no recognizer at all."""
    note = {"arg_aliased": {"select": "fields"}}
    out = json.loads(_run(where_used(
        _WUClient(rows=[_row()]), _FakeRBAC(), _SESSION,
        target="what is part PART-100 used to make", limit=5, soft=note)))

    assert out["resolved"]["assumptions"]["arg_aliased"] == {"select": "fields"}


def test_where_used_honours_the_aliased_select_projection():
    """The aliased `select` projection must reach routes that have no `fields`
    parameter of their own."""
    out = json.loads(_run(where_used(
        _WUClient(rows=[_row()]), _FakeRBAC(), _SESSION,
        target="what is part PART-100 used to make",
        limit=5, fields="PartNum")))

    assert set(out["records"][0]) == {"PartNum"}
    assert "columns" in out["resolved"]["assumptions"]["fields"]


@pytest.mark.parametrize("order_by", ["qty * cost", "sum(OrderQty) desc"])
def test_expression_refusal_is_uniform_across_routes(order_by):
    """A caller must never see two different messages for the same bad clause
    depending on which route it happened to hit (INV-1 uniformity)."""
    out = json.loads(_run(where_used(
        _WUClient(rows=[_row()]), _FakeRBAC(), _SESSION,
        target="what is part PART-100 used to make", order_by=order_by)))
    assert out["error"] == "order_expression_unsupported"

    # The attachment route materialises its own rows too, so it must route the
    # refusal through the same sort_records/order_refusal pair, not invent one.
    att_out = json.loads(_run(read_attachments(
        _AttClient(_att_dataset(2)), _AttIdx(), _FakeRBAC(), _SESSION,
        target="ap invoice attachments", where="GroupID = 'BATCH-100'",
        order_by=order_by)))
    assert att_out["error"] == out["error"]
    assert att_out["message"] == out["message"]

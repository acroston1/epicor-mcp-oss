"""Caller `order_by` on epicor_read (Area 2).

`order_by` must be honored, not parsed and discarded: a discarded `order_by`
leaves the read on `default_order_clause`'s newest-first order. The most harmful
case is due-date INVERSION: a `DueDate asc` request on JobHead/POHeader (an
expediting worklist) comes back in the exact opposite order, looking correct.
Server-side ordering + $top/$skip is a TRUE top-N, not a
page-local sort — that is what makes honoring it safe.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from epicor_mcp.tools._aggregate import aggregate_records
from epicor_mcp.tools._inline_schema import parse_order_by
from epicor_mcp.tools.read import _decode_cursor
from tests.test_read_routing_e2e import _clear_caches, _Client, _Idx, _make


# --------------------------------------------------------------------------- #
# parse_order_by
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expect", [
    ("DueDate", [("DueDate", "asc")]),
    ("DueDate desc", [("DueDate", "desc")]),
    ("OrderDate DESC", [("OrderDate", "desc")]),
    ("PONum, POLine", [("PONum", "asc"), ("POLine", "asc")]),
])
def test_parse_order_by(raw, expect):
    assert parse_order_by(raw) == (expect, "")


@pytest.mark.parametrize("raw", [
    "UnitPrice * XRelQty desc",
    "sum(SalesAmt) desc",
    "count(PONum) desc",
    "strftime('%Y-%m', OrderDate)",
])
def test_parse_order_by_rejects_expressions(raw):
    """$orderby 500s on these — the refusal converts to a rollup, it is not a
    dead end. Never strip them silently."""
    assert parse_order_by(raw) == ([], "expression")


def test_bare_column_is_ascending():
    """If this ever inherits the newest-first house style it silently
    RE-CREATES the due-date inversion this change fixes."""
    assert parse_order_by("DueDate")[0][0][1] == "asc"


# --------------------------------------------------------------------------- #
# Plain read: caller order beats the newest-first default, server-side
# --------------------------------------------------------------------------- #

# ReqSvc/ReqHead: NOT in HEAVY_SERVICES, so this exercises the OData path
# (GetRows would hide the $orderby param behind a whereClause). Its curated
# default order is "RequestDate desc" — the thing a caller order_by must beat.
REQHEAD = [
    {"field_name": "PONum", "field_type": "Edm.Int32"},
    {"field_name": "RequestDate", "field_type": "Edm.DateTimeOffset"},
    {"field_name": "OrderDate", "field_type": "Edm.DateTimeOffset"},
    {"field_name": "DueDate", "field_type": "Edm.DateTimeOffset"},
    {"field_name": "VendorNum", "field_type": "Edm.Int32"},
]


def _reqhead_read(monkeypatch, **kw):
    _clear_caches()
    idx = _Idx({("Erp.BO.ReqSvc", "ReqHead"): REQHEAD},
               entity_sets={"Erp.BO.ReqSvc": ["ReqHead"]},
               hosts={"reqhead": [{"service_id": "Erp.BO.ReqSvc",
                                   "entity_set_name": "ReqHead"}]})
    client = _Client(get_result={"value": [{"PONum": 1}]})
    fn = _make(idx, client, monkeypatch)
    out = json.loads(asyncio.run(fn(target="Erp.BO.ReqSvc/ReqHead", **kw)))
    return client, out


def test_caller_order_reaches_the_wire_and_beats_the_default(monkeypatch):
    """The due-date inversion. Default here is 'RequestDate desc'."""
    client, out = _reqhead_read(monkeypatch, order_by="DueDate asc")
    assert client.gets[-1][1]["$orderby"] == "DueDate asc"
    assert out["resolved"]["order"] == "DueDate asc"
    assert out["resolved"]["order_source"] == "caller"


def test_no_order_by_keeps_the_newest_first_default(monkeypatch):
    """Regression fence for the documented default-sort contract."""
    client, out = _reqhead_read(monkeypatch)
    assert client.gets[-1][1]["$orderby"] == "RequestDate desc"
    assert out["resolved"]["order_source"] == "default"


def test_order_column_absent_from_the_projection_is_carried(monkeypatch):
    """A sort column projected away is invisible — the model cannot see WHY
    the rows are in that order and re-queries."""
    client, out = _reqhead_read(
        monkeypatch, fields="PONum", order_by="DueDate asc")
    assert "DueDate" in client.gets[-1][1]["$select"]
    assert out["assumptions"]["order_columns_added"] == ["DueDate"]


def test_unknown_order_column_is_a_precise_error(monkeypatch):
    _client, out = _reqhead_read(monkeypatch, order_by="Frobnicate desc")
    assert out["error"] == "unknown_columns"


def test_confident_order_typo_is_corrected(monkeypatch):
    client, out = _reqhead_read(monkeypatch, order_by="DueDte asc")
    assert client.gets[-1][1]["$orderby"] == "DueDate asc"
    assert out["assumptions"]["order_corrected"] == {"DueDte": "DueDate"}


def test_expression_order_by_returns_a_rollup_conversion(monkeypatch):
    _client, out = _reqhead_read(monkeypatch, order_by="RequestDate * 2 desc")
    assert out["error"] == "order_expression_unsupported"
    assert "group_by" in out["retry_with"]


def test_order_by_rides_the_cursor(monkeypatch):
    """Page 2 re-ordered under a $skip computed against page 1's ordering
    silently duplicates and drops rows — the same trap `having` documents."""
    _clear_caches()
    idx = _Idx({("Erp.BO.ReqSvc", "ReqHead"): REQHEAD},
               entity_sets={"Erp.BO.ReqSvc": ["ReqHead"]},
               hosts={"reqhead": [{"service_id": "Erp.BO.ReqSvc",
                                   "entity_set_name": "ReqHead"}]})
    client = _Client(get_result={"value": [{"PONum": i} for i in range(2)]})
    fn = _make(idx, client, monkeypatch)
    out = json.loads(asyncio.run(fn(
        target="Erp.BO.ReqSvc/ReqHead", order_by="DueDate asc", limit=2)))
    assert _decode_cursor(out["next_cursor"])["order_by"] == "DueDate asc"


# --------------------------------------------------------------------------- #
# Rollup: order_by ranks the GROUPS, client-side over a completed scan
# --------------------------------------------------------------------------- #

ROWS = [
    {"V": "A", "Q": 1, "P": 100.0},
    {"V": "B", "Q": 9, "P": 5.0},
]


def test_rollup_order_by_names_the_second_aggregate():
    """Without this the rollup ranked by the FIRST aggregate and returned a
    wrong ranking under a right-looking header."""
    res = aggregate_records(
        ROWS, group_by="V",
        aggregate="sum(Q) as TotalQty, sum(P) as TotalValue",
        order_by="TotalValue desc")
    assert [r["V"] for r in res["records"]] == ["A", "B"]


def test_rollup_default_first_aggregate_desc_is_unchanged():
    res = aggregate_records(
        ROWS, group_by="V",
        aggregate="sum(Q) as TotalQty, sum(P) as TotalValue")
    assert [r["V"] for r in res["records"]] == ["B", "A"]


def test_rollup_order_by_a_group_key():
    res = aggregate_records(ROWS, group_by="V", aggregate="sum(Q) as TotalQty",
                            order_by="V desc")
    assert [r["V"] for r in res["records"]] == ["B", "A"]


def test_rollup_order_runs_after_having():
    res = aggregate_records(
        ROWS, group_by="V", aggregate="sum(P) as TotalValue",
        having="TotalValue > 10", order_by="TotalValue desc")
    assert [r["V"] for r in res["records"]] == ["A"]


def test_rollup_ignores_an_unrelated_sort_key_rather_than_fabricating_a_rank():
    """A key that is neither a group key nor an aggregate alias cannot rank
    anything — fall back to the documented default, never sort by None."""
    res = aggregate_records(ROWS, group_by="V", aggregate="sum(Q) as TotalQty",
                            order_by="Nonsense desc")
    assert [r["V"] for r in res["records"]] == ["B", "A"]

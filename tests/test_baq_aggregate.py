"""Unit tests for BAQ aggregate / GROUP BY composition (``baq.py``).

The bug these pin: ``epicor_baq(action='create')`` could only emit a flat
``select … from … join`` — it had no vocabulary for count/sum/avg, so
"total number of POs per buyer" was inexpressible and the model thrashed on
raw column lists. ``fields="BuyerID, count(PONum)"`` must now compose a
GROUP BY aggregate. Live Epicor acceptance is proven separately; these tests
pin the parsing + SQL shape with no network.
"""

from __future__ import annotations

import pytest

from epicor_mcp.tools.baq import (
    _compose_baq_sql,
    _parse_agg,
    _resolve_fields,
    _split_agg_alias,
)


def _tbl(alias, table_name, full_name, cols):
    """Build a resolved-table dict shaped like _resolve_tables emits."""
    return {
        "full_name": full_name,
        "table_name": table_name,
        "alias": alias,
        "cols": {c.lower(): c for c in cols},
    }


POHEADER = _tbl(
    "POHeader", "POHeader", "Erp.POHeader",
    ["Company", "PONum", "BuyerID", "OrderDate", "DocTotalOrder", "VendorNum"],
)
PODETAIL = _tbl(
    "PODetail", "PODetail", "Erp.PODetail",
    ["Company", "PONum", "POLine", "OrderQty", "PartNum"],
)


# --------------------------------------------------------------------------- #
# _parse_agg
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("term,expected", [
    ("count(PONum)", ("count", False, "PONum")),
    ("COUNT(POHeader.PONum)", ("count", False, "POHeader.PONum")),
    ("count(*)", ("count", False, "*")),
    ("count()", ("count", False, "*")),
    ("cnt(PONum)", ("count", False, "PONum")),
    ("sum(OrderQty)", ("sum", False, "OrderQty")),
    ("total(DocTotalOrder)", ("sum", False, "DocTotalOrder")),
    ("avg(DocTotalOrder)", ("avg", False, "DocTotalOrder")),
    ("average(DocTotalOrder)", ("avg", False, "DocTotalOrder")),
    ("min(OrderDate)", ("min", False, "OrderDate")),
    ("maximum(OrderDate)", ("max", False, "OrderDate")),
    ("count(distinct VendorNum)", ("count", True, "VendorNum")),
    # Not aggregates:
    ("BuyerID", None),
    ("POHeader.PONum", None),
    ("", None),
    # sum(*)/avg(*) are nonsense -> not a valid aggregate.
    ("sum(*)", None),
    ("avg()", None),
])
def test_parse_agg(term, expected):
    assert _parse_agg(term) == expected


# --------------------------------------------------------------------------- #
# _resolve_fields aggregate branch
# --------------------------------------------------------------------------- #

def test_resolve_fields_count_with_group_by():
    selected, unknown, _valid, _dym, aggregates = _resolve_fields(
        [POHEADER], "BuyerID, count(PONum)")
    assert unknown == []
    assert selected == [("POHeader", "BuyerID")]        # becomes GROUP BY
    assert len(aggregates) == 1
    a = aggregates[0]
    assert (a["fn"], a["alias"], a["real"], a["distinct"]) == (
        "count", "POHeader", "PONum", False)
    assert a["out"] == "Count_PONum"


@pytest.mark.parametrize("term,core,alias", [
    ("sum(OrderQty) as TotalQty", "sum(OrderQty)", "TotalQty"),
    ("count(*) as n", "count(*)", "n"),
    ("sum(OrderDtl.OrderQty) as amt", "sum(OrderDtl.OrderQty)", "amt"),
    ("count(PONum)", "count(PONum)", None),      # no alias clause
    ("OrderHed.CustNum", "OrderHed.CustNum", None),
])
def test_split_agg_alias(term, core, alias):
    assert _split_agg_alias(term) == (core, alias)


def test_resolve_fields_aggregate_alias_honored():
    # The "sum(x) as Name" must compose an aggregate (not be
    # rejected as an unknown column) and use the caller's alias as the output.
    selected, unknown, _v, _d, aggregates = _resolve_fields(
        [POHEADER], "BuyerID, sum(DocTotalOrder) as TotalAmt")
    assert unknown == []
    assert selected == [("POHeader", "BuyerID")]
    assert len(aggregates) == 1
    assert aggregates[0]["out"] == "TotalAmt"
    assert (aggregates[0]["fn"], aggregates[0]["real"]) == ("sum", "DocTotalOrder")


def test_resolve_fields_count_star_alias_honored():
    _s, _u, _v, _d, aggregates = _resolve_fields([POHEADER], "count(*) as NumRows")
    assert aggregates[0]["real"] is None
    assert aggregates[0]["out"] == "NumRows"


def test_resolve_fields_count_star_grand_total():
    selected, unknown, _v, _d, aggregates = _resolve_fields([POHEADER], "count(*)")
    assert selected == [] and unknown == []
    assert aggregates[0]["real"] is None and aggregates[0]["out"] == "Count_All"


def test_resolve_fields_distinct_labels_out_alias():
    _s, _u, _v, _d, aggregates = _resolve_fields(
        [POHEADER], "count(distinct VendorNum)")
    assert aggregates[0]["out"] == "CountDistinct_VendorNum"
    assert aggregates[0]["distinct"] is True


def test_resolve_fields_duplicate_out_alias_deduped():
    # Two aggregates that would collide on out-alias get a numeric suffix.
    _s, _u, _v, _d, aggregates = _resolve_fields(
        [POHEADER], "count(PONum), count(distinct PONum), count(PONum)")
    outs = [a["out"] for a in aggregates]
    assert len(outs) == len(set(o.lower() for o in outs)), outs


def test_resolve_fields_unknown_agg_column_reported():
    selected, unknown, _v, dym, aggregates = _resolve_fields(
        [POHEADER], "BuyerID, sum(Nonexistent)")
    assert "sum(Nonexistent)" in unknown
    assert aggregates == []
    assert dym.get("sum(Nonexistent)") is not None


def test_resolve_fields_no_aggregate_is_backward_compatible():
    selected, unknown, _v, _d, aggregates = _resolve_fields(
        [POHEADER], "BuyerID, OrderDate")
    assert aggregates == []
    assert selected == [("POHeader", "BuyerID"), ("POHeader", "OrderDate")]


# --------------------------------------------------------------------------- #
# _compose_baq_sql aggregate branch
# --------------------------------------------------------------------------- #

def test_compose_group_by_single_table():
    aggregates = [{"fn": "count", "alias": "POHeader", "real": "PONum",
                   "distinct": False, "out": "Count_PONum"}]
    sql, joins = _compose_baq_sql(
        [POHEADER], [("POHeader", "BuyerID")], "", aggregates)
    assert "count([POHeader].[PONum]) as [Count_PONum]" in sql
    assert "[POHeader].[BuyerID] as [POHeader_BuyerID]" in sql
    assert sql.strip().endswith("group by [POHeader].[BuyerID]")
    assert joins == []


def test_compose_grand_total_no_group_by():
    aggregates = [{"fn": "count", "alias": None, "real": None,
                   "distinct": False, "out": "Count_All"}]
    sql, _joins = _compose_baq_sql([POHEADER], [], "", aggregates)
    assert "count(*) as [Count_All]" in sql
    assert "group by" not in sql.lower()


def test_compose_group_by_comes_after_where():
    aggregates = [{"fn": "sum", "alias": "PODetail", "real": "OrderQty",
                   "distinct": False, "out": "Sum_OrderQty"}]
    sql, _joins = _compose_baq_sql(
        [POHEADER, PODETAIL], [("POHeader", "BuyerID")],
        "[POHeader].[OrderDate] >= '2026-01-01'", aggregates)
    where_pos = sql.lower().index("where")
    group_pos = sql.lower().index("group by")
    assert where_pos < group_pos
    assert "sum([PODetail].[OrderQty]) as [Sum_OrderQty]" in sql
    # The two tables were still joined on Company + PONum.
    assert "inner join Erp.PODetail" in sql


def test_compose_distinct_inner():
    aggregates = [{"fn": "count", "alias": "POHeader", "real": "VendorNum",
                   "distinct": True, "out": "CountDistinct_VendorNum"}]
    sql, _joins = _compose_baq_sql([POHEADER], [], "", aggregates)
    assert "count(distinct [POHeader].[VendorNum])" in sql

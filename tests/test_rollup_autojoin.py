"""Auto-join decision for header rollups that reach a detail column.

The bug this pins: "open sales orders by customer, by month, by part" targeted
the header (OrderHed) but grouped/summed OrderDtl columns (PartNum, OrderQty).
Without auto-joining the child, those columns validate against the header alone
and come back unknown_columns — the ~15-call thrash. ``_rollup_reaches_child``
is the decision that flips on the header/detail join in that case.
"""
from __future__ import annotations

from epicor_mcp.tools.read import _rollup_reaches_child

# OrderHed owns these; PartNum/OrderQty/DocExtPriceDtl live on OrderDtl.
HEADER = {"Company", "CustNum", "CustomerName", "OrderDate", "Plant", "OpenOrder"}


def test_reaches_child_when_group_by_names_detail_column():
    assert _rollup_reaches_child(
        HEADER, "CustomerName, month(OrderDate), PartNum", "sum(OrderQty) as q")


def test_reaches_child_when_only_aggregate_is_on_detail():
    assert _rollup_reaches_child(HEADER, "CustNum", "sum(OrderQty) as q")


def test_no_join_when_everything_is_on_the_header():
    # month(OrderDate) unwraps to OrderDate, which the header has.
    assert not _rollup_reaches_child(
        HEADER, "CustNum, month(OrderDate)", "count(*) as n")


def test_no_join_when_parent_cols_unknown():
    # Empty schema -> can't prove a column is missing; don't force a join.
    assert not _rollup_reaches_child(set(), "PartNum", "sum(OrderQty) as q")


def test_colon_shorthand_aggregate_is_understood():
    # The 'Col:fn' shorthand must be parsed for the reach check too.
    assert _rollup_reaches_child(HEADER, "CustNum", "OrderQty:sum")

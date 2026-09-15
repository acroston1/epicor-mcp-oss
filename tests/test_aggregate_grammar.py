"""Grammar tolerance for ``epicor_read`` rollups (``_aggregate.parse_aggregates``).

The bug these pin: a "prepare an open sales order by customer, by month, by
part" report thrashed because the model's natural aggregate spellings were
rejected. ``parse_aggregates`` must now accept the ``Col:fn`` shorthand,
function synonyms (total/average/cnt/...), and a qualified ``Table.Col`` field —
so the no-BAQ rollup path succeeds on the first attempt.
"""
from __future__ import annotations

import pytest

from epicor_mcp.tools._aggregate import parse_aggregates


@pytest.mark.parametrize("spec,expected", [
    # Canonical form still works.
    ("sum(OrderQty) as qty", [{"func": "sum", "field": "OrderQty", "alias": "qty"}]),
    ("count(*)", [{"func": "count", "field": "*", "alias": "count"}]),
    # The "Col:fn" shorthand the model reflexively types.
    ("OrderQty:sum", [{"func": "sum", "field": "OrderQty", "alias": "sum_OrderQty"}]),
    ("PONum:count as n", [{"func": "count", "field": "PONum", "alias": "n"}]),
    # Function synonyms fold to the canonical five.
    ("total(OrderQty)", [{"func": "sum", "field": "OrderQty", "alias": "sum_OrderQty"}]),
    ("average(UnitPrice)", [{"func": "avg", "field": "UnitPrice", "alias": "avg_UnitPrice"}]),
    ("cnt(PONum)", [{"func": "count", "field": "PONum", "alias": "count_PONum"}]),
    ("maximum(OrderDate)", [{"func": "max", "field": "OrderDate", "alias": "max_OrderDate"}]),
    # Qualified field: the table qualifier is stripped (rollup rows are flat).
    ("sum(OrderDtl.OrderQty) as amt",
     [{"func": "sum", "field": "OrderQty", "alias": "amt"}]),
    ("OrderDtl.OrderQty:sum",
     [{"func": "sum", "field": "OrderQty", "alias": "sum_OrderQty"}]),
    # Multiple aggregates in one spec.
    ("sum(OrderQty) as qty, sum(DocExtPriceDtl) as amount",
     [{"func": "sum", "field": "OrderQty", "alias": "qty"},
      {"func": "sum", "field": "DocExtPriceDtl", "alias": "amount"}]),
    ("", []),
])
def test_parse_aggregates_accepts(spec, expected):
    assert parse_aggregates(spec) == expected


@pytest.mark.parametrize("spec", [
    "sum(*)",          # sum/avg/min/max need a column
    "avg()",           # empty arg
    "bogus(OrderQty)",  # unknown function
    "OrderQty",        # not an aggregate at all
])
def test_parse_aggregates_rejects(spec):
    with pytest.raises(ValueError):
        parse_aggregates(spec)


# =========================================================================== #
# `having` must never be dropped in SILENCE.
#
# Only the group_by branch warned. summary, distinct and plain reads dropped
# the threshold without a word and returned MORE rows than were asked for --
# which reads as the filtered answer. That is exactly the failure the group_by
# branch's own comment describes; the other three paths just didn't say it.
# =========================================================================== #

def test_having_applied_to_a_grand_total():
    from epicor_mcp.tools._aggregate import aggregate_records

    rows = [{"Q": 5.0}, {"Q": 6.0}]
    passing = aggregate_records(rows, aggregate="sum(Q)", having="sum_Q > 1")
    assert passing["records"] == [{"sum_Q": 11.0}]
    assert passing.get("having")

    failing = aggregate_records(rows, aggregate="sum(Q)", having="sum_Q > 100")
    assert failing["records"] == [], "the total does not meet the threshold"


def test_having_on_distinct_warns_instead_of_vanishing():
    from epicor_mcp.tools._aggregate import aggregate_records

    out = aggregate_records([{"P": "A"}, {"P": "B"}], distinct="P",
                            having="x > 1")
    assert out["having_warning"], "a dropped threshold must announce itself"
    assert out["having_applied"] is False
    assert "UNFILTERED" in out["having_warning"]


def test_having_without_any_aggregation_warns():
    """`having` alone never even reached the aggregation code.

    run_odata/run_getrows only called aggregate_records when
    (group_by or aggregate or distinct), so a `having` with none of them was
    dropped before it got there — and it is persisted into cursor_ctx, so
    every subsequent page was silently unfiltered too.
    """
    from epicor_mcp.tools._aggregate import _warn_having_ignored

    result = {"records": [{"OnHandQty": 1}], "record_count": 1}
    _warn_having_ignored(result, "OnHandQty > 100", "plain")
    assert result["having_applied"] is False
    assert "UNFILTERED" in result["having_warning"]
    # INV-1 flavour: name the parameter that WOULD work.
    assert "`where`" in result["having_warning"]


def test_no_having_means_no_warning():
    """Fence: the warning must not appear on every plain read."""
    from epicor_mcp.tools._aggregate import _warn_having_ignored

    result = {"records": [], "record_count": 0}
    _warn_having_ignored(result, "", "plain")
    assert "having_warning" not in result
    assert "having_applied" not in result


def test_engine_plain_read_surfaces_the_ignored_having():
    """End to end through run_odata, not just the helper."""
    import asyncio
    import json as _json

    from epicor_mcp.tools._engine import run_odata

    class _C:
        async def get(self, url, api_key, params=None):
            return {"value": [{"OnHandQty": 1}, {"OnHandQty": 2}]}

    out = _json.loads(asyncio.run(run_odata(
        _C(), "Erp.BO.PartWhseSvc", "PartWhse", "K",
        filter="Plant eq '10'", select="", orderby="", top=100,
        expand="", count_only=False, having="OnHandQty > 100",
        format="json")))

    assert out["having_warning"]
    assert out["having_applied"] is False
    # The rows really are unfiltered — the warning is telling the truth.
    assert out["record_count"] == 2

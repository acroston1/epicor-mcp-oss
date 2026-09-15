"""Computed/arithmetic expressions across both tools.

Arithmetic expressions must preserve the requested operands: substituting
AvgCost with ExtCost would silently change the meaning of inventory value.

The translation contracts represented by these synthetic cases are:
  $filter=UnitPrice mul 2 gt 100     -> 200, genuinely applied
  $filter=UnitPrice * 2 gt 100       -> 400 "Syntax error at position 11"
  $orderby=UnitPrice mul 2 desc      -> 500 generic apology
  $apply=aggregate(...)              -> SILENTLY IGNORED (raw rows returned)
  GetRows whereClause `OnHandQty * 2 > 100` -> 200; `... mul 2 gt 100` -> 500
So: arithmetic belongs in $filter only, and aggregation must stay client-side.
"""

from __future__ import annotations

import json

import pytest

from epicor_mcp.tools._aggregate import (
    aggregate_records,
    eval_expr,
    parse_aggregates,
    parse_expr_fields,
)
from epicor_mcp.tools.query import (
    _extract_filter_identifiers,
    _odata_to_sql_where,
    _sql_to_odata_filter,
)
from epicor_mcp.tools.read import _recase_columns, _rollup_columns


# --------------------------------------------------------------------------- #
# (a) computed WHERE
# --------------------------------------------------------------------------- #

def test_sql_star_translates_to_odata_mul():
    assert (_sql_to_odata_filter("OnHandQty * AvgCost > 50000")
            == "OnHandQty mul AvgCost gt 50000")
    # The already-OData form does NOT trip the has_sql gate — the case a fix
    # placed only inside the SQL branch would miss entirely.
    assert (_sql_to_odata_filter("OnHandQty * AvgCost gt 50000")
            == "OnHandQty mul AvgCost gt 50000")


def test_odata_arithmetic_round_trips_back_to_sql():
    assert (_odata_to_sql_where("OnHandQty mul AvgCost gt 50000")
            == "OnHandQty * AvgCost > 50000")
    # Mixed filter takes the early-return shortcut; `mul` must still come back
    # as `*` or GetRows 500s.
    mixed = _odata_to_sql_where("Plant = '10' and OnHandQty mul AvgCost gt 50000")
    assert "OnHandQty * AvgCost" in mixed
    assert " mul " not in mixed


def test_arithmetic_words_are_not_column_candidates():
    assert (_extract_filter_identifiers("OnHandQty mul AvgCost gt 50000")
            == {"OnHandQty", "AvgCost"})


@pytest.mark.parametrize("where", [
    "OrderDate ge 2024-01-01",
    "OrderDate ge 2024-01-01T00:00:00Z",
    "Qty gt -5",
    "PartNum eq '100-000200-03'",
])
def test_minus_does_not_corrupt_dates_or_negatives(where):
    """Highest-regression-risk case in the whole change."""
    assert _sql_to_odata_filter(where, date_columns=frozenset({"orderdate"})) == (
        where if "2024-01-01T" in where or "-5" in where or "'" in where
        else "OrderDate ge 2024-01-01T00:00:00Z")


def test_minus_still_translates_as_infix():
    assert _sql_to_odata_filter("Cost - Freight > 5") == "Cost sub Freight gt 5"


# --------------------------------------------------------------------------- #
# (b) computed AGGREGATE + having
# --------------------------------------------------------------------------- #

def test_parse_aggregates_accepts_expression():
    got = parse_aggregates("sum(OnHandQty * AvgCost) as InventoryValue")
    assert len(got) == 1
    assert got[0]["func"] == "sum"
    assert got[0]["alias"] == "InventoryValue"
    assert "OnHandQty" in got[0]["expr"] and "AvgCost" in got[0]["expr"]


def test_plain_aggregate_dict_shape_unchanged():
    """Regression lock: existing specs must parse bit-identically (no `expr`)."""
    assert parse_aggregates("sum(ExtPrice) as total") == [
        {"func": "sum", "field": "ExtPrice", "alias": "total"}]
    assert parse_aggregates("count(*)") == [
        {"func": "count", "field": "*", "alias": "count"}]


def test_parse_expr_fields():
    assert parse_expr_fields("OnHandQty * AvgCost") == ["OnHandQty", "AvgCost"]
    assert parse_expr_fields("(A + B) / 2") == ["A", "B"]


def test_eval_expr_skips_missing_components():
    assert eval_expr("OnHandQty * AvgCost", {"OnHandQty": 10, "AvgCost": 5}) == 50
    # A missing/None component makes the ROW None — never a silent 0.
    assert eval_expr("OnHandQty * AvgCost", {"OnHandQty": 10}) is None
    assert eval_expr("OnHandQty * AvgCost",
                     {"OnHandQty": 10, "AvgCost": None}) is None


def test_expression_aggregate_computes_per_row():
    rows = [
        {"PartNum": "A", "OnHandQty": 10, "AvgCost": 5},
        {"PartNum": "B", "OnHandQty": 2, "AvgCost": 100},
        {"PartNum": "B", "OnHandQty": 3, "AvgCost": None},  # skipped, not 0
    ]
    out = aggregate_records(
        rows, group_by="PartNum",
        aggregate="sum(OnHandQty * AvgCost) as InventoryValue")
    by_part = {r["PartNum"]: r["InventoryValue"] for r in out["records"]}
    assert by_part == {"A": 50, "B": 200}


def test_expression_component_columns_ride_on_the_projection():
    """Guards the silent null-bucket collapse documented at read.py."""
    cols = _rollup_columns("", "sum(OnHandQty * AvgCost) as InventoryValue")
    assert "OnHandQty" in cols and "AvgCost" in cols


def test_expression_aggregate_recases_columns():
    assert (_recase_columns("sum(onhandqty * avgcost)", {"OnHandQty", "AvgCost"})
            == "sum(OnHandQty * AvgCost)")


def test_unknown_component_column_warns_not_silently_zeroes():
    rows = [{"PartNum": "A", "OnHandQty": 10, "AvgCost": 5}]
    out = aggregate_records(
        rows, group_by="PartNum", aggregate="sum(OnHandQty * AvgCst) as V")
    assert "aggregate_warning" in out
    assert "AvgCst" in out["aggregate_unknown_fields"]
    assert "AvgCost" in out["aggregate_unknown_fields"]["AvgCst"]


@pytest.mark.parametrize("having", [
    "InventoryValue > 50000",
    "sum(OnHandQty * AvgCost) > 50000",
])
def test_having_filters_groups_by_alias_or_spec(having):
    rows = [
        {"PartNum": "A", "OnHandQty": 60000, "AvgCost": 1},
        {"PartNum": "B", "OnHandQty": 40000, "AvgCost": 1},
    ]
    out = aggregate_records(
        rows, group_by="PartNum",
        aggregate="sum(OnHandQty * AvgCost) as InventoryValue",
        having=having)
    assert [r["PartNum"] for r in out["records"]] == ["A"]
    assert out["having"] == "InventoryValue > 50000"


def test_unparseable_having_is_flagged_not_silently_dropped():
    rows = [{"PartNum": "A", "Qty": 5}]
    out = aggregate_records(rows, group_by="PartNum",
                            aggregate="sum(Qty) as Total", having="nonsense")
    assert "having" not in out
    assert "having_warning" in out
    assert len(out["records"]) == 1


def test_having_survives_cursor_round_trip():
    from epicor_mcp.tools.read import _decode_cursor, _encode_cursor
    ctx = {"target": "Part", "group_by": "PartNum",
           "aggregate": "sum(OnHandQty * AvgCost) as V",
           "having": "V > 100", "skip": 100}
    assert _decode_cursor(_encode_cursor(ctx))["having"] == "V > 100"


def test_invalid_aggregate_message_names_the_supported_path():
    """The 'message that only says NO' is what produced the ExtCost swap."""
    with pytest.raises(ValueError) as ei:
        parse_aggregates("median(OnHandQty)")
    text = str(ei.value)
    assert "epicor_baq" in text or "count/sum/avg/min/max" in text

    with pytest.raises(ValueError) as ei2:
        parse_aggregates("sum(CASE WHEN x THEN y END)")
    assert "epicor_baq" in str(ei2.value)


def test_count_of_expression_is_rejected_clearly():
    with pytest.raises(ValueError, match="not meaningful"):
        parse_aggregates("count(OnHandQty * AvgCost)")


# --------------------------------------------------------------------------- #
# (c) epicor_baq create — computed fields
# --------------------------------------------------------------------------- #

PARTWHSE = {
    "full_name": "Erp.PartWhse", "table_name": "PartWhse", "alias": "pw",
    "cols": {"partnum": "PartNum", "onhandqty": "OnHandQty",
             "company": "Company"},
}
PART = {
    "full_name": "Erp.Part", "table_name": "Part", "alias": "p",
    "cols": {"partnum": "PartNum", "avgcost": "AvgCost", "company": "Company"},
}


def test_baq_plain_field_alias_is_peeled():
    """Regression for the `term`-vs-`core` bug: `X as Y` used to be unknown."""
    from epicor_mcp.tools.baq import _resolve_fields_ex
    selected, unknown, _v, _d, _a, _c = _resolve_fields_ex([PART], "PartNum as P")
    assert unknown == []
    assert selected == [("p", "PartNum")]


def test_baq_create_composes_computed_column():
    from epicor_mcp.tools.baq import _compose_baq_sql, _resolve_fields_ex
    selected, unknown, _v, _d, aggs, computed = _resolve_fields_ex(
        [PARTWHSE, PART], "PartNum, (OnHandQty * AvgCost) as InventoryValue")
    assert unknown == []
    assert len(computed) == 1
    sql, _joins = _compose_baq_sql([PARTWHSE, PART], selected, "", aggs, computed)
    assert "[pw].[OnHandQty] * [p].[AvgCost]" in sql
    assert "as [InventoryValue]" in sql
    # A computed column must NEVER become a grouping key.
    assert "group by" not in sql.lower()


def test_baq_computed_where_is_qualified():
    from epicor_mcp.tools.baq import _translate_where
    sql, unknown, unparsed = _translate_where(
        "OnHandQty * AvgCost > 50000", [PARTWHSE, PART])
    assert unknown == [] and unparsed == []
    assert "[pw].[OnHandQty]" in sql and "[p].[AvgCost]" in sql
    assert "> 50000" in sql


def test_baq_unparseable_where_fragment_is_reported():
    from epicor_mcp.tools.baq import _translate_where
    _sql, _unknown, unparsed = _translate_where(
        "SOMETHING WEIRD HERE", [PARTWHSE, PART])
    assert unparsed == ["SOMETHING WEIRD HERE"]


def test_baq_unknown_component_column_returns_inv1():
    from epicor_mcp.tools.baq import _resolve_fields_ex
    _s, unknown, _v, did, _a, computed = _resolve_fields_ex(
        [PARTWHSE, PART], "(OnHandQty * AvgCst) as V")
    assert computed == []
    assert "AvgCst" in unknown
    assert "AvgCost" in did.get("AvgCst", [])


def test_baq_plain_where_still_translates():
    """Regression: the ordinary condition path is untouched."""
    from epicor_mcp.tools.baq import _translate_where
    sql, unknown, unparsed = _translate_where("PartNum = 'X'", [PART])
    assert unknown == [] and unparsed == []
    assert sql == "[p].[PartNum] = 'X'"


# =========================================================================== #
# (d) The BAQ *run* filter path — `_baq_filter_to_odata`.
#
# Sections (a)-(c) cover epicor_read and BAQ *create*. The run path had ZERO
# test references anywhere in tests/ and did NOT receive the arithmetic
# translation, so the gap was invisible: a model that follows the read
# envelope's own advice to pivot to epicor_baq for an inventory-value filter
# hit the same wall it had just escaped.
# =========================================================================== #

def test_baq_run_filter_translates_arithmetic():
    from epicor_mcp.tools.baq import _baq_filter_to_odata

    assert (_baq_filter_to_odata("OnHandQty * AvgCost > 50000")
            == "OnHandQty mul AvgCost gt 50000")
    assert (_baq_filter_to_odata("Qty * Cost >= 10 and PartNum like '%WIDGET%'")
            == "Qty mul Cost ge 10 and contains(PartNum,'WIDGET')")


def test_baq_run_filter_matches_the_read_path():
    """The two tools must agree on what an arithmetic filter means."""
    from epicor_mcp.tools.baq import _baq_filter_to_odata
    from epicor_mcp.tools.query import _sql_to_odata_filter

    where = "OnHandQty * AvgCost > 50000"
    assert _baq_filter_to_odata(where) == _sql_to_odata_filter(where)


@pytest.mark.parametrize("where", [
    "PartNum eq '10000-2000-0003'",     # a synthetic part number, hyphens intact
    "contains(PartNum,'A-B')",          # already-valid OData with a function
    "OrderDate ge 2025-01-01T00:00:00Z",
])
def test_baq_run_filter_leaves_valid_input_alone(where):
    """Regression fence: the new branch is arithmetic-only."""
    from epicor_mcp.tools.baq import _baq_filter_to_odata

    assert _baq_filter_to_odata(where) == where


def test_baq_run_filter_keeps_iso_date_coercion():
    """The pre-existing date handling must survive the new branch."""
    from epicor_mcp.tools.baq import _baq_filter_to_odata

    assert (_baq_filter_to_odata("OrderDate ge '2025-01-01'")
            == "OrderDate ge 2025-01-01T00:00:00Z")

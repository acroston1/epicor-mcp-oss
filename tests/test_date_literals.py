"""Date-literal + filter-translation coercion in the where -> OData path.

The quoted ISO date (`TranDate >= '2025-07-20'`) is the shape a model
reflexively emits — every OTHER value it writes is quoted — and it was the one
shape neither existing rewrite handled, so Epicor answered "A binary operator
with incompatible types was detected. Found operand types 'Edm.DateTimeOffset'
and 'Edm.String'". Two adjacent gaps in the same code path: `between` was never
translated at all, and `IS NULL` reached the wire verbatim.

The crux of the whole file is `test_string_column_date_shaped_value_untouched`:
coercion is TYPE-driven, never shape-driven.
"""

from __future__ import annotations

import pytest

from epicor_mcp.tools.query import (
    _odata_to_sql_where,
    _sql_to_odata_filter,
    _to_odata_datetime,
    date_columns_for,
)

DC = frozenset({"trandate", "lasttrandate", "orderdate", "countddate"})


def test_quoted_iso_date_becomes_unquoted_z():
    """A quoted date literal must become a typed OData datetime."""
    assert (_sql_to_odata_filter("TranDate >= '2025-07-20'", date_columns=DC)
            == "TranDate ge 2025-07-20T00:00:00Z")


@pytest.mark.parametrize("sql_op,odata_op", [
    (">=", "ge"), ("<=", "le"), (">", "gt"), ("<", "lt"),
    ("=", "eq"), ("<>", "ne"), ("!=", "ne"),
    ("eq", "eq"), ("ne", "ne"), ("ge", "ge"),
    ("le", "le"), ("gt", "gt"), ("lt", "lt"),
])
def test_all_comparators(sql_op, odata_op):
    out = _sql_to_odata_filter(f"TranDate {sql_op} '2025-07-20'", date_columns=DC)
    assert out == f"TranDate {odata_op} 2025-07-20T00:00:00Z"


def test_already_odata_branch():
    """No SQL operator -> the short-circuit branch. Easily-missed second path."""
    assert (_sql_to_odata_filter("TranDate eq '2025-07-20'", date_columns=DC)
            == "TranDate eq 2025-07-20T00:00:00Z")


def test_string_column_date_shaped_value_untouched():
    """THE crux: a date-SHAPED value on a non-date column keeps its quotes."""
    assert (_sql_to_odata_filter("PartNum = '2025-07-20'", date_columns=DC)
            == "PartNum eq '2025-07-20'")


@pytest.mark.parametrize("dc", [DC, None])
def test_part_number_never_corrupted(dc):
    """Regression guard on the anchored regex, with and without the type gate."""
    assert (_sql_to_odata_filter("PartNum = '12345-6789-0001'", date_columns=dc)
            == "PartNum eq '12345-6789-0001'")
    assert "2025-07-20" in _sql_to_odata_filter(
        "PartNum like '%2025-07-20%'", date_columns=dc)
    # A rev code that merely CONTAINS an ISO date must not be rewritten.
    assert (_sql_to_odata_filter("RevisionNum = 'REV-2025-07-20-A'", date_columns=dc)
            == "RevisionNum eq 'REV-2025-07-20-A'")


def test_bare_and_legacy_forms_unregressed():
    """Captures today's already-passing behavior at both existing rewrites."""
    for where in ("TranDate >= 2025-07-20",
                  "TranDate >= datetime'2025-07-20T00:00:00'"):
        assert (_sql_to_odata_filter(where, date_columns=DC)
                == "TranDate ge 2025-07-20T00:00:00Z")


def test_time_bearing_literals():
    assert (_sql_to_odata_filter("TranDate >= '2025-07-20 14:30:00'", date_columns=DC)
            == "TranDate ge 2025-07-20T14:30:00Z")
    already = "TranDate ge 2025-07-20T14:30:00Z"
    assert _sql_to_odata_filter(already, date_columns=DC) == already


@pytest.mark.parametrize("where", [
    "TranDate >= '2025-07-20'",
    "TranDate between '2025-01-01' and '2025-06-30'",
    "OnHandQty * AvgCost > 50000",
    "LastTranDate IS NULL",
    "OrderDate ge 2024-01-01",
])
def test_translation_is_idempotent(where):
    once = _sql_to_odata_filter(where, date_columns=DC)
    assert _sql_to_odata_filter(once, date_columns=DC) == once


def test_between_on_a_date_column_includes_the_whole_final_day():
    """Regression coverage: test between on a date column includes the whole final day."""
    assert (_sql_to_odata_filter(
        "TranDate between '2025-01-01' and '2025-06-30'", date_columns=DC)
        == "(TranDate ge 2025-01-01T00:00:00Z and "
           "TranDate lt 2025-07-01T00:00:00Z)")
    # Month and year rollover must carry, not string-increment.
    assert ("TranDate lt 2026-01-01T00:00:00Z" in _sql_to_odata_filter(
        "TranDate between '2025-12-01' and '2025-12-31'", date_columns=DC))


def test_between_on_a_non_date_column_stays_inclusive_le():
    """Only DATE bounds are advanced — a numeric range means exactly le."""
    assert (_sql_to_odata_filter("OrderNum between 100 and 200", date_columns=DC)
            == "(OrderNum ge 100 and OrderNum le 200)")
    # A string column that merely LOOKS like a date keeps `le` and its quotes:
    # `RevisionNum` is Edm.String, so it is not in DC.
    assert (_sql_to_odata_filter(
        "RevisionNum between '2025-01-01' and '2025-06-30'", date_columns=DC)
        == "(RevisionNum ge '2025-01-01' and RevisionNum le '2025-06-30')")


def test_between_bound_carrying_a_time_is_taken_literally():
    """An explicit time bound means what it says — do not advance it a day."""
    out = _sql_to_odata_filter(
        "TranDate between '2025-01-01' and '2025-06-30 17:00'", date_columns=DC)
    assert " le " in out and "2025-07-01" not in out


def test_is_null_translation():
    assert (_sql_to_odata_filter("LastTranDate IS NULL", date_columns=DC)
            == "LastTranDate eq null")
    assert (_sql_to_odata_filter("LastTranDate IS NOT NULL", date_columns=DC)
            == "LastTranDate ne null")
    out = _sql_to_odata_filter(
        "OnHandQty > 0 AND (LastTranDate < '2025-07-20' OR LastTranDate IS NULL)",
        date_columns=DC)
    assert "LastTranDate lt 2025-07-20T00:00:00Z" in out
    assert "LastTranDate eq null" in out


def test_compound_filter():
    out = _sql_to_odata_filter(
        "TranDate >= '2025-01-01' and PartNum = '12345-6789-0001' "
        "and TranDate <= '2025-06-30'", date_columns=DC)
    assert "TranDate ge 2025-01-01T00:00:00Z" in out
    assert "TranDate le 2025-06-30T00:00:00Z" in out
    assert "PartNum eq '12345-6789-0001'" in out


def test_uppercase_and_or_preserved():
    """Epicor tolerates AND/OR; rewriting them is out of scope."""
    out = _sql_to_odata_filter("A eq 1 AND B eq 2 OR C eq 3", date_columns=None)
    assert " AND " in out and " OR " in out


def test_to_odata_datetime_rejects_non_dates():
    assert _to_odata_datetime("'12345-6789-0001'") is None
    assert _to_odata_datetime("REV-2025-07-20-A") is None
    assert _to_odata_datetime("'2025-07-20'") == "2025-07-20T00:00:00Z"


def test_getrows_roundtrip_preserved():
    odata = _sql_to_odata_filter("TranDate >= '2025-07-20'", date_columns=DC)
    assert _odata_to_sql_where(odata) == "TranDate >= '2025-07-20T00:00:00Z'"


def test_getrows_roundtrip_is_null():
    odata = _sql_to_odata_filter("LastTranDate IS NULL", date_columns=DC)
    assert _odata_to_sql_where(odata) == "LastTranDate is null"


# --------------------------------------------------------------------------- #
# date_columns_for — type set from the index, never a name heuristic
# --------------------------------------------------------------------------- #

class _Idx:
    def __init__(self, rows):
        self._rows = rows

    def get_fields(self, service, entity_set):
        if self._rows is None:
            raise RuntimeError("entity not indexed")
        return self._rows


def _clear_cache():
    from epicor_mcp.tools.query import _DATE_COLS_CACHE
    _DATE_COLS_CACHE.clear()


def test_date_columns_for_from_mock_index():
    """A Boolean named *Date is excluded — the name heuristic is disproven."""
    _clear_cache()
    idx = _Idx([
        {"field_name": "TranDate", "field_type": "Edm.DateTimeOffset"},
        {"field_name": "PartNum", "field_type": "Edm.String"},
        {"field_name": "EnableDueDate", "field_type": "Edm.Boolean"},
    ])
    assert date_columns_for(idx, "Svc", "Ent") == frozenset({"trandate"})


@pytest.mark.parametrize("rows", [None, []])
def test_date_columns_for_unknown_entity_returns_none(rows):
    """None, not an empty set — an empty set would silently disable coercion."""
    _clear_cache()
    assert date_columns_for(_Idx(rows), "Svc", f"Ent{rows!r}") is None
    # With None the anchored fallback still coerces.
    assert (_sql_to_odata_filter("TranDate >= '2025-07-20'", date_columns=None)
            == "TranDate ge 2025-07-20T00:00:00Z")


def test_date_columns_for_entity_with_no_dates_is_empty_not_none():
    _clear_cache()
    idx = _Idx([{"field_name": "PartNum", "field_type": "Edm.String"}])
    got = date_columns_for(idx, "Svc", "NoDates")
    assert got == frozenset()
    assert got is not None

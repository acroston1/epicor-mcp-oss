"""BAQ user-defined (``_c``) column guards.

A base-table reference to a user-defined column can parse successfully but
fail when the SQL query executes. The synthetic fixtures exercise this gap
using order-line and order-release extension fields.

UD columns live in the ``_UD``
extension table, joined on ``SysRowID = ForeignSysRowID``. The BO/OData surface
hides that — ``epicor_query`` reads ``OrderRel.Note_c`` fine — and so does
the Swagger-derived schema index, which mirrors every ``_c`` field onto the base
table. BAQ SQL does not. ``ParseFromSQL`` accepts the base-table reference,
resolves it to a field with an empty ``DataType``, saves happily, and the
runtime then can't bind the column.

    [OrderRel].[Note_c]        -> DataType ''         -> 400 on run
    [OrderRel_UD].[Note_c]     -> DataType 'nvarchar' -> 200 + data

Two guards: a pre-flight that rewrites the ref before any network call, and a
post-parse check that catches the whole unresolvable-field class. The post-parse
check must NOT fire on calculated or subquery-projected fields, which carry an
empty ``DataType`` legitimately — it keys off ``QueryTableDesigner.TableType``
(``DB`` real table vs ``SQ`` subquery alias vs ``TT`` calculated).
"""

from __future__ import annotations

from epicor_mcp.tools._baq_helpers import (
    find_ud_field_refs,
    find_unresolved_parsed_fields,
)


class _Index:
    """Schema index carrying the ``_UD`` extension tables and nothing else."""

    _UD_FIELDS = {
        "Erp.OrderRel_UD": ["Note_c", "Flagged_c", "ForeignSysRowID"],
        "Erp.OrderDtl_UD": ["ShipNote_c", "ForeignSysRowID"],
    }

    def get_fields(self, full_table_name):
        return [
            {"field_name": f}
            for f in self._UD_FIELDS.get(full_table_name, [])
        ]


REL = "FROM Erp.OrderRel as [OrderRel]"
REL_UD_JOIN = (
    "LEFT OUTER JOIN Erp.OrderRel_UD as [OrderRel_UD] "
    "ON OrderRel.SysRowID = OrderRel_UD.ForeignSysRowID"
)


# ---------------------------------------------------------------------------
# Pre-flight: find_ud_field_refs
# ---------------------------------------------------------------------------


def test_base_table_ud_ref_is_flagged_with_a_rewrite():
    sql = f"SELECT [OrderRel].[Note_c] as [OrderRel_Note_c] {REL}"
    refs = find_ud_field_refs(sql, _Index())

    assert len(refs) == 1
    assert refs[0]["ref"] == "[OrderRel].[Note_c]"
    assert refs[0]["ud_table"] == "Erp.OrderRel_UD"
    assert refs[0]["use_instead"] == (
        "[OrderRel_UD].[Note_c] as [OrderRel_UD_Note_c]"
    )
    # LEFT OUTER, so rows without a _UD record survive.
    assert refs[0]["add_join"].startswith("LEFT OUTER JOIN Erp.OrderRel_UD")
    assert "OrderRel.SysRowID = OrderRel_UD.ForeignSysRowID" in refs[0]["add_join"]


def test_reading_from_the_ud_table_is_not_flagged():
    sql = (
        f"SELECT [OrderRel_UD].[Note_c] as [OrderRel_UD_Note_c] "
        f"{REL} {REL_UD_JOIN}"
    )
    assert find_ud_field_refs(sql, _Index()) == []


def test_existing_ud_alias_is_reused_and_keeps_its_casing():
    """Joined the _UD table but still referenced the base table."""
    sql = f"SELECT [OrderRel].[Note_c] as [X] {REL} {REL_UD_JOIN}"
    refs = find_ud_field_refs(sql, _Index())

    assert len(refs) == 1
    # No second join proposed — the query already has one.
    assert refs[0]["add_join"] == ""
    # Alias map is lower-cased internally; the suggestion must not be.
    assert refs[0]["use_instead"] == (
        "[OrderRel_UD].[Note_c] as [OrderRel_UD_Note_c]"
    )


def test_non_ud_fields_are_ignored():
    sql = f"SELECT [OrderRel].[ReqDate] as [OrderRel_ReqDate] {REL}"
    assert find_ud_field_refs(sql, _Index()) == []


def test_ud_field_without_a_known_ud_table_is_left_to_the_post_parse_guard():
    """No rewrite we can prove -> stay quiet rather than guess a join."""
    sql = "SELECT [Part].[Widget_c] as [Part_Widget_c] FROM Erp.Part as [Part]"
    assert find_ud_field_refs(sql, _Index()) == []


def test_every_flagged_ref_is_reported_once():
    sql = (
        "SELECT [OrderDtl].[ShipNote_c] as [A], "
        "[OrderRel].[Note_c] as [B], [OrderRel].[Note_c] as [C] "
        "FROM Erp.OrderHed as [OrderHed] "
        "INNER JOIN Erp.OrderDtl as [OrderDtl] "
        "  ON OrderDtl.Company = OrderHed.Company "
        "INNER JOIN Erp.OrderRel as [OrderRel] "
        "  ON OrderRel.Company = OrderDtl.Company"
    )
    refs = find_ud_field_refs(sql, _Index())
    assert sorted(r["ref"] for r in refs) == [
        "[OrderDtl].[ShipNote_c]",
        "[OrderRel].[Note_c]",
    ]


# ---------------------------------------------------------------------------
# Post-parse: find_unresolved_parsed_fields
# ---------------------------------------------------------------------------
#
# Fixtures below follow the shape of real ParseFromSQL responses.


def test_unbound_column_on_a_db_table_is_flagged():
    ds = {
        "QueryTableDesigner": [{"TableID": "OrderRel", "TableType": "DB"}],
        "QueryFieldDesigner": [
            {"TableID": "OrderRel", "FieldName": "OrderNum",
             "DataType": "int", "Formula": ""},
            {"TableID": "OrderRel", "FieldName": "Note_c",
             "DataType": "", "Formula": ""},
        ],
    }
    assert [d["ref"] for d in find_unresolved_parsed_fields(ds)] == [
        "[OrderRel].[Note_c]"
    ]


def test_subquery_and_calculated_fields_are_not_flagged():
    """Both carry an empty DataType legitimately — this BAQ runs fine."""
    ds = {
        "QueryTableDesigner": [
            {"TableID": "OrderDtl", "TableType": "DB"},
            {"TableID": "PartWhse", "TableType": "DB"},
            {"TableID": "Calculated", "TableType": "TT"},
            {"TableID": "PartWhseAgg", "TableType": "SQ"},
        ],
        "QueryFieldDesigner": [
            {"TableID": "OrderDtl", "FieldName": "PartNum",
             "DataType": "nvarchar", "Formula": ""},
            # aggregate inside the subquery
            {"TableID": "Calculated", "FieldName": "SumOnHand",
             "DataType": "", "Formula": "SUM(PartWhse.OnHandQty)"},
            # the subquery's projected column: blank DataType AND blank Formula
            {"TableID": "PartWhseAgg", "FieldName": "SumOnHand",
             "DataType": "", "Formula": ""},
        ],
    }
    assert find_unresolved_parsed_fields(ds) == []


def test_inline_formula_is_not_flagged():
    ds = {
        "QueryTableDesigner": [
            {"TableID": "OrderRel", "TableType": "DB"},
            {"TableID": "Calculated", "TableType": "TT"},
        ],
        "QueryFieldDesigner": [
            {"TableID": "OrderRel", "FieldName": "OrderNum",
             "DataType": "int", "Formula": ""},
            {"TableID": "Calculated", "FieldName": "Calculated_OpenQty",
             "DataType": "", "Formula": "(OrderRel.SellingReqQty - 1)"},
        ],
    }
    assert find_unresolved_parsed_fields(ds) == []


def test_wholly_inaccessible_table_is_flagged():
    """IM.IMCustomer parses but no column binds — Epicor only says so on run."""
    ds = {
        "QueryTableDesigner": [{"TableID": "IMCustomer", "TableType": "DB"}],
        "QueryFieldDesigner": [
            {"TableID": "IMCustomer", "FieldName": "CustID",
             "DataType": "", "Formula": ""},
            {"TableID": "IMCustomer", "FieldName": "Flagged_c",
             "DataType": "", "Formula": ""},
        ],
    }
    assert len(find_unresolved_parsed_fields(ds)) == 2


def test_empty_dataset_is_safe():
    assert find_unresolved_parsed_fields({}) == []

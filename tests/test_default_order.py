"""Unit tests for the newest-first default sort (``default_order_clause``).

A plain listing with no $orderby comes back oldest-first (PK order), which
surfaced years-old rows as if current. ``default_order_clause`` picks the best
date column DESC — curated where the recency column isn't obvious, heuristic
otherwise, and empty when the entity has no usable date column.
"""

from __future__ import annotations

from epicor_mcp.tools._inline_schema import DEFAULT_ORDER, default_order_clause


def test_curated_entities_get_their_order():
    # Curated map wins when its column is present on the entity.
    assert default_order_clause(
        "Erp.BO.POSvc", "POHeader",
        ["PONum", "OrderDate", "DueDate"]) == "OrderDate desc"
    assert default_order_clause(
        "Erp.BO.JobEntrySvc", "JobHead",
        ["JobNum", "CreateDate", "DueDate"]) == "CreateDate desc"


def test_curated_falls_through_when_column_absent():
    # Curated column not on the entity -> heuristic picks another real date col.
    out = default_order_clause(
        "Erp.BO.POSvc", "POHeader", ["PONum", "DueDate"])
    assert out == "DueDate desc"


def test_heuristic_prefers_transaction_date_over_audit_date():
    cols = ["Num", "ChangeDate", "InvoiceDate", "Name"]
    assert default_order_clause(
        "Erp.BO.SomeSvc", "SomeEntity", cols) == "InvoiceDate desc"


def test_heuristic_preserves_real_column_casing():
    out = default_order_clause(
        "Erp.BO.SomeSvc", "SomeEntity", ["OrDeRdAtE", "X"])
    assert out == "OrDeRdAtE desc"


def test_no_date_column_returns_empty():
    assert default_order_clause(
        "Erp.BO.CustomerSvc", "Customers",
        ["CustNum", "CustID", "Name"]) == ""


def test_no_schema_uses_curated_only():
    # Without a column list we can't validate; curated still applies, heuristic
    # cannot (nothing to scan).
    assert default_order_clause("Erp.BO.POSvc", "POHeader", None) == "OrderDate desc"
    assert default_order_clause("Erp.BO.Unknown", "Nope", None) == ""


def test_every_curated_column_is_bare_col_plus_desc():
    for (svc, ent), clause in DEFAULT_ORDER.items():
        parts = clause.split()
        assert len(parts) == 2 and parts[1] == "desc", (svc, ent, clause)

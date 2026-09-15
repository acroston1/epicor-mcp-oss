"""Regression coverage: test denylist expressions."""

from __future__ import annotations

import pytest

from epicor_mcp.sql import denylist
from tests.wedge_fixtures import load

# --------------------------------------------------------------------------- #
# NEGATIVE — written BEFORE the fix. A denied column must not be smuggleable
# through ANY expression, in ANY clause.
# --------------------------------------------------------------------------- #

#: fixture -> the ``Schema.Table.Column`` the gate must name.
SMUGGLING_SHAPES = {
    # where sum([E].[PayRate]) > 0
    "deny_expr_where_sum_payrate": "Erp.EmpBasic.PayRate",
    # having max([E].[PayRate]) > 0
    "deny_expr_having_max_payrate": "Erp.EmpBasic.PayRate",
    # where [E].[PayRate] * 2 > 0
    "deny_expr_arith_payrate": "Erp.EmpBasic.PayRate",
    # where isnull([E].[PayRate], 0) > 0
    "deny_expr_isnull_payrate": "Erp.EmpBasic.PayRate",
    # select sum([E].[PayRate]) as [x]
    "deny_expr_select_sum_payrate": "Erp.EmpBasic.PayRate",
    # where year([E].[BirthDate]) = 1980  — PII, not pay, and still denied
    "deny_expr_year_birthdate": "Erp.EmpBasic.BirthDate",
    # where (case when [E].[PayRate] > 20 then 1 else 0 end) = 1
    "deny_expr_case_payrate": "Erp.EmpBasic.PayRate",
}


@pytest.mark.parametrize("fixture", sorted(SMUGGLING_SHAPES))
def test_a_denied_column_cannot_be_smuggled_through_an_expression(fixture):
    """THE security property: the statement is refused. Held before the fix
    (as a fail-closed anomaly) and must hold after it."""
    _, ds = load(fixture)
    denial = denylist.check_parsed_ds(ds)
    assert denial, f"{fixture} was NOT denied — a denied column escaped through an expression"


@pytest.mark.parametrize("fixture,column", sorted(SMUGGLING_SHAPES.items()))
def test_the_smuggled_column_is_named_not_just_refused(fixture, column):
    """And it is refused as a COLUMN denial, naming the column.

    Before the fix these were refused as *anomalies* — right answer, wrong
    reason, and the reason is what tells the caller (and the audit log) that a
    pay-rate column was reached for rather than that the parser confused us.
    """
    _, ds = load(fixture)
    denial = denylist.check_parsed_ds(ds)
    assert column in denial.denied_columns, (
        f"{fixture}: expected {column} in denied_columns, got "
        f"columns={denial.denied_columns} anomalies={denial.anomalies}"
    )


def test_the_denial_envelope_for_a_smuggled_column_names_the_column():
    _, ds = load("deny_expr_where_sum_payrate")
    env = denylist.denial_envelope(denylist.check_parsed_ds(ds), sql="select ...")
    assert env["success"] is False
    assert env["error"] == "column_access_denied"
    assert env["terminal"] is True
    assert "Erp.EmpBasic.PayRate" in env["message"]
    assert "NOT executed" in env["message"]


def test_a_denied_table_inside_an_expression_is_still_a_table_denial():
    """The table gate is evaluated on the QueryTable rows and is untouched by
    the expression path: an expression over a payroll table stays denied."""
    ds = {
        "QueryTable": [
            {"TableID": "PR", "DBSchemaName": "Erp", "DBTableName": "PREmpMas",
             "TableType": "DB"},
        ],
        "QueryWhereItem": [
            {"TableID": "", "FieldName": "sum(PR.GrossPay)", "CompOp": ">", "RValue": "0"},
        ],
    }
    denial = denylist.check_parsed_ds(ds)
    assert "Erp.PREmpMas" in denial.denied_tables


def test_a_bare_sensitive_name_inside_a_where_expression_fails_closed():
    """No table prefix at all — the sensitive VOCABULARY still catches it, the
    same way it already does inside a computed Formula."""
    ds = {
        "QueryTable": [{"TableID": "A", "DBSchemaName": "Erp", "DBTableName": "JobHead",
                        "TableType": "DB"}],
        "QueryWhereItem": [{"TableID": "", "FieldName": "isnull(LaborRate, 0)",
                            "CompOp": ">", "RValue": "0"}],
    }
    denial = denylist.check_parsed_ds(ds)
    assert any("LaborRate" in c for c in denial.denied_columns), denial


def test_a_person_keyed_rate_inside_an_expression_is_denied():
    """``ChargeRate`` is compensation only on a person-keyed table, so this one
    can only be caught by RESOLVING the prefix — the vocabulary alone misses it.
    It is the case that proves the expression path really does attribute."""
    ds = {
        "QueryTable": [{"TableID": "LD", "DBSchemaName": "Erp", "DBTableName": "LaborDtl",
                        "TableType": "DB"}],
        "QueryWhereItem": [{"TableID": "", "FieldName": "(LD.ChargeRate * 2)",
                            "CompOp": ">", "RValue": "0"}],
    }
    denial = denylist.check_parsed_ds(ds)
    assert "Erp.LaborDtl.ChargeRate" in denial.denied_columns, denial


def test_an_expression_over_a_table_id_epicor_never_resolved_is_still_an_anomaly():
    """The anomaly-denial is RESERVED for a genuinely unresolvable table — it is
    not removed. A prefix that names no table in the tableset means we cannot
    say which table the column came from, so the query is denied."""
    ds = {
        "QueryTable": [{"TableID": "A", "DBSchemaName": "Erp", "DBTableName": "JobHead",
                        "TableType": "DB"}],
        "QueryWhereItem": [{"TableID": "", "FieldName": "sum(GHOST.Something)",
                            "CompOp": ">", "RValue": "0"}],
    }
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies and denial, denial


def test_a_non_expression_unattributable_column_still_denies():
    """Unchanged from before: a WHERE item with a NON-EMPTY TableID that names
    no table is an anomaly. The fix narrows nothing here."""
    ds = {
        "QueryTable": [{"TableID": "A", "DBSchemaName": "Erp", "DBTableName": "JobHead",
                        "TableType": "DB"}],
        "QueryWhereItem": [{"TableID": "GHOST", "FieldName": "Something", "RValue": "1"}],
    }
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies and denial


def test_a_bare_unprefixed_name_with_no_expression_still_denies():
    """``TableID=''`` and a FieldName that is a plain identifier is NOT an
    expression — there is nothing to attribute, so it stays an anomaly."""
    ds = {
        "QueryTable": [{"TableID": "A", "DBSchemaName": "Erp", "DBTableName": "JobHead",
                        "TableType": "DB"}],
        "QueryWhereItem": [{"TableID": "", "FieldName": "Something", "RValue": "1"}],
    }
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies and denial


def test_a_denied_name_inside_a_string_literal_in_an_expression_does_not_deny():
    """the gate reads Epicor's resolution, never text. A quoted literal
    is data. ``isnull(P.ClassID, 'LaborRate')`` is a clean query and the
    ``x.y``-looking text inside a literal must not be read as a column either."""
    ds = {
        "QueryTable": [{"TableID": "P", "DBSchemaName": "Erp", "DBTableName": "Part",
                        "TableType": "DB"}],
        "QueryWhereItem": [{"TableID": "", "FieldName": "isnull(P.ClassID, 'LaborRate')",
                            "CompOp": "=", "RValue": "'X'"}],
    }
    assert not denylist.check_parsed_ds(ds)

    literal_dotted = {
        "QueryTable": [{"TableID": "P", "DBSchemaName": "Erp", "DBTableName": "Part",
                        "TableType": "DB"}],
        "QueryWhereItem": [{"TableID": "", "FieldName": "isnull(P.PartNum, 'GHOST.Col')",
                            "CompOp": "=", "RValue": "'X'"}],
    }
    assert not denylist.check_parsed_ds(literal_dotted)


# --------------------------------------------------------------------------- #
# POSITIVE — the six shapes D1 refused. Each fixture is a live parse.
# --------------------------------------------------------------------------- #

LEGAL_SHAPES = (
    "expr_having_sum",      # having sum([OD].[OrderQty]) > 100
    "expr_having_count",    # having count(*) > 5
    "expr_where_year",      # where year([OH].[OrderDate]) = 2025
    "expr_where_arith",     # where [OD].[OrderQty] * [OD].[UnitPrice] > 1000
    "expr_where_case",      # where (case when ... end) = 1
    "expr_where_isnull",    # where isnull([P].[PartDescription], '') = ''
)


@pytest.mark.parametrize("fixture", LEGAL_SHAPES)
def test_a_legal_expression_predicate_is_not_denied(fixture):
    _, ds = load(fixture)
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies == [], denial.anomalies
    assert not denial, denial


@pytest.mark.parametrize("fixture", LEGAL_SHAPES)
def test_the_fixtures_really_do_carry_the_empty_tableid_expression_shape(fixture):
    """The premise, asserted rather than assumed. If Epicor ever starts
    attributing these to a TableID, this test says so before the next reader
    concludes the branch is dead code."""
    _, ds = load(fixture)
    where = ds["QueryWhereItem"]
    assert where, f"{fixture} carries no QueryWhereItem"
    assert where[0]["TableID"] == "", where[0]
    assert "(" in where[0]["FieldName"], where[0]


@pytest.mark.parametrize("fixture", ("expr_cte_alias", "expr_derived_alias"))
def test_an_expression_over_a_cte_or_derived_alias_is_not_an_anomaly(fixture):
    """Regression coverage: test an expression over a cte or derived alias is not an anomaly."""
    _, ds = load(fixture)
    alias_rows = [t for t in ds["QueryTable"] if t["TableType"] == "SQ"]
    assert alias_rows, f"{fixture} carries no SQ row — the premise is gone"
    assert ds["QueryWhereItem"][0]["TableID"] == ""
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies == [], denial.anomalies
    assert not denial, denial


def test_a_denied_name_on_a_cte_alias_still_fails_closed():
    """The same SQ path must not become the hole either: a CTE that projects a
    pay column out under its own name is caught on the vocabulary."""
    ds = {
        "QueryTable": [
            {"TableID": "OD", "DBSchemaName": "Erp", "DBTableName": "LaborDtl",
             "TableType": "DB"},
            {"TableID": "c", "DBSchemaName": "", "DBTableName": "guid", "TableType": "SQ"},
        ],
        "QueryWhereItem": [{"TableID": "", "FieldName": "isnull(c.LaborRate, 0)",
                            "CompOp": ">", "RValue": "0"}],
    }
    denial = denylist.check_parsed_ds(ds)
    assert any("LaborRate" in c for c in denial.denied_columns), denial


def test_the_dialect_block_teaches_only_shapes_the_gate_permits():
    """``sql/tool.py`` tells the model to write ``having sum(x) > 1000``. The
    server must not teach a form it then permanently refuses."""
    _, ds = load("expr_having_sum")
    from epicor_mcp.sql.tool import SQL_PARAM_DESCRIPTION

    assert "having sum(x) > 1000" in SQL_PARAM_DESCRIPTION
    assert not denylist.check_parsed_ds(ds)


# --------------------------------------------------------------------------- #
# `unattributed_denies=False` — the saved-BAQ relaxation, and its four limits
# --------------------------------------------------------------------------- #


def _synthetic_invoice_ds(where_items: list[dict]) -> dict:
    """An allowed synthetic invoice definition with the supplied WHERE rows."""
    return {
        "QueryTable": [
            {"TableID": "IH", "TableType": "DB",
             "DBSchemaName": "Erp", "DBTableName": "InvcHead"},
            {"TableID": "ExampleData", "TableType": "DB",
             "DBSchemaName": "Ice", "DBTableName": "UD01"},
        ],
        "QueryWhereItem": where_items,
    }


#: Synthetic unqualified system and cross-subquery references have no TableID.
#: Neither contains a denied column, and both declared tables are allowed.
_SYNTHETIC_UNATTRIBUTED_REFS = [
    {"TableID": "", "FieldName": "CurrentUserID"},
    {"TableID": "", "FieldName": "ExampleData_Date01"},
]


def test_an_unattributable_reference_still_denies_ad_hoc_sql_by_default():
    """The default is UNCHANGED and must stay fail-closed: in ad-hoc SQL the
    caller writes the text, so a reference we cannot place could be a denied
    column hidden behind an alias we cannot resolve."""
    denial = denylist.check_parsed_ds(_synthetic_invoice_ds(_SYNTHETIC_UNATTRIBUTED_REFS))
    assert denial, "ad-hoc SQL must still fail closed on an unplaceable reference"
    assert denial.anomalies
    assert not denial.denied_tables and not denial.denied_columns


def test_the_saved_baq_path_does_not_deny_on_an_unattributable_reference():
    """A saved BAQ has no smuggling vector — the caller sends an ID, not SQL —
    and `CurrentUserID` / a Query Parameter / a cross-subquery reference is the
    ordinary vocabulary of a hand-authored BAQ."""
    denial = denylist.check_parsed_ds(
        _synthetic_invoice_ds(_SYNTHETIC_UNATTRIBUTED_REFS), unattributed_denies=False
    )
    assert not denial, f"Allowed synthetic dashboard must run; got {denial.anomalies}"
    assert denial.allowed_tables == ["Erp.InvcHead", "Ice.UD01"]


def test_the_relaxation_does_not_reach_the_table_deny_list():
    ds = _synthetic_invoice_ds(_SYNTHETIC_UNATTRIBUTED_REFS)
    ds["QueryTable"].append(
        {"TableID": "PR", "TableType": "DB",
         "DBSchemaName": "Erp", "DBTableName": "PREmpMas"}
    )
    denial = denylist.check_parsed_ds(ds, unattributed_denies=False)
    assert denial.denied_tables == ["Erp.PREmpMas"]
    assert denial, "a payroll table denies regardless of the attribution rule"


def test_the_relaxation_does_not_reach_a_RESOLVED_denied_column():
    """The reference is placeable — so it is judged, exactly as before."""
    ds = _synthetic_invoice_ds(_SYNTHETIC_UNATTRIBUTED_REFS)
    ds["QueryTable"].append(
        {"TableID": "E", "TableType": "DB",
         "DBSchemaName": "Erp", "DBTableName": "EmpBasic"}
    )
    ds["QueryWhereItem"].append({"TableID": "E", "FieldName": "PayRate"})
    denial = denylist.check_parsed_ds(ds, unattributed_denies=False)
    assert denial.denied_columns == ["Erp.EmpBasic.PayRate"]


def test_a_crashed_evaluator_denies_even_with_the_relaxation_on():
    """An evaluator that could not complete tells us nothing about the
    statement, on ANY path — so it is a HARD anomaly, tracked separately, and
    `unattributed_denies=False` must never reach it."""

    class Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    denial = denylist.check_parsed_ds(Exploding(), unattributed_denies=False)
    assert denial, "a crashed deny-list evaluator must still fail closed"
    assert any("must fail closed" in a for a in denial.anomalies)

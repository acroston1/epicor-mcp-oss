"""Denylist enforcement against Epicor's resolved object list.

Tests cover tables, column aliases, CTEs, subqueries, derived tables and both
halves of a union. The local denylist must still apply when the underlying
service account can read a sensitive table or column.
"""

from __future__ import annotations

import pytest

from epicor_mcp.sql import denylist
from tests.wedge_fixtures import load

DENIED_SHAPES = {
    "deny_projected": "Erp.LaborDtl.LaborRate",     # in the SELECT list
    "deny_filtered": "Erp.EmpBasic.LaborRate",      # in the WHERE clause only
    "deny_sorted": "Erp.EmpBasic.LaborRate",        # in the ORDER BY only
    "deny_formula": "Erp.LaborDtl.LaborRate",       # inside a computed column
    "deny_cte": "Erp.LaborDtl.LaborRate",           # inside a CTE
    "deny_subquery": "Erp.LaborDtl.LaborRate",      # inside a scalar/IN subquery
    "deny_derived": "Erp.LaborDtl.LaborRate",       # inside a derived table
    "deny_union_arm2": "Erp.EmpBasic.LaborRate",    # in the SECOND union arm
}


@pytest.mark.parametrize("fixture,column", sorted(DENIED_SHAPES.items()))
def test_a_denied_column_is_caught_in_every_shape(fixture, column):
    _, ds = load(fixture)
    denial = denylist.check_parsed_ds(ds)
    assert denial, f"{fixture} was NOT denied"
    assert column in denial.denied_columns, denial.denied_columns


def test_labordtl_laborrate_is_denied_the_control_that_did_not_survive():
    """`LaborRate` is a pay column: without a deny-list entry,
    `select top 5 [LaborDtl].[LaborRate] …` returns pay rates, so the denylist
    must cover it on every table that carries it, even with no other policy."""
    assert denylist.is_denied_column("Erp.LaborDtl", "LaborRate")
    assert denylist.is_denied_column("Erp.EmpBasic", "LaborRate")
    assert denylist.is_denied_column("Erp.JobOper", "LaborRate")
    assert denylist.is_denied_column("Erp.FSCallSv", "LaborRate")
    assert denylist.is_denied_column("Erp.LabExpCd", "LaborRate")
    assert denylist.is_denied_column("Erp.LaborDtlImport", "LaborRate")


def test_the_vocabulary_covers_a_table_nobody_listed():
    """The wedge applies the sensitive vocabulary to EVERY table, so a table the
    curator never thought of is still covered without a corpus."""
    assert denylist.is_denied_column("Erp.SomeTableInventedToday", "LaborRate")
    assert denylist.is_denied_column("Erp.Whatever", "SocSecNum")
    assert denylist.is_denied_column("Erp.Whatever", "BirthDate")
    assert denylist.is_denied_column("Erp.Whatever", "PayRateHourly")
    assert denylist.is_denied_column("Erp.Whatever", "SSNMask")


def test_aliases_of_the_same_column_are_all_denied():
    """Feature S1's `test_denylist_covers_column_aliases`: the deny decision is
    made on Epicor's resolved DBFieldName, so renaming the OUTPUT changes
    nothing."""
    ds = {
        "QueryTable": [
            {"TableID": "X", "DBSchemaName": "Erp", "DBTableName": "LaborDtl",
             "TableType": "DB"}
        ],
        "QueryField": [
            {"TableID": "X", "DBTableName": "LaborDtl", "DBFieldName": "LaborRate",
             "FieldName": "LaborRate", "Alias": "TotallyInnocentNumber",
             "DataType": "decimal"}
        ],
    }
    denial = denylist.check_parsed_ds(ds)
    assert denial.denied_columns == ["Erp.LaborDtl.LaborRate"]


def test_wholly_sensitive_tables_are_denied_at_table_level():
    _, ds = load("deny_table")
    denial = denylist.check_parsed_ds(ds)
    assert denial.denied_tables == ["Erp.PREmpMas"]
    for table in (
        "Erp.PREmpMas", "Erp.PayrollExp", "Erp.ExtPREmp", "Erp.EmpBasicAttch",
        "Ice.SecurityGroup", "Ice.UserFile", "Ice.UserComp", "Ice.SysUserFile",
        "Ice.ExtSecurity",
    ):
        assert denylist.is_denied_table(table), table


#: Every entity name beginning `Pr` in `data/service_index.db` (139 of them),
#: split by hand-checked family. Embedded rather than read from the index so the
#: assertion is deterministic and needs no live Epicor — these are schema names,

#: `_matches` is a case-insensitive prefix test, so `erp.prodgrup` and
#: `erp.project` both started with `erp.pr`. LIVE, `select top 3 [G].[ProdCode]
#: from Erp.ProdGrup` was refused with *"hold payroll / security data and are
#: denied to every user"*. The product-group master and every project-costing
#: table in the install were denied to the whole company.
_PAYROLL_NAMES = (
    "PRCheck PRCheckTGLC PRChkDed PRChkDtl PRChkGrp PRChkTax PRClass PRClsDed "
    "PRClsDedEGLC PRClsTax PRClsTaxEGLC PRDeduct PREmpDed PREmpMas PREmpMasAttch "
    "PREmpRt PREmpTax PREmployees PRHoldy PRSyst PRTaxCrd PRTaxDtl PRTaxDtlSearches "
    "PRTaxExp PRTaxMas PRTaxTbl PRW2Dtl PRW2DtlBox PRW2DtlExport PRWrkCmp"
).split()
_NON_PAYROLL_PR_NAMES = (
    "PredictiveSearch PrefScheme PrefSchemeCtry PriceGroup PriceGrpValBrk "
    "PriceListInquiry PriceLst PriceLstAttch PriceLstParts PrjMkUp PrjRoleRt "
    "ProcessSet ProcessTask ProdActDays ProdCal ProdCalDay ProdCalPlantList "
    "ProdCalWeek ProdGrup ProdGrupPlt ProdTeam ProductVersion ProjChkLstType "
    "ProjFilter ProjMulti ProjPhase ProjPhaseGLC ProjRevenueRec Project "
    "ProjectAttch ProjectCost ProjectCst ProjectHour ProjectJob ProjectMilestone "
    "ProjectOrderLine ProjectPO ProjectQuot ProjectSumry ProjectTask "
    "PromptInstallSettings PropertyValueSearches PrcChg Prospect"
).split()


def test_the_payroll_family_is_denied_and_nothing_else_beginning_Pr_is():
    """The whole of F4, one assertion each way. The deny direction must not lose
    a single payroll table; the allow direction must not cost the company
    'sales by product group' or any project-costing question."""
    for name in _PAYROLL_NAMES:
        assert denylist.is_denied_table(f"Erp.{name}"), name
        assert denylist.is_denied_table(name), name
    for name in _NON_PAYROLL_PR_NAMES:
        assert not denylist.is_denied_table(f"Erp.{name}"), name
        assert not denylist.is_denied_table(name), name


def test_a_future_payroll_table_no_enumerated_root_names_is_still_denied():
    """The enumerated roots are GENERATED from today's schema index, so the
    structural backstop is what keeps the list fail-CLOSED: Epicor names every
    payroll table `PR` + an upper-case word, and every non-payroll `Pr` table is
    `Pr` + lower case. It runs on Epicor's own canonically-cased resolution."""
    assert denylist.is_denied_table("Erp.PRVacation")
    assert denylist.is_denied_table("Erp.PRGarnish")
    assert not denylist.is_denied_table("Erp.Prospect")
    assert not denylist.is_denied_table("Erp.PriceLst")


def test_ice_userfile_is_denied_by_us_even_though_it_parses_fine():
    """ParseFromSQL on Ice.UserFile returns 200 — only Execute refuses.
    Epicor's refusal is a free FLOOR, not the gate."""
    _, ds = load("deny_ice_userfile")
    denial = denylist.check_parsed_ds(ds)
    assert denial.denied_tables == ["Ice.UserFile"]


def test_empbasic_itself_stays_usable():
    """table-level denial removes a capability users have today. 'Who ran
    job 100001' must keep working; the pay rate must not."""
    _, ds = load("clean_empbasic")
    assert not denylist.check_parsed_ds(ds)
    assert not denylist.is_denied_table("Erp.EmpBasic")
    assert not denylist.is_denied_column("Erp.EmpBasic", "EmpID")
    assert not denylist.is_denied_column("Erp.EmpBasic", "FirstName")
    assert not denylist.is_denied_column("Erp.LaborDtl", "EmployeeNum")


def test_a_material_burden_rate_is_not_somebody_s_pay():
    """The denylist scopes ChargeRate to person-keyed tables. Denying MtlBurRate on a
    part would remove costing for no security gain."""
    assert not denylist.is_denied_column("Erp.Part", "BurdenRate")
    assert not denylist.is_denied_column("Erp.PartCost", "StdBurdenCost")
    assert denylist.is_denied_column("Erp.LaborDtl", "BurdenRate")
    assert denylist.is_denied_column("Erp.LaborDtl", "ChargeRate")


def test_clean_statements_are_not_denied():
    for fixture in ("clean_top", "clean_rollup", "wedge_rollup", "clean_in_subquery",
                    "gov_bounded_join", "deny_union_arm"):
        _, ds = load(fixture)
        assert not denylist.check_parsed_ds(ds), fixture


def test_an_in_subquery_does_not_produce_a_false_anomaly():
    """Epicor writes the SUBQUERY's id in RValue and its projected name in
    ToFieldName with no ToTableID. Treating that as unattributable would deny
    every `where x in (select ...)`."""
    _, ds = load("clean_in_subquery")
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies == []
    assert not denial


def test_only_db_rows_are_evaluated_and_an_odd_tabletype_denies():
    """SQ/TT rows carry GUIDs and blanks and are attributable to no BO.
    Ignoring them loses nothing; an UNKNOWN TableType is an anomaly => deny."""
    _, ds = load("deny_cte")
    types = {t["TableType"] for t in ds["QueryTable"]}
    assert types == {"DB", "SQ"}

    weird = {"QueryTable": [{"TableID": "Z", "DBTableName": "Part", "TableType": "XX"}]}
    assert denylist.check_parsed_ds(weird).anomalies


def test_a_db_row_with_no_table_name_denies():
    ds = {"QueryTable": [{"TableID": "Z", "DBSchemaName": "Erp", "DBTableName": "",
                          "TableType": "DB"}]}
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies and denial


def test_an_unattributable_column_reference_denies_the_whole_query():
    """fail closed when a referenced column cannot be placed."""
    ds = {
        "QueryTable": [{"TableID": "A", "DBSchemaName": "Erp", "DBTableName": "JobHead",
                        "TableType": "DB"}],
        "QueryWhereItem": [{"TableID": "GHOST", "FieldName": "Something", "RValue": "1"}],
    }
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies and denial


def test_an_ordinal_sort_is_not_treated_as_an_authz_anomaly():
    """`order by 1` parses to TableID='' / FieldName='1'. The lint refuses it;
    the deny-list must not mislabel it 'access denied'."""
    _, ds = load("sort_ordinal")
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies == []
    assert not denial


def test_a_bare_sensitive_name_inside_a_formula_fails_closed():
    ds = {
        "QueryTable": [{"TableID": "A", "DBSchemaName": "Erp", "DBTableName": "JobHead",
                        "TableType": "DB"},
                       {"TableID": "Calculated", "DBTableName": "", "TableType": "TT"}],
        "QueryField": [{"TableID": "Calculated", "IsCalculated": True,
                        "Formula": "sum(LaborRate * 2)", "FieldName": "C"}],
    }
    denial = denylist.check_parsed_ds(ds)
    assert any("LaborRate" in c for c in denial.denied_columns)


def test_the_evaluator_fails_closed_if_it_raises():
    class Exploding(dict):
        def get(self, *a, **k):  # noqa: D105
            raise RuntimeError("boom")

    denial = denylist.check_parsed_ds(Exploding())
    assert denial and denial.anomalies


def test_the_denial_envelope_is_a_product_feature_not_a_bare_no():
    _, ds = load("deny_filtered")
    env = denylist.denial_envelope(denylist.check_parsed_ds(ds), sql="select ...")
    assert env["success"] is False
    assert env["error"] == "column_access_denied"
    assert env["terminal"] is True
    assert "Erp.EmpBasic.LaborRate" in env["message"]
    assert "NOT executed" in env["message"]
    assert env["retry_with"]["how"]
    assert "EmpID" in env["valid"]["policy"]
    assert "never the SQL text" in env["detail"]["enforced_on"]


def test_a_denied_table_reports_the_table_error_code():
    _, ds = load("deny_table")
    env = denylist.denial_envelope(denylist.check_parsed_ds(ds))
    assert env["error"] == "table_access_denied"
    assert "Erp.PREmpMas" in env["message"]


def test_the_gate_never_text_matches_the_sql():
    """A table name hidden in a comment must not be an input, and a
    denied name in a STRING LITERAL must not deny a clean query."""
    ds = {
        "QueryTable": [{"TableID": "P", "DBSchemaName": "Erp", "DBTableName": "Part",
                        "TableType": "DB"}],
        "QueryField": [{"TableID": "P", "DBTableName": "Part", "DBFieldName": "PartNum",
                        "FieldName": "PartNum", "DataType": "nvarchar"}],
        "QueryWhereItem": [{"TableID": "P", "FieldName": "ClassID", "CompOp": "=",
                            "RValue": "'LaborRate'"}],
    }
    assert not denylist.check_parsed_ds(ds)
    import inspect

    source = inspect.getsource(denylist)
    assert "DisplayPhrase" not in source
    assert "re.search(sql" not in source


# --------------------------------------------------------------------------- #
# Aggregate sort keys: an expression is not an unattributable column
# --------------------------------------------------------------------------- #


def test_an_aggregate_sort_key_is_not_an_unattributable_column():
    """An aggregate sort key is an expression, not a column.
    `order by sum([OrderDtl].[ExtPriceDtl]) desc` resolves to
    QuerySortBy{TableID:'', FieldName:'sum(OrderDtl.ExtPriceDtl)'} — an
    EXPRESSION, not a column. A fail-closed rule that read the empty TableID
    as unattributable would DENY this ordinary query."""
    _, ds = load("agg_order_by")
    sort = ds["QuerySortBy"][0]
    assert sort["TableID"] == "" and sort["FieldName"] == "sum(OrderDtl.ExtPriceDtl)"
    denial = denylist.check_parsed_ds(ds)
    assert denial.anomalies == []
    assert not denial


def test_but_a_denied_column_inside_an_aggregate_sort_key_is_still_caught():
    """The fix must not become a hole: `order by sum([LD].[LaborRate]) desc`
    ranks by pay rate without ever selecting it."""
    _, ds = load("agg_order_by_denied")
    denial = denylist.check_parsed_ds(ds)
    assert "Erp.LaborDtl.LaborRate" in denial.denied_columns


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #


def test_both_spellings_of_the_social_security_number_are_denied():
    """Regression coverage: test both spellings of the social security number are denied."""
    for column in ("SocSecNum", "DspSocSecNum", "SSNum"):
        assert denylist.is_denied_column("Erp.EmpBasic", column), column
    # ...and the fence: matching stays PREFIX-based, never substring, so a
    # column that merely contains a denied word is not denied by accident.
    assert not denylist.is_denied_column("Erp.Part", "ClassName")
    assert not denylist.is_denied_column("Erp.OrderDtl", "ProcessName")
    assert not denylist.is_denied_column("Erp.Part", "PartNum")

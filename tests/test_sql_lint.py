"""Regression coverage: test sql lint."""

from __future__ import annotations

import pytest

from tests.harness.sql_lint import Category, Severity, Verdict, lint, mask_literals

# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #

GOOD_FILTERED_LIST = """
select top 20 [Part].[PartNum] as [PartNum], [Part].[PartDescription] as [PartDescription],
       [Part].[ClassID] as [ClassID]
from Erp.Part as [Part]
where [Part].[ClassID] = 'FG'
order by [Part].[PartNum] asc
"""

GOOD_JOIN = """
select top 20 [Part].[PartNum] as [PartNum], [PartWhse].[WarehouseCode] as [Warehouse],
       [PartWhse].[OnHandQty] as [OnHandQty]
from Erp.Part as [Part]
inner join Erp.PartWhse as [PartWhse]
  on [Part].[Company] = [PartWhse].[Company] and [Part].[PartNum] = [PartWhse].[PartNum]
where [PartWhse].[OnHandQty] > 0
order by [PartWhse].[OnHandQty] desc
"""

GOOD_ROLLUP = """
select top 100 [OrderDtl].[PartNum] as [PartNum], sum([OrderDtl].[ExtPriceDtl]) as [Revenue]
from Erp.OrderDtl as [OrderDtl]
where [OrderDtl].[RequestDate] >= '2025-01-01'
group by [OrderDtl].[PartNum]
having sum([OrderDtl].[ExtPriceDtl]) > 100000
order by sum([OrderDtl].[ExtPriceDtl]) desc
"""

GOOD_GRAND_TOTAL = "select count(*) as [N] from Erp.Part as [Part]"

GOOD_ANTIJOIN = """
select top 100 [A].[PartNum] as [PartNum]
from Erp.Part as [A]
left outer join Erp.PartTran as [B]
  on [A].[Company] = [B].[Company] and [A].[PartNum] = [B].[PartNum]
where [B].[PartNum] is null
"""

GOOD_DERIVED_COUNT_DISTINCT = """
select count(*) as [N]
from (select distinct [OrderDtl].[PartNum] as [PartNum] from Erp.OrderDtl as [OrderDtl]) as [t]
"""


@pytest.mark.parametrize(
    "sql",
    [
        GOOD_FILTERED_LIST,
        GOOD_JOIN,
        GOOD_ROLLUP,
        GOOD_GRAND_TOTAL,
        GOOD_ANTIJOIN,
        GOOD_DERIVED_COUNT_DISTINCT,
    ],
)
def test_verified_templates_are_clean(sql: str) -> None:
    res = lint(sql)
    assert res.verdict is Verdict.CLEAN, f"false positive: {res.rules}\n{sql}"


# --------------------------------------------------------------------------- #
# The silent-wrong class — the only findings that produce a confident wrong answer
# --------------------------------------------------------------------------- #


def test_top_parenthesised_is_repairable() -> None:
    res = lint("select top (100) [P].[PartNum] as [PartNum] from Erp.Part as [P]")
    assert res.verdict is Verdict.REPAIRABLE
    assert "top_parenthesised" in res.rules
    finding = next(f for f in res.findings if f.rule == "top_parenthesised")
    assert finding.rewrite_status == "VERIFIED"


def test_top_percent_is_unsupported() -> None:
    res = lint("select top 5 percent [P].[PartNum] as [PartNum] from Erp.Part as [P]")
    assert res.verdict is Verdict.UNSUPPORTED
    assert "top_percent" in res.rules


def test_top_zero_is_unsupported() -> None:
    res = lint("select top 0 [P].[PartNum] as [PartNum] from Erp.Part as [P]")
    assert res.verdict is Verdict.UNSUPPORTED
    assert "top_zero" in res.rules


def test_order_by_ordinal_is_repairable() -> None:
    res = lint(
        "select top 10 [P].[ClassID] as [ClassID], count(*) as [Cnt] "
        "from Erp.Part as [P] group by [P].[ClassID] order by 1 desc"
    )
    assert "order_by_ordinal" in res.rules
    assert res.verdict is Verdict.REPAIRABLE


def test_count_distinct_is_unsupported_and_names_the_correct_form() -> None:
    res = lint("select count(distinct [OrderDtl].[PartNum]) as [N] from Erp.OrderDtl as [OrderDtl]")
    assert res.verdict is Verdict.UNSUPPORTED
    finding = next(f for f in res.findings if f.rule == "count_distinct")
    assert "silently ignored" in finding.evidence
    assert "incorrect count" in finding.evidence
    assert "select distinct" in (finding.rewrite or "")


def test_distinct_with_top_is_unsupported() -> None:
    res = lint("select distinct top 10 [P].[ClassID] as [ClassID] from Erp.Part as [P]")
    assert res.verdict is Verdict.UNSUPPORTED
    assert "distinct_with_top" in res.rules


def test_select_into_is_unsupported() -> None:
    res = lint("select top 10 [P].[PartNum] as [PartNum] into #tmp from Erp.Part as [P]")
    assert "select_into" in res.rules
    assert res.verdict is Verdict.UNSUPPORTED


# --------------------------------------------------------------------------- #
# Parse-time refusals
# --------------------------------------------------------------------------- #


def test_limit_is_repairable_even_though_sqlglot_normalises_it() -> None:
    # sqlglot rewrites `limit 5` to `TOP 5`, so this rule MUST read raw text.
    res = lint("select [P].[PartNum] as [PartNum] from Erp.Part as [P] limit 5")
    assert "limit_clause" in res.rules
    assert res.verdict is Verdict.REPAIRABLE


def test_fetch_first_without_offset_is_repairable() -> None:
    res = lint("select [P].[PartNum] as [PartNum] from Erp.Part as [P] fetch first 5 rows only")
    assert "fetch_clause" in res.rules


def test_offset_fetch_with_order_by_is_not_flagged() -> None:
    sql = (
        "select [P].[PartNum] as [PartNum] from Erp.Part as [P] "
        "order by [P].[PartNum] offset 0 rows fetch next 5 rows only"
    )
    assert "fetch_clause" not in lint(sql).rules


def test_missing_schema_prefix_is_repairable() -> None:
    res = lint("select top 10 [P].[PartNum] as [PartNum] from Part as [P]")
    assert "missing_schema_prefix" in res.rules
    assert res.verdict is Verdict.REPAIRABLE


def test_non_select_is_unsupported() -> None:
    res = lint("update Erp.Part set [ClassID] = 'FG'")
    assert res.verdict is Verdict.UNSUPPORTED
    assert "non_select_statement" in res.rules


def test_two_statements_is_unsupported() -> None:
    res = lint("select top 1 [P].[PartNum] as [P] from Erp.Part as [P]; select 1 as [One]")
    assert "multiple_statements" in res.rules
    assert res.verdict is Verdict.UNSUPPORTED


def test_pivot_is_unsupported() -> None:
    sql = (
        "select top 10 [t].[a] as [a] from (select [P].[ClassID] as [a] from Erp.Part as [P]) as [src] "
        "pivot (count([a]) for [a] in ([FG])) as [t]"
    )
    assert "pivot" in lint(sql).rules


def test_derived_table_order_without_top_is_repairable() -> None:
    sql = (
        "select top 10 [t].[PartNum] as [PartNum] from "
        "(select [P].[PartNum] as [PartNum] from Erp.Part as [P] order by [P].[PartNum]) as [t]"
    )
    res = lint(sql)
    assert "derived_order_without_top" in res.rules


# --------------------------------------------------------------------------- #
# Run-time refusals
# --------------------------------------------------------------------------- #


def test_order_by_aggregate_alias_is_repairable() -> None:
    sql = (
        "select top 10 [OrderDtl].[PartNum] as [PartNum], sum([OrderDtl].[ExtPriceDtl]) as [Revenue] "
        "from Erp.OrderDtl as [OrderDtl] group by [OrderDtl].[PartNum] order by [Revenue] desc"
    )
    res = lint(sql)
    assert "order_by_alias" in res.rules
    finding = next(f for f in res.findings if f.rule == "order_by_alias")
    assert "sum" in (finding.rewrite or "").lower()


def test_order_by_alias_equal_to_its_own_column_is_allowed() -> None:
    
    sql = (
        "select top 10 [P].[PartNum] as [PartNum] from Erp.Part as [P] order by PartNum asc"
    )
    assert "order_by_alias" not in lint(sql).rules


def test_having_alias_is_repairable() -> None:
    sql = (
        "select [P].[ClassID] as [ClassID], count(*) as [Cnt] from Erp.Part as [P] "
        "group by [P].[ClassID] having [Cnt] > 100"
    )
    assert "having_alias" in lint(sql).rules


def test_group_by_ordinal_is_repairable() -> None:
    sql = (
        "select [P].[ClassID] as [ClassID], count(*) as [Cnt] from Erp.Part as [P] "
        "group by 1 order by count(*) desc"
    )
    assert "group_by_ordinal" in lint(sql).rules


def test_exists_is_unsupported() -> None:
    sql = (
        "select top 10 [P].[PartNum] as [PartNum] from Erp.Part as [P] "
        "where exists (select 1 from Erp.PartWhse as [W] where [W].[PartNum] = [P].[PartNum])"
    )
    res = lint(sql)
    assert res.verdict is Verdict.UNSUPPORTED
    assert "exists_predicate" in res.rules


def test_window_function_is_unsupported() -> None:
    sql = (
        "select top 10 row_number() over (order by [P].[PartNum]) as [Rn], "
        "[P].[PartNum] as [PartNum] from Erp.Part as [P]"
    )
    assert lint(sql).verdict is Verdict.UNSUPPORTED


def test_parameter_marker_is_unsupported() -> None:
    sql = "select top 10 [OH].[OrderNum] as [OrderNum] from Erp.OrderHed as [OH] where [OH].[OrderNum] = @OrderNum"
    res = lint(sql)
    assert res.verdict is Verdict.UNSUPPORTED
    assert "parameter_marker" in res.rules


def test_unqualified_column_on_join_is_repairable() -> None:
    sql = (
        "select top 10 [Part].[PartNum] as [PartNum], OnHandQty as [OnHandQty] "
        "from Erp.Part as [Part] inner join Erp.PartWhse as [PartWhse] "
        "on [Part].[Company] = [PartWhse].[Company] and [Part].[PartNum] = [PartWhse].[PartNum]"
    )
    assert "unqualified_column_on_join" in lint(sql).rules


def test_plain_column_not_in_group_by_is_repairable() -> None:
    sql = (
        "select top 10 [OrderDtl].[PartNum] as [PartNum], [OrderDtl].[OrderNum] as [OrderNum], "
        "sum([OrderDtl].[ExtPriceDtl]) as [Revenue] from Erp.OrderDtl as [OrderDtl] "
        "group by [OrderDtl].[PartNum]"
    )
    assert "plain_column_not_grouped" in lint(sql).rules


def test_aggregate_in_where_is_repairable() -> None:
    sql = (
        "select [OrderDtl].[PartNum] as [PartNum], sum([OrderDtl].[ExtPriceDtl]) as [Revenue] "
        "from Erp.OrderDtl as [OrderDtl] where sum([OrderDtl].[ExtPriceDtl]) > 100 "
        "group by [OrderDtl].[PartNum]"
    )
    assert "aggregate_in_where" in lint(sql).rules


def test_nested_aggregate_is_unsupported() -> None:
    sql = "select max(count(*)) as [M] from Erp.Part as [P] group by [P].[ClassID]"
    assert lint(sql).verdict is Verdict.UNSUPPORTED


def test_unquoted_date_is_repairable() -> None:
    sql = (
        "select top 10 [OH].[OrderNum] as [OrderNum] from Erp.OrderHed as [OH] "
        "where [OH].[OrderDate] >= 2025-01-01"
    )
    assert "unquoted_date_literal" in lint(sql).rules


def test_odbc_date_escape_is_repairable() -> None:
    sql = (
        "select top 10 [OH].[OrderNum] as [OrderNum] from Erp.OrderHed as [OH] "
        "where [OH].[OrderDate] >= {d '2025-01-01'}"
    )
    assert "odbc_date_escape" in lint(sql).rules


# --------------------------------------------------------------------------- #
# Policy refusals and bounding
# --------------------------------------------------------------------------- #


def test_select_star_is_repairable_and_needs_schema() -> None:
    res = lint("select top 10 * from Erp.Part as [P]")
    assert "select_star" in res.rules
    assert next(f for f in res.findings if f.rule == "select_star").requires_schema


def test_missing_top_is_repairable() -> None:
    res = lint("select [P].[PartNum] as [PartNum] from Erp.Part as [P]")
    assert "missing_row_bound" in res.rules


def test_grand_total_aggregate_needs_no_top() -> None:
    assert "missing_row_bound" not in lint(GOOD_GRAND_TOTAL).rules


def test_cross_join_is_unsupported() -> None:
    sql = "select top 10 [A].[PartNum] as [PartNum] from Erp.Part as [A] cross join Erp.Plant as [B]"
    res = lint(sql)
    assert res.verdict is Verdict.UNSUPPORTED
    assert "cross_join" in res.rules


def test_comma_join_without_predicate_is_unsupported() -> None:
    sql = "select top 10 [A].[PartNum] as [PartNum] from Erp.Part as [A], Erp.Plant as [B]"
    assert "comma_join_no_predicate" in lint(sql).rules


# --------------------------------------------------------------------------- #
# Advisories must not move the verdict
# --------------------------------------------------------------------------- #


def test_missing_company_join_key_is_advisory_only() -> None:
    sql = (
        "select top 20 [Part].[PartNum] as [PartNum], [PartWhse].[OnHandQty] as [OnHandQty] "
        "from Erp.Part as [Part] inner join Erp.PartWhse as [PartWhse] "
        "on [Part].[PartNum] = [PartWhse].[PartNum]"
    )
    res = lint(sql)
    assert "join_missing_company" in res.rules
    assert res.verdict is Verdict.CLEAN
    assert next(f for f in res.findings if f.rule == "join_missing_company").severity is Severity.ADVISORY


def test_fanout_risk_is_advisory_only() -> None:
    sql = (
        "select top 10 [JH].[JobNum] as [JobNum], sum([LD].[LaborHrs]) as [Hrs] "
        "from Erp.JobHead as [JH] "
        "inner join Erp.JobOper as [JO] on [JH].[Company] = [JO].[Company] and [JH].[JobNum] = [JO].[JobNum] "
        "inner join Erp.LaborDtl as [LD] on [JO].[Company] = [LD].[Company] and [JO].[JobNum] = [LD].[JobNum] "
        "group by [JH].[JobNum]"
    )
    res = lint(sql)
    assert "fanout_risk" in res.rules
    assert res.verdict is Verdict.CLEAN


def test_untested_construct_is_advisory_not_supported() -> None:
    sql = "select top 10 string_agg([P].[PartNum], ',') as [S] from Erp.Part as [P]"
    res = lint(sql)
    assert "untested_string_agg" in res.rules
    assert next(f for f in res.findings if f.rule == "untested_string_agg").severity is Severity.ADVISORY


def test_nolock_is_advisory() -> None:
    sql = "select top 10 [P].[PartNum] as [PartNum] from Erp.Part as [P] with (nolock)"
    res = lint(sql)
    assert "table_hint_nolock" in res.rules
    assert res.verdict is Verdict.CLEAN


# --------------------------------------------------------------------------- #
# Masking — a literal must never be blamed for looking like a trap
# --------------------------------------------------------------------------- #


def test_literal_containing_trap_text_is_not_flagged() -> None:
    sql = (
        "select top 10 [P].[PartNum] as [PartNum] from Erp.Part as [P] "
        "where [P].[PartNum] = 'top (100) order by 1 limit 5'"
    )
    res = lint(sql)
    for bad in ("top_parenthesised", "order_by_ordinal", "limit_clause"):
        assert bad not in res.rules


def test_comment_containing_trap_text_is_not_flagged() -> None:
    sql = (
        "-- never write top (100) or limit 5\n"
        "select top 10 [P].[PartNum] as [PartNum] from Erp.Part as [P]"
    )
    res = lint(sql)
    assert "top_parenthesised" not in res.rules
    assert "limit_clause" not in res.rules


def test_iso_date_inside_a_string_is_not_flagged_as_unquoted() -> None:
    sql = (
        "select top 10 [P].[PartNum] as [PartNum] from Erp.Part as [P] "
        "where [P].[PartNum] = 'REV-2025-07-20-A'"
    )
    assert "unquoted_date_literal" not in lint(sql).rules


def test_mask_literals_preserves_length() -> None:
    sql = "select 'abc' as [x] -- trailing\n from Erp.Part as [P]"
    assert len(mask_literals(sql)) == len(sql)


# --------------------------------------------------------------------------- #

#
# Correct lint classification is necessary for reliable query scoring.
# --------------------------------------------------------------------------- #





def test_d1_bare_column_in_single_table_subquery_is_not_flagged() -> None:
    """A bare column in a single-table subquery is a legal shape.

    The subquery has exactly ONE source, so `CustNum` and `Name` are unambiguous.
    A rule that counted sources statement-wide would flag them falsely.
    """
    sql = (
        "select top 100 [od].[PartNum] as [PartNum], [oh].[OrderNum] as [OrderNum] "
        "from Erp.OrderDtl as [od] "
        "inner join Erp.OrderHed as [oh] on [od].[Company] = [oh].[Company] "
        "and [od].[OrderNum] = [oh].[OrderNum] "
        "where [od].[CustNum] = (select top 1 CustNum from Erp.Customer where Name = 'AcmeIndustrial')"
    )
    res = lint(sql)
    assert "unqualified_column_on_join" not in res.rules
    assert res.verdict is Verdict.CLEAN


def test_d1_a_genuinely_ambiguous_bare_column_still_fires() -> None:
    """Regression coverage: test d1 a genuinely ambiguous bare column still fires."""
    sql = (
        "select top 10 [Part].[PartNum] as [PartNum], OnHandQty as [OnHandQty] "
        "from Erp.Part as [Part] inner join Erp.PartWhse as [PartWhse] "
        "on [Part].[Company] = [PartWhse].[Company] and [Part].[PartNum] = [PartWhse].[PartNum]"
    )
    assert "unqualified_column_on_join" in lint(sql).rules


# --- D2: `plain_column_not_grouped` demanded an exact expression match ---------


def test_d2_expression_over_grouped_columns_is_not_flagged() -> None:
    """Regression coverage: test d2 expression over grouped columns is not flagged."""
    sql = (
        "select top 100 [oh].[Company] + ': ' + [c].[Name] as [Customer], "
        "sum([oh].[OrderAmt]) as [Total] "
        "from Erp.OrderHed as [oh] "
        "inner join Erp.Customer as [c] on [oh].[Company] = [c].[Company] "
        "and [oh].[CustNum] = [c].[CustNum] "
        "group by [oh].[Company], [c].[Name]"
    )
    assert "plain_column_not_grouped" not in lint(sql).rules


def test_d2_a_constant_select_item_is_not_an_ungrouped_column() -> None:
    """A literal has no columns at all. The model writes `'DMR' as [SourceType]`."""
    sql = (
        "select top 10 'DMR' as [SourceType], count(*) as [N] "
        "from Erp.DMRHead as [h] group by [h].[Company]"
    )
    assert "plain_column_not_grouped" not in lint(sql).rules


def test_d2_quoting_difference_between_select_and_group_by_is_not_a_miss() -> None:
    sql = (
        "select top 5 [P].[ClassID] as [C], count(*) as [N] "
        "from Erp.Part as [P] group by P.ClassID"
    )
    assert "plain_column_not_grouped" not in lint(sql).rules


def test_d2_a_genuinely_ungrouped_column_still_fires() -> None:
    sql = (
        "select top 10 [OrderDtl].[PartNum] as [PartNum], [OrderDtl].[OrderNum] as [OrderNum], "
        "sum([OrderDtl].[ExtPriceDtl]) as [Revenue] from Erp.OrderDtl as [OrderDtl] "
        "group by [OrderDtl].[PartNum]"
    )
    res = lint(sql)
    assert "plain_column_not_grouped" in res.rules
    assert "OrderNum" in next(
        f for f in res.findings if f.rule == "plain_column_not_grouped"
    ).detail





def test_d3_join_keyed_only_on_company_is_refused() -> None:
    """The single most damaging shape the model produced, and it lint-CLEANed.

    `r_join_missing_company` sees `Company` in the predicate and stays silent.
    Every job x every customer, returned with no error on any channel.
    """
    sql = (
        "select top 200 [jh].[JobNum] as [JobNumber], [c].[Name] as [CustomerName] "
        "from Erp.JobHead as [jh] inner join Erp.Customer as [c] "
        "on [jh].[Company] = [c].[Company]"
    )
    res = lint(sql)
    assert res.verdict is Verdict.UNSUPPORTED
    finding = next(f for f in res.findings if f.rule == "join_on_company_only")
    assert finding.category is Category.SILENT_WRONG
    assert finding.rewrite_status == "NONE"  # the business key cannot be invented


def test_d3_a_real_business_key_alongside_company_is_clean() -> None:
    sql = (
        "select top 20 [Part].[PartNum] as [PartNum], [PartWhse].[OnHandQty] as [OnHandQty] "
        "from Erp.Part as [Part] inner join Erp.PartWhse as [PartWhse] "
        "on [Part].[Company] = [PartWhse].[Company] and [Part].[PartNum] = [PartWhse].[PartNum]"
    )
    res = lint(sql)
    assert "join_on_company_only" not in res.rules
    assert res.verdict is Verdict.CLEAN


def test_d3_join_on_a_filter_predicate_only_is_refused() -> None:
    """Regression coverage: test d3 join on a filter predicate only is refused."""
    sql = (
        "select top 500 [h].[DMRNum] as [DMRNum], [l].[ScrapQty] as [ScrapQty] "
        "from Erp.DMRHead as [h] full outer join Erp.LaborDtl as [l] on [l].[ScrapQty] > 0"
    )
    res = lint(sql)
    assert "join_no_key_predicate" in res.rules
    assert res.verdict is Verdict.UNSUPPORTED


def test_d3_unqualified_on_clause_is_left_to_the_qualification_rule() -> None:
    """A rule that cannot SEE the keys must not claim there are none."""
    sql = (
        "select top 10 [jh].[JobNum] as [JobNum], [c].[Name] as [Name] "
        "from Erp.JobHead as [jh] inner join Erp.Customer as [c] on Company = Company"
    )
    res = lint(sql)
    assert "join_on_company_only" not in res.rules
    assert "join_no_key_predicate" not in res.rules





def test_d4_union_order_by_is_reported_as_discarded() -> None:
    """Regression coverage: test d4 union order by is reported as discarded."""
    sql = (
        "select top 100 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select top 100 [PW].[PartNum] as [PN] from Erp.PartWhse as [PW] "
        "order by [PN] desc"
    )
    res = lint(sql)
    assert "union_order_by_discarded" in res.rules
    finding = next(f for f in res.findings if f.rule == "union_order_by_discarded")
    assert finding.category is Category.SILENT_WRONG
    
    # derived-table wrap with per-branch bounds ranks a truncated sample (17 S2).
    assert "CTE" in (finding.rewrite or "")


def test_d4_qualified_union_order_by_is_discarded_too() -> None:
    """The qualified form was CLEAN, and it is discarded exactly the same."""
    sql = (
        "select top 100 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select top 100 [PW].[PartNum] as [PN] from Erp.PartWhse as [PW] "
        "order by [PW].[PartNum] desc"
    )
    assert "union_order_by_discarded" in lint(sql).rules


def test_d4_union_order_by_does_not_also_fire_the_alias_rule() -> None:
    """Regression coverage: test d4 union order by does not also fire the alias rule."""
    sql = (
        "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select top 5 [PW].[PartNum] as [PN] from Erp.PartWhse as [PW] "
        "order by [PN] desc"
    )
    assert "order_by_alias" not in lint(sql).rules


def test_d4_top_wrapping_a_set_operation_is_reported_as_ignored() -> None:
    """Regression coverage: test d4 top wrapping a set operation is reported as ignored."""
    sql = (
        "select top 7 [u].[PN] as [PN] from ("
        "select [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]) as [u] "
        "order by [u].[PN] desc"
    )
    res = lint(sql)
    assert "setop_outer_bound_ignored" in res.rules
    finding = next(f for f in res.findings if f.rule == "setop_outer_bound_ignored")
    assert finding.category is Category.SILENT_WRONG
    assert "CTE" in (finding.rewrite or "")


def test_d4_top_over_a_plain_derived_table_is_honoured_and_not_flagged() -> None:
    """Regression coverage: test d4 top over a plain derived table is honoured and not flagged."""
    sql = (
        "select top 7 [t].[PN] as [PN] from "
        "(select [P].[PartNum] as [PN] from Erp.Part as [P]) as [t]"
    )
    assert "setop_outer_bound_ignored" not in lint(sql).rules


def test_d4_branch_bounded_union_wrapper_is_not_called_unbounded() -> None:
    """The transpiler's own output shape — the source of its 3 phantom "regressions".

    The wrapper deliberately carries no `top`, because an outer `top` is ignored;
    the branches carry the bound, which is the only form Epicor honours.
    """
    sql = (
        "select [u].[PN] as [PN] from ("
        "select top 100 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select top 100 [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]) as [u] "
        "order by [u].[PN] desc"
    )
    res = lint(sql)
    assert "missing_row_bound" not in res.rules
    assert res.verdict is Verdict.CLEAN


def test_d4_unbounded_union_wrapper_is_still_unbounded() -> None:
    sql = (
        "select [u].[PN] as [PN] from ("
        "select [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]) as [u]"
    )
    res = lint(sql)
    finding = next(f for f in res.findings if f.rule == "missing_row_bound")
    assert "each branch" in (finding.rewrite or "").lower()


# --- D5: `missing_row_bound`'s grand-total test descended into subqueries ------


def test_d5_subquery_aggregate_does_not_make_a_select_a_grand_total() -> None:
    """Regression coverage: test d5 subquery aggregate does not make a select a grand total."""
    sql = (
        "select (select max([PW].[OnHandQty]) from Erp.PartWhse as [PW]) as [N] "
        "from Erp.Part as [P] where [P].[ClassID] = 'FG'"
    )
    assert "missing_row_bound" in lint(sql).rules


def test_d5_a_real_grand_total_still_needs_no_bound() -> None:
    assert "missing_row_bound" not in lint("select count(*) as [N] from Erp.Part as [Part]").rules


def test_d5_aggregate_wrapped_in_a_scalar_function_is_still_a_grand_total() -> None:
    """Regression coverage: test d5 aggregate wrapped in a scalar function is still a grand total."""
    sql = "select coalesce(sum([PW].[OnHandQty]), 0) as [T] from Erp.PartWhse as [PW]"
    assert "missing_row_bound" not in lint(sql).rules


# --- D6: `fanout_risk` required >= 2 joins; the canonical shape has ONE --------


def test_d6_two_table_fanout_is_flagged() -> None:
    """Regression coverage: test d6 two table fanout is flagged."""
    sql = (
        "select sum([OH].[OrderAmt]) as [T] from Erp.OrderHed as [OH] "
        "join Erp.OrderDtl as [OD] on [OH].[Company] = [OD].[Company] "
        "and [OH].[OrderNum] = [OD].[OrderNum] where [OH].[OrderNum] = 200001"
    )
    res = lint(sql)
    assert "fanout_risk" in res.rules
    finding = next(f for f in res.findings if f.rule == "fanout_risk")
    assert finding.severity is Severity.ADVISORY  # cardinality is unknowable here
    assert finding.detail == "parent_side_aggregate"
    assert res.verdict is Verdict.CLEAN  # an advisory never moves the verdict


def test_d6_aggregating_the_child_side_is_not_a_fanout() -> None:
    """`sum(child)` grouped by the parent key is the CORRECT shape — no warning."""
    sql = (
        "select top 10 [OH].[OrderNum] as [OrderNum], sum([OD].[OrderQty]) as [Q] "
        "from Erp.OrderHed as [OH] join Erp.OrderDtl as [OD] "
        "on [OH].[Company] = [OD].[Company] and [OH].[OrderNum] = [OD].[OrderNum] "
        "group by [OH].[OrderNum]"
    )
    assert "fanout_risk" not in lint(sql).rules


def test_d6_count_star_over_a_join_is_not_a_fanout() -> None:
    sql = (
        "select count(*) as [N] from Erp.OrderHed as [OH] join Erp.OrderDtl as [OD] "
        "on [OH].[Company] = [OD].[Company] and [OH].[OrderNum] = [OD].[OrderNum]"
    )
    assert "fanout_risk" not in lint(sql).rules


# --- Audit findings: set operations and APPLY ---------------------------------


def test_except_and_intersect_are_not_refused_as_non_select() -> None:
    """Regression coverage: test except and intersect are not refused as non select."""
    for op in ("except", "intersect"):
        sql = (
            f"select top 10 [P].[PartNum] as [PN] from Erp.Part as [P] {op} "
            "select top 10 [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]"
        )
        res = lint(sql)
        assert "non_select_statement" not in res.rules, op
        assert res.verdict is Verdict.CLEAN, (op, res.rules)


def test_root_select_rules_reach_every_set_operation_branch() -> None:
    """An unbounded EXCEPT was skipped by every root-select rule, not just one."""
    sql = (
        "select [P].[PartNum] as [PN] from Erp.Part as [P] except "
        "select [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]"
    )
    assert "missing_row_bound" in lint(sql).rules


def test_aggregate_over_a_derived_set_operation_is_refused() -> None:
    """Regression coverage: test aggregate over a derived set operation is refused."""
    sql = (
        "select count(*) as [N] from ("
        "select [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]) as [u]"
    )
    res = lint(sql)
    assert res.verdict is Verdict.UNSUPPORTED
    finding = next(f for f in res.findings if f.rule == "aggregate_over_derived_setop")
    assert finding.rewrite_status == "VERIFIED"
    assert "CTE" in (finding.rewrite or "")


def test_aggregate_over_a_CTE_set_operation_is_clean() -> None:
    """The CTE form supports an aggregate over the whole set operation."""
    sql = (
        "with [u] as ("
        "select [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]) "
        "select count(*) as [N] from [u]"
    )
    assert lint(sql).verdict is Verdict.CLEAN


def test_top_over_a_CTE_set_operation_binds_and_is_not_flagged() -> None:
    """Regression coverage: test top over a CTE set operation binds and is not flagged."""
    sql = (
        "with [u] as ("
        "select [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]) "
        "select top 7 [u].[PN] as [PN] from [u] order by [u].[PN] desc"
    )
    res = lint(sql)
    assert res.verdict is Verdict.CLEAN, res.rules


def test_grand_total_over_a_set_operation_is_not_called_unbounded() -> None:
    """It returns one row whatever the source — the row-bound rule must not fire."""
    sql = (
        "with [u] as ("
        "select [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all "
        "select [PW].[PartNum] as [PN] from Erp.PartWhse as [PW]) "
        "select count(*) as [N] from [u]"
    )
    assert "missing_row_bound" not in lint(sql).rules


def test_mismatched_set_operation_branch_widths_are_refused() -> None:
    """Regression coverage: test mismatched set operation branch widths are refused."""
    sql = (
        "select top 10 [P].[PartNum] as [A], [P].[ClassID] as [B] from Erp.Part as [P] "
        "union all "
        "select top 10 [PW].[PartNum] as [A] from Erp.PartWhse as [PW]"
    )
    res = lint(sql)
    assert "setop_branch_arity_mismatch" in res.rules
    assert res.verdict is Verdict.UNSUPPORTED


def test_star_branches_are_not_given_a_clean_arity_bill() -> None:
    """`select *` is uncountable without a schema, so the rule must NOT claim a match.

    Both live instances of the arity error are exactly this shape; `select_star`
    covers them for a policy reason, which is not the same as detecting the arity.
    """
    sql = "select * from Erp.APInvHed union all select * from Erp.APInvDtl"
    res = lint(sql)
    assert "setop_branch_arity_mismatch" not in res.rules
    assert "select_star" in res.rules


def test_matching_branch_widths_are_clean() -> None:
    sql = (
        "select top 10 [P].[PartNum] as [A] from Erp.Part as [P] "
        "union all "
        "select top 10 [PW].[PartNum] as [A] from Erp.PartWhse as [PW]"
    )
    assert "setop_branch_arity_mismatch" not in lint(sql).rules


def test_cross_apply_is_not_misnamed_as_a_comma_join() -> None:
    """Regression coverage: test cross apply is not misnamed as a comma join."""
    sql = (
        "select top 10 [P].[PartNum] as [PN], [x].[Q] as [Q] from Erp.Part as [P] "
        "cross apply (select top 1 [PW].[OnHandQty] as [Q] from Erp.PartWhse as [PW] "
        "where [PW].[PartNum] = [P].[PartNum]) as [x]"
    )
    res = lint(sql)
    assert "comma_join_no_predicate" not in res.rules
    assert "untested_apply" in res.rules  


def test_comma_join_without_predicate_is_still_refused() -> None:
    sql = "select top 10 [A].[PartNum] as [PartNum] from Erp.Part as [A], Erp.Plant as [B]"
    assert "comma_join_no_predicate" in lint(sql).rules


# --------------------------------------------------------------------------- #
# Degenerate input
# --------------------------------------------------------------------------- #


def test_prose_is_unparseable_not_unsupported() -> None:
    res = lint("I would need to know which table holds the on-hand quantity.")
    assert res.verdict is Verdict.UNPARSEABLE


def test_empty_is_unparseable() -> None:
    assert lint("").verdict is Verdict.UNPARSEABLE

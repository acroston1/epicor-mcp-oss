"""Regression coverage: test sql lint parsed."""

from __future__ import annotations

import pytest

from epicor_mcp.sql.lint import Severity, lint_parsed, mask_sql
from tests.wedge_fixtures import load, names

SILENT_WRONG_CASES = {
    "top_paren": "row_bound_dropped",
    "top_percent": "top_percent",
    "top_zero": "top_zero",
    "sort_ordinal": "sort_ordinal",
    "sort_invented_alias": "sort_invented_alias",
    "count_distinct": "distinct_dropped_in_aggregate",
    "union_order_by": "setop_order_by_discarded",
}

CLEAN_CASES = [
    "clean_top", "clean_rollup", "clean_empbasic", "clean_in_subquery", "wedge_rollup",
    "gov_bounded_join", "deny_projected", "deny_filtered", "deny_sorted", "deny_cte",
]


@pytest.mark.parametrize("fixture,rule", sorted(SILENT_WRONG_CASES.items()))
def test_each_silent_wrong_shape_is_caught_and_refused(fixture, rule):
    sql, ds = load(fixture)
    findings = lint_parsed(sql, ds)
    rules = [f.rule for f in findings]
    assert rule in rules, f"{fixture}: expected {rule}, got {rules}"
    hit = next(f for f in findings if f.rule == rule)
    assert hit.severity == Severity.REFUSE
    assert hit.evidence, "every finding must carry the measurement it rests on"


@pytest.mark.parametrize("fixture", CLEAN_CASES)
def test_no_false_positive_on_a_good_statement(fixture):
    sql, ds = load(fixture)
    findings = [f for f in lint_parsed(sql, ds) if f.severity == Severity.REFUSE]
    assert findings == [], f"{fixture} lints dirty: {[f.rule for f in findings]}"


def test_the_wedge_query_lints_clean():
    """The supported rollup statement must not be blocked by our own lint."""
    sql, ds = load("wedge_rollup")
    assert lint_parsed(sql, ds) == []


def test_select_star_is_refused_and_serves_the_real_column_list():
    sql, ds = load("select_star")
    findings = lint_parsed(sql, ds)
    star = next(f for f in findings if f.rule == "select_star")
    assert star.severity == Severity.REFUSE
    assert star.detail["column_count"] == 3
    assert "PartNum" in star.detail["columns"]


def test_a_bare_grand_total_is_not_mistaken_for_select_star():
    """A bare `All` is insufficient evidence of `select *`:
    `select count(distinct ...)` also parses to `All` with no star anywhere."""
    sql, ds = load("count_distinct")
    rules = [f.rule for f in lint_parsed(sql, ds)]
    assert "select_star" not in rules
    assert "distinct_dropped_in_aggregate" in rules


def test_phantom_column_is_named_with_its_table():
    sql, ds = load("unknown_column")
    hit = next(f for f in lint_parsed(sql, ds) if f.rule == "unknown_column")
    assert hit.detail == {"table": "OrderDtl", "column": "DocExtPrice"}


def test_datatype_guard_is_load_bearing_for_calculated_and_derived_fields():
    """A `TT`/`SQ` field legitimately carries a blank DataType. Without the
    TableType=='DB' guard every derived-table top-N is rejected, naming a
    subquery GUID as the table."""
    sql, ds = load("clean_rollup")
    calculated = [f for f in ds["QueryField"] if f["TableID"] == "Calculated"]
    assert calculated and calculated[0]["DataType"] == ""
    assert [f.rule for f in lint_parsed(sql, ds)] == []

    sql, ds = load("deny_derived")  # a derived table: an SQ row with a blank DataType
    assert "unknown_column" not in [f.rule for f in lint_parsed(sql, ds)]


def test_fanout_is_a_warning_not_a_refusal():
    sql, ds = load("gov_fanout")
    hit = next(
        f for f in lint_parsed(sql, ds, fanout_warning=True)
        if f.rule == "aggregate_fanout"
    )
    assert hit.severity == Severity.WARN
    assert "JobOper" in hit.detail["aggregated_tables"]


def test_the_key_blind_fanout_warning_is_OFF_by_default():
    """Regression coverage: test the key blind fanout warning is OFF by default."""
    sql, ds = load("gov_fanout")
    assert "aggregate_fanout" not in [f.rule for f in lint_parsed(sql, ds)]


def test_a_single_join_does_not_warn_about_fanout():
    sql, ds = load("gov_bounded_join")
    assert "aggregate_fanout" not in [
        f.rule for f in lint_parsed(sql, ds, fanout_warning=True)
    ]


def test_top_rules_run_on_masked_text_so_a_literal_cannot_trip_them():
    """The legacy unknown_columns envelope blamed a quoted literal for a column name; the lint
    demands the raw-text rules run over MASKED text for the same reason."""
    _, ds = load("clean_top")
    sql = "select [P].[PartNum] as [PN] from Erp.Part as [P] where [P].[ClassID] = 'top (100)'"
    assert lint_parsed(sql, ds) == []
    assert "top (100)" not in mask_sql(sql)
    assert "ClassID" in mask_sql(sql)


def test_a_comment_cannot_trip_a_rule_either():
    _, ds = load("clean_top")
    sql = "select [P].[PartNum] as [PN] from Erp.Part as [P] -- top 5 percent, order by 1"
    assert lint_parsed(sql, ds) == []


def test_lint_never_raises_and_says_so_when_it_cannot_run():
    findings = lint_parsed("select 1", {"QuerySubQuery": "not-a-list"})
    assert [f.rule for f in findings] in ([], ["lint_unavailable"])
    findings = lint_parsed("select top 1 x", {"QueryField": [None, 3, "x"]})
    assert all(f.severity in (Severity.REFUSE, Severity.WARN) for f in findings)


def test_every_fixture_lints_without_an_exception():
    for name in names():
        sql, ds = load(name)
        for finding in lint_parsed(sql, ds):
            assert finding.rule != "lint_unavailable", f"{name} broke the lint"


# --------------------------------------------------------------------------- #
# Regressions beyond the original fixtures
# --------------------------------------------------------------------------- #


def test_the_correct_count_distinct_replacement_is_not_refused():
    """`select count(*) from (select distinct ...) as [t]`
    is the exact recipe the tool description tells the model to use instead of
    `count(distinct)`. The first cut of this rule fired on any `distinct`
    anywhere in the statement and REFUSED it — the server would have refused its
    own documented workaround for counting distinct values."""
    sql, ds = load("distinct_derived_count")
    assert "distinct" in sql
    assert lint_parsed(sql, ds) == []


def test_distinct_inside_the_aggregate_is_still_caught():
    sql, ds = load("count_distinct")
    hit = next(
        f for f in lint_parsed(sql, ds) if f.rule == "distinct_dropped_in_aggregate"
    )
    assert hit.severity == Severity.REFUSE
    assert "Engine compatibility behavior" in hit.evidence


def test_a_bare_select_distinct_is_not_flagged():
    _, ds = load("clean_top")
    sql = "select distinct [P].[ClassID] as [C] from Erp.Part as [P]"
    assert lint_parsed(sql, ds) == []


def test_an_aggregate_order_by_lints_clean():
    """A supported top-N recipe: `order by sum([OD].[ExtPriceDtl]) desc`."""
    sql, ds = load("agg_order_by")
    assert lint_parsed(sql, ds) == []


# --------------------------------------------------------------------------- #

# operation, not tell the model to re-write a `top` it already wrote
# --------------------------------------------------------------------------- #

_ALL_CLAUSE_DS = {"QuerySubQuery": [{"Type": "TopLevel", "SelectListClause": "All"}]}


def test_a_dropped_bound_on_a_set_operation_names_the_set_operation():
    """Regression coverage: test a dropped bound on a set operation names the set operation."""
    sql = (
        "select top 100 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all select top 100 [Q].[PartNum] as [PN] from Erp.QuoteDtl as [Q]"
    )
    hit = next(
        f for f in lint_parsed(sql, _ALL_CLAUSE_DS) if f.rule == "row_bound_dropped"
    )
    assert hit.detail["set_operation"] == "union"
    assert "SILENTLY DISCARDED" in hit.message
    # ...and it must NOT repeat the advice the caller already followed.
    assert "never `top (100)`" not in hit.message


def test_the_setop_refusal_prescribes_a_CTE_and_not_the_shape_it_refuses():
    """The suggested repair must avoid a derived-table wrapper:

    1. Running that exact statement through the pipe returns THIS SAME refusal
       ("2 problems found") — the transpiler moved the wrapper's `top` onto the
       branches, so the outer select then declared no `top` at all.
    2. ``[w].*`` is refused independently by ``select_star`` — the message
       prescribed a shape two rules in this file reject.
    3. Epicor can ignore TOP and GROUP BY on a derived-table wrapper around
       a set operation. The CTE form preserves the requested behavior.
    """
    sql = (
        "select top 100 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "union all select top 100 [Q].[PartNum] as [PN] from Erp.QuoteDtl as [Q]"
    )
    hit = next(
        f for f in lint_parsed(sql, _ALL_CLAUSE_DS) if f.rule == "row_bound_dropped"
    )
    # What it PRESCRIBES is a CTE that names its columns.
    assert "with [u] as (<your set operation>) select top 100 [u].[Col]" in hit.message
    # Both refused shapes appear ONLY as explicit prohibitions. Asserting mere
    # absence would pass on a message that dropped the warnings entirely, which
    # is how the model reaches for them again.
    assert "do NOT write `[u].*`" in hit.message
    assert "Do NOT wrap the set operation in a derived table" in hit.message
    # The prescriptive half — everything before the first prohibition — must be
    # free of both.
    prescription = hit.message.split("Name the columns")[0]
    assert ".*" not in prescription
    assert "as [w]" not in prescription


# --------------------------------------------------------------------------- #
# `has_top` must not be a regex over the WHOLE statement
# --------------------------------------------------------------------------- #

#: A `top` on an INNER select leaves the top-level clause at 'All'. Every shape
#: below parses to `SelectListClause='All'`, `TopRowExpr=0.0`, and a
#: whole-statement `top` regex REFUSED every one.
INNER_TOP_ONLY = {
    "derived table, no set operation at all":
        "select [t].[PartNum] as [PartNum] from "
        "(select top 10 [A].[PartNum] as [PartNum] from Erp.Part as [A]) as [t]",
    "in-subquery":
        "select [P].[PartNum] as [PartNum] from Erp.Part as [P] where [P].[PartNum] "
        "in (select top 5 [O].[PartNum] from Erp.OrderDtl as [O])",
    "derived-table-wrapped union with per-branch tops":
        "select [w].[PartNum] as [PartNum], count(*) as [N] from "
        "(select top 200 [A].[PartNum] as [PartNum] from Erp.OrderDtl as [A] "
        "union all select top 200 [B].[PartNum] as [PartNum] from Erp.OrderDtl as [B]) "
        "as [w] group by [w].[PartNum]",
    "CTE union with per-branch tops":
        "with [w] as (select top 200 [A].[PartNum] as [PartNum] from Erp.OrderDtl as [A] "
        "union all select top 200 [B].[PartNum] as [PartNum] from Erp.OrderDtl as [B]) "
        "select [w].[PartNum] as [PartNum], count(*) as [N] from [w] "
        "group by [w].[PartNum]",
    "scalar subquery in the projection":
        "select [P].[PartNum] as [PartNum], (select top 1 [O].[UnitPrice] from "
        "Erp.OrderDtl as [O] where [O].[PartNum] = [P].[PartNum]) as [Px] "
        "from Erp.Part as [P]",
}


@pytest.mark.parametrize("label,sql", sorted(INNER_TOP_ONLY.items()))
def test_a_top_on_an_INNER_select_is_not_a_dropped_outer_bound(label, sql):
    """`SelectListClause` describes the OUTER select, so the `top` compared
    against it must be the OUTER one.

    Comparing a whole-statement `top` regex against a top-level-only clause
    refused all five of these. Two of them — the derived-table wrap and the CTE
    — are the documented ways OUT of a set-operation refusal, so the false
    positive closed the exits and turned that refusal into a loop.
    """
    findings = lint_parsed(sql, _ALL_CLAUSE_DS)
    assert "row_bound_dropped" not in [f.rule for f in findings], (
        f"{label}: falsely refused as a dropped row bound"
    )


def test_a_top_on_ANY_branch_of_a_BARE_set_operation_still_counts():
    """Regression coverage: test a top on ANY branch of a BARE set operation still counts."""
    sql = (
        "select [A].[PartNum] as [PN] from Erp.OrderDtl as [A] "
        "union all select top 5 [B].[PartNum] as [PN] from Erp.QuoteDtl as [B]"
    )
    rules = [f.rule for f in lint_parsed(sql, _ALL_CLAUSE_DS)]
    assert "row_bound_dropped" in rules


def test_a_column_named_select_or_top_cannot_open_a_phantom_select_head():
    """Bracket depth is tracked, not just paren depth."""
    sql = "select [P].[select] as [top 5], [P].[PartNum] as [PN] from Erp.Part as [P]"
    rules = [f.rule for f in lint_parsed(sql, _ALL_CLAUSE_DS)]
    assert "row_bound_dropped" not in rules


def test_a_dropped_bound_with_no_set_operation_keeps_the_syntax_advice():
    """Regression coverage: test a dropped bound with no set operation keeps the syntax advice."""
    sql = "select top (100) [P].[PartNum] as [PN] from Erp.Part as [P]"
    hit = next(
        f for f in lint_parsed(sql, _ALL_CLAUSE_DS) if f.rule == "row_bound_dropped"
    )
    assert hit.detail["set_operation"] is None
    assert "never `top (100)`" in hit.message

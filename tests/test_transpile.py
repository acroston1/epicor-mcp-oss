"""Regression coverage: test transpile."""

from __future__ import annotations

import re

import pytest

from epicor_mcp.sql import Outcome, Policy, TableSchema, transpile

# --------------------------------------------------------------------------- #
# Fixtures: synthetic SQL retaining the regression query shapes
# --------------------------------------------------------------------------- #

#: Synthetic aggregate ordering: replace the POCount alias with its
#: COUNT(ph.PONum) expression so the SQL dialect can resolve the sort key.

REAL_B22 = """SELECT
    pa.BuyerID AS BuyerID,
    pa.Name AS BuyerName,
    COUNT(ph.PONum) AS POCount
FROM
    Erp.POHeader AS ph
    INNER JOIN Erp.PurAgent AS pa ON ph.BuyerID = pa.BuyerID AND ph.Company = pa.Company
WHERE
    ph.OrderDate >= DATEADD(MONTH, -6, GETDATE())
    AND ph.OpenOrder = 1
    AND ph.VoidOrder = 0
GROUP BY
    pa.BuyerID, pa.Name
ORDER BY
    POCount DESC"""

#: count(distinct) sitting NEXT TO a sum() in the same grouped
#: select. This is why count(distinct) is REFUSED and not rewritten: the one
#: proven replacement (count(*) over a `select distinct` derived table) answers
#: a single distinct count and cannot carry the sibling SUM.
REAL_B14 = """SELECT
    h.Company AS Company,
    YEAR(h.InvoiceDate) AS SalesYear,
    COUNT(DISTINCT h.InvoiceNum) AS InvoiceCount,
    SUM(d.ExtPrice) AS TotalSalesAmount
FROM
    Erp.InvcHead h
    INNER JOIN Erp.InvcDtl d ON h.Company = d.Company AND h.InvoiceNum = d.InvoiceNum
WHERE
    h.InvoiceDate >= '2020-01-01'
    AND h.Posted = 1
GROUP BY
    h.Company,
    YEAR(h.InvoiceDate)
ORDER BY
    h.Company,
    SalesYear"""

#: an OR of two correlated multi-key EXISTS, which is why it is refused.
REAL_B10_EXISTS = """select top 100 distinct [P].[PartNum] as [PartNum]
from Erp.Part as [P]
where [P].[PartNum] like 'SYNTHETIC%'
  and (
    exists (
      select 1 from Erp.JobHead as [J]
      where [J].[Company] = [P].[Company] and [J].[PartNum] = [P].[PartNum]
        and [J].[JobClosed] = false
    )
    or exists (
      select 1 from Erp.OrderDtl as [OD]
      where [OD].[Company] = [P].[Company] and [OD].[PartNum] = [P].[PartNum]
        and [OD].[OpenLine] = true
    )
  )"""

#: The bare `[CustNum]` is in
#: a SINGLE-TABLE subquery, where it is perfectly legal. A scope-blind check
#: flags it; the transpiler must not.
REAL_B10_SUBQUERY = """select top 100 [Part].[PartNum] as [PartNum]
from Erp.Part as [Part]
inner join Erp.OrderDtl as [OrderDtl]
  on [Part].[Company] = [OrderDtl].[Company] and [Part].[PartNum] = [OrderDtl].[PartNum]
where [Part].[PartNum] like 'SYNTHETIC%'
  and [OrderDtl].[CustNum] = (select top 1 [CustNum] from Erp.Customer as [Customer]
                              where [Customer].[Name] = 'AcmeIndustrial')"""

#: A UNION ALL whose trailing ORDER BY requires a wrapper for reliable sorting.

REAL_B21_UNION = """SELECT
    'Header' AS RecordType,
    h.Company AS Company,
    h.DMRNum AS DMRNum,
    h.PartNum AS PartNum,
    NULL AS ActionNum
FROM Erp.DMRHead h
WHERE h.DMRNum IN (6101, 6102, 6103)

UNION ALL

SELECT
    'Action' AS RecordType,
    a.Company AS Company,
    a.DMRNum AS DMRNum,
    NULL AS PartNum,
    a.ActionNum AS ActionNum
FROM Erp.DMRActn a
WHERE a.DMRNum = 6101
ORDER BY RecordType, DMRNum, ActionNum"""

#: Adding TOP to SELECT DISTINCT can invalidate a proposed row-bound repair.
#: The transpiler must never manufacture this shape.
REAL_C_REPAIR_DISTINCT_TOP = """SELECT TOP 500 DISTINCT
    p.PartNum AS AcmeIndustrialCouplerPartNumber
FROM Erp.Part AS p
    INNER JOIN Erp.OrderDtl AS od ON p.Company = od.Company AND p.PartNum = od.PartNum"""

#: An ordinary unbounded read requires an explicit row-bound policy.
REAL_B02_NO_BOUND = """SELECT
    pw.OnHandQty AS OnHandQuantity
FROM
    Erp.PartWhse AS pw
WHERE
    pw.PartNum = 'PART-200'"""

#: OFFSET/FETCH already supplies a bound; preserve it instead of adding TOP.
REAL_B01_FETCH = """SELECT
    jh.JobNum AS JobNumber,
    jh.PartNum AS PartNumber
FROM
    Erp.JobHead jh
WHERE
    jh.PartNum = 'PART-200'
ORDER BY
    jh.JobNum
OFFSET 0 ROWS FETCH NEXT 500 ROWS ONLY"""


SCHEMA = TableSchema(
    {
        "Erp.Part": ["Company", "PartNum", "PartDescription", "ClassID", "HasOnHandQty"],
        "Erp.PartWhse": ["Company", "PartNum", "WarehouseCode", "OnHandQty"],
        "Erp.OrderDtl": ["Company", "OrderNum", "OrderLine", "PartNum", "OrderQty"],
        "Erp.Customer": ["Company", "CustNum", "Name", "CustID"],
    }
)


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


# --------------------------------------------------------------------------- #
# 1. Row bound — the safety control
# --------------------------------------------------------------------------- #


class TestRowBound:
    def test_injects_top_when_absent(self):
        r = transpile(REAL_B02_NO_BOUND)
        assert r.outcome is Outcome.REWRITTEN
        assert "row_bound_injected" in r.rules
        assert "TOP 100" in r.sql
        assert r.row_bound.kind == "top"
        assert r.row_bound.value == 100
        assert r.row_bound.source == "injected"

    def test_respects_a_caller_bound(self):
        r = transpile("select top 25 p.PartNum as PN from Erp.Part as p")
        assert r.outcome is Outcome.UNCHANGED
        assert r.sql == "select top 25 p.PartNum as PN from Erp.Part as p"
        assert r.row_bound == r.row_bound  # dataclass sanity
        assert r.row_bound.value == 25
        assert r.row_bound.source == "caller"

    def test_clamps_an_oversized_bound(self):
        r = transpile("select top 99999 p.PartNum as PN from Erp.Part as p")
        assert r.outcome is Outcome.REWRITTEN
        assert "row_bound_clamped" in r.rules
        assert "TOP 1000" in r.sql
        assert r.row_bound.source == "clamped"

    def test_policy_limits_are_honoured(self):
        r = transpile(
            REAL_B02_NO_BOUND, policy=Policy(default_limit=7, max_rows=50)
        )
        assert "TOP 7" in r.sql
        r2 = transpile(
            "select top 900 p.PartNum as PN from Erp.Part as p",
            policy=Policy(default_limit=7, max_rows=50),
        )
        assert "TOP 50" in r2.sql

    def test_offset_fetch_is_a_bound_and_is_left_alone(self):
        """Regression coverage: test offset fetch is a bound and is left alone."""
        r = transpile(REAL_B01_FETCH, policy=Policy(max_rows=1000))
        assert r.outcome is Outcome.UNCHANGED
        assert r.row_bound.kind == "fetch"
        assert r.row_bound.value == 500

    def test_offset_fetch_is_clamped(self):
        r = transpile(REAL_B01_FETCH, policy=Policy(max_rows=100))
        assert r.outcome is Outcome.REWRITTEN
        assert "row_bound_clamped" in r.rules
        assert "FETCH NEXT 100 ROWS ONLY" in r.sql.upper()

    def test_grand_total_aggregate_needs_no_bound(self):
        r = transpile("select count(*) as N from Erp.Part as p")
        assert r.outcome is Outcome.UNCHANGED
        assert r.row_bound.value == 1

    def test_distinct_select_is_NOT_given_a_top(self):
        """Regression coverage: test distinct select is NOT given a top."""
        r = transpile("select distinct p.ClassID as C from Erp.Part as p")
        assert r.outcome is Outcome.UNCHANGED
        assert r.row_bound.kind == "page_size_only"
        assert any(a.rule == "distinct_bounded_by_page_size" for a in r.advisories)
        assert "top" not in r.sql.lower()

    def test_union_is_bounded_per_branch_and_says_so(self):
        r = transpile(
            "select a.X as X from Erp.A as a union all select b.X as X from Erp.B as b"
        )
        assert r.outcome is Outcome.REWRITTEN
        assert r.sql.upper().count("TOP 100") == 2
        assert r.row_bound.source == "per_branch"
        assert r.row_bound.value == 200
        assert "PER BRANCH" in r.row_bound.note


# --------------------------------------------------------------------------- #
# 2. Silent-limit shapes — sqlglot normalises two of these away, so the
#    announcement has to come from a raw-text check.
# --------------------------------------------------------------------------- #


class TestSilentLimitShapes:
    def test_top_parenthesised_is_rewritten_and_announced(self):
        r = transpile("select top (100) p.PartNum as PN from Erp.Part as p")
        assert r.outcome is Outcome.REWRITTEN
        assert "top_parenthesised" in r.rules
        assert "TOP 100" in r.sql and "TOP (100)" not in r.sql
        assert r.row_bound.value == 100

    def test_limit_clause_is_rewritten_and_announced(self):
        r = transpile("select p.PartNum as PN from Erp.Part as p limit 5")
        assert r.outcome is Outcome.REWRITTEN
        assert "limit_clause" in r.rules
        assert "TOP 5" in r.sql

    def test_top_percent_is_refused_not_converted(self):
        r = transpile("select top 5 percent p.PartNum as PN from Erp.Part as p")
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_top_percent"
        assert "100" in str(r.error["valid"])

    def test_top_zero_is_refused(self):
        r = transpile("select top 0 p.PartNum as PN from Erp.Part as p")
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_top_zero"

    def test_a_literal_containing_top_paren_does_not_trip_the_rule(self):
        r = transpile(
            "select top 10 p.PartNum as PN from Erp.Part as p "
            "where p.PartDescription = 'top (100) widget'"
        )
        assert "top_parenthesised" not in r.rules

    def test_a_comment_containing_limit_does_not_trip_the_rule(self):
        r = transpile("select top 10 p.PartNum as PN from Erp.Part as p -- limit 5 rows")
        assert "limit_clause" not in r.rules


# --------------------------------------------------------------------------- #
# 3. ORDER BY / HAVING alias — the dominant real dialect rewrite
# --------------------------------------------------------------------------- #


class TestAliasSorts:
    def test_real_aggregate_alias_is_replaced_by_the_expression(self):
        r = transpile(REAL_B22)
        assert r.outcome is Outcome.REWRITTEN
        assert "order_by_alias" in r.rules
        assert "ORDER BY COUNT(ph.PONum) DESC" in r.sql
        assert "POCount DESC" not in r.sql

    def test_plain_field_alias(self):
        r = transpile(
            "select top 5 pw.PartNum as PN, pw.OnHandQty as Qty "
            "from Erp.PartWhse as pw order by Qty desc"
        )
        assert "ORDER BY pw.OnHandQty DESC" in r.sql

    def test_function_alias(self):
        r = transpile(
            "select top 5 year(oh.OrderDate) as Yr, count(*) as N from Erp.OrderHed as oh "
            "group by year(oh.OrderDate) order by Yr desc"
        )
        assert "ORDER BY YEAR(oh.OrderDate) DESC" in r.sql

    def test_having_alias(self):
        r = transpile(
            "select top 5 od.PartNum as PN, sum(od.OrderQty) as Q from Erp.OrderDtl as od "
            "group by od.PartNum having Q > 100"
        )
        assert "having_by_alias" in r.rules
        assert "HAVING SUM(od.OrderQty) > 100" in r.sql

    def test_alias_equal_to_its_own_column_is_left_alone(self):
        """Regression coverage: test alias equal to its own column is left alone."""
        r = transpile(
            "select top 5 p.PartNum as PartNum from Erp.Part as p order by PartNum"
        )
        assert r.outcome is Outcome.UNCHANGED
        assert r.rules == []

    def test_shadowing_alias_ERRORS_and_names_both_readings(self):
        """Regression coverage: test shadowing alias ERRORS and names both readings."""
        r = transpile(
            "select top 5 pw.PartNum as OnHandQty, pw.OnHandQty as Q "
            "from Erp.PartWhse as pw order by OnHandQty desc",
            schema=SCHEMA,
        )
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_ambiguous_sort_alias"
        assert "pw.OnHandQty" in str(r.error["valid"])
        assert "pw.PartNum" in str(r.error["valid"])

    def test_shadow_guard_works_without_a_schema_too(self):
        r = transpile(
            "select top 5 pw.PartNum as OnHandQty, pw.OnHandQty as Q "
            "from Erp.PartWhse as pw order by OnHandQty desc"
        )
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_ambiguous_sort_alias"


# --------------------------------------------------------------------------- #
# 4. Ordinals — silently discarded (ORDER BY) / run failure (GROUP BY)
# --------------------------------------------------------------------------- #


class TestOrdinals:
    def test_order_by_ordinal_is_substituted(self):
        r = transpile("select p.PartNum as PN, p.ClassID as C from Erp.Part as p order by 2 desc")
        assert "order_by_ordinal" in r.rules
        assert "ORDER BY p.ClassID DESC" in r.sql

    def test_group_by_ordinal_is_substituted(self):
        r = transpile(
            "select p.ClassID as C, count(*) as N from Erp.Part as p group by 1"
        )
        assert "group_by_ordinal" in r.rules
        assert "GROUP BY p.ClassID" in r.sql

    def test_out_of_range_ordinal_errors(self):
        r = transpile("select p.PartNum as PN from Erp.Part as p order by 4")
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_ordinal_out_of_range"

    def test_a_numeric_literal_in_where_is_not_an_ordinal(self):
        r = transpile(
            "select top 5 p.PartNum as PN from Erp.Part as p where p.ClassID = 1 "
            "order by p.PartNum"
        )
        assert "order_by_ordinal" not in r.rules


# --------------------------------------------------------------------------- #
# 5. DISTINCT + TOP — silently wrong, therefore refused
# --------------------------------------------------------------------------- #


class TestDistinctWithTop:
    @pytest.mark.parametrize(
        "sql",
        [
            "select top 50 distinct p.ClassID as C from Erp.Part as p",
            "select distinct top 50 p.ClassID as C from Erp.Part as p",
            REAL_C_REPAIR_DISTINCT_TOP,
        ],
        ids=["top_then_distinct", "distinct_then_top", "real_repair_loop_output"],
    )
    def test_both_keyword_orders_are_refused(self, sql):
        """Regression coverage: test both keyword orders are refused."""
        r = transpile(sql)
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_distinct_with_top"
        # The envelope must hand back the VERIFIED derived-table shape.
        assert "(select distinct" in r.error["valid"]["replacement"].lower()

    def test_the_refusal_carries_a_runnable_alternative(self):
        r = transpile("select distinct top 50 p.ClassID as C from Erp.Part as p")
        alt = r.error["valid"]["replacement"]
        assert "select distinct" in alt.lower()
        assert alt.lower().index("select top") < alt.lower().index("select distinct")

    def test_bare_distinct_is_fine(self):
        r = transpile("select distinct p.ClassID as C from Erp.Part as p")
        assert r.outcome is not Outcome.REFUSED


# --------------------------------------------------------------------------- #
# 6. Refusals with no proven equivalent
# --------------------------------------------------------------------------- #


class TestRefusals:
    def test_count_distinct_is_refused_not_rewritten(self):
        r = transpile(REAL_B14)
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_distinct_in_aggregate"
        assert "Engine compatibility behavior" in r.error["evidence"]
        # The refusal must explain WHY the known rewrite was not applied.
        assert "compose" in r.error["detail"]["why_not_rewritten"]

    def test_exists_is_refused_with_both_alternatives(self):
        r = transpile(REAL_B10_EXISTS)
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] in {"sql_exists_unsupported", "sql_distinct_with_top"}

    def test_bare_exists_is_refused(self):
        r = transpile(
            "select top 5 p.PartNum as PN from Erp.Part as p "
            "where exists (select 1 from Erp.JobHead as j where j.PartNum = p.PartNum)"
        )
        assert r.error["error"] == "sql_exists_unsupported"
        assert "in (select" in str(r.error["valid"]).lower()
        assert "left outer join" in str(r.error["valid"]).lower()

    def test_select_star_is_refused(self):
        r = transpile("select * from Erp.Part as p")
        assert r.error["error"] == "sql_select_star"

    def test_select_star_refusal_can_be_switched_off(self):
        r = transpile("select * from Erp.Part as p", policy=Policy(refuse_select_star=False))
        assert r.outcome is not Outcome.REFUSED

    def test_qualified_star_is_refused_too(self):
        r = transpile("select p.* from Erp.Part as p")
        assert r.error["error"] == "sql_select_star"

    @pytest.mark.parametrize(
        "sql,code",
        [
            ("update Erp.Part set PartNum = 'x'", "sql_not_a_select"),
            ("delete from Erp.Part", "sql_not_a_select"),
            ("insert into Erp.Part (PartNum) values ('x')", "sql_not_a_select"),
            ("select top 5 p.PartNum as PN from Erp.Part as p; select 1 as X", "sql_multiple_statements"),
            ("select top 5 p.PartNum as PN into #t from Erp.Part as p", "sql_select_into"),
            ("select top 5 p.PartNum as PN from Erp.Part as p where p.PartNum = @Part", "sql_parameter_marker"),
            ("select top 5 row_number() over (order by p.PartNum) as R from Erp.Part as p", "sql_window_unsupported"),
            ("select top 5 a.X as X from Erp.A as a cross join Erp.B as b", "sql_cross_join"),
            ("select top 5 a.X as X from Erp.A as a, Erp.B as b", "sql_comma_join_no_predicate"),
        ],
    )
    def test_statement_shape_refusals(self, sql, code):
        r = transpile(sql)
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == code

    def test_unparseable_input_is_refused_not_crashed(self):
        r = transpile("this is not sql at all ((((")
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] in {"sql_unparseable", "sql_not_a_select"}

    @pytest.mark.parametrize("sql", ["   ", ";", "-- only a comment", "/* nothing */"])
    def test_empty_input(self, sql):
        """`;` and a lone comment parse to NOTHING.

        Indexing `statements[0]` raised `IndexError` — an exception no caller of
        a validation function expects, and the kind of crash that takes a server
        down rather than returning an envelope.
        """
        r = transpile(sql)
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] in {"sql_empty", "sql_multiple_statements"}

    def test_no_input_raises_instead_of_returning(self):
        """Malformed input must return a refusal rather than raise an exception."""
        for sql in ["(((", "select", "\x00\x01", "select top 5 a from t where b = 'x",
                    "with x as () select 1", "select top 5 x from t order by"]:
            transpile(sql)  # must not raise

    def test_every_refusal_carries_evidence_and_a_message(self):
        for sql in [
            "select * from Erp.Part as p",
            "select top 50 distinct p.ClassID as C from Erp.Part as p",
            REAL_B14,
            "select top 5 percent p.PartNum as PN from Erp.Part as p",
            "delete from Erp.Part",
        ]:
            r = transpile(sql)
            assert r.outcome is Outcome.REFUSED
            assert r.error["message"] and len(r.error["message"]) > 30
            assert r.error["evidence"]
            assert r.sql is None


# --------------------------------------------------------------------------- #
# 7. UNION ORDER BY — silently discarded, wrapped
# --------------------------------------------------------------------------- #


class TestUnionOrderBy:
    def test_real_union_order_is_lifted_onto_a_wrapper(self):
        """Epicor can ignore TOP on a select that wraps a set operation. Moving
        the bound to each branch instead produces an unordered sample, which
        silently changes aggregate and ranking results.

        The assertions require that NO bound is invented
        anywhere, PageSize is the bound (invariant 9 — it is sent on every
        Execute regardless), and the reason rides in an advisory. The row set
        the ORDER BY ranks is therefore the WHOLE union, not a sample of it.
        """
        r = transpile(REAL_B21_UNION)
        assert r.outcome is Outcome.REWRITTEN
        assert "union_order_by_discarded" in r.rules
        assert r.sql.startswith("SELECT u.RecordType AS RecordType")
        assert "ORDER BY u.RecordType, u.DMRNum, u.ActionNum" in r.sql
        # The wrapper must not carry a `top` — Epicor ignores it there.
        assert not r.sql.upper().startswith("SELECT TOP")
        # ...and NEITHER MAY THE BRANCHES. This is the line that changed.
        assert r.sql.upper().count("SELECT TOP") == 0
        assert r.row_bound.kind == "page_size_only"
        assert r.row_bound.source == "injected"
        assert "setop_wrapper_bounded_by_page_size_only" in {
            a.rule for a in r.advisories
        }

    def test_a_setop_wrapper_never_silently_loses_the_callers_top(self):
        """The caller's own bound may not vanish.

        `setop_outer_bound_moved` deleted it and announced the deletion as a
        helpful rewrite. A bound Epicor cannot honor must be refused because
        silently moving it changes the requested result.
        """
        sql = (
            "select top 10 [w].[PartNum] as [PartNum], sum([w].[Amt]) as [Revenue] "
            "from (select [A].[PartNum] as [PartNum], [A].[ExtPriceDtl] as [Amt] "
            "from Erp.OrderDtl as [A] where [A].[OrderNum] < 100000 union all "
            "select [B].[PartNum] as [PartNum], [B].[ExtPriceDtl] as [Amt] "
            "from Erp.OrderDtl as [B] where [B].[OrderNum] >= 100000) as [w] "
            "group by [w].[PartNum]"
        )
        r = transpile(sql)
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_setop_wrapper_unsafe"
        assert "setop_outer_bound_moved" not in r.rules
        # The recovery is RUNNABLE, not a template, and it is a CTE.
        retry = r.error["retry_with"]["sql"]
        assert retry.upper().startswith("WITH [W] AS (")
        assert "TOP 10" in retry.upper()          # the caller's bound SURVIVES
        assert "GROUP BY [w].[PartNum]" in retry  # and so does the grouping

    def test_the_wrapper_does_NOT_emit_a_nulls_ordering_case(self):
        """The bug that only an Execute caught.

        sqlglot's tsql generator emulates NULLS FIRST/LAST with
        `ORDER BY CASE WHEN x IS NULL THEN 1 ELSE 0 END, x ASC` for any Ordered
        whose `desc` is False rather than None. Epicor PARSES that fine and then
        fails at run time with "An object or column name is missing or empty."
        """
        r = transpile(REAL_B21_UNION)
        assert "IS NULL THEN 1" not in r.sql.upper()

    def test_desc_direction_is_preserved(self):
        r = transpile(
            "select top 5 pw.PartNum as PN from Erp.PartWhse as pw union all "
            "select top 5 p.PartNum as PN from Erp.Part as p order by PN desc"
        )
        assert "ORDER BY u.PN DESC" in r.sql

    def test_union_sort_on_an_expression_is_refused(self):
        r = transpile(
            "select top 5 pw.PartNum as PN from Erp.PartWhse as pw union all "
            "select top 5 p.PartNum as PN from Erp.Part as p order by len(PN)"
        )
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_union_order_expression"

    def test_union_sort_on_an_unknown_column_is_refused(self):
        r = transpile(
            "select top 5 pw.PartNum as PN from Erp.PartWhse as pw union all "
            "select top 5 p.PartNum as PN from Erp.Part as p order by Nope"
        )
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_union_order_unknown_column"
        assert r.error["valid"]["union_output_columns"] == ["PN"]

    def test_union_without_an_order_by_is_left_alone(self):
        r = transpile(
            "select top 5 pw.PartNum as PN from Erp.PartWhse as pw union all "
            "select top 5 p.PartNum as PN from Erp.Part as p"
        )
        assert "union_order_by_discarded" not in r.rules


# --------------------------------------------------------------------------- #
# 8. Column qualification — the ambiguity path
# --------------------------------------------------------------------------- #


class TestQualification:
    JOIN = (
        "from Erp.Part as p inner join Erp.PartWhse as pw "
        "on p.Company = pw.Company and p.PartNum = pw.PartNum"
    )

    def test_a_uniquely_owned_column_is_qualified(self):
        r = transpile(f"select top 5 OnHandQty as Q {self.JOIN}", schema=SCHEMA)
        assert r.outcome is Outcome.REWRITTEN
        assert "unqualified_column_on_join" in r.rules
        assert "pw.OnHandQty" in r.sql

    def test_an_ambiguous_column_ERRORS_and_names_both_candidates(self):
        """`PartNum` exists on 477 Epicor tables. Guessing here re-creates the
        exact failure class this server exists to eliminate."""
        r = transpile(f"select top 5 PartNum as PN {self.JOIN}", schema=SCHEMA)
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_ambiguous_column"
        assert set(r.error["valid"]["candidates"]) == {"[p].[PartNum]", "[pw].[PartNum]"}

    def test_an_unknown_column_errors_with_the_real_column_list(self):
        r = transpile(f"select top 5 NoSuchCol as X {self.JOIN}", schema=SCHEMA)
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_unknown_column"
        cols = r.error["valid"]["columns_by_table"]["Erp.PartWhse"]
        assert "OnHandQty" in cols  # original casing, paste-able

    def test_without_a_schema_it_advises_and_never_guesses(self):
        r = transpile(f"select top 5 OnHandQty as Q {self.JOIN}")
        assert r.outcome is Outcome.UNCHANGED
        assert any(a.rule == "unqualified_column_no_schema" for a in r.advisories)

    def test_a_single_table_subquery_is_NOT_flagged(self):
        """sqlglot's own ``Scope.columns`` lifts a correlated subquery's columns
        into the OUTER scope, and ``sql_lint`` counts sources statement-wide.
        A bare column in a single-table subquery is legal.
        """
        r = transpile(REAL_B10_SUBQUERY, schema=SCHEMA)
        assert r.outcome is not Outcome.REFUSED
        assert "unqualified_column_on_join" not in r.rules
        assert not any(a.rule == "unqualified_column_no_schema" for a in r.advisories)

    def test_a_single_source_select_is_never_qualified(self):
        r = transpile("select top 5 PartNum as PN from Erp.Part as p", schema=SCHEMA)
        assert r.outcome is Outcome.UNCHANGED


# --------------------------------------------------------------------------- #
# 9. Advisories — reported, never silently "fixed"
# --------------------------------------------------------------------------- #


class TestAdvisories:
    def test_join_without_company_is_advisory_not_a_rewrite(self):
        """Regression coverage: test join without company is advisory not a rewrite."""
        r = transpile(
            "select top 5 a.X as X from Erp.A as a inner join Erp.B as b on a.K = b.K"
        )
        assert r.outcome is Outcome.UNCHANGED
        assert any(a.rule == "join_missing_company" for a in r.advisories)

    def test_company_only_join_is_called_out_as_a_cartesian(self):
        r = transpile(
            "select top 5 a.X as X from Erp.A as a "
            "inner join Erp.B as b on a.Company = b.Company"
        )
        adv = {a.rule for a in r.advisories}
        assert "join_on_company_only" in adv
        assert "join_missing_company" not in adv

    def test_fanout_risk_on_an_aggregate_over_three_tables(self):
        r = transpile(
            "select top 5 h.JobNum as J, sum(i.ExtPrice) as Rev from Erp.JobHead as h "
            "left join Erp.InvcDtl as i on h.Company = i.Company and h.JobNum = i.JobNum "
            "left join Erp.PartTran as t on h.Company = t.Company and h.JobNum = t.JobNum "
            "group by h.JobNum"
        )
        assert any(a.rule == "fanout_risk" for a in r.advisories)

    def test_missing_output_alias_is_advisory_and_explains_why(self):
        """Auto-aliasing is NOT done: two joined tables both carrying PartNum
        would collide on one output key — a silently wrong projection."""
        r = transpile("select top 5 p.PartNum from Erp.Part as p")
        adv = [a for a in r.advisories if a.rule == "missing_output_alias"]
        assert adv and "collide" in adv[0].message


# --------------------------------------------------------------------------- #
# 10. Contract-level properties
# --------------------------------------------------------------------------- #


class TestContract:
    def test_unchanged_returns_the_input_byte_for_byte(self):
        sql = "select top 25 [P].[PartNum] as [PN]  from   Erp.Part as [P]"
        r = transpile(sql)
        assert r.outcome is Outcome.UNCHANGED
        assert r.sql == sql

    def test_rewritten_lists_every_transformation(self):
        r = transpile(REAL_B22)
        assert r.transformations
        for t in r.transformations:
            assert t.message and t.evidence and t.proof == "VERIFIED"
        assert r.assumptions == [t.message for t in r.transformations]

    def test_refused_never_returns_sql(self):
        r = transpile("select * from Erp.Part as p")
        assert r.sql is None
        assert r.refused

    def test_to_dict_is_json_safe(self):
        import json

        for sql in [REAL_B22, REAL_B14, REAL_B02_NO_BOUND, "select * from Erp.Part as p"]:
            json.dumps(transpile(sql).to_dict())

    def test_tables_referenced_excludes_ctes(self):
        r = transpile(
            "with c as (select p.PartNum as PN from Erp.Part as p) "
            "select top 5 c.PN as PN from c"
        )
        assert r.tables_referenced == ["Erp.Part"]

    def test_predicate_injection_is_a_declared_but_unimplemented_seam(self):
        """It must FAIL LOUDLY rather than silently return unfiltered rows."""
        r = transpile(REAL_B02_NO_BOUND, inject_predicate=lambda tree, tables: {})
        assert r.outcome is Outcome.REFUSED
        assert r.error["error"] == "sql_predicate_injection_not_implemented"

    def test_bracketed_identifiers_survive(self):
        r = transpile("select [P].[PartNum] as [Part Number] from [Erp].[Part] as [P]")
        assert "[Part Number]" in r.sql
        assert "[Erp].[Part]" in r.sql

    def test_output_is_stable_under_a_second_pass(self):
        """Idempotence: transpiling the output must not change it again."""
        for sql in [REAL_B22, REAL_B02_NO_BOUND, REAL_B21_UNION, REAL_B01_FETCH]:
            first = transpile(sql)
            if first.refused:
                continue
            second = transpile(first.sql)
            assert second.outcome in {Outcome.UNCHANGED, Outcome.REWRITTEN}
            if second.outcome is Outcome.REWRITTEN:
                third = transpile(second.sql)
                assert third.outcome is Outcome.UNCHANGED, (
                    f"not idempotent after 2 passes: {second.rules}"
                )
            assert not second.refused


# --------------------------------------------------------------------------- #
# 11. Regression corpus — shared bound guarantees across the fixture shapes
# --------------------------------------------------------------------------- #

ALL_REAL = [
    REAL_B22,
    REAL_B14,
    REAL_B10_EXISTS,
    REAL_B10_SUBQUERY,
    REAL_B21_UNION,
    REAL_C_REPAIR_DISTINCT_TOP,
    REAL_B02_NO_BOUND,
    REAL_B01_FETCH,
]


@pytest.mark.parametrize("sql", ALL_REAL, ids=range(len(ALL_REAL)))
def test_every_real_statement_produces_a_bounded_result_or_a_refusal(sql):
    """The safety invariant: nothing leaves this module unbounded and unexplained."""
    r = transpile(sql)
    if r.refused:
        assert r.error["error"].startswith("sql_")
        return
    assert r.row_bound is not None
    assert r.row_bound.kind in {"top", "fetch", "page_size_only"}
    if r.row_bound.kind == "page_size_only":
        # The ONLY reasons a statement may leave here without a query-level
        # bound. Each requires PageSize alone and must ANNOUNCE itself:
        #   * `select distinct` — injecting TOP can invalidate distinctness.
        #   * a set operation wrapped in a derived table — Epicor IGNORES a
        #     `top` on that wrapper, so injecting one declares a bound that does
        #     not exist. A per-branch bound would instead aggregate an arbitrary
        #     unordered sample.
        # `per_branch` remains legal for a BARE set operation, where a
        # per-branch `top` is honored.
        assert any(
            a.rule
            in {
                "distinct_bounded_by_page_size",
                "setop_wrapper_bounded_by_page_size_only",
            }
            for a in r.advisories
        ) or r.row_bound.source == "per_branch"

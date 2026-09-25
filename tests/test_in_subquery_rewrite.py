"""`x [not] in (select …)` — Epicor never compares the value.

Measured against a live tenant: Epicor's BAQ engine evaluates ``x in
(<subquery>)`` as *"the subquery returned any row"*. An uncorrelated IN over
open POs returned EVERY open PO; ``not in`` returned 0; the CTE semi-join and
the left-join anti-join partitioned the open POs exactly.

The ONE shape it answers correctly is a subquery whose WHERE equates its own
projected column to the outer column — then "any row" IS the per-row answer.
Correlated on Company alone it is wrong again, and inside an ON clause it errors.

So: key-correlated in a WHERE → untouched; mechanical uncorrelated → rewritten
to a distinct-key CTE join (announced); everything else → refused.
"""

from __future__ import annotations

import sqlglot

from epicor_mcp.sql.transpile import WEDGE_POLICY, Outcome, transpile


def tp(sql: str):
    return transpile(sql, policy=WEDGE_POLICY)


def rules(result) -> set[str]:
    return {t.rule for t in result.transformations}


HEAD = "select top 5 count(*) as [N] from Erp.POHeader as [PH] where [PH].[OpenOrder] = 1 and "
IN_REL = "[PH].[PONum] in (select [R].[PONum] from Erp.PORel as [R] where [R].[OpenRelease] = 1)"


def test_an_uncorrelated_in_becomes_a_distinct_key_cte_inner_join():
    r = tp(HEAD + IN_REL)
    assert r.outcome is Outcome.REWRITTEN
    assert "in_subquery_semi_join" in rules(r)
    out = r.sql.upper()
    assert " IN (" not in out, "the broken predicate must not survive"
    assert "WITH [INKEYS1] AS (SELECT DISTINCT [R].[COMPANY] AS [COMPANY], [R].[PONUM]" in out
    # The CTE side LEADS each ON conjunct — Epicor files a conjunct under its
    # left-hand table (sql/CLAUDE.md), and the joined table must own it.
    assert "INNER JOIN [INKEYS1] ON [INKEYS1].[COMPANY] = [PH].[COMPANY] AND " \
           "[INKEYS1].[PONUM] = [PH].[PONUM]" in out
    # The caller's other predicate is kept, the subquery's own filter moved into the CTE.
    assert "WHERE [PH].[OPENORDER] = 1" in out
    assert "WHERE [R].[OPENRELEASE] = 1" in out


def test_not_in_becomes_a_left_join_anti_join_and_announces_the_null_difference():
    r = tp(HEAD + "[PH].[PONum] not in (select [R].[PONum] from Erp.PORel as [R] "
                  "where [R].[OpenRelease] = 1)")
    assert r.outcome is Outcome.REWRITTEN
    assert "not_in_subquery_anti_join" in rules(r)
    out = r.sql.upper()
    assert "LEFT OUTER JOIN [INKEYS1]" in out
    assert "[INKEYS1].[PONUM] IS NULL" in out
    msg = next(t.message for t in r.transformations if t.rule == "not_in_subquery_anti_join")
    assert "NULL" in msg


def test_the_rewrite_is_the_only_predicate_so_the_where_disappears_cleanly():
    r = tp("select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where " + IN_REL)
    assert r.outcome is Outcome.REWRITTEN
    main = r.sql.upper().split(") SELECT ", 1)[1]
    assert "WHERE" not in main


def test_a_grouped_subquery_adds_company_to_its_group_by_instead_of_distinct():
    r = tp("select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where [PH].[PONum] in "
           "(select [R].[PONum] from Erp.PORel as [R] group by [R].[PONum] having count(*) > 3)")
    assert r.outcome is Outcome.REWRITTEN
    out = r.sql.upper()
    assert "GROUP BY [R].[PONUM], [R].[COMPANY]" in out
    assert "HAVING COUNT(*) > 3" in out


def test_nested_ins_define_each_cte_before_the_cte_that_uses_it():
    r = tp("select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where [PH].[VendorNum] in "
           "(select [V].[VendorNum] from Erp.Vendor as [V] where [V].[VendorNum] in "
           "(select [AP].[VendorNum] from Erp.APInvHed as [AP] where [AP].[InvoiceAmt] > 1000))")
    assert r.outcome is Outcome.REWRITTEN
    out = r.sql.upper()
    assert out.index("[INKEYS2] AS (") < out.index("[INKEYS1] AS ("), \
        "T-SQL only lets a CTE reference CTEs defined before it"
    sqlglot.parse_one(r.sql, read="tsql")  # and the whole thing re-parses


def test_an_existing_cte_list_is_extended_not_replaced():
    r = tp("with [c] as (select [P].[Company] as [Company], [P].[PartNum] as [PartNum] "
           "from Erp.Part as [P]) select top 5 [c].[PartNum] as [PN] from [c] where [c].[PartNum] "
           "in (select [W].[PartNum] from Erp.PartWhse as [W] where [W].[OnHandQty] > 0)")
    assert r.outcome is Outcome.REWRITTEN
    out = r.sql.upper()
    assert "[C] AS (" in out and "[INKEYS1] AS (" in out
    # The outer column sits on a CTE, not an Erp table, so no Company term is invented.
    assert "[INKEYS1].[PARTNUM] = [C].[PARTNUM]" in out
    assert "[INKEYS1].[COMPANY]" not in out


def test_a_key_correlated_in_is_left_alone_because_epicor_answers_it_correctly():
    for neg in ("", "not "):
        sql = ("select top 5 count(*) as [N] from Erp.POHeader as [PH] where [PH].[VendorNum] "
               f"{neg}in (select [AP].[VendorNum] from Erp.APInvHed as [AP] "
               "where [AP].[VendorNum] = [PH].[VendorNum] and [AP].[InvoiceAmt] > 100000)")
        r = tp(sql)
        assert r.outcome is Outcome.UNCHANGED, (neg, r.error)
    # Either side of the equality.
    r = tp(HEAD + "[PH].[PONum] in (select [R].[PONum] from Erp.PORel as [R] "
                  "where [PH].[PONum] = [R].[PONum] and [R].[OpenRelease] = 1)")
    assert r.outcome is Outcome.UNCHANGED


def _refusal(sql: str) -> dict:
    r = tp(sql)
    assert r.outcome is Outcome.REFUSED, r.sql
    assert r.error["error"] == "sql_in_subquery_unsupported"
    assert r.error["valid"]["alternatives"], "a refusal must hand back the shape that works"
    return r.error


def test_correlated_on_something_other_than_the_key_is_refused_and_names_the_fix():
    """Measured: Company-only correlation returned every open PO again."""
    err = _refusal(HEAD + "[PH].[PONum] in (select [R].[PONum] from Erp.PORel as [R] "
                          "where [R].[Company] = [PH].[Company] and [R].[OpenRelease] = 1)")
    assert "[R].[PONum] = [PH].[PONum]" in err["detail"]["why_not_rewritten"]


def test_shapes_that_cannot_be_rewritten_mechanically_are_refused_never_run():
    cases = {
        "under_or": "select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where "
                    "[PH].[OpenOrder] = 1 or " + IN_REL,
        "in_on_clause": "select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] inner join "
                        "Erp.Vendor as [V] on [PH].[Company] = [V].[Company] and [PH].[VendorNum] "
                        "= [V].[VendorNum] and [V].[VendorNum] in (select [AP].[VendorNum] from "
                        "Erp.APInvHed as [AP])",
        "aggregate_projection": "select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where "
                                "[PH].[PONum] in (select max([R].[PONum]) from Erp.PORel as [R])",
        "top_in_subquery": "select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where "
                           "[PH].[PONum] in (select top 10 [R].[PONum] from Erp.PORel as [R])",
        "unqualified_inner": "select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where "
                             "[PH].[PONum] in (select [R].[PONum] from Erp.PORel as [R] "
                             "where OpenRelease = 1)",
        "expression_lhs": "select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where "
                          "[PH].[PONum] + 0 in (select [R].[PONum] from Erp.PORel as [R])",
        "inside_derived_table": "select top 5 [d].[P] as [P] from (select [PH].[PONum] as [P] "
                                "from Erp.POHeader as [PH] where " + IN_REL + ") as [d]",
    }
    for name, sql in cases.items():
        err = _refusal(sql)
        assert err["detail"]["why_not_rewritten"], name


def test_literal_lists_and_scalar_subqueries_are_untouched():
    for sql in (
        "select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where [PH].[VendorNum] in (1, 2, 3)",
        "select top 5 [PH].[PONum] as [P] from Erp.POHeader as [PH] where [PH].[PONum] = "
        "(select top 1 [R].[PONum] from Erp.PORel as [R] where [R].[PONum] = 520168)",
    ):
        r = tp(sql)
        assert r.outcome is Outcome.UNCHANGED, r.error


def test_the_rewrite_carries_a_known_answer_proof():
    """VERIFIED means an independently known answer."""
    t = next(t for t in tp(HEAD + IN_REL).transformations if t.rule == "in_subquery_semi_join")
    assert t.proof == "VERIFIED"
    assert "measured" in t.evidence and "CTE" in t.evidence


def test_the_dialect_block_no_longer_recommends_in_select_as_a_safe_form():
    from epicor_mcp.sql.tool import SQL_PARAM_DESCRIPTION

    assert "SILENTLY WRONG" in SQL_PARAM_DESCRIPTION
    assert "use `in (select ...)`" not in SQL_PARAM_DESCRIPTION
    # and the alias rule is taught as working, not as a failure
    assert "order by [Revenue] desc" in SQL_PARAM_DESCRIPTION
    assert "FAILS: Invalid column name" not in SQL_PARAM_DESCRIPTION

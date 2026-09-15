"""Regression coverage: test transpile ship gate."""

from __future__ import annotations

import inspect

from epicor_mcp.sql import adhoc
from epicor_mcp.sql.transpile import WEDGE_POLICY, Outcome, Policy, transpile

SCHEMA = {
    "Erp.PartWhse": ["Company", "PartNum", "WarehouseCode", "OnHandQty"],
    "Erp.Part": ["Company", "PartNum", "UnitPrice", "ClassID"],
}

S1_INPUT = """\
select [t].[PartNum] as [PartNum], [OnHandQty] as [Qty]
from (select [PW].[PartNum] as [PartNum], sum([PW].[OnHandQty]) as [OnHandQty]
      from Erp.PartWhse as [PW] where [PW].[PartNum] = '1234567-13'
      group by [PW].[PartNum]) as [t]
join Erp.PartWhse as [PW2] on [PW2].[PartNum] = [t].[PartNum]"""

S2_INPUT = """\
select [PW].[PartNum] as [PN], [PW].[OnHandQty] as [Q] from Erp.PartWhse as [PW]
union all
select [P].[PartNum] as [PN], [P].[UnitPrice] as [Q] from Erp.Part as [P]
where [P].[ClassID] = 'FG'
order by [Q] desc"""

S3_INPUT = """\
select top 5 [t].[PartNum] as [Qty], [t].[Qty] as [Q2]
from (select [PW].[PartNum] as [PartNum], [PW].[OnHandQty] as [Qty]
      from Erp.PartWhse as [PW]) as [t]
order by [Qty] desc"""


def test_the_wedge_policy_disables_exactly_the_three_gated_passes():
    assert WEDGE_POLICY.enable_qualify_pass is False
    assert WEDGE_POLICY.enable_setop_order_wrap is False
    assert WEDGE_POLICY.schema_derived_shadow_owners is False
    # ...and changes nothing else about the proven subset.
    default = Policy()
    assert WEDGE_POLICY.default_limit == default.default_limit
    assert WEDGE_POLICY.max_rows == default.max_rows
    assert WEDGE_POLICY.refuse_unknown_tables == default.refuse_unknown_tables
    # `select *` is refused ONE PARSE LATER, by sql/lint.py, which can serve the
    # table's real column list — the documented recovery path. This
    # module holds no schema with which to serve it.
    assert WEDGE_POLICY.refuse_select_star is False


def test_the_production_pipe_uses_the_wedge_policy_and_supplies_no_schema():
    """A disabled gate must not be advertised as enforcing query limits."""
    source = inspect.getsource(adhoc.run_sql)
    assert "replace(WEDGE_POLICY" in source
    assert "policy=policy_for_sql" in source
    assert "schema=None" in source
    # The only knobs the pipe is allowed to move are the row-bound ones.
    line = next(ln for ln in source.splitlines() if "replace(WEDGE_POLICY" in ln)
    assert "default_limit=page_size" in line and "max_rows=MAX_PAGE_SIZE" in line
    assert "enable_" not in line and "schema_derived" not in line


def test_S1_the_qualification_pass_no_longer_attaches_the_column_to_PW2():
    """Attaching the column to PW2 changes the answer: the rewrite returns 0.0
    where the input returns a non-zero quantity."""
    broken = transpile(S1_INPUT, schema=SCHEMA)
    assert "unqualified_column_on_join" in broken.rules
    assert "PW2.[OnHandQty]" in (broken.sql or "")

    gated = transpile(S1_INPUT, schema=SCHEMA, policy=WEDGE_POLICY)
    assert "unqualified_column_on_join" not in gated.rules
    assert "PW2.[OnHandQty]" not in (gated.sql or "")
    assert "[OnHandQty] AS [Qty]" in (gated.sql or "")
    # It is reported, not silently ignored.
    assert any(a.rule == "unqualified_column_no_schema" for a in gated.advisories)


def test_S2_the_union_wrap_is_not_emitted():
    """The wrap plus the injected per-branch bounds ranks a truncated sample, so
    the reported top row can be a different part with a value far below the true
    maximum."""
    broken = transpile(S2_INPUT, schema=SCHEMA)
    assert "union_order_by_discarded" in broken.rules
    assert "FROM (" in (broken.sql or "").upper()

    gated = transpile(S2_INPUT, schema=SCHEMA, policy=WEDGE_POLICY)
    assert "union_order_by_discarded" not in gated.rules
    assert " AS u" not in (gated.sql or "")
    # The caller is told, and the DS lint refuses the statement outright.
    assert any(
        a.rule == "setop_order_by_discarded_not_rewritten" for a in gated.advisories
    )


def test_S2_the_lint_is_what_stops_the_statement():
    """The safe alternative for an unsupported wrapper is 'REFUSE ... the way
    order_scan_unbounded does'. That refusal lives in sql/lint.py and fires on
    the real parse of exactly this shape."""
    from epicor_mcp.sql.lint import Severity, lint_parsed
    from tests.wedge_fixtures import load

    sql, ds = load("union_order_by")
    hit = next(f for f in lint_parsed(sql, ds) if f.rule == "setop_order_by_discarded")
    assert hit.severity == Severity.REFUSE


def test_S3_the_shadowed_sort_alias_is_refused_not_rewritten():
    """With a schema the guard is DEFEATED and the rewrite returns a different
    row (for example `PART-A / 0.0` where the input returns `PART-B / 1000.0`).
    Any guard whose safety decreases with more information is inverted."""
    broken = transpile(S3_INPUT, schema=SCHEMA)
    assert broken.outcome is Outcome.REWRITTEN
    assert "order_by_alias" in broken.rules

    gated = transpile(S3_INPUT, schema=SCHEMA, policy=WEDGE_POLICY)
    assert gated.outcome is Outcome.REFUSED
    assert gated.error["error"] == "sql_ambiguous_sort_alias"
    assert gated.sql is None


def test_the_proven_subset_still_works_under_the_gate():
    """the wedge ships the transpiler's proven subset — safety-class limit
    normalisation, `order by <alias>`, and the row bound."""
    r = transpile("select top (100) [P].[PartNum] as [PN] from Erp.Part as [P]",
                  policy=WEDGE_POLICY)
    assert "top_parenthesised" in r.rules
    assert "TOP 100" in (r.sql or "").upper()

    r = transpile("select limit 5", policy=WEDGE_POLICY)
    assert r.outcome is Outcome.REFUSED or "limit_clause" in r.rules

    r = transpile(
        "select [OD].[PartNum] as [PN], sum([OD].[ExtPriceDtl]) as [Rev] "
        "from Erp.OrderDtl as [OD] group by [OD].[PartNum] order by [Rev] desc",
        policy=WEDGE_POLICY,
    )
    assert "order_by_alias" in r.rules
    assert "ORDER BY SUM([OD].[ExtPriceDtl]) DESC" in (r.sql or "")

    r = transpile("select [P].[PartNum] as [PN] from Erp.Part as [P]", policy=WEDGE_POLICY)
    assert "row_bound_injected" in r.rules
    assert r.row_bound.value == 100

    r = transpile("select top 99999 [P].[PartNum] as [PN] from Erp.Part as [P]",
                  policy=WEDGE_POLICY)
    assert "row_bound_clamped" in r.rules
    assert r.row_bound.value == 1000


def test_the_gate_defaults_are_unchanged_so_no_existing_test_is_weakened():
    """The flags default to the module's historic behaviour; only the wedge
    turns them off. Feature E13 (Phase 1) either re-enables them with a
    known-answer proof or deletes those passes."""
    default = Policy()
    assert default.enable_qualify_pass is True
    assert default.enable_setop_order_wrap is True
    assert default.schema_derived_shadow_owners is True

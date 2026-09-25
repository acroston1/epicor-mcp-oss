"""Cost-governor checks for expensive statements that pass syntax and access checks.

Synthetic parsed datasets verify that Cartesian joins and large scans are
refused before execution even when the SQL is otherwise valid.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from epicor_mcp.sql.governor import (
    BIG_TABLES,
    CostGovernor,
    GovernorPolicy,
    check_cost,
    timeout_envelope,
)
from tests.wedge_fixtures import load

STRICT = GovernorPolicy(strict_scan_guard=True)


# --------------------------------------------------------------------------- #
# The three expensive query shapes
# --------------------------------------------------------------------------- #


def test_acceptance_shape_1_cross_join_is_refused():
    """`from Erp.PartTran as [A] cross join Erp.Part as [B] group by …`"""
    _, ds = load("gov_cross_join")
    env = check_cost(ds)
    assert env is not None
    assert env["error"] == "query_too_expensive"
    assert "Erp.PartTran" in env["detail"]["tables"]
    assert "cross join" in env["message"]


def test_acceptance_shape_2_unbounded_comma_join_is_refused():
    """`from Erp.JobHead as [A], Erp.LaborDtl as [B], Erp.JobOper as [C]
    where [A].[Company] = [B].[Company] group by [A].[JobNum]`"""
    _, ds = load("gov_unbounded_scan")
    env = check_cost(ds)
    assert env is not None
    assert env["error"] == "query_too_expensive"
    assert set(env["detail"]["tables"]) == {"Erp.JobHead", "Erp.LaborDtl", "Erp.JobOper"}


def test_acceptance_shape_2_also_warns_about_grain():
    """Regression coverage: test acceptance shape 2 also warns about grain."""
    from epicor_mcp.sql.grain import analyse_grain
    from epicor_mcp.sql.lint import lint_parsed

    sql, ds = load("gov_fanout")
    assert "aggregate_fanout" in [
        f.rule for f in lint_parsed(sql, ds, fanout_warning=True)
    ]
    assert "aggregate_fanout" in [f.rule for f in analyse_grain(sql).findings]


def test_acceptance_shape_3_is_the_timeout_and_it_is_honest_about_its_reach():
    env = timeout_envelope(25.0, "select ...")
    assert env["error"] == "query_too_expensive"
    assert "NOT yet established" in env["message"]
    assert env["detail"]["timeout_s"] == 25.0
    assert env["retry_with"]["sql"] == "select ..."


# --------------------------------------------------------------------------- #
# The company-only cartesian
# --------------------------------------------------------------------------- #


def test_a_join_on_company_alone_is_refused_and_names_the_missing_key():
    _, ds = load("gov_company_only_join")
    env = check_cost(ds)
    assert env is not None
    assert "ONLY Company" in env["message"]
    assert "JobNum" in env["valid"]["shape"] or "JobNum" in env["message"]
    assert env["detail"]["join_fields"] == [("Company", "Company")]


def test_a_join_on_company_plus_the_business_key_is_fine():
    _, ds = load("gov_bounded_join")
    assert check_cost(ds) is None


# --------------------------------------------------------------------------- #
# What must NOT be refused
# --------------------------------------------------------------------------- #


def test_the_wedge_query_is_allowed_by_default():
    """Regression coverage: test the wedge query is allowed by default."""
    _, ds = load("wedge_rollup")
    assert check_cost(ds) is None


def test_strict_scan_policy_refuses_the_rollup_query():
    """Strict scan policy rejects the rollup accepted by the default policy.
    Both policies remain explicitly testable."""
    _, ds = load("wedge_rollup")
    assert check_cost(ds) is None
    env = check_cost(ds, policy=STRICT)
    assert env is not None
    assert env["detail"]["table"] == "Erp.OrderDtl"
    assert "strict scan guard" in env["message"]


@pytest.mark.parametrize(
    "fixture",
    ["clean_top", "clean_rollup", "clean_empbasic", "clean_in_subquery",
     "gov_bounded_join", "gov_fanout", "deny_projected", "union_order_by"],
)
def test_no_false_refusal_on_a_reasonable_statement(fixture):
    _, ds = load(fixture)
    assert check_cost(ds) is None, fixture


def test_a_subquery_is_scoped_separately_so_it_is_not_a_cross_join():
    """JobHead (TopLevel) and LaborDtl (InnerSubQuery) live in different
    subqueries and have no QueryRelation between them. Comparing them would
    refuse every `where x in (select …)`."""
    _, ds = load("clean_in_subquery")
    subs = {t["SubQueryID"] for t in ds["QueryTable"]}
    assert len(subs) == 2
    assert check_cost(ds) is None


def test_union_branches_are_scoped_separately_too():
    _, ds = load("deny_union_arm")
    assert check_cost(ds) is None


# --------------------------------------------------------------------------- #
# BIG_TABLES is transaction tables, never masters
# --------------------------------------------------------------------------- #


def test_big_tables_carries_transactions_not_masters():
    for transaction in ("parttran", "labordtl", "orderdtl", "joboper", "gljrndtl"):
        assert transaction in BIG_TABLES
    for master in ("part", "customer", "vendor", "plant", "warehse", "resource"):
        assert master not in BIG_TABLES


def _two_tables(a: str, b: str, *, relation: bool = False) -> dict:
    ds: dict = {
        "QueryTable": [
            {"SubQueryID": "s", "TableID": "A", "DBSchemaName": "Erp",
             "DBTableName": a, "TableType": "DB"},
            {"SubQueryID": "s", "TableID": "B", "DBSchemaName": "Erp",
             "DBTableName": b, "TableType": "DB"},
        ],
    }
    if relation:
        ds["QueryRelation"] = [
            {"RelationID": "r1", "SubQueryID": "s", "ParentTableID": "A", "ChildTableID": "B"}
        ]
        ds["QueryRelationField"] = [
            {"RelationID": "r1", "ParentFieldName": "Company", "ChildFieldName": "Company"},
        ]
    return ds


# Master tables remain outside BIG_TABLES, but a Cartesian join between them
# can still multiply rows without a useful bound. Judge the join shape even
# when neither table belongs to the large-transaction-table classification.
def test_two_masters_cross_joined_are_refused_on_the_shape_not_the_table_class():
    env = check_cost(_two_tables("Customer", "Vendor"))
    assert env is not None and env["error"] == "query_too_expensive"
    assert "cross join" in env["message"]
    # ...and the envelope does not PRETEND a transaction table was involved.
    assert env["detail"]["big_tables"] == []


def test_a_master_self_join_on_company_alone_is_refused():
    """Regression coverage: test a master self join on company alone is refused."""
    env = check_cost(_two_tables("Part", "Part", relation=True))
    assert env is not None and env["error"] == "query_too_expensive"
    assert "ONLY Company" in env["message"]
    assert env["detail"]["big_tables"] == []


def test_a_master_joined_to_a_master_on_company_alone_is_refused():
    env = check_cost(_two_tables("Part", "Customer", relation=True))
    assert env is not None and "ONLY Company" in env["message"]


def test_the_transaction_table_case_still_names_the_transaction_tables():
    env = check_cost(_two_tables("PartTran", "Part"))
    assert env is not None
    assert env["detail"]["big_tables"] == ["Erp.PartTran"]
    assert "Erp.PartTran" in env["message"]


# --------------------------------------------------------------------------- #
# Fail-closed, concurrency and the session budget
# --------------------------------------------------------------------------- #


def test_the_governor_fails_closed_when_it_cannot_evaluate():
    class Exploding(dict):
        def get(self, *a, **k):  # noqa: D105
            raise RuntimeError("boom")

    env = check_cost(Exploding())
    assert env is not None and env["error"] == "query_too_expensive"


def test_session_budget_refuses_once_spent_and_rolls_over():
    gov = CostGovernor(GovernorPolicy(session_budget_s=10.0, session_budget_window_s=60.0))
    now = time.monotonic()
    assert gov.check_budget("u", now=now) is None
    gov.record("u", 6.0, now=now)
    assert gov.check_budget("u", now=now) is None
    gov.record("u", 6.0, now=now)
    env = gov.check_budget("u", now=now)
    assert env is not None and env["error"] == "query_budget_exhausted"
    assert env["detail"]["spent_s"] == 12.0
    # ... and another session is unaffected
    assert gov.check_budget("someone-else", now=now) is None
    # ... and the window rolls
    assert gov.check_budget("u", now=now + 61) is None


def test_the_concurrency_cap_is_a_real_semaphore():
    async def scenario():
        gov = CostGovernor(GovernorPolicy(max_inflight=2))
        peak = 0
        current = 0

        async def one():
            nonlocal peak, current
            async with gov.inflight():
                current += 1
                peak = max(peak, current)
                await asyncio.sleep(0.01)
                current -= 1

        await asyncio.gather(*[one() for _ in range(8)])
        return peak

    assert asyncio.run(scenario()) <= 2


# --------------------------------------------------------------------------- #
# Joins THROUGH a CTE / derived table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fixture", ["gov_joined_through_cte", "gov_joined_through_derived"])
def test_tables_joined_only_through_a_cte_or_derived_table_are_not_a_cross_join(fixture):
    """Epicor files a CTE / derived-table reference as its own QueryTable row
    (`TableType == 'SQ'`) and points the QueryRelation rows AT it. With DB
    tables as the only graph nodes every one of those edges was dropped, so
    `Part ⋈ oh`, `PartCost ⋈ oh`, `PartPlant ⋈ oh` — each keyed on Company AND
    PartNum — were refused as a cross join. Real parse output for both shapes."""
    _, ds = load(fixture)
    assert {t["TableType"] for t in ds["QueryTable"]} >= {"DB", "SQ"}
    assert check_cost(ds) is None


def test_a_real_cartesian_next_to_a_cte_is_still_refused():
    """The control: connectivity now runs over the SQ node too, and the
    `cross join Erp.Customer` still lands in its own group."""
    _, ds = load("gov_cartesian_beside_cte")
    env = check_cost(ds)
    assert env is not None
    assert env["error"] == "query_too_expensive"
    assert "Erp.Customer" in env["detail"]["tables"]
    # Only DB tables are named in the groups; the CTE node never is.
    assert sorted(map(sorted, env["detail"]["unjoined_groups"])) == [["C"], ["P"]]


def test_two_db_tables_bridged_only_by_an_sq_node_are_one_group():
    """Minimal hand shape of the same rule: A - [cte] - B is connected."""
    ds = {
        "QueryTable": [
            {"SubQueryID": "s", "TableID": "A", "TableType": "DB",
             "DBSchemaName": "Erp", "DBTableName": "Part"},
            {"SubQueryID": "s", "TableID": "q", "TableType": "SQ", "DBTableName": "sub-2"},
            {"SubQueryID": "s", "TableID": "B", "TableType": "DB",
             "DBSchemaName": "Erp", "DBTableName": "Customer"},
        ],
        "QueryRelation": [
            {"RelationID": "r1", "SubQueryID": "s", "ParentTableID": "q", "ChildTableID": "A"},
            {"RelationID": "r2", "SubQueryID": "s", "ParentTableID": "q", "ChildTableID": "B"},
        ],
        "QueryRelationField": [
            {"RelationID": "r1", "ParentFieldName": "Company", "ChildFieldName": "Company"},
            {"RelationID": "r1", "ParentFieldName": "PartNum", "ChildFieldName": "PartNum"},
            {"RelationID": "r2", "ParentFieldName": "Company", "ChildFieldName": "Company"},
            {"RelationID": "r2", "ParentFieldName": "CustNum", "ChildFieldName": "CustNum"},
        ],
    }
    assert check_cost(ds) is None
    # ...and cutting one bridge edge splits them again.
    ds["QueryRelation"] = ds["QueryRelation"][:1]
    env = check_cost(ds)
    assert env is not None and env["error"] == "query_too_expensive"
    assert sorted(map(sorted, env["detail"]["unjoined_groups"])) == [["A"], ["B"]]


def test_a_company_only_join_to_a_cte_is_still_rule_3():
    """Routing through an SQ node must not launder a cartesian: a Company-only
    relation to the CTE is still refused, whatever the node type."""
    ds = {
        "QueryTable": [
            {"SubQueryID": "s", "TableID": "q", "TableType": "SQ", "DBTableName": "sub-2"},
            {"SubQueryID": "s", "TableID": "A", "TableType": "DB",
             "DBSchemaName": "Erp", "DBTableName": "Part"},
        ],
        "QueryRelation": [
            {"RelationID": "r1", "SubQueryID": "s", "ParentTableID": "q", "ChildTableID": "A"},
        ],
        "QueryRelationField": [
            {"RelationID": "r1", "ParentFieldName": "Company", "ChildFieldName": "Company"},
        ],
    }
    env = check_cost(ds)
    assert env is not None
    assert env["error"] == "query_too_expensive"
    assert "ONLY Company" in env["message"]

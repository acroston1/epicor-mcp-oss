"""Regression coverage: test lint mask and star."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from epicor_mcp.sql.adhoc import EXECUTE_PATH, PARSE_PATH, run_sql
from epicor_mcp.sql.lint import Severity, lint_parsed, mask_sql, star_projection_item
from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

_transpile_mod = importlib.import_module("epicor_mcp.sql.transpile")
BASE = "https://example.invalid/api/v2/odata/DEMO"

#: The lint and transpile maskers must agree so quoted values cannot receive
#: different interpretations in the two stages.
MASKERS = (mask_sql, _transpile_mod._mask)


def call(sql: str, client: MockEpicorClient, **kw) -> dict:
    return asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE, **kw))


def client_for(fixture: str, rows: list[dict] | None = None, **kw) -> MockEpicorClient:
    _, ds = load(fixture)
    return MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute(rows if rows is not None else []), **kw
    )


# --------------------------------------------------------------------------- #
# A2 — a comment token inside a literal must not blind anything
# --------------------------------------------------------------------------- #

_DASHED = (
    "select top 5 [PW].[PartNum] as [PN], [PW].[OnHandQty] as [Q] from Erp.PartWhse as [PW] "
    "where [PW].[WarehouseCode] = 'FG--x' union all select top 5 [P].[PartNum] as [PN], "
    "[P].[UnitPrice] as [Q] from Erp.Part as [P] order by [Q] desc"
)


@pytest.mark.parametrize("masker", MASKERS)
def test_a_dash_inside_a_literal_does_not_blank_the_rest_of_the_statement(masker):
    masked = masker(_DASHED)
    assert "union" in masked.lower()
    assert "order by" in masked.lower()
    assert len(masked) == len(_DASHED)
    assert "FG--x" not in masked  # the literal itself IS still masked


@pytest.mark.parametrize("masker", MASKERS)
@pytest.mark.parametrize(
    "sql, must_survive",
    [
        ("select 1 from t where c = 'a--b' and d = 'top (100)'", "and d ="),
        ("select 1 from t where c = 'a/*b' and d = 2", "and d = 2"),
        ("select 1 from t where c = 'it''s--x' and d = 2", "and d = 2"),
        ("select 1 from t where c = '--' union all select 2 from u", "union all"),
        ("select 1 from t where c = 'x' -- and d = 2", ""),
    ],
)
def test_literal_boundaries(masker, sql, must_survive):
    masked = masker(sql)
    assert len(masked) == len(sql)
    if must_survive:
        assert must_survive in masked


@pytest.mark.parametrize("masker", MASKERS)
def test_a_real_comment_is_still_blanked_and_wins_when_it_opens_first(masker):
    # The dash opens BEFORE the quote here, so the comment must swallow it.
    sql = "select 1 -- 'abc' union all\nfrom t"
    masked = masker(sql)
    assert "union" not in masked.lower()
    assert "abc" not in masked
    assert masked.endswith("\nfrom t")
    block = masker("select /* 'a' union */ 1 from t")
    assert "union" not in block.lower() and len(block) == len("select /* 'a' union */ 1 from t")


@pytest.mark.parametrize("masker", MASKERS)
def test_the_two_maskers_agree_byte_for_byte(masker):
    for sql in (_DASHED, "select 'a--b' /* 'c' */ -- x\nfrom t", "select 1 from t"):
        assert mask_sql(sql) == _transpile_mod._mask(sql)


def test_the_setop_order_by_rule_fires_through_the_dashed_literal():
    """The lint rule itself, on the REAL parsed DS for the union shape."""
    _, ds = load("union_order_by")
    rules = [f.rule for f in lint_parsed(_DASHED, ds)]
    assert "setop_order_by_discarded" in rules


def test_the_dashed_union_is_refused_by_the_pipe_and_never_executes():
    """Regression coverage: test the dashed union is refused by the pipe and never executes."""
    client = client_for("union_order_by", [{"PN": "x", "Q": "0.0"}])
    out = call(_DASHED, client)
    assert out["success"] is False
    assert out["error"] == "sql_silently_wrong"
    assert "setop_order_by_discarded" in [
        f["rule"] for f in out["detail"]["findings"]
    ]
    assert client.paths == [f"{BASE}/{PARSE_PATH}"]
    assert not client.called(EXECUTE_PATH)


def test_a_dashed_literal_does_not_hide_a_dropped_row_bound():
    """`row_bound_dropped` reads masked text too, so A2 blinded it as well.

    ``select top (5) … where [P].[ClassID] = 'FG--x'``: the dash used to blank
    everything after it, but the `top (` sits BEFORE the literal, so the rule
    survived by luck of ordering. Pin the case where it does not — a dashed
    literal earlier in the statement.
    """
    sql, ds = load("top_paren")
    dashed = sql.replace(
        "select top (5)", "select /* 'a--b' */ top (5)", 1
    )
    assert "row_bound_dropped" in [f.rule for f in lint_parsed(dashed, ds)]


# --------------------------------------------------------------------------- #
# A3 — a star in ANY projection slot
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("select * from Erp.Part as [P]", "*"),
        ("select [P].* from Erp.Part as [P]", "[P].*"),
        ("select distinct [P].* from Erp.Part as [P]", "[P].*"),
        ("select top 200 [P].* from Erp.Part as [P]", "[P].*"),
        
        ("select top 3 [P].[PartNum] as [PN], [P].* from Erp.Part as [P]", "[P].*"),
        ("select top 20 [OD].[OrderNum], [OD].* from Erp.OrderDtl as [OD]", "[OD].*"),
        ("select [A].[x] as [a], [B].[y] as [b], [A].* from Erp.A as [A]", "[A].*"),
        ("select [P].[PartNum], * from Erp.Part as [P]", "*"),
    ],
)
def test_a_star_is_found_in_every_projection_slot(sql, expected):
    assert star_projection_item(mask_sql(sql)) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) as [N] from Erp.Part as [P]",
        "select [A].[Qty] * [A].[Price] as [X] from Erp.OrderDtl as [A]",
        "select sum([A].[Qty] * [A].[Price]) as [Rev] from Erp.OrderDtl as [A]",
        "select [A].[Qty]*[A].[Price] as [X], count(*) as [N] from Erp.OrderDtl as [A] "
        "group by [A].[Qty], [A].[Price]",
        "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] where [P].[PartNum] "
        "like 'a*b'",
        "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "order by [P].[PartNum] desc",
    ],
)
def test_multiplication_and_count_star_are_not_select_star(sql):
    assert star_projection_item(mask_sql(sql)) is None


def test_a_second_slot_star_is_refused_by_the_pipe_with_the_real_column_list():
    """A refused wildcard still returns the table's actual columns for recovery."""
    client = client_for("select_star_top", [{"x": 1}])
    out = call(
        "select top 200 [P].[PartNum] as [PN], [P].* from Erp.Part as [P]", client
    )
    assert out["success"] is False
    assert out["error"] == "sql_silently_wrong"
    assert [f["rule"] for f in out["detail"]["findings"]] == ["select_star"]
    assert set(out["valid"]["columns"]) == {"Company", "PartNum", "PartDescription"}
    assert client.paths == [f"{BASE}/{PARSE_PATH}"]
    assert not client.called(EXECUTE_PATH)


def test_the_first_slot_star_still_behaves_exactly_as_before():
    client = client_for("select_star", [{"x": 1}])
    out = call("select [P].* from Erp.Part as [P]", client)
    assert out["error"] == "sql_silently_wrong"
    assert set(out["valid"]["columns"]) == {"Company", "PartNum", "PartDescription"}


def test_no_false_positive_on_the_known_good_fixtures():
    """Every clean fixture, through the whole lint. A cost gate that refuses
    good queries gets turned off, which is how it becomes no gate at all."""
    for name in ("clean_top", "clean_rollup", "clean_empbasic", "clean_in_subquery",
                 "wedge_rollup", "gov_bounded_join", "distinct_derived_count"):
        sql, ds = load(name)
        assert "select_star" not in [f.rule for f in lint_parsed(sql, ds)], name


# --------------------------------------------------------------------------- #
# The lint fails OPEN by design — except for `select *`
# --------------------------------------------------------------------------- #


def test_select_star_is_still_refused_when_the_rest_of_the_lint_crashes():
    class Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    findings = lint_parsed("select [P].[PartNum], [P].* from Erp.Part as [P]", Exploding())
    by_rule = {f.rule: f for f in findings}
    assert by_rule["lint_unavailable"].severity == Severity.WARN
    assert by_rule["select_star"].severity == Severity.REFUSE
    assert by_rule["select_star"].detail["degraded"] is True


def test_a_clean_statement_still_fails_open_when_the_lint_crashes():
    class Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    findings = lint_parsed("select top 5 [P].[PartNum] from Erp.Part as [P]", Exploding())
    assert [f.rule for f in findings] == ["lint_unavailable"]
    assert all(f.severity == Severity.WARN for f in findings)

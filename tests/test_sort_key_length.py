"""Epicor cannot store a sort key longer than 125 characters.

Measured: each ORDER BY term is stored in ``QuerySortBy.FieldName`` as Epicor
re-renders it. Stepped over one expression, 122..125 run and 126..134 fail at
Execute with *"An object or column name is missing or empty"* — with or without
CASE (a CASE expression is usually just what makes the key long). The fixtures
``sort_key_125`` / ``sort_key_126`` / ``sort_key_long`` are the real parses on
either side of that line.

The pipe re-writes the outer ORDER BY ONCE into a CTE that sorts on a named
column (known answer: identical top 5 to an independently computed ranking) and
re-runs every gate on the result; anything it cannot wrap is refused BEFORE
Execute with the CTE shape as the fix.
"""

from __future__ import annotations

import asyncio
import copy

import sqlglot

from epicor_mcp.sql.adhoc import SORT_KEY_MAX_CHARS, run_sql
from epicor_mcp.sql.transpile import wrap_sort_in_cte
from tests.wedge_fixtures import MockEpicorClient, as_designer, load, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"


class SequencedParseClient(MockEpicorClient):
    """Answers the Nth ParseFromSQL with the Nth DS (the last one repeats)."""

    def __init__(self, parse_sequence, **kw):
        super().__init__(parse_ds=parse_sequence[0], **kw)
        self.parse_sequence = list(parse_sequence)
        self.parsed_sql: list[str] = []

    async def post(self, url, api_key, json_body=None):
        if "ParseFromSQL" in url:
            self.calls.append((url, json_body or {}))
            self.parsed_sql.append(json_body["ds"]["DynamicQueryDesigner"][0]["DisplayPhrase"])
            n = min(len(self.parsed_sql), len(self.parse_sequence)) - 1
            return {"parameters": {"ds": as_designer(self.parse_sequence[n])}}
        return await super().post(url, api_key, json_body)


def call(sql, client, **kw):
    return asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE, **kw))


def _ds(name):
    return load(name)[1]


# --------------------------------------------------------------------------- #
# The measured boundary
# --------------------------------------------------------------------------- #


def test_the_fixtures_sit_on_either_side_of_the_measured_limit():
    assert SORT_KEY_MAX_CHARS == 125
    assert len(_ds("sort_key_125")["QuerySortBy"][0]["FieldName"]) == 125
    assert len(_ds("sort_key_126")["QuerySortBy"][0]["FieldName"]) == 126


def test_a_125_character_sort_key_runs_untouched():
    sql, ds = load("sort_key_125")
    client = SequencedParseClient([ds], execute_response=ok_execute([{"PartNum": "A"}]))
    out = call(sql, client)
    assert out["success"] is True
    assert client.count("ParseFromSQL") == 1
    assert "sort_key_wrapped" not in [r["rule"] for r in out["assumptions"].get("rewrites", [])]
    assert out["sql_executed"] == sql


def test_a_126_character_sort_key_is_wrapped_reparsed_and_run():
    sql, ds = load("sort_key_126")
    _, wrapped_ds = load("sort_key_long_wrapped")
    client = SequencedParseClient(
        [ds, wrapped_ds], execute_response=ok_execute([{"PartNum": "A"}])
    )
    out = call(sql, client)
    assert out["success"] is True
    # parse (long key) -> parse (wrapped) -> ONE Execute, of the wrapped DS.
    assert [p.rsplit("/", 1)[-1] for p in client.paths] == [
        "ParseFromSQL", "ParseFromSQL", "Execute"
    ]
    assert client.parsed_sql[1].startswith("WITH [SortWrap] AS (")
    assert out["sql_executed"] == client.parsed_sql[1]
    executed = client.body_for("Execute")["queryDS"]["QuerySortBy"]
    assert all(len(r["FieldName"]) <= SORT_KEY_MAX_CHARS for r in executed)
    rules = [r["rule"] for r in out["assumptions"]["rewrites"]]
    assert rules == ["sort_key_wrapped"]
    # The caller's own bound, not a re-derived one.
    assert out["assumptions"]["row_bound"] == {"kind": "top", "value": 5, "source": "caller"}


def test_the_first_pass_rewrites_survive_the_re_entry():
    """`order by [V]` is first expanded by `order_by_alias` into the long
    expression, THEN wrapped — both must be announced, in that order, and the
    wrap sorts on the existing [V] column rather than recomputing it."""
    long_sql, ds = load("sort_key_long")
    expr = long_sql.split("order by ")[1].rsplit(" desc", 1)[0]
    sql = (
        f"select top 5 [PC].[PartNum] as [PartNum], {expr} as [V] "
        "from Erp.PartCost as [PC] order by [V] desc"
    )
    _, wrapped_ds = load("sort_key_long_wrapped")
    client = SequencedParseClient([ds, wrapped_ds], execute_response=ok_execute([]))
    out = call(sql, client)
    assert out["success"] is True
    assert [r["rule"] for r in out["assumptions"]["rewrites"]] == [
        "order_by_alias", "sort_key_wrapped"
    ]
    assert client.parsed_sql[1].endswith("ORDER BY [SortWrap].[V] DESC")
    assert "SortKey" not in client.parsed_sql[1]


def test_a_key_still_too_long_after_the_wrap_is_refused_and_never_executed():
    sql, ds = load("sort_key_long")
    client = SequencedParseClient([ds, ds], execute_response=ok_execute([{"x": 1}]))
    out = call(sql, client)
    assert out["success"] is False
    assert out["error"] == "sql_sort_key_too_long"
    assert out["detail"]["stage"] == "sort_key"
    assert client.count("ParseFromSQL") == 2  # wrapped exactly once, never twice
    assert not client.called("Execute")
    assert "sort_key_wrapped" in out["detail"]


def test_an_unwrappable_statement_is_refused_before_execute_with_the_cte_shape():
    """`select distinct` cannot take a hidden sort column without the risk of
    changing what DISTINCT collapses, so the wrap declines and 3c refuses."""
    long_sql, ds = load("sort_key_long")
    expr = long_sql.split("order by ")[1].rsplit(" desc", 1)[0]
    sql = (
        f"select distinct [PC].[PartNum] as [PartNum], {expr} as [V] "
        f"from Erp.PartCost as [PC] order by {expr} desc"
    )
    client = SequencedParseClient([ds], execute_response=ok_execute([{"x": 1}]))
    out = call(sql, client)
    assert out["error"] == "sql_sort_key_too_long"
    assert out["detail"]["stage"] == "sort_key"
    assert "select distinct" in out["message"]
    assert client.count("ParseFromSQL") == 1
    assert not client.called("Execute")
    assert "CTE" in out["message"] and "CASE" in out["message"]
    assert "with [q] as" in out["valid"]["shape"]
    assert out["detail"]["long_sort_keys"][0]["length"] == 166


def test_the_deny_list_still_wins_over_the_wrap():
    """Step 3c sits AFTER the deny-list: a denied table is refused on the
    first parse and is never re-written or re-parsed."""
    sql, ds = load("sort_key_long")
    ds = copy.deepcopy(ds)
    for t in ds["QueryTable"]:
        t["DBTableName"] = "PREmpMas"
    for f in ds["QueryField"]:
        f["DBTableName"] = "PREmpMas"
    client = SequencedParseClient([ds], execute_response=ok_execute([{"x": 1}]))
    out = call(sql, client)
    assert out["success"] is False
    assert out["detail"]["stage"] == "denylist"
    assert client.count("ParseFromSQL") == 1
    assert not client.called("Execute")


# --------------------------------------------------------------------------- #
# wrap_sort_in_cte — the rewrite itself
# --------------------------------------------------------------------------- #

LONG = (
    "[PC].[StdMaterialCost]+[PC].[StdLaborCost]+[PC].[StdBurdenCost]+[PC].[StdSubContCost]"
    "+[PC].[StdMtlBurCost]+[PC].[AvgMaterialCost]+[PC].[AvgLaborCost]+[PC].[AvgBurdenCost]"
)


def _outer(sql):
    return sqlglot.parse_one(sql, read="tsql")


def test_the_wrap_moves_top_outward_and_keeps_projection_names_and_directions():
    w = wrap_sort_in_cte(
        "select top 7 [PC].[PartNum] as [PartNum], [PC].[CostID] as [CostID] "
        f"from Erp.PartCost as [PC] where [PC].[CostID] = '10' order by {LONG} desc, "
        "[PC].[PartNum] asc"
    )
    assert w.sql is not None, w.why_not
    root = _outer(w.sql)
    assert root.args["limit"].expression.name == "7"
    assert [e.alias for e in root.expressions] == ["PartNum", "CostID"]
    keys = root.args["order"].expressions
    # The long key gets a hidden column; `[PC].[PartNum]` IS a projected item,
    # so it sorts on that output column instead of computing a second one.
    assert [k.this.sql("tsql") for k in keys] == ["[SortWrap].[SortKey1]", "[SortWrap].[PartNum]"]
    assert [bool(k.args.get("desc")) for k in keys] == [True, False]
    cte = root.args["with_"].expressions[0].this
    assert cte.args.get("limit") is None and cte.args.get("order") is None
    assert "WHERE [PC].[CostID] = '10'" in cte.sql("tsql")
    # Never a NULLS-ordering CASE (the `Ordered(desc=False)` generator trap).
    assert "IS NULL THEN 1 ELSE 0" not in w.sql.upper()
    assert w.transformation.rule == "sort_key_wrapped"


def test_top_percent_and_with_ties_survive_the_move():
    for top in ("top 5 percent", "top 5 with ties"):
        w = wrap_sort_in_cte(
            f"select {top} [PC].[PartNum] as [PartNum] from Erp.PartCost as [PC] "
            f"order by {LONG} desc"
        )
        assert top.upper() in w.sql.upper().split(" FROM [SORTWRAP]")[0].rsplit(") SELECT ", 1)[1]


def test_a_grouped_statement_keeps_its_group_by_and_having_inside_the_cte():
    w = wrap_sort_in_cte(
        "select top 5 [PC].[PartNum] as [PartNum], sum([PC].[StdLaborCost]) as [L] "
        "from Erp.PartCost as [PC] group by [PC].[PartNum] having count(*) > 1 "
        f"order by sum({LONG}) desc"
    )
    cte = _outer(w.sql).args["with_"].expressions[0].this
    assert cte.args.get("group") is not None and cte.args.get("having") is not None
    assert cte.expressions[-1].alias == "SortKey1"


def test_an_existing_cte_list_is_kept_and_the_names_never_collide():
    w = wrap_sort_in_cte(
        "with [SortWrap] as (select [P].[PartNum] as [PartNum] from Erp.Part as [P]) "
        "select top 5 [SortWrap].[PartNum] as [SortKey1] from [SortWrap] "
        f"inner join Erp.PartCost as [PC] on [SortWrap].[PartNum] = [PC].[PartNum] order by {LONG} desc"
    )
    root = _outer(w.sql)
    names = [c.alias for c in root.args["with_"].expressions]
    assert names == ["SortWrap", "SortWrap1"]
    assert root.args["order"].expressions[0].this.sql("tsql") == "[SortWrap1].[SortKey2]"


def test_the_wrap_declines_every_shape_it_would_have_to_guess_about():
    cases = {
        "set operation": "select [A].[x] as [x] from Erp.A as [A] union all "
        "select [B].[x] as [x] from Erp.B as [B]",
        "select distinct": f"select distinct [PC].[PartNum] as [P] from Erp.PartCost as [PC] order by {LONG}",
        "OFFSET/FETCH": f"select [PC].[PartNum] as [P] from Erp.PartCost as [PC] order by {LONG} "
        "offset 0 rows fetch next 5 rows only",
        "`*`": f"select top 5 [PC].* from Erp.PartCost as [PC] order by {LONG}",
        "no name": f"select top 5 [PC].[StdLaborCost] * 2 from Erp.PartCost as [PC] order by {LONG}",
        "share a name": "select top 5 [PC].[PartNum] as [P], [PC].[CostID] as [p] "
        f"from Erp.PartCost as [PC] order by {LONG}",
        "not in the outer ORDER BY": "select top 5 [PC].[PartNum] as [P] from Erp.PartCost as [PC]",
    }
    for fragment, sql in cases.items():
        w = wrap_sort_in_cte(sql)
        assert w.sql is None, fragment
        assert fragment.strip("`") in w.why_not, (fragment, w.why_not)

"""Regression coverage: test wedge paging."""

from __future__ import annotations

import asyncio

import pytest

from epicor_mcp.sql.adhoc import EXECUTE_PATH, PARSE_PATH, run_sql
from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"

#: The rollup statement, with NO row bound of its own — the exact shape
#: the transpiler has to bound and therefore the exact shape that used to lie.
UNBOUNDED_ROLLUP = (
    "select [OrderDtl].[PartNum] as [OrderDtl_PartNum], "
    "sum([OrderDtl].[ExtPriceDtl]) as [revenue] from Erp.OrderDtl as [OrderDtl] "
    "group by [OrderDtl].[PartNum] order by sum([OrderDtl].[ExtPriceDtl]) desc"
)


def call(sql: str, client: MockEpicorClient, **kw) -> dict:
    return asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE, **kw))


def client_for(fixture: str, rows: list[dict] | None = None, **kw) -> MockEpicorClient:
    _, ds = load(fixture)
    return MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute(rows if rows is not None else []), **kw
    )


def _rows(n: int) -> list[dict]:
    return [{"OrderDtl_PartNum": f"P{i}", "revenue": str(i)} for i in range(n)]


# --------------------------------------------------------------------------- #
# The defect, both directions
# --------------------------------------------------------------------------- #


def test_page_one_of_the_unbounded_rollup_is_a_full_page_and_says_INCOMPLETE():
    client = client_for("wedge_rollup_top", _rows(50))
    out = call(UNBOUNDED_ROLLUP, client, page_size=50, page_num=1)
    assert out["success"] is True
    assert out["row_count"] == 50
    assert out["complete"] is False
    assert out["terminal"] is False
    assert out["summary"].startswith("INCOMPLETE:")


@pytest.mark.parametrize("page", [2, 3, 4, 17])
def test_page_two_and_beyond_is_refused_not_returned_empty(page):
    """The headline fix. It used to return 0 rows and `complete: true`."""
    client = client_for("wedge_rollup_top", [])
    out = call(UNBOUNDED_ROLLUP, client, page_size=50, page_num=page)
    assert out["success"] is False
    assert out["error"] == "page_unreachable"
    assert out.get("complete") is not True
    assert out.get("terminal") is not True
    assert "exactly one page" in out["message"]
    # ...and it costs NOTHING: refused before ParseFromSQL.
    assert client.paths == []


def test_the_refusal_hands_back_the_recovery_that_actually_works():
    """Regression coverage: test the refusal hands back the recovery that actually works."""
    client = client_for("wedge_rollup_top", [])
    out = call(UNBOUNDED_ROLLUP, client, page_size=1000, page_num=2)
    assert "keyset" in out["message"].lower()
    assert out["retry_with"]["page"] == 1
    assert ">" in out["valid"]["keyset_example"]
    assert out["detail"]["row_bound"]["source"] == "injected"
    assert out["detail"]["row_bound"]["value"] == 1000
    assert out["detail"]["stage"] == "paging"


def test_a_caller_bound_larger_than_the_page_still_pages():
    """Regression coverage: test a caller bound larger than the page still pages."""
    sql = "select top 500 [P].[PartNum] as [PN] from Erp.Part as [P]"
    for page in (1, 2, 5):
        client = client_for("clean_top", _rows(100))
        out = call(sql, client, page_size=100, page_num=page)
        assert out["success"] is True, page
        settings = client.body_for("Execute")["executionParams"]["ExecutionSetting"]
        assert settings[1] == {"Name": "PageNum", "Value": str(page)}


def test_a_page_past_the_callers_own_bound_is_refused_and_names_the_last_page():
    sql = "select top 500 [P].[PartNum] as [PN] from Erp.Part as [P]"
    client = client_for("clean_top", [])
    out = call(sql, client, page_size=100, page_num=6)
    assert out["error"] == "page_beyond_row_bound"
    assert out["detail"]["last_page"] == 5
    assert out["retry_with"]["page"] == 5
    assert client.paths == []


def test_a_grand_total_says_there_is_no_page_two_rather_than_blaming_your_top():
    """The bound here is `top 1` the SERVER derived (one row, by definition).
    Telling the caller "your top 1" would be a lie about SQL they never wrote."""
    sql = "select count(*) as [N] from Erp.OrderDtl as [OrderDtl]"
    client = client_for("wedge_rollup_top", [])
    out = call(sql, client, page_size=200, page_num=2)
    assert out["error"] == "page_beyond_row_bound"
    assert "grand-total aggregate" in out["message"]
    assert "your `top" not in out["message"].lower()
    assert client.paths == []


def test_page_one_is_never_refused_by_the_reachability_rule():
    for sql in (UNBOUNDED_ROLLUP, "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]"):
        client = client_for("clean_top", _rows(3))
        out = call(sql, client, page_size=200, page_num=1)
        assert out["success"] is True


def test_a_bound_of_exactly_page_size_times_page_minus_one_is_still_refused():
    """Boundary: `top 200` at page_size 100 has pages 1-2, so page 3 is empty."""
    sql = "select top 200 [P].[PartNum] as [PN] from Erp.Part as [P]"
    client = client_for("clean_top", [])
    assert call(sql, client, page_size=100, page_num=3)["error"] == "page_beyond_row_bound"
    ok = client_for("clean_top", _rows(100))
    assert call(sql, ok, page_size=100, page_num=2)["success"] is True


# --------------------------------------------------------------------------- #
# The belt-and-braces: an empty later page is NEVER "complete"
# --------------------------------------------------------------------------- #


def test_an_empty_later_page_that_slips_through_is_not_reported_complete():
    """A `select distinct` is bounded by PageSize ALONE (injecting a `top` on a
    DISTINCT is measured silent-wrong), so the reachability rule lets it run and
    Epicor may legitimately return an empty page. It must not read as the end.
    """
    sql = "select distinct [P].[ClassID] as [C] from Erp.Part as [P]"
    client = client_for("clean_top", [])
    out = call(sql, client, page_size=200, page_num=3)
    assert out["success"] is True
    assert out["row_count"] == 0
    assert out["complete"] is False
    assert out["terminal"] is False
    assert out["summary"].startswith("EMPTY PAGE:")
    assert "NOT evidence" in out["summary"]


def test_an_empty_page_one_is_still_an_honest_complete_answer():
    """Zero rows on page 1 IS the answer — a filter that matched nothing."""
    sql = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]"
    out = call(sql, client_for("clean_top", []), page_size=200, page_num=1)
    assert out["success"] is True and out["row_count"] == 0
    assert out["complete"] is True and out["terminal"] is True


def test_a_partial_later_page_is_complete_the_way_it_always_was():
    sql = "select top 500 [P].[PartNum] as [PN] from Erp.Part as [P]"
    out = call(sql, client_for("clean_top", _rows(37)), page_size=100, page_num=3)
    assert out["success"] is True and out["row_count"] == 37
    assert out["complete"] is True


def test_the_tool_description_no_longer_promises_plain_page_numbers():
    from mcp.server.fastmcp import FastMCP

    from epicor_mcp.sql.tool import register_query_tool

    class _S:
        dev_mode = False
        environment = "live"

    mcp = FastMCP(name="t")

    async def _runner(**kw):
        return {}

    assert register_query_tool(mcp, _S(), _runner).allowed
    schema = asyncio.run(mcp.list_tools())[0].inputSchema
    text = schema["properties"]["page"]["description"]
    assert "keyset" in text.lower()
    assert "refused" in text.lower()

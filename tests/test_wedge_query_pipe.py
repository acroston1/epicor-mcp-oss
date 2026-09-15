"""The pipe end to end, through the REGISTERED tool, with a mock client.

Two properties are asserted over and over here because they are the ones a
refactor silently breaks:

* **Execute is NEVER called** when a gate refuses. A control that returns a
  refusal *after* the data has been read is not a control.
* **HTTP 200 is never success on its own.** Every run-time defect in Epicor
  surfaces as 200 + a populated ``returnObj.Errors`` whose text is always the
  useless ``Bad SQL statement.``
"""

from __future__ import annotations

import asyncio

import pytest

from epicor_mcp.sql.adhoc import EXECUTE_PATH, PARSE_PATH, run_sql, rows_to_tsv
from epicor_mcp.sql.governor import CostGovernor, GovernorPolicy
from epicor_mcp.sql.lint import top_level_subquery
from tests.wedge_fixtures import FakeEpicorError, MockEpicorClient, load, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"


def call(sql: str, client: MockEpicorClient, **kw) -> dict:
    return asyncio.run(
        run_sql(sql, client=client, api_key="k", base_url=BASE, **kw)
    )


def client_for(fixture: str, rows: list[dict] | None = None, **kw) -> MockEpicorClient:
    _, ds = load(fixture)
    return MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute(rows if rows is not None else []), **kw
    )


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_a_clean_query_runs_and_returns_tsv():
    sql, _ = load("clean_top")
    client = client_for("clean_top", [{"PN": "ABC-1"}, {"PN": "ABC-2"}])
    out = call(sql, client)
    assert out["success"] is True
    assert out["columns"] == ["PN"]
    assert out["rows"] == "PN\nABC-1\nABC-2"
    assert out["row_count"] == 2
    assert out["complete"] is True
    assert out["terminal"] is True
    assert "tab-separated" in out["format"]
    assert out["tables_read"] == ["Erp.Part"]
    assert out["sql_ms"] == 12.5
    assert client.paths == [f"{BASE}/{PARSE_PATH}", f"{BASE}/{EXECUTE_PATH}"]


def test_page_size_and_page_num_are_sent_on_every_call_with_the_exact_names():
    """PageNumber / Page / CurrentPage / SkipRows are SILENTLY
    IGNORED and re-serve page 1 forever; PageSize=0 is UNBOUNDED; values are
    strings."""
    # The caller's own `top` has to exceed page_size x (page-1) or the page
    # cannot contain rows and is refused before Execute (see
    # `_page_reachability_refusal`). `clean_top`'s fixture SQL is `top 5`, so
    # page 3 at page_size 250 asks for rows 501-750 of a 5-row bound. The SQL is
    # widened so the assertion under test — the ExecutionSetting NAMES and their
    # values — still executes; the mock returns the same parsed DS either way.
    sql = "select top 1000 [P].[PartNum] as [PN] from Erp.Part as [P]"
    client = client_for("clean_top", [{"PN": "x"}])
    call(sql, client, page_size=250, page_num=3)
    settings = client.body_for("Execute")["executionParams"]["ExecutionSetting"]
    assert settings == [
        {"Name": "PageSize", "Value": "250"},
        {"Name": "PageNum", "Value": "3"},
    ]


def test_page_size_is_clamped_and_never_zero():
    """`PageSize=0` is UNBOUNDED in Epicor, so a non-positive ask must never
    reach the wire as-is."""
    sql, _ = load("clean_top")
    for asked, expected in ((999999, "1000"), (0, "200"), (-5, "200"), ("nonsense", "200")):
        client = client_for("clean_top", [{"PN": "x"}])
        call(sql, client, page_size=asked)
        settings = client.body_for("Execute")["executionParams"]["ExecutionSetting"]
        assert settings[0]["Value"] == expected, asked


def test_the_injected_row_bound_tracks_the_page_the_caller_asked_for():
    """A fixed `top 100` under page_size=1000 would trim the answer to a tenth
    of the requested page and then report it complete."""
    _, ds = load("wedge_rollup_top")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([{"a": 1}]))
    out = call(
        "select [OD].[PartNum] as [PN], sum([OD].[ExtPriceDtl]) as [R] "
        "from Erp.OrderDtl as [OD] group by [OD].[PartNum]",
        client,
        page_size=1000,
    )
    assert out["assumptions"]["row_bound"]["value"] == 1000
    assert "TOP 1000" in out["sql_executed"].upper()


def test_the_parse_body_carries_queryid_and_rowmod():
    """without RowMod the parse 400s; without QueryID it parses
    200 and then Execute 500s."""
    sql, _ = load("clean_top")
    client = client_for("clean_top", [{"PN": "x"}])
    call(sql, client)
    row = client.body_for("ParseFromSQL")["ds"]["DynamicQueryDesigner"][0]
    assert row["RowMod"] == "A"
    assert row["QueryID"]
    assert row["DisplayPhrase"].startswith("select")


def test_the_designer_suffix_is_stripped_before_execute():
    sql, _ = load("clean_top")
    client = client_for("clean_top", [{"PN": "x"}])
    call(sql, client)
    queryds = client.body_for("Execute")["queryDS"]
    assert "QueryField" in queryds
    assert not any(k.endswith("Designer") for k in queryds if k != "DynamicQueryDesigner")


# --------------------------------------------------------------------------- #
# Paging honesty
# --------------------------------------------------------------------------- #


def test_a_full_page_is_never_complete():
    sql, _ = load("clean_top")
    rows = [{"PN": f"p{i}"} for i in range(10)]
    client = client_for("clean_top", rows)
    out = call(sql, client, page_size=10)
    assert out["complete"] is False
    assert out["terminal"] is False
    assert out["summary"].startswith("INCOMPLETE:")
    assert "next_cursor" not in out  # E6 is Phase 1; we never fake a token


def test_a_partial_page_says_it_is_the_complete_result():
    sql, _ = load("clean_top")
    client = client_for("clean_top", [{"PN": "a"}, {"PN": "b"}])
    out = call(sql, client, page_size=10)
    assert out["complete"] is True
    assert "complete result" in out["summary"]


def test_a_byte_truncated_page_is_never_complete():
    sql, _ = load("clean_top")
    rows = [{"PN": "x" * 500} for _ in range(50)]
    client = client_for("clean_top", rows)
    out = call(sql, client, page_size=1000, max_bytes=2000)
    assert out["rows_dropped_for_size"] > 0
    assert out["complete"] is False
    assert out["summary"].startswith("INCOMPLETE:")


def test_tsv_escapes_tabs_and_newlines_and_renders_null_as_empty():
    tsv = rows_to_tsv(
        [{"A": "x\ty", "B": None, "C": "line1\nline2"}], ["A", "B", "C"]
    )
    assert tsv == "A\tB\tC\nx y\t\tline1 line2"


# --------------------------------------------------------------------------- #
# Gate order: deny-list, then lint, then governor — each BEFORE Execute
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fixture,error",
    [
        ("deny_projected", "column_access_denied"),
        ("deny_filtered", "column_access_denied"),
        ("deny_sorted", "column_access_denied"),
        ("deny_cte", "column_access_denied"),
        ("deny_derived", "column_access_denied"),
        ("deny_subquery", "column_access_denied"),
        ("deny_union_arm2", "column_access_denied"),
        ("deny_table", "table_access_denied"),
        ("deny_ice_userfile", "table_access_denied"),
    ],
)
def test_a_denied_object_never_reaches_execute(fixture, error):
    sql, _ = load(fixture)
    client = client_for(fixture, [{"leaked": "this must never be returned"}])
    out = call(sql, client)
    assert out["success"] is False
    assert out["error"] == error
    assert client.called("ParseFromSQL")
    assert not client.called("Execute"), "the deny-list ran AFTER Execute"


def test_the_denylist_runs_before_the_lint_so_select_star_cannot_leak_a_schema():
    """A denied-table query must never expose columns from its parsed dataset."""
    sql, ds = load("deny_star_payroll")
    assert len(ds["QueryField"]) > 50
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([]))
    out = call(sql, client)
    assert out["error"] == "table_access_denied"
    # The DENIED objects are named on purpose (that is the INV-1 recovery). What
    # must not appear is the table's SCHEMA — the 73-column list the lint would
    # have served had it run first.
    assert "columns" not in (out.get("valid") or {})
    body = str(out)
    for column in ("EmpLink", "PayFrequency", "DeferredComp", "EmgContact", "Pension"):
        assert column not in body, f"leaked {column} from a denied table"
    assert not client.called("Execute")


#: Which gate stops each shape, and therefore whether Epicor's parser is reached
#: at all. `unknown_column` MOVED from the DS lint to E14 when the column
#: validator was wired into the pipe: the refusal is identical in
#: kind and now costs **zero** Epicor calls instead of one, so the guarantee is
#: strictly stronger, not weaker. The case stays parametrised here rather than
#: being dropped, and `test_the_ds_lint_still_catches_a_phantom_column_when_e14_
#: is_off` pins the post-parse half so shadowing it cannot make it silently dead.
_REFUSED_AT = {
    "top_paren": "lint",
    "sort_ordinal": "lint",
    "sort_invented_alias": "lint",
    "union_order_by": "lint",
    "select_star": "lint",
    "unknown_column": "validate_columns",
}


@pytest.mark.parametrize("fixture", sorted(_REFUSED_AT))
def test_a_pre_execute_refusal_never_reaches_execute(fixture):
    """The local transpiler cannot see any of these. Each is stopped before
    Execute — by E14 locally, or by the DS lint one round trip later."""
    stage = _REFUSED_AT[fixture]
    sql, _ = load(fixture)
    client = client_for(fixture, [{"x": 1}])
    out = call(sql, client)
    assert out["success"] is False
    assert out["error"] in ("sql_silently_wrong", "sql_unknown_column")
    assert out["detail"]["checked_before_running"] is True
    assert out["detail"]["stage"] == stage
    # The DS lint needs Epicor's own resolution and so must pay for the parse;
    # E14 is local and must NOT.
    assert client.called("ParseFromSQL") is (stage == "lint")
    assert not client.called("Execute")


@pytest.mark.parametrize(
    "fixture,error",
    [
        ("top_percent", "sql_top_percent"),
        ("top_zero", "sql_top_zero"),
        ("count_distinct", "sql_distinct_in_aggregate"),
        ("gov_cross_join", "sql_cross_join"),
        ("gov_unbounded_scan", "sql_comma_join_no_predicate"),
    ],
)
def test_shapes_the_transpiler_catches_never_touch_epicor_at_all(fixture, error):
    """Defence in depth: the transpiler refuses these locally, so they cost zero
    round trips. The DS lint and the governor still cover them independently —
    see test_sql_lint_parsed.py and test_sql_governor.py, which drive the same
    fixtures directly."""
    sql, _ = load(fixture)
    client = client_for(fixture, [{"x": 1}])
    out = call(sql, client)
    assert out["success"] is False
    assert out["error"] == error
    assert out["detail"]["stage"] == "transpile"
    assert client.calls == []


def test_select_star_refusal_serves_the_real_column_list():
    """And it survives the row-bound injection: `select top 200 [P].*` parses to
    SelectListClause='Top', so a bare `All` check would have MISSED it."""
    sql, ds = load("select_star_top")
    assert ds["QuerySubQuery"][0]["SelectListClause"] == "Top"
    client = client_for("select_star_top")
    out = call(sql, client)
    assert "PartNum" in out["valid"]["columns"]
    assert set(out["valid"]["columns"]) == {"Company", "PartNum", "PartDescription"}


def test_a_phantom_column_is_named_with_the_table_that_does_not_own_it():
    """E14 owns this now, and it names the column BEFORE any Epicor call.

    The recovery is richer than the lint's flat `phantom_columns` list: the
    clause that broke, the clauses that are fine, and the real column names.
    """
    sql, _ = load("unknown_column")
    client = client_for("unknown_column")
    out = call(sql, client)
    assert out["error"] == "sql_unknown_column"
    assert "OrderDtl.DocExtPrice" in str(out["valid"])
    assert "DocExtPrice" in out["message"]
    assert client.calls == []


def test_the_ds_lint_still_catches_a_phantom_column_when_e14_is_off():
    """Column validation complements the dataset lint without retiring it.

    The lint uses QueryField.DataType and can inspect tables absent from
    the local metadata catalogue. Disabling local validation proves that
    the independent lint path still rejects phantom columns."""
    sql, _ = load("unknown_column")
    client = client_for("unknown_column")
    out = call(sql, client, validate_columns=False)
    assert out["error"] == "sql_unknown_column"
    assert out["detail"]["stage"] == "lint"
    assert "OrderDtl.DocExtPrice" in out["valid"]["phantom_columns"]
    assert client.called("ParseFromSQL")
    assert not client.called("Execute")


def test_the_governor_refusal_that_only_the_ds_can_see_never_reaches_execute():
    """A join on Company alone is legal T-SQL and lints clean — only Epicor's
    resolved QueryRelationField rows reveal it as a cartesian."""
    sql, _ = load("gov_company_only_join")
    client = client_for("gov_company_only_join", [{"x": 1}])
    out = call(sql, client)
    assert out["error"] == "query_too_expensive"
    assert out["detail"]["stage"] == "governor"
    assert client.called("ParseFromSQL")
    assert not client.called("Execute")


def test_a_master_on_master_cartesian_never_reaches_execute_either():
    """An unbounded self-join is unsafe independently of table size or class."""
    ds = {
        "QuerySubQuery": [
            {"SubQueryID": "s", "Type": "TopLevel", "SelectListClause": "Top",
             "TopRowExpr": 100.0, "TopInPercent": False}
        ],
        "QueryTable": [
            {"SubQueryID": "s", "TableID": "A", "DBSchemaName": "Erp",
             "DBTableName": "Part", "TableType": "DB"},
            {"SubQueryID": "s", "TableID": "B", "DBSchemaName": "Erp",
             "DBTableName": "Part", "TableType": "DB"},
        ],
        "QueryRelation": [
            {"RelationID": "r1", "SubQueryID": "s", "ParentTableID": "A", "ChildTableID": "B"}
        ],
        "QueryRelationField": [
            {"RelationID": "r1", "ParentFieldName": "Company", "ChildFieldName": "Company"}
        ],
    }
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([{"x": 1}]))
    out = call(
        "select top 100 [A].[PartNum] as [P] from Erp.Part as [A] inner join Erp.Part as "
        "[B] on [A].[Company] = [B].[Company]",
        client,
    )
    assert out["error"] == "query_too_expensive"
    assert out["detail"]["stage"] == "governor"
    assert client.called("ParseFromSQL")
    assert not client.called("Execute")


def test_a_transpiler_refusal_never_reaches_epicor_at_all():
    client = MockEpicorClient()
    out = call("update Erp.Part set [PartNum] = 'x'", client)
    assert out["success"] is False
    assert client.calls == []


# --------------------------------------------------------------------------- #
# The wrapped-set-operation refusal LOOP
# --------------------------------------------------------------------------- #

_WRAPPED_SETOP_TOPN = (
    "select top 10 [w].[PartNum] as [PartNum], sum([w].[Amt]) as [Revenue] "
    "from (select [A].[PartNum] as [PartNum], [A].[ExtPriceDtl] as [Amt] "
    "from Erp.OrderDtl as [A] where [A].[OrderNum] < 100000 union all "
    "select [B].[PartNum] as [PartNum], [B].[ExtPriceDtl] as [Amt] "
    "from Erp.OrderDtl as [B] where [B].[OrderNum] >= 100000) as [w] "
    "group by [w].[PartNum] order by sum([w].[Amt]) desc"
)


def test_a_wrapped_set_operation_is_refused_locally_and_hands_back_a_runnable_CTE():
    """Before this fix the pipe answered this statement with
    ``row_bound_dropped``, whose message prescribed
    ``select top 100 [w].* from (<your union>) as [w]`` — and running THAT
    returned the same refusal again ("2 problems found"), because the
    transpiler had stripped the wrapper's `top` back onto the branches. A
    closed loop in the recovery path.

    The refusal is now local (zero round trips) and carries a CTE that runs.
    """
    client = MockEpicorClient()
    out = call(_WRAPPED_SETOP_TOPN, client)
    assert out["success"] is False
    assert out["error"] == "sql_setop_wrapper_unsafe"
    assert out["detail"]["stage"] == "transpile"
    assert client.calls == []          # refused before Epicor is asked anything

    retry = out["retry_with"]["sql"]
    assert retry.upper().startswith("WITH [W] AS (")
    # The caller's OWN bound and grouping survive into the recovery — the whole
    # point. `setop_outer_bound_moved` deleted the `top 10` outright.
    assert "TOP 10" in retry.upper()
    assert "GROUP BY [w].[PartNum]" in retry
    assert "ORDER BY SUM([W].[AMT]) DESC" in retry.upper()


def test_the_recovery_the_server_hands_back_is_not_refused_by_the_next_gate():
    """A recovery must pass the next gate as well as preserve caller intent.

    The CTE fixture includes a sort row and an outer Top expression. Check
    those dataset properties and execute the recovery through the real pipe."""
    client = MockEpicorClient()
    retry = call(_WRAPPED_SETOP_TOPN, client)["retry_with"]["sql"]

    sql, ds = load("setop_cte_topn")
    assert retry == sql, "the fixture must be the exact statement the server hands back"
    top = top_level_subquery(ds)
    assert top["SelectListClause"] == "Top"
    assert top["TopRowExpr"] == 10.0
    assert len(ds["QuerySortBy"]) == 1

    out = call(retry, client_for("setop_cte_topn", [{"PartNum": "X", "Revenue": "1"}]))
    assert out["success"] is True
    # The caller's `top 10` is honoured as the caller's, not re-invented.
    assert out["assumptions"]["row_bound"] == {
        "kind": "top", "value": 10, "source": "caller"
    }
    assert "rewrites" not in out["assumptions"]  # nothing was changed at all


def test_a_starred_wrapper_gets_no_retry_sql_because_select_star_would_refuse_it():
    """`select [w].*` survives a purely structural CTE rewrite, and
    `lint.select_star` refuses it one parse later. Handing that back would be a
    recovery that dies at the next gate, so the shape is described instead."""
    sql = (
        "select top 100 [w].* from (select [A].[PartNum] as [PN] from Erp.OrderDtl "
        "as [A] union all select [B].[PartNum] as [PN] from Erp.OrderDtl as [B]) as [w]"
    )
    out = call(sql, MockEpicorClient())
    assert out["error"] == "sql_setop_wrapper_unsafe"
    assert "retry_with" not in out or out.get("retry_with") is None
    assert "name the columns" in out["message"].lower()


def test_count_star_is_not_mistaken_for_select_star_and_still_gets_its_retry_sql():
    """`count(*)` CONTAINS an `exp.Star`, so a descendant search reads every
    grand-total aggregate as `select *` and withholds the recovery from exactly
    the queries that most need it.

    Same over-broad-scope defect as the whole-statement `top` regex this change
    removes from the lint: a question about the OUTER projection item answered
    by searching everything underneath it. Caught only because the message came
    out reading "; and and its projection is `*`".
    """
    sql = (
        "select top 5 [w].[PN] as [PN], count(*) as [N] from "
        "(select [A].[PartNum] as [PN] from Erp.OrderDtl as [A] union all "
        "select [B].[PartNum] as [PN] from Erp.OrderDtl as [B]) as [w] group by [w].[PN]"
    )
    out = call(sql, MockEpicorClient())
    assert out["error"] == "sql_setop_wrapper_unsafe"
    assert "its projection is `*`" not in out["message"]
    retry = out["retry_with"]["sql"]
    assert retry.upper().startswith("WITH [W] AS (")
    assert "COUNT(*)" in retry.upper()
    # Grammar: reasons are joined, never double-conjoined.
    assert "; and and " not in out["message"]


def test_a_plain_projection_over_a_wrapped_setop_still_runs_and_is_page_size_bounded():
    """A plain projection over a wrapped set operation remains supported.

    Bound it through page size without inventing branch-level Top clauses."""
    sql, ds = load("setop_wrapper_plain")
    # Fixture with no `top`: the outer select is represented as
    # 'All' — which the OLD whole-statement `has_top` would have been fine with,
    # but which the per-branch injection would have turned into a bounded one.
    assert top_level_subquery(ds)["SelectListClause"] == "All"
    out = call(sql, client_for("setop_wrapper_plain", [{"PN": "A"}]))
    assert out["success"] is True
    assert out["assumptions"]["row_bound"]["kind"] == "page_size_only"
    # No `top` was invented anywhere — Epicor would have ignored it on the
    # wrapper, and inventing one on the branches is what made the aggregate wrong.
    assert "TOP" not in out["sql_executed"].upper()
    assert any(
        a["rule"] == "setop_wrapper_bounded_by_page_size_only"
        for a in out["assumptions"]["advisories"]
    )


def test_the_wedge_query_passes_every_gate():
    sql, _ = load("wedge_rollup")
    rows = [{"OrderDtl_PartNum": "WIDGET-100", "revenue": "1234.500"}]
    client = client_for("wedge_rollup_top", rows)
    out = call(sql, client, page_size=1000)
    assert out["success"] is True
    assert out["tables_read"] == ["Erp.OrderDtl"]
    assert out["rows"].splitlines()[0] == "OrderDtl_PartNum\trevenue"


# --------------------------------------------------------------------------- #
# The four error channels
# --------------------------------------------------------------------------- #


def test_channel_1_a_parse_400_keeps_epicors_message_verbatim():
    client = MockEpicorClient(
        parse_error=FakeEpicorError(
            "SQL cannot be parsed: Incorrect syntax near 'NotATable'.", 400
        )
    )
    out = call("select top 5 [P].[PartNum] as [PN] from Erp.NotATable as [P]", client)
    assert out["error"] == "sql_parse_error"
    assert "Incorrect syntax near 'NotATable'" in out["message"]
    assert out["detail"]["status"] == 400
    assert not client.called("Execute")


def test_channel_1_an_inaccessible_table_is_terminal():
    client = MockEpicorClient(
        parse_error=FakeEpicorError("References to inaccessible tables detected", 400)
    )
    out = call("select top 5 [U].[X] as [X] from Ice.SessionState as [U]", client)
    assert out["error"] == "table_not_accessible"
    assert out["terminal"] is True


def test_channel_2_http_200_with_errors_is_a_failure_and_analyze_names_the_column():
    sql, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds,
        execute_response={
            "returnObj": {
                "Results": [],
                "Errors": [{"ErrorText": "Bad SQL statement."}],
                "ExecutionInfo": [],
            }
        },
        analyze_messages=["Invalid column name 'NoSuchColumn'."],
    )
    out = call(sql, client)
    assert out["success"] is False
    assert out["error"] == "sql_run_error"
    assert "Invalid column name 'NoSuchColumn'." in out["message"]
    assert client.called("Analyze")


def test_analyze_strips_the_order_by_when_the_message_is_the_known_mask():
    sql, ds = load("sort_invented_alias")
    ds = dict(ds)
    calls: list[dict] = []

    class AnalyzeClient(MockEpicorClient):
        async def post(self, url, api_key, json_body=None):
            if "Analyze" in url:
                calls.append(json_body or {})
                if len(calls) == 1:
                    return {"parameters": {"errorMessages": [
                        "An object or column name is missing or empty."]}}
                return {"parameters": {"errorMessages": ["Invalid column name 'Rev'."]}}
            return await super().post(url, api_key, json_body)

    client = AnalyzeClient(
        parse_ds=ds,
        execute_response={
            "returnObj": {"Results": [], "Errors": [{"ErrorText": "Bad SQL statement."}],
                          "ExecutionInfo": []}
        },
    )
    # Drive the sort-bearing fixture but neutralise the lint so we reach Execute.
    ds["QuerySortBy"] = [{"TableID": "OD", "FieldName": "PartNum", "Seq": 1, "IsAsc": True}]
    out = call(sql, client)
    assert "Invalid column name 'Rev'." in out["message"]
    assert len(calls) == 2
    assert calls[1]["queryDS"]["QuerySortBy"] == []


def test_channel_3_a_500_is_our_bug_and_never_blames_the_caller():
    sql, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds, execute_error=FakeEpicorError("Internal server error; GUID abc", 500)
    )
    out = call(sql, client)
    assert out["error"] == "server_error"
    assert "not a problem with your SQL" in out["message"]


def test_an_execute_side_inaccessible_table_is_terminal():
    sql, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds,
        execute_error=FakeEpicorError("References to inaccessible tables detected", 400),
    )
    out = call(sql, client)
    assert out["error"] == "table_not_accessible"
    assert out["terminal"] is True


# --------------------------------------------------------------------------- #
# The governor's runtime half
# --------------------------------------------------------------------------- #


def test_the_timeout_fires_and_returns_a_refusal_rather_than_hanging():
    sql, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute([]), execute_delay_s=0.5
    )
    governor = CostGovernor(GovernorPolicy(execute_timeout_s=0.05))
    out = call(sql, client, governor=governor)
    assert out["error"] == "query_too_expensive"
    assert "stopped waiting" in out["message"]
    assert "NOT yet established" in out["message"]


def test_an_exhausted_session_budget_refuses_before_parsing():
    sql, _ = load("clean_top")
    client = client_for("clean_top", [{"PN": "x"}])
    governor = CostGovernor(GovernorPolicy(session_budget_s=1.0))
    governor.record("u", 5.0)
    out = call(sql, client, governor=governor, session_id="u")
    assert out["error"] == "query_budget_exhausted"
    assert client.calls == []


def test_wall_clock_is_recorded_against_the_session():
    sql, _ = load("clean_top")
    client = client_for("clean_top", [{"PN": "x"}])
    governor = CostGovernor(GovernorPolicy())
    call(sql, client, governor=governor, session_id="u")
    assert governor.spent("u") >= 0.0
    assert governor.spent("someone-else") == 0.0


# --------------------------------------------------------------------------- #
# Honesty about what was actually run
# --------------------------------------------------------------------------- #


def test_a_rewrite_is_announced_and_the_executed_sql_is_returned():
    _, ds = load("clean_top_200")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([{"PN": "x"}]))
    out = call("select [P].[PartNum] as [PN] from Erp.Part as [P]", client)
    assert "row_bound_injected" in [r["rule"] for r in out["assumptions"]["rewrites"]]
    assert out["sql_executed"].upper().startswith("SELECT TOP 200")
    assert client.body_for("ParseFromSQL")["ds"]["DynamicQueryDesigner"][0][
        "DisplayPhrase"
    ] == out["sql_executed"]


def test_a_grain_warning_rides_back_on_a_query_that_still_runs():
    sql, _ = load("gov_fanout")
    client = client_for("gov_fanout", [{"J": "JOB-100", "Q": "5"}])
    out = call(sql, client)
    assert out["success"] is True
    assert out["summary"].startswith("GRAIN WARNING")
    assert out["notes"][0]["rule"] == "aggregate_fanout"

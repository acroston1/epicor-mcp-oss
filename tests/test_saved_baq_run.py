"""Regression coverage: test saved baq run."""

from __future__ import annotations

import asyncio
import json

import pytest

from epicor_mcp.baq_ops.saved_run import (
    coerce_baq_params,
    describe_saved_baq,
    run_saved_baq,
)
from epicor_mcp.sql.governor import CostGovernor, GovernorPolicy
from tests.wedge_fixtures import (
    FakeEpicorError,
    MockEpicorClient,
    getbyid_returnobj,
    load,
    ok_execute,
)

BASE = "https://example.invalid/api/v2/odata/DEMO"


def call(client: MockEpicorClient, baq_id: str = "AUTO-parts", **kw) -> dict:
    return asyncio.run(
        run_saved_baq(client=client, api_key="k", base_url=BASE, baq_id=baq_id, **kw)
    )


def client_for(rows: list[dict] | None = None, **kw) -> MockEpicorClient:
    kw.setdefault("baq_data_rows", rows if rows is not None else [{"PN": "ABC-1"}])
    return MockEpicorClient(**kw)


# --------------------------------------------------------------------------- #
# 25-26 — the happy path, and the order that IS the authorization
# --------------------------------------------------------------------------- #


def test_25_the_result_shape_matches_an_ad_hoc_run():
    """A SUBSET, not set equality: `annotate_next_step` pops `next_step` on a
    clean result and `grain_checks`/`diagnosis`/`stage_ms` are conditional, so
    equality would be untestable and would end up weakened rather than met."""
    from epicor_mcp.sql.adhoc import run_sql

    sql, ds = load("clean_top")
    adhoc = asyncio.run(
        run_sql(sql, client=MockEpicorClient(parse_ds=ds,
                                             execute_response=ok_execute([{"PN": "ABC-1"}])),
                api_key="k", base_url=BASE, diagnose=False, validate_columns=False,
                ground_domains=False, company_id="DEMO")
    )
    out = call(client_for())

    shared = {
        "success", "columns", "rows", "format", "row_count", "rows_dropped_for_size",
        "complete", "terminal", "summary", "tables_read", "assumptions", "notes",
        "sql_ms", "parse_ms", "execute_ms", "elapsed_s",
    }
    assert shared <= set(out)
    assert shared <= set(adhoc)
    assert out["columns"] == adhoc["columns"] == ["PN"]
    assert out["rows"] == adhoc["rows"] == "PN\nABC-1"
    assert out["format"] == adhoc["format"]
    assert out["tables_read"] == adhoc["tables_read"] == ["Erp.Part"]
    assert "records" not in out, "two row formats from one tool is the defect"
    assert out["saved_baq_id"] == "AUTO-parts"


def test_25b_the_key_SET_difference_is_pinned_exactly_not_just_a_subset():
    """Test 25 asserts a subset, which cannot catch a key the saved path FORGOT
    or one it invented. This pins the difference in both directions, so any new
    key on either path has to be justified here rather than drift in.

    * ``saved_baq_id`` is the ONE key the saved path adds deliberately
      (``sql_executed`` stays present but EMPTY — a DisplayPhrase pasted back
      into ``sql`` is a different, ungoverned statement). It used to add
      ``baq_definition_sql`` too; definition text adds payload unrelated to the
      requested rows and is omitted.
    * ``stage_ms`` is the only permitted absence: the ad-hoc path emits it only
      when a LOCAL stage crossed 1 ms, and there is no transpile here to spend
      it. Nothing else may go missing.
    """
    from epicor_mcp.sql.adhoc import run_sql

    sql, ds = load("clean_top")
    adhoc = asyncio.run(
        run_sql(sql, client=MockEpicorClient(parse_ds=ds,
                                             execute_response=ok_execute([{"PN": "ABC-1"}])),
                api_key="k", base_url=BASE, diagnose=False, validate_columns=False,
                ground_domains=False, company_id="DEMO")
    )
    out = call(client_for())

    assert set(out) - set(adhoc) == {"saved_baq_id"}
    assert set(adhoc) - set(out) <= {"stage_ms"}
    assert out["sql_executed"] == "" and "sql_executed" in out


def test_26_getbyid_always_precedes_the_execution():
    client = client_for()
    call(client)
    assert client.index_of("GetByID") < client.index_of("/Data")


# --------------------------------------------------------------------------- #
# 27 — the SHAPE GUARD, which closes a fail-open
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tables",
    [
        [],                                                            # no rows at all
        [{"TableID": "t", "TableType": "SQ", "DBTableName": ""}],      # only SQ/TT
        [{"TableID": "t", "TableType": "TT", "DBTableName": ""}],
        [{"TableID": "P", "TableType": "DB", "DBTableName": "  "}],    # blank name
    ],
)
def test_27_an_unreadable_definition_is_refused_not_run(tables):
    """`check_parsed_ds` returns an ALL-EMPTY Denial for an unrecognised tableset
    and an empty Denial is falsy, so `if denial:` does not
    fire and the query would RUN — ungated. This is the guard that closes it."""
    client = client_for(getbyid_obj=getbyid_returnobj(tables=tables))
    out = call(client)
    assert out["error"] == "saved_baq_definition_unreadable"
    assert out["terminal"] is True
    assert not client.called("/Data"), "an unauthorizable BAQ must NEVER execute"


# --------------------------------------------------------------------------- #
# 28-29 — the deny-list, over Epicor's own rows
# --------------------------------------------------------------------------- #


_PAYROLL = [
    {"TableID": "PR", "DBSchemaName": "Erp", "DBTableName": "PREmpMas", "TableType": "DB"}
]


def test_28_a_deny_listed_table_in_the_definition_refuses_before_execution():
    client = client_for(getbyid_obj=getbyid_returnobj(tables=_PAYROLL))
    out = call(client)
    assert out["error"] == "table_access_denied"
    assert out["detail"]["stage"] == "denylist"
    assert out["detail"]["source"] == "saved_baq"
    assert not client.called("/Data")


def test_29_the_deny_list_reads_both_array_namings():
    """GetByID returns the RUNTIME names; ParseFromSQL returns `*Designer`. Both
    `denylist._rows` and `governor._rows` fall back, so the same definition under
    either spelling must produce the identical denial."""
    runtime_obj = getbyid_returnobj(tables=_PAYROLL)
    designer_obj = {
        "DynamicQueryDesigner": runtime_obj["DynamicQuery"],
        "QueryTableDesigner": runtime_obj["QueryTable"],
        "QueryFieldDesigner": runtime_obj["QueryField"],
    }
    a = call(client_for(getbyid_obj=runtime_obj))
    b = call(client_for(getbyid_obj=designer_obj))
    assert a["error"] == b["error"] == "table_access_denied"
    assert a["message"] == b["message"]


# --------------------------------------------------------------------------- #
# 30-32 — parameters
# --------------------------------------------------------------------------- #


_MANDATORY = [{"ParameterID": "FromDate", "ParameterType": "date", "SkipIfEmpty": False}]


def test_30_a_mandatory_parameter_left_unsupplied_annotates_and_still_runs():
    """`mandatory` is the heuristic `not SkipIfEmpty` and in
    the shipped code it is used ONLY to attribute a failure Epicor already
    returned. Promoting it to a pre-flight refusal would blame the caller for
    something Epicor never complained about."""
    client = client_for(getbyid_obj=getbyid_returnobj(parameters=_MANDATORY))
    out = call(client)

    assert out["success"] is True
    assert client.called("/Data"), "advisory means it RUNS"
    note = out["assumptions"]["saved_baq"]["parameters_unsupplied"]
    assert note["parameters"] == [
        {"name": "FromDate", "type": "date", "mandatory": True}
    ]
    assert note["retry_with"] == {
        "saved_baq": "AUTO-parts", "params": {"FromDate": "<date>"}
    }


def test_31_only_an_actual_failure_earns_the_params_blame():
    client = client_for(
        getbyid_obj=getbyid_returnobj(parameters=_MANDATORY),
        baq_data_error=FakeEpicorError("Parameter 'FromDate' is mandatory", 400),
    )
    out = call(client)
    assert out["error"] == "baq_needs_params"
    assert out["valid"]["parameters"] == [
        {"name": "FromDate", "type": "date", "mandatory": True}
    ]
    assert out["retry_with"] == {
        "saved_baq": "AUTO-parts", "params": {"FromDate": "<date>"}
    }


def test_32_params_ride_the_query_string_and_cannot_overwrite_the_row_bound():
    client = client_for()
    call(client, params={"FromDate": "2026-01-01", "$top": 999999}, page_size=25)
    sent = client.body_for("/Data")
    assert sent["$top"] == 25, "a $-prefixed param must never unbound the read"
    assert sent["FromDate"] == "2026-01-01"


def test_32b_an_uncoercible_params_is_refused_rather_than_silently_emptied():
    """The legacy helper returns `{}` for anything it cannot read, which drops the caller's
    parameters and then blames the BAQ for needing them."""
    assert coerce_baq_params({"a": 1}) == ({"a": 1}, "")
    assert coerce_baq_params('{"a": 1}') == ({"a": 1}, "")
    assert coerce_baq_params(None) == ({}, "")
    assert coerce_baq_params("[1,2]")[1] == "a JSON list, not a JSON object"
    assert coerce_baq_params("nonsense")[1] == "a string that is not JSON"

    client = client_for()
    out = call(client, params=["a", "b"])
    assert out["error"] == "invalid_params_type"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# 33-34 — resolution
# --------------------------------------------------------------------------- #


def test_33_a_bare_id_is_retried_as_auto_and_the_correction_is_announced():
    class MissesBareId(MockEpicorClient):
        async def post(self, url, api_key, json_body=None):
            if "GetByID" in url and json_body.get("queryID") == "parts":
                self.calls.append((url, json_body))
                raise FakeEpicorError("Dynamic query is not found parts", 404)
            return await super().post(url, api_key, json_body)

    client = MissesBareId(baq_data_rows=[{"PN": "A"}])
    out = call(client, baq_id="parts")
    assert out["success"] is True
    assert out["saved_baq_id"] == "AUTO-parts"
    assert out["assumptions"]["saved_baq"]["baq_id_corrected"] == {
        "submitted": "parts",
        "used": "AUTO-parts",
        "why": "no BAQ of the submitted id exists; the AUTO- prefix resolved it",
    }


def test_33b_the_auto_retrys_404_does_not_mask_the_first_spellings_real_error():
    """A failed prefix retry must not hide the original spelling's real error.
    Reporting only the retry's 404 would incorrectly suggest changing the ID."""
    client = client_for(getbyid_error=FakeEpicorError("Bad request on the id", 400))
    out = call(client, baq_id="parts")
    assert out["error"] == "baq_not_found"
    assert out["detail"]["message"] == "Bad request on the id"
    assert out["detail"]["message_is_from"] == "parts"
    assert out["detail"]["tried"] == ["parts", "AUTO-parts"]


def test_34_both_spellings_missing_is_terminal():
    client = client_for(getbyid_missing=True)
    out = call(client, baq_id="parts")
    assert out["error"] == "baq_not_found"
    assert out["terminal"] is True
    assert not client.called("/Data")


# --------------------------------------------------------------------------- #
# 35-36 — paging and completeness
# --------------------------------------------------------------------------- #


def test_35_page_two_is_refused_and_no_skip_is_ever_sent():
    """The shipped runner sends `$top` only, and the sibling endpoint SILENTLY
    IGNORES unrecognised paging settings — an optimistic `$skip` would return
    page 1's rows labelled page 2."""
    client = client_for()
    out = call(client, page=2)
    assert out["error"] == "saved_baq_paging_unsupported"
    assert client.calls == []
    assert "never been measured" in out["message"]
    assert "page_size" in out["retry_with"]
    assert "params" in out["message"]


def test_35b_no_skip_key_is_constructible_anywhere_in_the_module():
    """The refusal is only half the control. A ``$skip`` query-string key can only
    come from the exact string literal ``"$skip"``, so the AST is scanned for one
    — which a future edit that wires paging up optimistically cannot avoid, and
    which the refusal's own prose (where ``$skip`` appears inside a sentence)
    cannot trip. Same idiom, and same reason, as ``test_query_no_write_methods``:
    the failure guarded against is a new call site, not a subtle logic error."""
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "src" / "epicor_mcp" / "baq_ops" / "saved_run.py"
    )
    source = path.read_text()
    assert "$skip" in source, "the module docstring names it, to forbid it"
    literals = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "$skip" not in literals
    assert "$top" in literals, "…and the stripper is not silently matching nothing"


def test_36_completeness_carries_all_three_terms():
    partial = call(client_for([{"PN": "A"}]), page_size=10)
    assert partial["complete"] is True and partial["terminal"] is True

    full = call(client_for([{"PN": str(i)} for i in range(10)]), page_size=10)
    assert full["complete"] is False and full["terminal"] is False
    assert full["summary"].startswith("INCOMPLETE:")

    truncated = call(client_for([{"PN": "x" * 500} for _ in range(50)]), max_bytes=800)
    assert truncated["rows_dropped_for_size"] > 0
    assert truncated["complete"] is False

    # The third term. `allow_paging` is the test seam that keeps this branch
    # reachable while page > 1 is refused; production never sets it.
    later = asyncio.run(
        run_saved_baq(client=client_for([]), api_key="k", base_url=BASE,
                      baq_id="AUTO-parts", page=2, allow_paging=True)
    )
    assert later["complete"] is False and later["terminal"] is False
    assert later["summary"].startswith("EMPTY PAGE:")
    assert "the complete result" not in later["summary"]


# --------------------------------------------------------------------------- #
# 37-38 — honesty about what this server did and did not write
# --------------------------------------------------------------------------- #


def test_37_zero_rows_are_labelled_not_diagnosed():
    client = client_for([])
    out = call(client)
    assert out["row_count"] == 0
    assert "did not write this BAQ's SQL" in out["summary"]
    # `diagnose_empty` issues bounded probes through Execute. None may appear.
    assert not client.called("Execute")


def test_38_sql_executed_is_empty_and_the_definition_is_NOT_returned():
    """Two separate rules, and the second is about weight.

    `sql_executed` is copy-pasteable by contract, and a DisplayPhrase pasted
    back into `sql` is a DIFFERENT, ungoverned, possibly unbounded statement —
    so it stays present and EMPTY.

    The definition SQL is not returned AT ALL. It is not runnable through
    `sql`, so including it would add unrelated payload before the requested
    rows. `saved_baq_id` identifies what ran.
    """
    client = client_for()
    out = call(client)
    assert out["sql_executed"] == ""
    assert "baq_definition_sql" not in out
    assert out["saved_baq_id"] == "AUTO-parts"
    blob = json.dumps(out)
    assert "select top 5" not in blob, "the definition SQL must not ride in ANY key"


def test_38b_tables_read_excludes_sq_and_tt_and_is_schema_qualified():
    obj = getbyid_returnobj(
        tables=[
            {"TableID": "P", "DBSchemaName": "Erp", "DBTableName": "Part",
             "TableType": "DB"},
            {"TableID": "t", "DBSchemaName": "", "DBTableName": "", "TableType": "SQ"},
        ]
    )
    assert call(client_for(getbyid_obj=obj))["tables_read"] == ["Erp.Part"]


def test_38c_an_exhausted_budget_refuses_with_no_epicor_call():
    governor = CostGovernor(GovernorPolicy())
    governor.record("u", governor.policy.session_budget_s + 1)
    client = client_for()
    out = call(client, governor=governor, session_id="u")
    assert out["error"] == "query_budget_exhausted"
    assert client.calls == []


def test_38d_the_governor_is_charged_on_every_exit_path_including_refusals():
    governor = CostGovernor(GovernorPolicy())
    assert governor.spent("u") == 0.0
    call(client_for(getbyid_missing=True), baq_id="nope", governor=governor, session_id="u")
    after_refusal = governor.spent("u")
    assert after_refusal > 0.0

    call(client_for(), governor=governor, session_id="u")
    assert governor.spent("u") > after_refusal


def test_38e_describe_saved_baq_is_pure_and_reads_both_namings():
    obj = getbyid_returnobj(parameters=_MANDATORY)
    described = describe_saved_baq(obj)
    assert described["parameters"][0]["mandatory"] is True
    assert described["columns"] == ["PN"]
    assert described["display_phrase"].startswith("select top 5")
    assert describe_saved_baq({}) == {
        "parameters": [], "columns": [], "display_phrase": ""
    }


# --------------------------------------------------------------------------- #
# 39 — a synthetic dashboard with unresolved parameter references
# --------------------------------------------------------------------------- #


def test_39_a_hand_authored_baq_with_unplaceable_references_RUNS():
    """Regression coverage: test 39 a hand authored baq with unplaceable references RUNS."""
    obj = getbyid_returnobj(query_id="SYNTHETIC-Invoice-Summary")
    obj["QueryTable"] = [
        {"TableID": "IH", "TableType": "DB",
         "DBSchemaName": "Erp", "DBTableName": "InvcHead"},
        {"TableID": "ExampleData", "TableType": "DB",
         "DBSchemaName": "Ice", "DBTableName": "UD01"},
    ]
    obj["QueryWhereItem"] = [
        {"TableID": "", "FieldName": "CurrentUserID"},
        {"TableID": "", "FieldName": "ExampleData_Date01"},
    ]
    client = client_for(rows=[{"Customer_Name": "ACME"}],
                        getbyid_obj=obj)
    out = call(client, baq_id="SYNTHETIC-Invoice-Summary")

    assert out["success"] is True, out.get("message")
    assert out["row_count"] == 1
    assert client.called("/Data"), "the BAQ must actually execute"
    assert sorted(out["tables_read"]) == ["Erp.InvcHead", "Ice.UD01"]


def test_39b_the_relaxation_stops_at_the_table_deny_list():
    """The same shape, plus one payroll table, is still refused — the relaxation
    is scoped to UNPLACEABLE references and touches no real control."""
    obj = getbyid_returnobj(query_id="SYNTHETIC-Invoice-Summary", tables=_PAYROLL)
    obj["QueryWhereItem"] = [{"TableID": "", "FieldName": "CurrentUserID"}]
    client = client_for(getbyid_obj=obj)
    out = call(client, baq_id="SYNTHETIC-Invoice-Summary")

    assert out["error"] == "table_access_denied"
    assert not client.called("/Data")


# --------------------------------------------------------------------------- #
# 40 — response weight: what BaqSvc adds that the caller did not ask for
# --------------------------------------------------------------------------- #


def test_40_baqsvc_row_handles_are_dropped_and_announced():
    """`RowIdent` is BaqSvc's own grid handle, not a column of the BAQ: it is
    appended to every saved-BAQ result, its values are synthesised per page
    (`00000001-0000-…`), adding a 36-char GUID on every row. The ad-hoc path
    does not return it, so
    carrying it here also broke the shape parity this path exists to hold.

    Dropped, never SILENTLY: the module's rule is announce-or-envelope.
    """
    client = client_for(rows=[
        {"Customer_Name": "ACME", "RowIdent": "00000001-0000-0000-0000-000000000000"},
        {"Customer_Name": "BETA", "RowIdent": "00000002-0000-0000-0000-000000000000"},
    ])
    out = call(client)

    assert out["columns"] == ["Customer_Name"]
    assert "RowIdent" not in out["rows"]
    assert "00000001-0000" not in out["rows"]
    assert out["row_count"] == 2, "dropping a COLUMN must not drop a ROW"
    dropped = out["assumptions"]["saved_baq"]["transport_columns_dropped"]
    assert dropped["columns"] == ["RowIdent"]
    assert "not columns of the BAQ" in dropped["why"]


def test_40b_a_baq_that_really_selects_RowIdent_keeps_it():
    """The drop is keyed on the BAQ's OWN declared result columns, so this can
    never eat a column the caller actually asked for."""
    obj = getbyid_returnobj(
        fields=[{"TableID": "P", "FieldName": "RowIdent", "DBFieldName": "RowIdent"}],
    )
    obj["QueryField"] = [
        {"TableID": "P", "FieldName": "RowIdent", "DBFieldName": "RowIdent",
         "DisplayName": "RowIdent", "FieldAlias": "RowIdent"}
    ]
    client = client_for(rows=[{"RowIdent": "mine"}], getbyid_obj=obj)
    out = call(client)

    # Assert the precondition rather than guarding on it — a conditional assert
    # here would pass vacuously the moment the fixture stopped declaring it.
    assert describe_saved_baq(obj)["columns"] == ["RowIdent"]
    assert out["columns"] == ["RowIdent"]
    assert "mine" in out["rows"]
    assert "transport_columns_dropped" not in out["assumptions"].get("saved_baq", {})

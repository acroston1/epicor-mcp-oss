"""Regression coverage: test lint unknown table."""

from __future__ import annotations

import asyncio
import json

import pytest

from epicor_mcp.sql.adhoc import run_sql
from epicor_mcp.sql.lint import Severity, lint_parsed, qualify_tables
from tests.wedge_fixtures import MockEpicorClient, load, names, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"


def call(sql: str, client: MockEpicorClient, **kw) -> dict:
    return asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE, **kw))


def client_for(fixture: str, rows: list[dict] | None = None) -> MockEpicorClient:
    _, ds = load(fixture)
    return MockEpicorClient(parse_ds=ds, execute_response=ok_execute(rows or []))


def rules(fixture: str) -> list[str]:
    sql, ds = load(fixture)
    return [f.rule for f in lint_parsed(sql, ds)]


def only(fixture: str, rule: str):
    sql, ds = load(fixture)
    hits = [f for f in lint_parsed(sql, ds) if f.rule == rule]
    assert hits, f"{fixture}: expected {rule}, got {[f.rule for f in lint_parsed(sql, ds)]}"
    return hits[0]


# --------------------------------------------------------------------------- #
# The defect itself
# --------------------------------------------------------------------------- #


def test_a_missing_schema_prefix_is_an_unknown_TABLE_not_three_phantom_columns():
    hit = only("unknown_table_no_schema", "unknown_table")
    assert hit.severity == Severity.REFUSE
    assert rules("unknown_table_no_schema") == ["unknown_table"]
    assert hit.detail["reason"] == "missing_schema_prefix"
    assert hit.detail["schema_returned"] == ""
    assert hit.detail["write"] == "Erp.JobHead"
    # The three columns the old rule called phantoms are named as NOT the
    # problem, and nothing in the message says they do not exist.
    assert hit.detail["columns_reported_blank"] == ["JobNum", "PartNum", "ProdQty"]
    assert "Erp.JobHead" in hit.message
    assert "does not exist at the SQL layer" not in hit.message
    for column in ("JobNum", "PartNum", "ProdQty"):
        assert f"JobHead.{column}` does not exist" not in hit.message


def test_the_real_columns_are_never_named_as_the_things_that_do_not_exist():
    """The exact `valid.phantom_columns` used to carry three real
    columns of Erp.JobHead."""
    sql, _ = load("unknown_table_no_schema")
    client = client_for("unknown_table_no_schema")
    out = call(sql, client, validate_columns=False)
    assert out["error"] == "sql_unknown_table"
    assert "phantom_columns" not in (out.get("valid") or {})
    assert out["valid"]["tables"] == {"JobHead": "Erp.JobHead"}
    blob = json.dumps(out)
    assert "JobHead.JobNum" not in blob
    assert "JobHead.ProdQty" not in blob


def test_the_recovery_is_a_runnable_statement_not_a_template():
    sql, _ = load("unknown_table_no_schema")
    out = call(sql, client_for("unknown_table_no_schema"), validate_columns=False)
    assert out["retry_with"]["sql"] == (
        "select top 10 [J].[JobNum] as [Job], [J].[PartNum] as [PN], "
        "[J].[ProdQty] as [Qty] from Erp.JobHead as [J]"
    )
    # ...and it is the caller's own statement, changed in exactly one place.
    assert out["retry_with"]["sql"].replace("Erp.JobHead", "JobHead") == sql


def test_the_column_clean_bill_is_EARNED_from_the_catalogue_not_asserted():
    """`columns_verified_on` may only appear when every named column was found
    on the table the caller meant. A clean bill nobody checked is worse than the
    ambiguity it replaces."""
    hit = only("unknown_table_no_schema", "unknown_table")
    assert hit.detail["columns_verified_on"] == "Erp.JobHead"
    assert "all of them exist on `Erp.JobHead`" in hit.message


# --------------------------------------------------------------------------- #
# The other side: a genuine phantom column must still be a phantom COLUMN
# --------------------------------------------------------------------------- #


def test_a_resolved_table_whose_ONLY_column_is_a_phantom_is_still_a_column_error():
    """The case that makes the naive all-blank rule wrong. `Erp.Part` resolves
    (DBSchemaName == 'Erp') and `OnHandQty` is an OData-only field, so every
    column of the table is blank and the TABLE is fine."""
    hit = only("phantom_only_column", "unknown_column")
    assert rules("phantom_only_column") == ["unknown_column"]
    assert hit.detail == {"table": "Part", "column": "OnHandQty"}


def test_a_phantom_beside_a_real_column_is_unchanged():
    hit = only("unknown_column", "unknown_column")
    assert hit.detail == {"table": "OrderDtl", "column": "DocExtPrice"}
    assert rules("unknown_column") == ["unknown_column"]


def test_no_other_captured_statement_gains_an_unknown_table_finding():
    """The whole corpus, so a false REFUSE on a good statement cannot slip in.
    Only the four D4 shapes and the deny-listed one resolve to a bad table."""
    expected = {
        "unknown_table_no_schema",
        "unknown_table_wrong_schema",
        "unknown_table_absent",
        "unknown_table_mixed_join",
        "deny_ice_userfile",  # `Ice.UserFile` — the real table is `Erp.UserFile`
    }
    got = {n for n in names() if "unknown_table" in rules(n)}
    assert got == expected


# --------------------------------------------------------------------------- #
# The two shapes only the catalogue can see
# --------------------------------------------------------------------------- #


def test_a_real_table_under_the_wrong_schema_names_the_right_one():
    hit = only("unknown_table_wrong_schema", "unknown_table")
    assert hit.detail["reason"] == "wrong_schema"
    assert hit.detail["schema_returned"] == "Ice"
    assert hit.detail["write"] == "Erp.JobHead"
    assert "lives in schema `Erp`, not `Ice`" in hit.message


def test_a_table_that_does_not_exist_says_so_and_offers_no_rewrite():
    hit = only("unknown_table_absent", "unknown_table")
    assert hit.detail["reason"] == "unknown_table_name"
    assert hit.detail["write"] is None
    assert "not a table in this database" in hit.message
    out = call(
        load("unknown_table_absent")[0],
        client_for("unknown_table_absent"),
        validate_columns=False,
    )
    assert out["error"] == "sql_unknown_table"
    assert out["valid"]["unknown_tables"] == ["Erp.NoSuchTableXyz"]
    assert "retry_with" not in out  # nothing to run; inventing one is a second hop


def test_one_unresolved_table_beside_one_good_table_is_judged_per_table():
    sql, ds = load("unknown_table_mixed_join")
    findings = lint_parsed(sql, ds)
    assert [f.rule for f in findings] == ["unknown_table"]
    assert findings[0].detail["table"] == "JobHead"
    out = call(sql, client_for("unknown_table_mixed_join"), validate_columns=False)
    assert out["retry_with"]["sql"].count("Erp.Part") == 1
    assert "from Erp.JobHead as [J]" in out["retry_with"]["sql"]


# --------------------------------------------------------------------------- #
# It is still a REFUSAL: nothing new reaches Execute
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fixture",
    ["unknown_table_no_schema", "unknown_table_wrong_schema", "unknown_table_absent"],
)
def test_an_unresolved_table_never_reaches_execute(fixture):
    sql, _ = load(fixture)
    client = client_for(fixture, [{"x": 1}])
    out = call(sql, client, validate_columns=False)
    assert out["success"] is False
    assert out["detail"]["stage"] == "lint"
    assert out["detail"]["checked_before_running"] is True
    assert client.called("ParseFromSQL")
    assert not client.called("Execute")


def test_the_deny_list_still_refuses_before_the_lint_is_reached():
    """Ordering matters more than the message: a deny-listed table is refused at
    the deny-list stage, so the new rule never gets to talk about it."""
    sql, _ = load("deny_ice_userfile")
    out = call(sql, client_for("deny_ice_userfile"), validate_columns=False)
    assert out["success"] is False
    assert out["detail"]["stage"] == "denylist"


def test_no_recovery_is_served_for_a_deny_listed_table():
    """`Ice.UserFile` resolves to a blank DataType like any other unresolved
    table, and the catalogue's answer for it is `Erp.UserFile` — the reachable
    spelling of a hard-denied table. The refusal fires; the recovery does not."""
    hit = only("deny_ice_userfile", "unknown_table")
    assert hit.detail["recovery_withheld"] == "deny_listed"
    assert hit.detail["write"] is None
    assert "columns_verified_on" not in hit.detail
    assert "Erp.UserFile" not in json.dumps(hit.to_dict())


# --------------------------------------------------------------------------- #
# The catalogue is GITIGNORED: every path must survive its absence
# --------------------------------------------------------------------------- #


def test_without_the_catalogue_the_card_still_names_the_schema(monkeypatch):
    monkeypatch.setenv("EPICOR_MCP_SCHEMA_CATALOGUE", "/nonexistent/schema_catalogue.json")
    hit = only("unknown_table_no_schema", "unknown_table")
    assert hit.detail["catalogue"] == "card"
    assert hit.detail["write"] == "Erp.JobHead"
    # The card carries no column lists, so the clean bill is NOT claimed.
    assert "columns_verified_on" not in hit.detail
    assert "NOT the problem" in hit.message


def test_without_the_catalogue_the_two_catalogue_only_tiers_fall_back_quietly(monkeypatch):
    """A wrong schema and a nonexistent table are invisible without the
    catalogue — the card knows only a small fraction of the schema, so absence
    from it proves nothing. Both degrade to the OLD behaviour rather than guessing."""
    monkeypatch.setenv("EPICOR_MCP_SCHEMA_CATALOGUE", "/nonexistent/schema_catalogue.json")
    assert rules("unknown_table_wrong_schema") == ["unknown_column"]
    assert rules("unknown_table_absent") == ["unknown_column"]
    assert rules("phantom_only_column") == ["unknown_column"]


def test_the_catalogue_is_loaded_ONLY_on_the_error_path(monkeypatch):
    """The catalogue must not load for a statement that lints clean. The load
    is lazy, so making it explode proves nothing else calls it."""
    from epicor_mcp.sql import lint as lintmod

    def boom(*_a, **_k):
        raise AssertionError("the schema catalogue was loaded on a clean statement")

    monkeypatch.setattr(lintmod, "_schema_index", boom)
    for fixture in ("clean_top", "wedge_rollup", "clean_rollup", "unknown_column"):
        sql, ds = load(fixture)
        lint_parsed(sql, ds)  # must not raise


def test_an_off_card_table_with_no_catalogue_is_still_named_as_the_TABLE(monkeypatch):
    """Tier 1 needs no catalogue at all: `DBSchemaName == ''` is Epicor's own
    verdict. Only the concrete `Erp.X` suggestion depends on a lookup."""
    monkeypatch.setenv("EPICOR_MCP_SCHEMA_CATALOGUE", "/nonexistent/schema_catalogue.json")
    ds = {
        "QueryTable": [
            {"TableID": "X", "DBSchemaName": "", "DBTableName": "ZzOffCard", "TableType": "DB"}
        ],
        "QueryField": [
            {"TableID": "X", "DBTableName": "ZzOffCard", "DBFieldName": "SomeCol", "DataType": ""}
        ],
    }
    hit = next(
        f
        for f in lint_parsed("select top 1 [X].[SomeCol] as [C] from ZzOffCard as [X]", ds)
        if f.rule == "unknown_table"
    )
    assert hit.detail["write"] is None
    assert "Erp.ZzOffCard" in hit.message  # offered as a shape, not as a lookup
    assert "Ice.ZzOffCard" in hit.message


# --------------------------------------------------------------------------- #
# `qualify_tables` — the rewrite that builds `retry_with.sql`
# --------------------------------------------------------------------------- #


def test_the_rewrite_only_touches_from_and_join_slots():
    sql = (
        "select top 10 [J].[JobNum] as [JobHead] from JobHead as [J] "
        "inner join JobOper as [O] on [J].[JobNum] = [O].[JobNum]"
    )
    out = qualify_tables(sql, {"JobHead": "Erp.JobHead", "JobOper": "Erp.JobOper"})
    assert out == (
        "select top 10 [J].[JobNum] as [JobHead] from Erp.JobHead as [J] "
        "inner join Erp.JobOper as [O] on [J].[JobNum] = [O].[JobNum]"
    )


def test_the_rewrite_cannot_reach_inside_a_string_literal_or_a_comment():
    """The same masked-offset rule the `top` and `select *` rules follow;
    The legacy `unknown_columns` envelope blaming a quoted literal is the class it prevents."""
    sql = (
        "select top 10 [J].[JobNum] as [J1] from JobHead as [J] "
        "where [J].[PartNum] = 'from JobHead' -- from JobHead\n"
    )
    out = qualify_tables(sql, {"JobHead": "Erp.JobHead"})
    assert out.count("Erp.JobHead") == 1
    assert "= 'from JobHead'" in out
    assert "-- from JobHead" in out


def test_the_rewrite_handles_the_bracketed_spelling_and_is_idempotent():
    assert qualify_tables("select 1 from [JobHead] as [J]", {"JobHead": "Erp.JobHead"}) == (
        "select 1 from Erp.JobHead as [J]"
    )
    assert qualify_tables("select 1 from Erp.JobHead as [J]", {"JobHead": "Erp.JobHead"}) is None
    assert qualify_tables("select 1 from Erp.Part as [P]", {}) is None


def test_one_typed_column_proves_the_table_resolved_whatever_the_schema_says():
    """The guard that keeps a REFUSING rule off a statement that would have
    returned rows: a typed `DataType` is proof of resolution, so a table with a
    blank `DBSchemaName` and any typed column falls back to the column rule."""
    ds = {
        "QueryTable": [
            {"TableID": "J", "DBSchemaName": "", "DBTableName": "JobHead", "TableType": "DB"}
        ],
        "QueryField": [
            {"TableID": "J", "DBTableName": "JobHead", "DBFieldName": "JobNum",
             "DataType": "nvarchar"},
            {"TableID": "J", "DBTableName": "JobHead", "DBFieldName": "Nope", "DataType": ""},
        ],
    }
    # (the synthetic DS carries no QuerySubQuery, so rule 1 fires too — this
    # test is about rule 4 only)
    found = [
        f
        for f in lint_parsed("select top 1 [J].[JobNum] as [A] from JobHead as [J]", ds)
        if f.rule in ("unknown_table", "unknown_column")
    ]
    assert [f.rule for f in found] == ["unknown_column"]
    assert found[0].detail == {"table": "JobHead", "column": "Nope"}

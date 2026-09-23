"""The menu-derived table gate on the AD-HOC SQL path (sql/scope_gate.py).

FOUR PROPERTIES ARE THE POINT OF THIS FILE:

* **Deny beats scope.** A deny-listed table INSIDE the caller's scope is still
  ``table_access_denied`` (stage ``denylist``) — the scope gate runs strictly
  AFTER the deny-list and can never soften it into a request-wider-access hint.
* **A failure never reads as unlimited.** UNAVAILABLE fails CLOSED with a
  RETRYABLE envelope (``terminal: false``) and — at the WedgeRuntime seam —
  with ZERO Epicor calls.
* **`None` scope is byte-identical passthrough.** The standalone wedge server
  and every pre-gate test stay untouched.
* **Gate before execution or saving.** Ad-hoc queries are checked against the
  caller's table scope. Saved BAQs are checked against their definitions.
  A scope-refused run never reaches the writer.

The scope objects are the REAL ``AuthzScope`` (discovery/authz.py), so the
normalisation contract — qualified ``Erp.Part`` in, bare-lowercase membership —
is integration-tested here, not mocked.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from epicor_mcp.discovery.authz import AuthzScope
from epicor_mcp.sql import denylist, scope_gate
from epicor_mcp.sql.adhoc import DEFAULT_PAGE_SIZE, EXECUTE_PATH, PARSE_PATH, run_sql
from epicor_mcp.sql.governor import CostGovernor, GovernorPolicy
from epicor_mcp.sql.tool import _point_schema_miss_at_discovery
from epicor_mcp.wedge_server import WedgeRuntime
from tests.test_denylist_expressions import LEGAL_SHAPES
from tests.wedge_fixtures import MockEpicorClient, load, names, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"

# Real AuthzScope objects — the gate's whole membership contract in four values.
PART_ONLY = AuthzScope.scoped("user@example.org", {"Part"}, "menu chain")
NO_PART = AuthzScope.scoped("user@example.org", {"JobHead"}, "menu chain")
UNLIMITED = AuthzScope.unlimited("mgr@example.org", "SecurityMgr — every table is reachable")
UNAVAILABLE = AuthzScope.unavailable("user@example.org", "snapshot failed: Boom")


def call(sql: str, client: MockEpicorClient, **kw) -> dict:
    return asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE, **kw))


def client_for(fixture: str, rows: list[dict] | None = None, **kw) -> MockEpicorClient:
    _, ds = load(fixture)
    return MockEpicorClient(
        parse_ds=ds,
        execute_response=ok_execute(rows if rows is not None else [{"PN": "x"}]),
        **kw,
    )


# --------------------------------------------------------------------------- #
# db_tables_read — ONE extraction, shared with the deny-list
# --------------------------------------------------------------------------- #


def test_db_tables_read_skips_sq_and_tt_and_dedupes():
    ds = {
        "QueryTable": [
            {"TableID": "A", "TableType": "DB", "DBSchemaName": "Erp",
             "DBTableName": "JobHead"},
            {"TableID": "B", "TableType": "DB", "DBSchemaName": "Erp",
             "DBTableName": "LaborDtl"},
            # A second alias of the same table must not double-count.
            {"TableID": "A2", "TableType": "DB", "DBSchemaName": "Erp",
             "DBTableName": "JobHead"},
            # SQ/TT rows are CTE / derived / Calculated — never DB reads.
            {"TableID": "c", "TableType": "SQ", "DBSchemaName": "",
             "DBTableName": "17376a7a-guid"},
            {"TableID": "Calculated", "TableType": "TT", "DBSchemaName": "",
             "DBTableName": ""},
        ]
    }
    assert denylist.db_tables_read(ds) == ["Erp.JobHead", "Erp.LaborDtl"]


@pytest.mark.parametrize("fixture", names())
def test_extraction_parity_with_the_deny_list_on_every_captured_fixture(fixture):
    """The deny-list's own table universe (denied ∪ allowed) and the scope
    gate's `db_tables_read` are the SAME list on every real ParseFromSQL output
    — the one-extraction rule, asserted rather than promised."""
    _, ds = load(fixture)
    denial = denylist.check_parsed_ds(ds, unattributed_denies=False)
    assert set(denylist.db_tables_read(ds)) == set(
        denial.denied_tables + denial.allowed_tables
    ), fixture


@pytest.mark.parametrize("fixture", ("expr_cte_alias", "expr_derived_alias"))
def test_a_cte_or_derived_alias_never_appears_as_a_read_table(fixture):
    """The SQ alias (a GUID DBTableName) must not reach the gate — a scope
    would refuse it as an unauthorized 'table' on every CTE otherwise."""
    _, ds = load(fixture)
    tables = denylist.db_tables_read(ds)
    assert tables == ["Erp.OrderDtl"], tables


# --------------------------------------------------------------------------- #
# check_table_scope — the pure gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fixture", LEGAL_SHAPES)
def test_a_scope_holding_the_read_tables_passes_every_expression_shape(fixture):
    """Extraction parity with the deny-list's expression handling: the shapes
    the deny-list fixed D1 for (having/where expressions, TableID='') must not
    confuse the scope gate either — it reads tables, never column refs."""
    _, ds = load(fixture)
    scope = AuthzScope.scoped("u@x", denylist.db_tables_read(ds), "test")
    assert scope_gate.check_table_scope(scope, ds) is None


@pytest.mark.parametrize("fixture", LEGAL_SHAPES)
def test_a_scope_missing_the_read_table_refuses_every_expression_shape(fixture):
    _, ds = load(fixture)
    read = denylist.db_tables_read(ds)  # OrderDtl / OrderHed / Part per fixture
    empty_scope = AuthzScope.scoped("u@x", set(), "test")
    env = scope_gate.check_table_scope(empty_scope, ds)
    assert env is not None and env["error"] == "table_not_authorized"
    assert env["detail"]["unauthorized_tables"] == sorted(read)


def test_an_unlimited_scope_checks_nothing():
    _, ds = load("clean_top")
    assert scope_gate.check_table_scope(UNLIMITED, ds) is None


def test_a_none_scope_checks_nothing():
    _, ds = load("clean_top")
    assert scope_gate.check_table_scope(None, ds) is None


def test_an_unavailable_scope_refuses_even_at_the_gate_site():
    """Defence in depth: WedgeRuntime refuses UNAVAILABLE before the pipe, but
    a scope passed through anyway must refuse — never fall into the SCOPED
    branch where allows()==False would blame every table by name."""
    _, ds = load("clean_top")
    env = scope_gate.check_table_scope(UNAVAILABLE, ds)
    assert env["error"] == "authorization_unavailable"
    assert env["terminal"] is False
    assert "snapshot failed: Boom" in env["message"]


def test_zero_extracted_tables_fails_closed_not_open():
    """The shape-guard precedent (an all-empty Denial is falsy): a gate that
    cannot see what a statement reads refuses it — nothing failing the
    membership test is NOT a pass."""
    env = scope_gate.check_table_scope(PART_ONLY, {"QueryTable": []})
    assert env is not None
    assert env["error"] == "authorization_unavailable"
    assert env["terminal"] is False


def test_a_crashing_extraction_fails_closed():
    class Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    env = scope_gate.check_table_scope(PART_ONLY, Exploding())
    assert env["error"] == "authorization_unavailable"


def test_the_envelope_never_names_a_discovery_tool():
    """The SQL package must keep working when the
    discovery tools are not registered, so the pointer is added at tool.py's
    funnel, conditionally — never baked into the envelope."""
    _, ds = load("clean_top")
    env = scope_gate.check_table_scope(NO_PART, ds)
    assert "epicor_tables" not in json.dumps(env)
    assert "epicor_fields" not in json.dumps(env)


# --------------------------------------------------------------------------- #
# run_sql — placement and honesty of the refusal
# --------------------------------------------------------------------------- #


def test_a_scoped_allow_runs_to_completion():
    sql, _ = load("clean_top")
    client = client_for("clean_top")
    out = call(sql, client, table_scope=PART_ONLY)
    assert out["success"] is True
    assert client.paths == [f"{BASE}/{PARSE_PATH}", f"{BASE}/{EXECUTE_PATH}"]


def test_a_scoped_deny_names_the_tables_the_stage_and_never_executes():
    sql, _ = load("clean_top")
    client = client_for("clean_top")
    out = call(sql, client, table_scope=NO_PART)
    assert out["success"] is False
    assert out["error"] == "table_not_authorized"
    assert out["terminal"] is True
    assert out["detail"]["stage"] == "authz"
    assert out["detail"]["unauthorized_tables"] == ["Erp.Part"]
    assert "Erp.Part" in out["message"]
    assert "not executed" in out["message"].lower()
    # Parse ran (the gate reads Epicor's own resolution); Execute never did.
    assert client.called("ParseFromSQL")
    assert not client.called("Execute")


def test_a_partial_join_denial_names_only_the_missing_table():
    """The caller wrote both names, so echoing them leaks nothing — and naming
    the AUTHORIZED one under `valid` is what turns the refusal into a plan."""
    sql, _ = load("gov_bounded_join")  # Erp.JobHead ⋈ Erp.LaborDtl
    client = client_for("gov_bounded_join")
    scope = AuthzScope.scoped("u@x", {"JobHead"}, "test")
    out = call(sql, client, table_scope=scope)
    assert out["error"] == "table_not_authorized"
    assert out["detail"]["unauthorized_tables"] == ["Erp.LaborDtl"]
    assert out["valid"]["authorized_tables_in_this_query"] == ["Erp.JobHead"]
    assert not client.called("Execute")


def test_the_deny_list_still_beats_an_in_scope_table():
    """Deny beats everything. A payroll table INSIDE the caller's
    scope is refused by the deny-list FIRST — stage `denylist`, not `authz` —
    so scope membership can never be read as a way past the hard policy."""
    sql, _ = load("deny_table")  # select ... from Erp.PREmpMas
    client = client_for("deny_table")
    generous = AuthzScope.scoped("u@x", {"PREmpMas", "Part"}, "test")
    assert generous.allows("Erp.PREmpMas"), "premise: the scope really does hold it"
    out = call(sql, client, table_scope=generous)
    assert out["error"] == "table_access_denied"
    assert out["detail"]["stage"] == "denylist"
    assert not client.called("Execute")


def test_an_unlimited_scope_changes_nothing_at_the_pipe():
    sql, _ = load("clean_top")
    client = client_for("clean_top")
    out = call(sql, client, table_scope=UNLIMITED)
    assert out["success"] is True


def test_a_none_scope_is_byte_identical_to_the_pre_gate_pipe():
    """`table_scope=None` (and the parameter simply absent) must be the pipe
    exactly as it was — same keys, same values — so the standalone wedge server
    and every pre-gate caller are untouched."""
    sql, _ = load("clean_top")
    volatile = {"elapsed_s", "parse_ms", "execute_ms", "stage_ms", "diagnose_ms"}
    a = call(sql, client_for("clean_top"))
    b = call(sql, client_for("clean_top"), table_scope=None)
    assert set(a) - volatile == set(b) - volatile
    for key in set(a) - volatile:
        assert a[key] == b[key], key


# --------------------------------------------------------------------------- #
# WedgeRuntime — the seam server.py injects through
# --------------------------------------------------------------------------- #


class _Settings:
    auth_mode = "azure_ad"
    dev_mode = False
    environment = "live"
    epicor_company_id = "DEMO"
    response_max_bytes = 700_000
    sql_diagnose_empty = False
    sql_validate_columns = False
    sql_ground_domains = False
    port = 8061


class _AllowedRight:
    allowed = True
    reason = "ok"
    user_id = "adminuser@example.org"
    access_level = "read_only"
    can_write_baqs = True
    epicor_username = "adminuser"
    right_source = "users_json"


class FakeAuthorizer:
    """Duck-typed TableAuthorizer that RECORDS what the runtime asked of it —
    the saved-BAQ pin below is an assertion that `scope_calls` stays empty."""

    def __init__(self, scope, mode: str = "gate", raises: Exception | None = None):
        self.scope = scope
        self.mode = mode
        self.raises = raises
        self.scope_calls: list[str] = []

    # resolve_identity dropped `for_email` —
    # it outranked the session in gate mode, an identity-spoofing vector.
    def resolve_identity(self, session_email: str = "") -> str:
        return session_email or "dev@example.org"

    async def scope_for(self, email: str):
        self.scope_calls.append(email)
        if self.raises is not None:
            raise self.raises
        return self.scope


def runtime_for(
    client: MockEpicorClient, *, authorizer=None, set_attr: bool = True
) -> WedgeRuntime:
    """The suite's stubbed-runtime pattern (tests/test_baq_save.py)."""
    rt = WedgeRuntime.__new__(WedgeRuntime)
    rt.settings = _Settings()
    rt.credentials = None
    rt.api_key = "k"
    rt.base_url = BASE
    rt.governor = CostGovernor(GovernorPolicy())
    rt.domain_cache = None
    rt.client = client
    rt.can_save = lambda: _AllowedRight()
    if set_attr:
        rt.table_authorizer = authorizer
    return rt


def run(rt: WedgeRuntime, **kw) -> dict:
    return asyncio.run(rt.run(**kw))


CLEAN_SQL = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]"


def test_the_constructor_takes_table_authorizer_keyword_only_default_none():
    """The seam the wiring builder codes against — exactly this name, keyword
    only, defaulting to None so the bare wedge entry points stay ungated."""
    sig = inspect.signature(WedgeRuntime.__init__)
    param = sig.parameters["table_authorizer"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


def test_gate_mode_resolves_the_scope_and_enforces_it():
    client = client_for("clean_top")
    authorizer = FakeAuthorizer(NO_PART)
    out = run(runtime_for(client, authorizer=authorizer), sql=CLEAN_SQL)
    assert out["error"] == "table_not_authorized"
    assert authorizer.scope_calls == ["dev@example.org"]


def test_gate_mode_with_a_covering_scope_returns_rows():
    client = client_for("clean_top")
    out = run(runtime_for(client, authorizer=FakeAuthorizer(PART_ONLY)), sql=CLEAN_SQL)
    assert out["success"] is True


def test_boost_mode_never_asks_the_authorizer():
    """Only mode=='gate' gates. In boost/off the runtime must not
    even resolve a scope — a menu snapshot is real work."""
    for mode in ("boost", "off"):
        client = client_for("clean_top")
        authorizer = FakeAuthorizer(NO_PART, mode=mode)
        out = run(runtime_for(client, authorizer=authorizer), sql=CLEAN_SQL)
        assert out["success"] is True, mode
        assert authorizer.scope_calls == [], mode


def test_the_saved_baq_path_is_not_scope_gated():
    """A saved BAQ is its own grant. The authorizer is never
    consulted, and the run succeeds under a scope that would refuse the same
    table ad hoc. GetByID -> shape guard -> deny-list is unchanged."""
    client = MockEpicorClient(baq_data_rows=[{"PN": "ABC-1"}])
    authorizer = FakeAuthorizer(NO_PART)  # would refuse Erp.Part ad hoc
    out = run(runtime_for(client, authorizer=authorizer), saved_baq="AUTO-parts")
    assert authorizer.scope_calls == [], "the saved-BAQ branch asked the authorizer"
    assert out["success"] is True
    assert out["saved_baq_id"] == "AUTO-parts"


def test_a_scope_refusal_on_the_run_never_reaches_the_save_writer():
    """Running successfully is a prerequisite to saving. The refused run never
    returns success, so DeleteByID/Update are never issued — asserted on the
    call log, not the response shape."""
    client = client_for("clean_top")
    out = run(
        runtime_for(client, authorizer=FakeAuthorizer(NO_PART)),
        sql=CLEAN_SQL,
        save_as="my-query",
    )
    assert out["error"] == "table_not_authorized"
    assert out["saved"]["saved"] is False
    assert out["saved"]["reason"] == "statement_did_not_run"
    assert not client.called("DeleteByID")
    assert not client.called("Update")
    assert not client.called("Execute")


def test_an_unavailable_scope_refuses_with_zero_epicor_calls():
    """At the runtime seam the UNAVAILABLE refusal costs NOTHING: no transpile,
    no parse — and it is retryable, because the scope is never cached."""
    client = client_for("clean_top")
    out = run(runtime_for(client, authorizer=FakeAuthorizer(UNAVAILABLE)), sql=CLEAN_SQL)
    assert out["error"] == "authorization_unavailable"
    assert out["terminal"] is False
    assert out["detail"]["stage"] == "authz"
    assert client.calls == []


def test_an_authorizer_that_raises_fails_closed_with_zero_epicor_calls():
    client = client_for("clean_top")
    authorizer = FakeAuthorizer(PART_ONLY, raises=RuntimeError("boom"))
    out = run(runtime_for(client, authorizer=authorizer), sql=CLEAN_SQL)
    assert out["error"] == "authorization_unavailable"
    assert out["terminal"] is False
    assert client.calls == []


def test_an_empty_identity_in_gate_mode_fails_closed():
    """The REAL TableAuthorizer end to end: no session, no dev identity ->
    resolve_identity('') -> UNAVAILABLE ('no identity supplied') -> refusal
    with zero Epicor calls."""
    from epicor_mcp.discovery.authz import TableAuthorizer

    client = client_for("clean_top")
    authorizer = TableAuthorizer(object(), object(), mode="gate")
    out = run(runtime_for(client, authorizer=authorizer), sql=CLEAN_SQL)
    assert out["error"] == "authorization_unavailable"
    assert "no identity supplied" in out["message"]
    assert client.calls == []


def test_a_stubbed_runtime_without_the_attribute_stays_ungated():
    """The suite builds runtimes via WedgeRuntime.__new__ (test_baq_save.py's
    runtime_for), which never runs __init__ — those must keep working exactly
    as before the attribute existed."""
    client = client_for("clean_top")
    out = run(runtime_for(client, set_attr=False), sql=CLEAN_SQL)
    assert out["success"] is True


def test_the_empty_call_still_refuses_before_the_authorizer_is_consulted():
    """`{}` must refuse with zero work of ANY kind — no Epicor call and no menu
    snapshot — or test_every_listed_tool_passes_the_gate starts doing real
    authorization work per listing probe."""
    client = MockEpicorClient()
    authorizer = FakeAuthorizer(UNAVAILABLE)
    out = run(runtime_for(client, authorizer=authorizer))
    assert out["error"] == "no_statement"
    assert authorizer.scope_calls == []
    assert client.calls == []


# --------------------------------------------------------------------------- #
# tool.py — the conditional discovery pointer
# --------------------------------------------------------------------------- #


def _authz_env() -> dict:
    return scope_gate.table_not_authorized_envelope(
        ["Erp.LaborDtl"], ["Erp.JobHead"], email="u@x", sql="select ..."
    )


def test_table_not_authorized_gets_the_discovery_pointer_when_registered():
    out = _point_schema_miss_at_discovery(
        _authz_env(), "select ...", discovery_available=True
    )
    assert "epicor_tables" in out["how_to_fix"]
    assert "authorization" in out["how_to_fix"]
    assert out["retry_with"]["tool"] == "epicor_tables"
    assert "Erp.LaborDtl" in out["retry_with"]["query"]


def test_table_not_authorized_gets_no_pointer_when_discovery_is_absent():
    """Naming a tool the model cannot call is a guaranteed dead turn — the
    same conditionality as the schema-miss pointer."""
    env = _authz_env()
    out = _point_schema_miss_at_discovery(env, "select ...", discovery_available=False)
    assert out is env
    assert "how_to_fix" not in out


def test_the_schema_miss_pointer_is_unchanged_by_the_authz_branch():
    """Regression: the pre-existing funnel behaviour, driven through the same
    function the authz branch was added to."""
    env = {
        "success": False,
        "error": "sql_unknown_table",
        "valid": {"unknown_tables": ["POOrder"]},
    }
    out = _point_schema_miss_at_discovery(env, "select ...", discovery_available=True)
    assert "epicor_tables" in out["how_to_fix"]
    assert out["retry_with"]["query"] == "POOrder"

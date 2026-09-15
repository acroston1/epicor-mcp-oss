"""Gate-mode enforcement inside the two discovery tools.

The contract: ``EPICOR_MCP_TABLE_AUTHZ_MODE`` defaults to **gate**, the
scope is the explicit three-state (UNLIMITED | SCOPED | UNAVAILABLE), and the
discovery tools enforce it — ``epicor_tables`` hard-filters, ``epicor_fields``
refuses out-of-scope tables with ``table_not_authorized``. Everything here runs
OFFLINE against a duck-typed index and a fake authorizer that hands back REAL
:class:`~epicor_mcp.discovery.authz.AuthzScope` objects, so the membership
semantics under test are the shipped ones, not a re-implementation.

The pinned behaviours, each with its reason:

* gate filtering fills the page even after the deny-list drop (over-fetch);
* an UNAVAILABLE scope fails CLOSED in gate mode (retryable, never terminal)
  and degrades to no-boost in boost mode;
* the ``table_not_authorized`` refusal carries NO column data but MAY name
  reachable alternatives — the deliberate asymmetry vs ``table_access_denied``;
* the deny-list beats authz on the same table, without even consulting identity;
* a mixed request serves the allowed tables ALONGSIDE a named refusal;
* every suggestion channel (closest_tables, both ``elsewhere`` legs) stays
  inside the scope in gate mode — a suggestion the gate then refuses is a
  guaranteed dead round trip;
* boost mode is byte-identical AND snapshot-call-identical to the pre-gate tool;
* the ``{}``-safety envelopes precede the scope fetch entirely.
"""

from __future__ import annotations

from epicor_mcp.discovery.authz import AuthzScope
from epicor_mcp.discovery.tools import register_discovery_tools
from epicor_mcp.sql.denylist import is_denied_column, is_denied_table

# --------------------------------------------------------------------------- #
# Fixtures: three servable tables plus one deny-listed payroll table, so every
# test exercises the deny-vs-authz seam with the REAL deny-list callables.
# --------------------------------------------------------------------------- #
_TABLES = {
    "JobHead": "Erp.JobHead",
    "POHeader": "Erp.POHeader",
    "PartTran": "Erp.PartTran",
    "PREmpMas": "Erp.PREmpMas",
}
_FIELDS = {
    "JobHead": [("JobNum", "nvarchar"), ("PartNum", "nvarchar")],
    "POHeader": [("PONum", "int"), ("BuyerID", "nvarchar")],
    "PartTran": [("TranQty", "decimal"), ("ScrapQty", "decimal")],
    "PREmpMas": [("PayRate", "decimal"), ("EmpID", "nvarchar")],
}


class _Hit:
    def __init__(self, table: str, full: str) -> None:
        self.table, self.full_name = table, full
        self.description, self.field_count, self.score = "", 2, 0.9


class _FieldHit:
    def __init__(self, table: str, field: str, sql_type: str) -> None:
        self.table, self.field, self.sql_type = table, field, sql_type
        self.label, self.description, self.required = "", "", False


class _Index:
    """Duck-typed DiscoveryIndex honouring the real ``allowed=`` contract:
    bare lower-cased names, hard filter applied before the limit fills."""

    manifest = {"table_count": len(_TABLES), "field_count": 8}

    def search_tables(self, vec, q, limit=5, allowed=None):
        out = []
        for name, full in _TABLES.items():
            if allowed is not None and name.lower() not in allowed:
                continue
            out.append(_Hit(name, full))
            if len(out) >= limit:
                break
        return out

    def search_fields(self, table, vec, q, limit=6):
        return [_FieldHit(table, n, t) for n, t in _FIELDS.get(table, [])][:limit]

    def fields_of(self, table):
        return [{"name": n} for n, _ in _FIELDS.get(table, [])]

    def resolve_table(self, name):
        return {k.lower(): k for k in _TABLES}.get(str(name).strip().lower())

    def table_info(self, canon):
        return {"full_name": _TABLES[canon], "field_count": 2, "description": ""}

    def find_column_elsewhere(self, core):
        # The distinctive-column channel: ScrapQty lives on exactly one other
        # table, which lets the tests steer it in and out of scope.
        if core.lower() == "scrapqty":
            return ["PartTran"], 1
        return [], 0

    def name_matches_elsewhere(self, q, here):
        if "scrapqty" in q.lower() and "PartTran" not in here:
            return [("PartTran", "ScrapQty")]
        return []


class _Authorizer:
    """Fake with exactly the TableAuthorizer surface the tools consume, handing
    back REAL AuthzScope objects. ``calls`` records every snapshot fetch so the
    tests can pin WHICH paths consult identity at all."""

    def __init__(self, scope: AuthzScope, mode: str = "gate") -> None:
        self._scope, self.mode = scope, mode
        self.calls: list[str] = []

    # `for_email` was removed from the surface
    # AND from resolve_identity — it outranked the session in gate mode, an
    # identity-spoofing vector. The fake mirrors the new signature.
    def resolve_identity(self, session_email: str = "") -> str:
        return session_email or "user@example.org"

    async def scope_for(self, email: str) -> AuthzScope:
        self.calls.append(email)
        return self._scope


async def _embed(text, prefix):
    return None


def _register(scope: AuthzScope | None = None, mode: str = "gate"):
    registered: dict = {}

    class _MCP:
        def tool(self, *, name, description):
            def deco(fn):
                registered[name] = fn
                return fn
            return deco

    auth = _Authorizer(scope, mode) if scope is not None else None
    register_discovery_tools(
        _MCP(), _Index(), embed_query=_embed, authorizer=auth,
        denied_column=is_denied_column, denied_table=is_denied_table,
    )
    return registered, auth


_JOB_ONLY = AuthzScope.scoped("u@example.org", ["JobHead"], "test scope")


# --------------------------------------------------------------------------- #
# epicor_tables under the gate
# --------------------------------------------------------------------------- #
async def test_gate_mode_hard_filters_epicor_tables_to_the_scope():
    tools, _ = _register(_JOB_ONLY, mode="gate")
    resp = await tools["epicor_tables"](query="jobs and purchase orders")
    assert resp["success"] is True
    assert [t["name"] for t in resp["tables"]] == ["JobHead"]
    assert resp["tables"][0]["reachable_by_you"] is True
    assert resp["authz"]["mode"] == "gate"


async def test_gate_filtering_still_fills_the_page_past_the_denylist():
    """Scope admits three tables, one deny-listed. The limit*3 over-fetch must
    absorb the deny drop and still return exactly ``limit`` rows."""
    scope = AuthzScope.scoped("u@x", ["JobHead", "POHeader", "PREmpMas"], "t")
    tools, _ = _register(scope, mode="gate")
    resp = await tools["epicor_tables"](query="work in process", limit=2)
    names = [t["name"] for t in resp["tables"]]
    assert names == ["JobHead", "POHeader"], "the page must be FULL after the deny drop"


async def test_unlimited_scope_serves_unfiltered_results_with_no_flag_noise():
    """Today's SecurityMgr behaviour: no filter, no reachable_by_you flags —
    and the deny-list still beats UNLIMITED."""
    scope = AuthzScope.unlimited("u@x", "SecurityMgr", security_mgr=True)
    tools, _ = _register(scope, mode="gate")
    resp = await tools["epicor_tables"](query="purchasing")
    names = [t["name"] for t in resp["tables"]]
    assert "POHeader" in names and "PartTran" in names
    assert "PREmpMas" not in names, "deny beats everything, including SecurityMgr"
    assert all(t["reachable_by_you"] is None for t in resp["tables"])


async def test_unavailable_scope_fails_closed_in_gate_mode_tables():
    scope = AuthzScope.unavailable("u@x", "snapshot failed: Boom")
    tools, _ = _register(scope, mode="gate")
    resp = await tools["epicor_tables"](query="purchasing")
    assert resp["success"] is False
    assert resp["error"] == "authorization_unavailable"
    assert resp["terminal"] is False, (
        "retryable — the authorizer never caches UNAVAILABLE, so the next call "
        "retries the snapshot; terminal would teach the model to give up"
    )
    assert resp["detail"]["stage"] == "authz"
    assert "tables" not in resp, "fail closed means NO results, personalised or not"
    assert resp["retry_with"] == {"query": "purchasing"}


async def test_unavailable_scope_degrades_to_no_boost_in_boost_mode():
    """Boost mode keeps its pre-gate semantics: a failure costs the boost,
    never the results."""
    scope = AuthzScope.unavailable("u@x", "snapshot failed: Boom")
    tools, _ = _register(scope, mode="boost")
    resp = await tools["epicor_tables"](query="purchasing")
    assert resp["success"] is True
    assert [t["name"] for t in resp["tables"]] == ["JobHead", "POHeader", "PartTran"]


async def test_boost_mode_reorders_but_hides_nothing():
    scope = AuthzScope.scoped("u@x", ["PartTran"], "t")
    tools, _ = _register(scope, mode="boost")
    resp = await tools["epicor_tables"](query="inventory transactions")
    names = [t["name"] for t in resp["tables"]]
    assert names[0] == "PartTran", "reachable-first is a stable re-rank"
    assert set(names) == {"JobHead", "POHeader", "PartTran"}, "boost hides nothing"
    flags = {t["name"]: t["reachable_by_you"] for t in resp["tables"]}
    assert flags == {"PartTran": True, "JobHead": False, "POHeader": False}


async def test_gate_mode_empty_result_names_the_gate_as_a_possible_cause():
    """Under the gate a miss has two causes — no match, or matches exist and
    the caller cannot reach them. The envelope must say which world it is in."""
    scope = AuthzScope.scoped("u@x", ["Warehse"], "t")  # nothing in the fake corpus
    tools, _ = _register(scope, mode="gate")
    resp = await tools["epicor_tables"](query="warehouses")
    assert resp["error"] == "no_tables_found"
    assert resp["authz"]["mode"] == "gate"


# --------------------------------------------------------------------------- #
# epicor_fields under the gate
# --------------------------------------------------------------------------- #
async def test_fields_refuses_an_out_of_scope_table_with_no_column_data():
    tools, _ = _register(_JOB_ONLY, mode="gate")
    resp = await tools["epicor_fields"](table="POHeader", query="buyer")
    assert resp["error"] == "table_not_authorized"
    assert resp["terminal"] is True
    assert resp["detail"]["stage"] == "authz"
    body = repr(resp)
    for leak in ("PONum", "BuyerID", "total_columns", "field_count"):
        assert leak not in body, f"the refusal leaked {leak}"


async def test_fields_refusal_may_suggest_reachable_alternatives():
    """The deliberate asymmetry vs ``table_access_denied``: an authz miss is
    not a schema-leak risk the way a payroll table is, and a dead-end envelope
    costs the model a wasted turn. Alternatives are scope- AND deny-filtered."""
    tools, _ = _register(_JOB_ONLY, mode="gate")
    resp = await tools["epicor_fields"](table="POHeader", query="buyer")
    assert resp["valid"]["reachable_tables"] == ["JobHead"]
    assert resp["retry_with"] == {"table": "JobHead", "query": "buyer"}


async def test_fields_alternatives_exclude_denied_tables_even_when_in_scope():
    scope = AuthzScope.scoped("u@x", ["JobHead", "PREmpMas"], "t")
    tools, _ = _register(scope, mode="gate")
    resp = await tools["epicor_fields"](table="POHeader")
    assert resp["error"] == "table_not_authorized"
    assert "PREmpMas" not in repr(resp), (
        "a suggested table epicor_query refuses is a dead round trip — and "
        "naming a payroll table at all is the leak the deny-list exists for"
    )


async def test_the_denylist_beats_authz_on_the_same_table():
    """A denied table keeps its stronger, identity-independent refusal —
    never downgraded to an authz message — whether it is in scope or not.
    ``calls == []`` pins the ordering: the deny check precedes the snapshot."""
    for tables in (["JobHead"], ["JobHead", "PREmpMas"]):
        scope = AuthzScope.scoped("u@x", tables, "t")
        tools, auth = _register(scope, mode="gate")
        resp = await tools["epicor_fields"](table="PREmpMas", query="pay")
        assert resp["error"] == "table_access_denied"
        assert resp["terminal"] is True
        assert auth.calls == [], "the deny refusal must not depend on identity"


async def test_fields_mixed_request_serves_the_allowed_and_names_the_refusal():
    """PINNED DECISION: a mixed request serves the in-scope tables ALONGSIDE a
    named refusal of the blocked subset. Wholesale refusal would cost a whole
    extra turn to re-request columns the caller is entitled to; silent
    narrowing is the silent-drop class this repo bans."""
    tools, _ = _register(_JOB_ONLY, mode="gate")
    resp = await tools["epicor_fields"](table=["JobHead", "POHeader"], query="numbers")
    assert resp["success"] is True
    assert [b["name"] for b in resp["tables"]] == ["JobHead"]
    na = resp["not_authorized"]
    assert na["error"] == "table_not_authorized"
    assert na["tables"] == ["POHeader"]
    assert na["terminal"] is True
    body = repr(resp)
    assert "JobNum" in body, "the allowed table's columns ARE served"
    assert "PONum" not in body and "BuyerID" not in body, "no blocked columns leak"


async def test_fields_unavailable_scope_fails_closed():
    scope = AuthzScope.unavailable("u@x", "no identity supplied")
    tools, _ = _register(scope, mode="gate")
    resp = await tools["epicor_fields"](table="JobHead")
    assert resp["error"] == "authorization_unavailable"
    assert resp["terminal"] is False
    assert "JobNum" not in repr(resp), "fail closed serves no columns"


async def test_unlimited_scope_serves_fields_ungated():
    scope = AuthzScope.unlimited("u@x", "SecurityMgr", security_mgr=True)
    tools, _ = _register(scope, mode="gate")
    resp = await tools["epicor_fields"](table="POHeader")
    assert resp["success"] is True
    assert "not_authorized" not in resp


async def test_boost_mode_fields_is_untouched_and_fetches_no_scope():
    """Byte-identical AND call-identical: the fields path never consults the
    snapshot outside gate mode — the contract keeps boost exactly pre-gate."""
    tools, auth = _register(_JOB_ONLY, mode="boost")
    resp = await tools["epicor_fields"](table="POHeader", query="buyer")
    assert resp["success"] is True
    assert "PONum" in repr(resp)
    assert "not_authorized" not in resp
    assert auth.calls == []


# --------------------------------------------------------------------------- #
# Suggestion channels stay inside the scope
# --------------------------------------------------------------------------- #
async def test_unknown_table_suggestions_stay_inside_the_scope():
    tools, _ = _register(_JOB_ONLY, mode="gate")
    resp = await tools["epicor_fields"](table="NoSuchTable")
    assert resp["error"] == "unknown_table"
    assert resp["valid"]["closest_tables"] == ["JobHead"]
    assert resp["retry_with"]["table"] == "JobHead"


async def test_elsewhere_channels_never_name_an_out_of_scope_table():
    """Both `elsewhere` legs point at OTHER tables, so both are a second way to
    surface one the gate will refuse."""
    tools, _ = _register(_JOB_ONLY, mode="gate")
    resp = await tools["epicor_fields"](table="JobHead", query="ScrapQty")
    assert resp["success"] is True
    assert "PartTran" not in repr(resp.get("elsewhere", ""))


async def test_elsewhere_still_fires_for_an_in_scope_owner():
    """The filter must be a scope test, not a channel kill switch."""
    scope = AuthzScope.scoped("u@x", ["JobHead", "PartTran"], "t")
    tools, _ = _register(scope, mode="gate")
    resp = await tools["epicor_fields"](table="JobHead", query="ScrapQty")
    assert "PartTran" in repr(resp["elsewhere"])


# --------------------------------------------------------------------------- #
# {}-safety: the guard envelopes precede the scope fetch entirely
# --------------------------------------------------------------------------- #
async def test_missing_query_envelope_unchanged_and_costs_no_snapshot():
    tools, auth = _register(_JOB_ONLY, mode="gate")
    resp = await tools["epicor_tables"](query="")
    assert resp["error"] == "missing_query"
    assert auth.calls == [], "the guard envelope must precede the scope fetch"


async def test_missing_table_envelope_unchanged_and_costs_no_snapshot():
    tools, auth = _register(_JOB_ONLY, mode="gate")
    resp = await tools["epicor_fields"](table="")
    assert resp["error"] == "missing_table"
    assert auth.calls == []

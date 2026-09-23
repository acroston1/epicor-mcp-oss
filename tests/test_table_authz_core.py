"""The table-authz core: three-state scope, session-pinned cache, default deny.

Every test here is mock-only — no live Epicor, no index files. The contract
under test is the one recorded in ``discovery/authz.py``'s docstring: gate
default, session-pinned cache with UNAVAILABLE never cached, the three-state
split that makes a snapshot failure impossible to confuse with SecurityMgr, and
DEFAULT DENY: a SCOPED user's tables are EXACTLY the menu-chain projection,
nothing unioned in; a successful zero-service snapshot is a cached SCOPED answer
with an EMPTY table set; a table no menu maps is reachable only by UNLIMITED
(SecurityMgr). There is no curated baseline of tables granted to everyone.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
from pathlib import Path

import pytest

from epicor_mcp.discovery.authz import (
    AuthzScope,
    ScopeState,
    TABLE_AUTHZ_MODES,
    TableAuthorizer,
    normalize_table,
)
from epicor_mcp.discovery.tools import register_discovery_tools
from epicor_mcp.sql.denylist import is_denied_column, is_denied_table
from tests.test_query_table_gate import runtime_for
from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

REPO = Path(__file__).resolve().parents[1]
CATALOGUE = REPO / "data" / "schema_catalogue.json"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# mocks
# --------------------------------------------------------------------------- #
class _Snap:
    def __init__(self, *, is_error=False, error="", security_mgr=False,
                 allow_all=False, services=()):
        self.is_error = is_error
        self.error = error
        self.security_mgr = security_mgr
        self.allow_all = allow_all
        self.allowed_services = tuple(services)


class _MenuAuthorizer:
    """Scriptable ensure_snapshot: a list of outcomes consumed one per call.

    An outcome that is an Exception instance is raised; anything else is
    returned. The LAST outcome repeats forever, and ``calls`` counts every
    invocation — that counter is what proves the session pin.
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def ensure_snapshot(self, email):
        self.calls += 1
        out = self.outcomes[0] if len(self.outcomes) == 1 else self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class _ServiceIndex:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def get_entity_sets(self, sid):
        return self.mapping.get(sid, [])


def _authorizer(menu, index=None, *, mode="gate", dev_identity=""):
    return TableAuthorizer(
        menu, index if index is not None else _ServiceIndex(),
        mode=mode, dev_identity=dev_identity,
    )


_GOOD_SNAP = _Snap(services=["Erp.BO.CustomerSvc"])
_GOOD_INDEX = _ServiceIndex({"Erp.BO.CustomerSvc": ["Customers", "CustCnt"]})


# --------------------------------------------------------------------------- #
# the three-state split — the single most load-bearing change
# --------------------------------------------------------------------------- #
def test_a_snapshot_failure_is_UNAVAILABLE_not_unlimited():
    auth = _authorizer(_MenuAuthorizer(RuntimeError("boom")), _GOOD_INDEX)
    scope = _run(auth.scope_for("a@example.org"))
    assert scope.state is ScopeState.UNAVAILABLE
    assert scope.is_unavailable and not scope.is_unlimited and not scope.active
    assert scope.tables is None
    # The failure must fail CLOSED through allows() too, even if a consumer
    # forgets to branch on the state first.
    assert scope.allows("Erp.JobHead") is False
    assert scope.allows("jobhead") is False


def test_securitymgr_is_UNLIMITED_and_cannot_be_confused_with_a_failure():
    auth = _authorizer(_MenuAuthorizer(_Snap(security_mgr=True)), _GOOD_INDEX)
    scope = _run(auth.scope_for("owner@example.org"))
    assert scope.state is ScopeState.UNLIMITED
    assert scope.security_mgr is True
    assert scope.tables is None and not scope.active
    assert scope.allows("Erp.AnythingAtAll") is True
    # The old API answered `tables is None` for BOTH this and a failure. The
    # states must now differ — this assertion is the whole point of the rewrite.
    fail = _run(
        _authorizer(_MenuAuthorizer(RuntimeError("x")), _GOOD_INDEX).scope_for("b@c")
    )
    assert scope.state is not fail.state


def test_allow_all_snapshot_is_also_UNLIMITED():
    auth = _authorizer(_MenuAuthorizer(_Snap(allow_all=True)), _GOOD_INDEX)
    scope = _run(auth.scope_for("x@example.org"))
    assert scope.state is ScopeState.UNLIMITED


def test_the_illegal_state_combinations_are_unrepresentable():
    with pytest.raises(ValueError):
        AuthzScope("a@b", ScopeState.SCOPED, None, "scoped with no tables")
    with pytest.raises(ValueError):
        AuthzScope("a@b", ScopeState.UNLIMITED, frozenset({"jobhead"}), "unlimited with tables")
    with pytest.raises(ValueError):
        AuthzScope("a@b", ScopeState.UNAVAILABLE, frozenset(), "unavailable with a set")


# --------------------------------------------------------------------------- #
# cache: session-pinned success, never-pinned failure
# --------------------------------------------------------------------------- #
def test_a_success_is_pinned_forever_flipping_the_mock_changes_nothing():
    menu = _MenuAuthorizer(_GOOD_SNAP, RuntimeError("would fail now"))
    auth = _authorizer(menu, _GOOD_INDEX)
    first = _run(auth.scope_for("a@example.org"))
    second = _run(auth.scope_for("a@example.org"))
    assert first.state is ScopeState.SCOPED
    assert second is first          # the pinned object, not a recomputation
    assert menu.calls == 1          # the flipped mock was never consulted
    # Case-insensitive pin: the same person under a different spelling.
    third = _run(auth.scope_for("A@EXAMPLE.ORG"))
    assert third is first
    assert menu.calls == 1


def test_a_failure_is_never_cached_and_the_next_request_retries():
    menu = _MenuAuthorizer(RuntimeError("transient"), _GOOD_SNAP)
    auth = _authorizer(menu, _GOOD_INDEX)
    first = _run(auth.scope_for("a@example.org"))
    assert first.is_unavailable
    second = _run(auth.scope_for("a@example.org"))
    assert second.state is ScopeState.SCOPED   # the retry succeeded
    assert menu.calls == 2


def test_an_error_snapshot_is_UNAVAILABLE_and_also_not_cached():
    menu = _MenuAuthorizer(_Snap(is_error=True, error="ldap down"), _GOOD_SNAP)
    auth = _authorizer(menu, _GOOD_INDEX)
    first = _run(auth.scope_for("a@example.org"))
    assert first.is_unavailable and "ldap down" in first.reason
    second = _run(auth.scope_for("a@example.org"))
    assert second.state is ScopeState.SCOPED
    assert menu.calls == 2


def test_evict_then_recompute():
    # Two DISJOINT menu projections (FiscalYear/UDCode), so each scope's
    # membership discriminates between the pin and the recomputation.
    menu = _MenuAuthorizer(
        _Snap(services=["Erp.BO.FiscalYearSvc"]),
        _Snap(services=["Ice.BO.UDCodeSvc"]),
    )
    index = _ServiceIndex(
        {"Erp.BO.FiscalYearSvc": ["FiscalYears"], "Ice.BO.UDCodeSvc": ["UDCodes"]}
    )
    auth = _authorizer(menu, index)
    first = _run(auth.scope_for("a@example.org"))
    assert first.allows("FiscalYear") and not first.allows("UDCode")
    assert auth.evict("A@EXAMPLE.ORG") is True     # case-insensitive
    assert auth.evict("a@example.org") is False    # honest no-op report
    second = _run(auth.scope_for("a@example.org"))
    assert second is not first
    assert second.allows("UDCode") and not second.allows("FiscalYear")
    assert menu.calls == 2


def test_evict_all_drops_every_pin_and_reports_the_count():
    menu = _MenuAuthorizer(_GOOD_SNAP)
    auth = _authorizer(menu, _GOOD_INDEX)
    _run(auth.scope_for("a@example.org"))
    _run(auth.scope_for("b@example.org"))
    assert auth.evict_all() == 2
    _run(auth.scope_for("a@example.org"))
    assert menu.calls == 3


# --------------------------------------------------------------------------- #
# default deny — the scope is EXACTLY the menu projection
# --------------------------------------------------------------------------- #
# Tables no Epicor menu maps to a business object, plus a sample of menu-mappable
# tables that a "grant everyone a small floor" policy would typically include.
# None of them may appear in a scope unless the user's own menus project them.
_UNMAPPABLE = ("PartMtl", "PartOpr", "PartOpDtl", "SugPOChg")
_FORMER_BASELINE_MAPPABLE = ("APInvHed", "CheckHed", "InvcHead", "JobHead", "Part",
                             "LaborDtl", "PartRev")


def test_a_SCOPED_result_is_exactly_the_menu_projection_nothing_unioned():
    auth = _authorizer(_MenuAuthorizer(_GOOD_SNAP), _GOOD_INDEX)
    scope = _run(auth.scope_for("a@example.org"))
    assert scope.state is ScopeState.SCOPED
    # Customers -> {customers, customer}; CustCnt -> {custcnt}. Nothing else.
    assert scope.tables == frozenset({"customers", "customer", "custcnt"})
    assert scope.allows("Customers") and scope.allows("Erp.Customer")
    assert scope.reason == "menu chain"
    for name in _UNMAPPABLE + _FORMER_BASELINE_MAPPABLE:
        assert not scope.allows(name), f"{name} leaked into a menu-only scope"
        assert not scope.allows(f"Erp.{name}"), name
    assert "baseline" not in scope.reason.lower()
    assert "baseline" not in scope.note("gate").lower()
    assert "baseline" not in scope.note().lower()


def test_zero_services_is_a_cached_SCOPED_EMPTY_scope_not_a_failure():
    """A successful snapshot granting zero services is an ANSWER — SCOPED with
    an empty table set, pinned — not UNAVAILABLE (which would be retried every
    call) and not a floor of anything."""
    menu = _MenuAuthorizer(_Snap(services=[]))
    auth = _authorizer(menu, _GOOD_INDEX)
    scope = _run(auth.scope_for("a@example.org"))
    assert scope.state is ScopeState.SCOPED
    assert not scope.is_unavailable and not scope.is_unlimited
    assert scope.active is True
    assert scope.tables == frozenset()
    assert scope.reason == "menu chain resolved zero services — no tables"
    for name in ("Part", "Erp.Part", "PartMtl", "Erp.PartMtl", "Part_UD",
                 "JobHead", "Customer", "APInvHed"):
        assert scope.allows(name) is False, name
    # A successful empty snapshot is an ANSWER, so it pins:
    again = _run(auth.scope_for("a@example.org"))
    assert again is scope and menu.calls == 1


def test_services_that_project_to_zero_tables_is_UNAVAILABLE_not_a_tiny_pin():
    # The index knows nothing about the granted service — an index anomaly,
    # not a statement about the user. Must fail closed AND retry next call.
    menu = _MenuAuthorizer(_Snap(services=["Erp.BO.GhostSvc"]))
    auth = _authorizer(menu, _ServiceIndex({}))
    scope = _run(auth.scope_for("a@example.org"))
    assert scope.is_unavailable
    _run(auth.scope_for("a@example.org"))
    assert menu.calls == 2


def test_UNLIMITED_has_no_table_set_and_reaches_the_unmappable_tables():
    auth = _authorizer(_MenuAuthorizer(_Snap(security_mgr=True)), _GOOD_INDEX)
    scope = _run(auth.scope_for("owner@example.org"))
    assert scope.tables is None
    for name in _UNMAPPABLE:
        assert scope.allows(name) and scope.allows(f"Erp.{name}"), name


def test_the_curated_baseline_module_is_gone():
    """The curated table baseline was deleted; nothing may re-export it."""
    import epicor_mcp.discovery as discovery

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("epicor_mcp.discovery.baseline")
    assert "BASELINE_TABLES" not in discovery.__all__
    assert not hasattr(discovery, "BASELINE_TABLES")
    import epicor_mcp.discovery.authz as authz_mod
    assert not hasattr(authz_mod, "BASELINE_TABLES")


# --------------------------------------------------------------------------- #
# allows() — the one normalisation implementation
# --------------------------------------------------------------------------- #
def test_allows_normalises_every_spelling_to_the_same_bare_lowercase_name():
    scope = AuthzScope.scoped("a@b", {"jobhead"}, "test")
    for spelling in ("jobhead", "JobHead", "JOBHEAD", "Erp.JobHead",
                     "ERP.JOBHEAD", "erp.jobhead", " Erp.JobHead ",
                     "[Erp].[JobHead]"):
        assert scope.allows(spelling), spelling
    assert not scope.allows("Erp.PREmpMas")
    assert not scope.allows("")


def test_scoped_constructor_normalises_stored_names_too():
    # Membership is normalised on BOTH sides, through the same function —
    # nobody has to pre-lowercase before building a scope.
    scope = AuthzScope.scoped("a@b", {"Erp.JobHead", "PARTTRAN"}, "test")
    assert scope.tables == frozenset({"jobhead", "parttran"})
    assert scope.allows("JobHead") and scope.allows("Erp.PartTran")


def test_normalize_table_is_the_documented_mapping():
    assert normalize_table("Erp.JobHead") == "jobhead"
    assert normalize_table("[Erp].[JobHead]") == "jobhead"
    assert normalize_table("  JOBHEAD  ") == "jobhead"
    assert normalize_table("") == ""


# --------------------------------------------------------------------------- #
# none-mode whitelist is independent of the menu-derived scope
# --------------------------------------------------------------------------- #
def test_empty_oss_whitelist_grants_none_of_the_menu_tables(tmp_path):
    """An empty `none`-mode whitelist grants nothing — not the unmappable
    tables, not any commonly-granted menu table."""
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    path = tmp_path/'none.txt'; path.write_text('')
    scope = TableWhitelist.from_file(path)
    for name in _UNMAPPABLE + _FORMER_BASELINE_MAPPABLE:
        assert not scope.allows(f'Erp.{name}'), name
        assert not scope.allows(name), name


# --------------------------------------------------------------------------- #
# identity + modes
# --------------------------------------------------------------------------- #
def test_empty_identity_is_UNAVAILABLE_fail_closed():
    auth = _authorizer(_MenuAuthorizer(_GOOD_SNAP), _GOOD_INDEX)
    scope = _run(auth.scope_for(""))
    assert scope.is_unavailable
    assert scope.allows("Erp.JobHead") is False


def test_missing_menu_authorizer_or_index_is_UNAVAILABLE():
    scope = _run(
        TableAuthorizer(None, _GOOD_INDEX, mode="gate").scope_for("a@b")
    )
    assert scope.is_unavailable
    scope = _run(
        TableAuthorizer(_MenuAuthorizer(_GOOD_SNAP), None, mode="gate").scope_for("a@b")
    )
    assert scope.is_unavailable


def test_mode_off_is_UNLIMITED_the_kill_switch_must_not_deny_anyone():
    # DOCUMENTED CHOICE: `off` returns UNLIMITED, not UNAVAILABLE. `off` is the
    # rollback value, and rollback must degrade to no gating even in a consumer
    # that branches only on the scope state — UNAVAILABLE here would turn the
    # kill switch into a deny-everyone switch.
    menu = _MenuAuthorizer(RuntimeError("never called"))
    auth = _authorizer(menu, _GOOD_INDEX, mode="off")
    scope = _run(auth.scope_for("a@example.org"))
    assert scope.state is ScopeState.UNLIMITED
    assert "off" in scope.reason
    assert scope.allows("Erp.Anything") is True
    assert menu.calls == 0   # off never touches the chain


def test_mode_defaults_to_gate_and_an_invalid_mode_fails_closed_to_gate():
    assert TableAuthorizer(None, None).mode == "gate"
    assert TableAuthorizer(None, None, mode="bogus").mode == "gate"
    for m in TABLE_AUTHZ_MODES:
        assert TableAuthorizer(None, None, mode=m).mode == m


def test_resolve_identity_precedence_is_dev_then_session_and_nothing_else():
    """`for_email` — which used to be
    the FIRST precedence tier — is gone from the signature entirely. In gate
    mode a caller-supplied identity outranked the session, so any caller could
    pick whose authorization applied to them: identity spoofing. Identity now
    comes only from server-side config (dev identity) or the session."""
    import inspect

    auth = _authorizer(_MenuAuthorizer(_GOOD_SNAP), dev_identity="dev@example.org")
    assert "for_email" not in inspect.signature(auth.resolve_identity).parameters
    assert auth.resolve_identity("sess@example.net") == "dev@example.org"
    no_dev = _authorizer(_MenuAuthorizer(_GOOD_SNAP))
    assert no_dev.resolve_identity("sess@example.net") == "sess@example.net"
    assert no_dev.resolve_identity("") == ""


# --------------------------------------------------------------------------- #
# transition compatibility: what discovery/tools.py reads TODAY keeps working
# --------------------------------------------------------------------------- #
def test_todays_tools_py_attributes_survive_the_rewrite():
    scoped = AuthzScope.scoped("a@b", {"jobhead"}, "menu chain", service_count=3)
    assert scoped.active is True
    assert "jobhead" in scoped.tables
    assert isinstance(scoped.note(), str)          # zero-arg call stays valid
    assert isinstance(scoped.note("gate"), str)
    assert isinstance(scoped.note("boost"), str)

    for inactive in (
        AuthzScope.unlimited("a@b", "SecurityMgr", security_mgr=True),
        AuthzScope.unavailable("a@b", "snapshot failed: RuntimeError"),
    ):
        # tools.py treats both as "no personalisation" until it grows its own
        # UNAVAILABLE refusal branch — the pre-gate behaviour, preserved.
        assert inactive.active is False
        assert inactive.tables is None
        assert isinstance(inactive.note(), str)


# --------------------------------------------------------------------------- #
# audit O1 — the request-path wait on a snapshot build is BOUNDED
# --------------------------------------------------------------------------- #

class _HangingMenuAuthorizer:
    """ensure_snapshot that never completes — a cold scoped user's tenant
    crawl, as seen from the request path."""

    def __init__(self):
        self.calls = 0
        self._never = asyncio.Event()

    async def ensure_snapshot(self, email):
        self.calls += 1
        await self._never.wait()


@pytest.mark.asyncio
async def test_a_hung_snapshot_build_yields_a_bounded_retryable_unavailable():
    """An unbounded snapshot wait can cause a transport timeout.
    The bounded wait returns the retryable
    authorization_unavailable envelope while the (shielded) build continues;
    the shield half is pinned in test_authz_cold_start.py."""
    menu = _HangingMenuAuthorizer()
    auth = _authorizer(menu, _GOOD_INDEX)
    auth._snapshot_wait_s = 0.05  # keep the suite fast; floor is 1 s in prod

    scope = await auth.scope_for("cold@example.org")
    assert scope.is_unavailable
    assert "still building" in scope.reason

    # NOT cached: the next call goes back to the (still-hung) build rather
    # than freezing the timeout into a permanent refusal.
    scope2 = await auth.scope_for("cold@example.org")
    assert scope2.is_unavailable
    assert menu.calls == 2


# --------------------------------------------------------------------------- #
# default deny END TO END — the REAL TableAuthorizer projection driven through
# the ad-hoc SQL seam (WedgeRuntime.run) and epicor_tables. Nothing here hands
# the consumer a pre-built scope: the scope comes from the menu snapshot, so a
# re-introduced union anywhere in the chain turns these red.
# --------------------------------------------------------------------------- #
_E2E_INDEX = _ServiceIndex({
    "Erp.BO.PartSvc": ["Parts", "PartRevs"],          # -> part, partrev (no partmtl)
    "Erp.BO.APInvoiceSvc": ["APInvHeds", "APInvDtls"],
})
_PART_MENU_USER = "scoped@example.org"


def _real_authorizer(snap, *, email=_PART_MENU_USER):
    return TableAuthorizer(_MenuAuthorizer(snap), _E2E_INDEX, mode="gate",
                           dev_identity=email)


def _ds_reading(table: str, column: str) -> tuple[str, dict]:
    """A statement reading ONE Erp table, DERIVED from the captured `clean_top`
    ParseFromSQL fixture (Erp.Part) by renaming the table/column in place — the
    DS shape stays Epicor's own, only the names change."""
    _, ds = load("clean_top")
    ds = copy.deepcopy(ds)
    for row in ds["QueryTable"]:
        row["DBTableName"] = table
    for row in ds["QueryField"]:
        row["DBTableName"] = table
        row["DBFieldName"] = row["FieldName"] = column
    sql = f"select top 5 [P].[{column}] as [PN] from Erp.{table} as [P]"
    return sql, ds


def _query(authorizer, table: str, column: str) -> tuple[dict, MockEpicorClient]:
    sql, ds = _ds_reading(table, column)
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([{"PN": "x"}]))
    out = _run(runtime_for(client, authorizer=authorizer).run(sql=sql))
    return out, client


class _Hit:
    def __init__(self, table: str, full: str) -> None:
        self.table, self.full_name = table, full
        self.description, self.field_count, self.score = "", 1, 0.9


class _TablesIndex:
    """Duck-typed DiscoveryIndex honouring the real store's `allowed=` contract
    (`is not None` hard filter on bare-lowercase names — store.search_tables)."""

    _TABLES = {"Part": "Erp.Part", "PartMtl": "Erp.PartMtl",
               "APInvHed": "Erp.APInvHed", "JobHead": "Erp.JobHead"}
    manifest = {"table_count": 4, "field_count": 4}

    def search_tables(self, vec, q, limit=5, allowed=None):
        return [
            _Hit(n, f) for n, f in self._TABLES.items()
            if allowed is None or n.lower() in allowed
        ][:limit]

    def search_fields(self, table, vec, q, limit=6):
        return []

    def fields_of(self, table):
        return [{"name": "PartNum"}]

    def resolve_table(self, name):
        return {k.lower(): k for k in self._TABLES}.get(str(name).strip().lower())

    def table_info(self, canon):
        return {"full_name": self._TABLES[canon], "field_count": 1, "description": ""}

    def find_column_elsewhere(self, core):
        return [], 0

    def name_matches_elsewhere(self, q, here):
        return []


async def _no_embed(text, prefix):
    return None


def _listed_tables(authorizer) -> tuple[list[str], dict]:
    registered: dict = {}

    class _MCP:
        def tool(self, *, name, description):
            def deco(fn):
                registered[name] = fn
                return fn
            return deco

    register_discovery_tools(
        _MCP(), _TablesIndex(), embed_query=_no_embed, authorizer=authorizer,
        denied_column=is_denied_column, denied_table=is_denied_table,
    )
    resp = _run(registered["epicor_tables"](query="part materials", limit=25))
    return [t["table"] for t in resp.get("tables") or []], resp


def test_default_deny_a_scoped_user_without_PartMtl_in_menus_is_refused_ad_hoc_SQL():
    auth = _real_authorizer(_Snap(services=["Erp.BO.PartSvc"]))
    out, client = _query(auth, "PartMtl", "PartNum")
    assert out["success"] is False
    assert out["error"] == "table_not_authorized"
    assert out["terminal"] is True
    assert out["detail"]["stage"] == "authz"
    assert out["detail"]["unauthorized_tables"] == ["Erp.PartMtl"]
    assert not client.called("Execute"), "a refused table must never execute"
    # Positive control — the same user, a menu-mapped table, same DS shape:
    ok, ok_client = _query(auth, "Part", "PartNum")
    assert ok["success"] is True, ok
    assert ok_client.called("Execute")


def test_default_deny_PartMtl_is_absent_from_epicor_tables_for_that_user():
    names, _ = _listed_tables(_real_authorizer(_Snap(services=["Erp.BO.PartSvc"])))
    assert "Erp.Part" in names, "positive control: the menu table is listed"
    assert "Erp.PartMtl" not in names
    assert "Erp.APInvHed" not in names and "Erp.JobHead" not in names


def test_default_deny_a_commonly_granted_table_outside_the_users_menus_is_refused():
    """APInvHed is menu-mappable but must follow the menus: refused for a
    Part-menu user, reachable for an AP-menu user (it is per-user, not globally
    blocked)."""
    out, client = _query(_real_authorizer(_Snap(services=["Erp.BO.PartSvc"])),
                         "APInvHed", "InvoiceNum")
    assert out["error"] == "table_not_authorized"
    assert out["detail"]["unauthorized_tables"] == ["Erp.APInvHed"]
    assert not client.called("Execute")

    ap_user = _real_authorizer(_Snap(services=["Erp.BO.APInvoiceSvc"]),
                               email="ap@example.org")
    ok, ok_client = _query(ap_user, "APInvHed", "InvoiceNum")
    assert ok["success"] is True, ok
    assert ok_client.called("Execute")


def test_default_deny_a_zero_service_user_is_refused_everything_and_lists_nothing():
    auth = _real_authorizer(_Snap(services=[]), email="nomenus@example.org")
    for table in ("Part", "PartMtl"):
        out, client = _query(auth, table, "PartNum")
        assert out["error"] == "table_not_authorized", (table, out)
        assert out["terminal"] is True, "an empty SCOPED answer is terminal, not retryable"
        assert not client.called("Execute")
    names, resp = _listed_tables(auth)
    assert names == [], f"a zero-service user was served tables: {names}"
    assert "authorization_unavailable" not in json.dumps(resp, default=str)


def test_default_deny_UNLIMITED_securitymgr_still_reaches_PartMtl():
    auth = _real_authorizer(_Snap(security_mgr=True), email="owner@example.org")
    out, client = _query(auth, "PartMtl", "PartNum")
    assert out["success"] is True, out
    assert client.called("Execute")
    names, _ = _listed_tables(auth)
    assert "Erp.PartMtl" in names


def test_default_deny_the_refusal_policy_text_never_mentions_a_baseline():
    out, _ = _query(_real_authorizer(_Snap(services=["Erp.BO.PartSvc"])),
                    "PartMtl", "PartNum")
    assert out["error"] == "table_not_authorized"
    policy = out["valid"]["policy"]
    assert policy.strip(), "the refusal must state its policy"
    assert "baseline" not in policy.lower()
    assert "baseline" not in out["message"].lower()
    assert "baseline" not in json.dumps(out, default=str).lower()

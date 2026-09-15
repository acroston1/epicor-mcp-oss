"""The table-authz core: three-state scope, session-pinned cache, baseline union.

Every test here is mock-only — no live Epicor, no index files. The contract
under test is the one recorded in
``discovery/authz.py``'s docstring: gate default, curated baseline gap policy,
session-pinned cache with UNAVAILABLE never cached, and the three-state split
that makes a snapshot failure impossible to confuse with SecurityMgr.
"""

from __future__ import annotations

import asyncio
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
from epicor_mcp.discovery.baseline import BASELINE_TABLES
from epicor_mcp.sql.denylist import is_denied_table

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
    # Deliberately NON-baseline entities (FiscalYear/UDCode): a baseline table
    # like Customer would be allowed in BOTH scopes and prove nothing.
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
# baseline union
# --------------------------------------------------------------------------- #
def test_every_SCOPED_result_carries_the_baseline_union():
    auth = _authorizer(_MenuAuthorizer(_GOOD_SNAP), _GOOD_INDEX)
    scope = _run(auth.scope_for("a@example.org"))
    assert scope.state is ScopeState.SCOPED
    assert BASELINE_TABLES <= scope.tables
    # The gap trio the menu chain can never reach:
    assert scope.allows("Erp.PartMtl")
    assert scope.allows("PartOpr")
    assert scope.allows("SugPOChg")
    # And the chain's own contribution survives alongside it:
    assert scope.allows("Customers") and scope.allows("Customer")


def test_zero_services_is_a_cached_SCOPED_baseline_floor_not_a_failure():
    menu = _MenuAuthorizer(_Snap(services=[]))
    auth = _authorizer(menu, _GOOD_INDEX)
    scope = _run(auth.scope_for("a@example.org"))
    assert scope.state is ScopeState.SCOPED
    assert scope.tables == BASELINE_TABLES
    assert "baseline" in scope.reason
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


def test_UNLIMITED_has_no_tables_the_baseline_concept_does_not_apply():
    auth = _authorizer(_MenuAuthorizer(_Snap(security_mgr=True)), _GOOD_INDEX)
    scope = _run(auth.scope_for("owner@example.org"))
    assert scope.tables is None


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
# baseline content
# --------------------------------------------------------------------------- #
def test_baseline_is_bare_lowercase_and_deny_beats_baseline():
    assert BASELINE_TABLES, "baseline must not be empty"
    for name in BASELINE_TABLES:
        assert name == name.lower() and "." not in name, name
        # Deny beats everything including the baseline: nothing here may be a
        # table the deny-list refuses, under either spelling.
        assert not is_denied_table(name), name
        assert not is_denied_table(f"Erp.{name}"), name
    # A deny-listed table can never be argued back in through the baseline:
    for denied in ("prempmas", "userfile", "usercomp", "payrollexp"):
        assert denied not in BASELINE_TABLES


def test_baseline_carries_the_measured_gap_and_the_card():
    for required in ("partmtl", "partopr", "sugpochg",   # unreachable-by-anybody
                     "jobhead", "part", "labordtl",       # the card
                     "partrev", "partopdtl"):             # judgement additions
        assert required in BASELINE_TABLES, required


def test_empty_oss_whitelist_never_inherits_the_microsoft_baseline(tmp_path):
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    path = tmp_path/'none.txt'; path.write_text('')
    scope = TableWhitelist.from_file(path)
    for name in BASELINE_TABLES:
        assert not scope.allows(f'Erp.{name}')


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

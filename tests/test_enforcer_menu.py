"""RBACEnforcer mode switch (off / shadow / enforce).

The enforcer stays synchronous and network-free: it reads a cached
``UserAuthzSnapshot`` from an injected authorizer via the sync ``get_snapshot``
hot path.  This suite pins:

  * ``AccessCheckResult{allowed, message, api_key}`` is preserved on every path.
  * ``mode="off"``   => byte-compatible legacy department behavior; the
    authorizer is ignored entirely.
  * ``mode="enforce"`` => the menu decision is authoritative, the empty-department
    allow-all fallback is unreachable, normalization + the
    wrong-service-name guidance survive, SecurityMgr bypasses, and a missing
    snapshot fails closed.
  * ``mode="shadow"`` => the returned result is the LEGACY decision, but a
    divergence is recorded whenever legacy and menu disagree (including
    allow-all-fallback hits).
  * ``check_method_access`` / ``check_baq_access`` keep their orthogonal tiers on
    top of whatever service decision the mode produces.

All seams (index, user_map, authorizer, snapshots) are injected.
"""

from __future__ import annotations

import pytest

from epicor_mcp.context import clear_authz_decision, get_authz_decision
from epicor_mcp.rbac.enforcer import AccessCheckResult, AccessLevel, RBACEnforcer

from fixtures.authz.fakes import (
    FakeAuthorizer,
    FakeIndex,
    FakeSnapshot,
    FakeUserMap,
    profile,
)

LABOR = "Erp.BO.LaborSvc"
APINV = "Erp.BO.APInvoiceSvc"


def _enforcer(mode, *, users, dept_services=None, existing=(LABOR, APINV),
              snapshots=None, record_divergence=None):
    index = FakeIndex(dept_services=dept_services or {}, existing_services=existing)
    user_map = FakeUserMap(users)
    authorizer = FakeAuthorizer(snapshots or {})
    return RBACEnforcer(
        index,
        user_map,
        authorizer=authorizer,
        mode=mode,
        record_divergence=record_divergence,
    )


# ====================================================================== #
# Result contract
# ====================================================================== #

def test_result_shape_preserved():
    enf = _enforcer(
        "off",
        users={"u@example.com": profile("u", department="Finance")},
        dept_services={"Finance": {LABOR}},
    )
    res = enf.check_service_access("u@example.com", LABOR)
    assert isinstance(res, AccessCheckResult)
    assert res.allowed is True
    assert isinstance(res.message, str) and res.message
    assert res.api_key == FakeUserMap.READ_KEY


# ====================================================================== #
# mode = off  (legacy department behavior, authorizer ignored)
# ====================================================================== #

def test_off_mode_allows_via_department():
    enf = _enforcer(
        "off",
        users={"u@example.com": profile("u", department="Finance")},
        dept_services={"Finance": {LABOR}},
        snapshots={"u": FakeSnapshot(allowed=[])},  # would deny — must be ignored
    )
    res = enf.check_service_access("u@example.com", LABOR)
    assert res.allowed is True
    assert "Finance" in res.message


def test_off_mode_denies_service_outside_department():
    enf = _enforcer(
        "off",
        users={"u@example.com": profile("u", department="Finance")},
        dept_services={"Finance": {LABOR}},
    )
    res = enf.check_service_access("u@example.com", APINV)
    assert res.allowed is False


def test_off_mode_empty_department_is_allow_all_legacy():
    # Documents the legacy fallback that enforce mode kills: an empty dept list
    # grants everything.
    enf = _enforcer(
        "off",
        users={"u@example.com": profile("u", department="Ghost")},
        dept_services={"Ghost": set()},
    )
    assert enf.check_service_access("u@example.com", APINV).allowed is True


# ====================================================================== #
# mode = enforce  (menu decision authoritative)
# ====================================================================== #

def test_enforce_allows_when_snapshot_grants_service():
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance")},
        dept_services={"Finance": set()},   # legacy would allow-all; irrelevant now
        snapshots={"u": FakeSnapshot(allowed=[APINV])},
    )
    res = enf.check_service_access("u@example.com", APINV)
    assert res.allowed is True
    assert res.api_key == FakeUserMap.READ_KEY
    assert "menu" in res.message.lower()


def test_enforce_denies_when_snapshot_does_not_grant():
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance")},
        snapshots={"u": FakeSnapshot(allowed=[LABOR])},
    )
    assert enf.check_service_access("u@example.com", APINV).allowed is False


def test_enforce_empty_department_fallback_is_dead():
    # THE regression this whole feature exists to prevent: a user whose
    # department owns no service rows must be DENIED a service no menu grants.
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Ghost")},
        dept_services={"Ghost": set()},         # legacy allow-all fallback
        snapshots={"u": FakeSnapshot(allowed=[])},
    )
    assert enf.check_service_access("u@example.com", APINV).allowed is False


def test_enforce_security_mgr_bypasses():
    enf = _enforcer(
        "enforce",
        users={"a@example.com": profile("a", department="Finance")},
        snapshots={"a": FakeSnapshot(security_mgr=True)},
    )
    res = enf.check_service_access("a@example.com", "Erp.BO.AnySvc")
    assert res.allowed is True
    assert "security manager" in res.message.lower()


def test_enforce_normalizes_missing_svc_suffix():
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance")},
        snapshots={"u": FakeSnapshot(allowed=[LABOR])},
    )
    # Caller passes the bare BO name; enforcer appends "Svc" before checking.
    assert enf.check_service_access("u@example.com", "Erp.BO.Labor").allowed is True


def test_enforce_keeps_wrong_service_name_guidance():
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance")},
        existing=(LABOR,),  # RMASvc does not exist in the index
        snapshots={"u": FakeSnapshot(allowed=[LABOR])},
    )
    res = enf.check_service_access("u@example.com", "Erp.BO.RMASvc")
    assert res.allowed is False
    assert "does not exist" in res.message.lower()


def test_enforce_fails_closed_when_no_snapshot():
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance")},
        snapshots={},  # middleware never populated one
    )
    assert enf.check_service_access("u@example.com", APINV).allowed is False


def test_enforce_fails_closed_on_error_snapshot():
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance")},
        snapshots={"u": FakeSnapshot(is_error=True)},
    )
    assert enf.check_service_access("u@example.com", APINV).allowed is False


# ====================================================================== #
# mode = shadow  (enforce legacy, record divergences)
# ====================================================================== #

def test_shadow_enforces_legacy_decision():
    divs: list[dict] = []
    enf = _enforcer(
        "shadow",
        users={"u@example.com": profile("u", department="Finance")},
        dept_services={"Finance": {APINV}},          # legacy: ALLOW
        snapshots={"u": FakeSnapshot(allowed=[])},   # menu: DENY
        record_divergence=divs.append,
    )
    res = enf.check_service_access("u@example.com", APINV)
    assert res.allowed is True  # legacy decision is what ships in shadow
    assert len(divs) == 1
    d = divs[0]
    assert d["dept_allowed"] is True
    assert d["menu_allowed"] is False
    assert d["service_id"] == APINV


def test_shadow_records_allow_all_fallback_divergence():
    divs: list[dict] = []
    enf = _enforcer(
        "shadow",
        users={"u@example.com": profile("u", department="Ghost")},
        dept_services={"Ghost": set()},              # legacy allow-all fallback
        snapshots={"u": FakeSnapshot(allowed=[])},   # menu: DENY
        record_divergence=divs.append,
    )
    res = enf.check_service_access("u@example.com", APINV)
    assert res.allowed is True                       # fallback still enforced in shadow
    assert len(divs) == 1
    assert divs[0]["menu_allowed"] is False


def test_shadow_no_divergence_when_decisions_agree():
    divs: list[dict] = []
    enf = _enforcer(
        "shadow",
        users={"u@example.com": profile("u", department="Finance")},
        dept_services={"Finance": {APINV}},          # legacy: ALLOW
        snapshots={"u": FakeSnapshot(allowed=[APINV])},  # menu: ALLOW
        record_divergence=divs.append,
    )
    assert enf.check_service_access("u@example.com", APINV).allowed is True
    assert divs == []


# ====================================================================== #
# Orthogonal tiers survive the mode change
# ====================================================================== #

def test_enforce_method_tier_blocks_write_for_read_only_user():
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance",
                                  access=AccessLevel.READ_ONLY)},
        snapshots={"u": FakeSnapshot(allowed=[APINV])},
    )
    res = enf.check_method_access("u@example.com", APINV, "Update")
    assert res.allowed is False


def test_enforce_method_tier_allows_write_for_read_write_user():
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance",
                                  access=AccessLevel.READ_WRITE)},
        snapshots={"u": FakeSnapshot(allowed=[APINV])},
    )
    res = enf.check_method_access("u@example.com", APINV, "Update")
    assert res.allowed is True
    assert res.api_key == FakeUserMap.WRITE_KEY


def test_enforce_service_tier_denies_write_when_menu_denies():
    # read_write via portal, but the service is not in the user's menus ->
    # denied at the SERVICE tier before the method tier is even reached.
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance",
                                  access=AccessLevel.READ_WRITE)},
        snapshots={"u": FakeSnapshot(allowed=[LABOR])},
    )
    assert enf.check_method_access("u@example.com", APINV, "Update").allowed is False


# ====================================================================== #
# Decision-reason contextvar (feeds audit authz_source/reason)
# ====================================================================== #

def test_off_mode_sets_department_decision_contextvar():
    clear_authz_decision()
    enf = _enforcer(
        "off",
        users={"u@example.com": profile("u", department="Finance")},
        dept_services={"Finance": {LABOR}},
    )
    enf.check_service_access("u@example.com", LABOR)
    decision = get_authz_decision()
    assert decision is not None
    assert decision["source"] == "department"
    clear_authz_decision()


def test_enforce_mode_sets_menu_decision_contextvar():
    clear_authz_decision()
    enf = _enforcer(
        "enforce",
        users={"u@example.com": profile("u", department="Finance")},
        snapshots={"u": FakeSnapshot(allowed=[APINV])},
    )
    enf.check_service_access("u@example.com", APINV)
    decision = get_authz_decision()
    assert decision is not None
    assert decision["source"] == "menu"
    assert isinstance(decision["reason"], str) and decision["reason"]
    clear_authz_decision()

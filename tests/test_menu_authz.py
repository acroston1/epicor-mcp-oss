"""MenuAuthorizer: snapshot compute, TTL, stale-grace, fail-closed.

Two halves:

  * ``compute_snapshot`` — pure translation of (identity, live menu rows, live
    security rows, structural menu->service map) into a ``UserAuthzSnapshot``.
    SecurityMgr => allow-all sentinel; disabled/unknown => error snapshot;
    launchable = enabled AND has program; unconfigured SecCode = allow; grants
    carry the granting menu + SecCode + matched principals.

  * ``ensure_snapshot`` / ``get_snapshot`` — the async fetch+cache path used by
    middleware and the sync hot path used by the enforcer.  Per-user TTL 300s,
    tenant menus/security fetched once and shared, stale-grace 3600s on fetch
    failure, fail-closed past grace and for unknown/disabled users.

All seams are injected: a ``FakeAuthzClient`` (no network), a ``FakeStore`` (no
SQLite), and a ``FakeClock`` (no real time).
"""

from __future__ import annotations

import pytest

from epicor_mcp.rbac.menu_authz import MenuAuthorizer

from fixtures.authz.fakes import (
    FakeAuthzClient,
    FakeClock,
    FakeStore,
    ident,
    mrow,
    srow,
)

# --- shared tenant -------------------------------------------------------- #

MENUS = [
    mrow("AP0100", sec_code="APSEC", program="Erp.UI.APInvoiceEntry"),
    mrow("AP0200", sec_code="APSEC", program="Erp.UI.APAdjustmentEntry"),
    mrow("PO0100", sec_code="POSEC", program="Erp.UI.POEntry"),
    mrow("QA0100", sec_code="", program="Erp.UI.UnmappedEntry"),  # launchable, unmapped
    mrow("HDR0100", sec_code="", program=""),                     # not launchable (no program)
]
SECURITY = [
    srow("APSEC", entry_list="APP"),
    srow("POSEC", entry_list="PURCH"),
]


def _store(loaded: bool = True) -> FakeStore:
    return FakeStore(
        menu_services={
            "AP0100": {"Erp.BO.APInvoiceSvc"},
            "AP0200": {"Erp.BO.APAdjustmentSvc", "Erp.BO.VendorSvc"},
            "PO0100": {"Erp.BO.POSvc"},
        },
        baseline={"Ice.BO.CompanySvc"},
        loaded=loaded,
    )


def _make(clock: FakeClock, *, identities=None, store=None):
    """Return ``(authorizer, client)`` so tests can assert fetch counts on the
    injected client without reaching into MenuAuthorizer internals."""
    client = FakeAuthzClient(
        identities=identities
        or {
            "apuser@example.org": ident("apuser", groups=("APP",)),
            "enguser@example.org": ident("enguser", groups=("ENG",)),
            "adminuser@example.org": ident(
                "adminuser", groups=("APP", "SysAdmin"), security_mgr=True
            ),
            "olduser@example.org": ident(
                "olduser", groups=("APP",), disabled=True
            ),
        },
        menus=MENUS,
        security=SECURITY,
    )
    authz = MenuAuthorizer(
        client,
        store or _store(),
        ttl_seconds=300,
        stale_grace_seconds=3600,
        clock=clock,
    )
    return authz, client


def _authorizer(clock: FakeClock, *, identities=None, store=None) -> MenuAuthorizer:
    authz, _ = _make(clock, identities=identities, store=store)
    return authz


# ====================================================================== #
# compute_snapshot — pure
# ====================================================================== #

def test_compute_grants_services_from_launchable_allowed_menus():
    authz = _authorizer(FakeClock())
    snap = authz.compute_snapshot(
        "apuser@example.org",
        ident("apuser", groups=("APP",)),
        MENUS,
        SECURITY,
    )
    # AP menus are APSEC (EntryList APP) -> allowed; PO is POSEC (PURCH) -> not.
    assert snap.allows("Erp.BO.APInvoiceSvc") is True
    assert snap.allows("Erp.BO.APAdjustmentSvc") is True
    assert snap.allows("Erp.BO.VendorSvc") is True
    assert snap.allows("Erp.BO.POSvc") is False
    # Baseline is always granted.
    assert snap.allows("Ice.BO.CompanySvc") is True
    assert snap.is_error is False
    assert snap.security_mgr is False


def test_compute_grant_chain_names_menu_seccode_and_matched_principal():
    authz = _authorizer(FakeClock())
    snap = authz.compute_snapshot(
        "apuser@example.org",
        ident("apuser", groups=("APP",)),
        MENUS,
        SECURITY,
    )
    grant = snap.grants["Erp.BO.APInvoiceSvc"]
    menu_ids = {m.menu_id for m in grant.menus}
    assert "AP0100" in menu_ids
    ap = next(m for m in grant.menus if m.menu_id == "AP0100")
    assert ap.sec_code == "APSEC"
    assert "APP" in ap.matched_principals


def test_compute_unmapped_launchable_menu_yields_no_service():
    # QA0100 is launchable + unlocked (allowed) but has no map entry.
    authz = _authorizer(FakeClock())
    snap = authz.compute_snapshot(
        "enguser@example.org",
        ident("enguser", groups=("ENG",)),
        MENUS,
        SECURITY,
    )
    # ENG is on no allow-list -> only the baseline survives.
    assert snap.allows("Erp.BO.APInvoiceSvc") is False
    assert snap.allows("Erp.BO.POSvc") is False
    assert snap.allows("Ice.BO.CompanySvc") is True


def test_compute_security_mgr_is_allow_all_sentinel():
    authz = _authorizer(FakeClock())
    snap = authz.compute_snapshot(
        "adminuser@example.org",
        ident("adminuser", groups=("APP",), security_mgr=True),
        MENUS,
        SECURITY,
    )
    assert snap.security_mgr is True
    assert snap.allow_all is True
    # Allows anything, even a service no menu grants.
    assert snap.allows("Erp.BO.AnythingSvc") is True
    assert snap.allows("Erp.BO.POSvc") is True


def test_compute_disabled_user_is_error_snapshot():
    authz = _authorizer(FakeClock())
    snap = authz.compute_snapshot(
        "olduser@example.org",
        ident("olduser", groups=("APP",), disabled=True),
        MENUS,
        SECURITY,
    )
    assert snap.is_error is True
    assert snap.allows("Erp.BO.APInvoiceSvc") is False


def test_compute_unknown_user_is_error_snapshot():
    authz = _authorizer(FakeClock())
    snap = authz.compute_snapshot("ghost@example.com", None, MENUS, SECURITY)
    assert snap.is_error is True
    assert snap.allows("Ice.BO.CompanySvc") is False


def test_compute_fails_closed_when_map_store_not_loaded():
    authz = _authorizer(FakeClock(), store=_store(loaded=False))
    snap = authz.compute_snapshot(
        "apuser@example.org",
        ident("apuser", groups=("APP",)),
        MENUS,
        SECURITY,
    )
    assert snap.is_error is True
    assert snap.allows("Erp.BO.APInvoiceSvc") is False


def test_compute_security_mgr_bypasses_missing_map_store():
    # SecurityMgr allow-all does not need the structural map.
    authz = _authorizer(FakeClock(), store=_store(loaded=False))
    snap = authz.compute_snapshot(
        "adminuser@example.org",
        ident("adminuser", security_mgr=True),
        MENUS,
        SECURITY,
    )
    assert snap.allow_all is True
    assert snap.allows("Erp.BO.APInvoiceSvc") is True


# ====================================================================== #
# ensure_snapshot / get_snapshot — async cache path
# ====================================================================== #

async def test_get_snapshot_is_none_before_ensure():
    authz = _authorizer(FakeClock())
    assert authz.get_snapshot("apuser@example.org") is None


async def test_ensure_then_get_returns_same_snapshot():
    authz = _authorizer(FakeClock())
    snap = await authz.ensure_snapshot("apuser@example.org")
    assert snap.allows("Erp.BO.APInvoiceSvc") is True
    assert snap.stale is False
    assert authz.get_snapshot("apuser@example.org") is snap


async def test_tenant_menus_fetched_once_and_shared_across_users():
    authz, client = _make(FakeClock())
    await authz.ensure_snapshot("apuser@example.org")
    await authz.ensure_snapshot("enguser@example.org")
    # Menus + security are tenant-scoped: fetched once, reused for user #2.
    assert client.menu_calls == 1
    assert client.security_calls == 1
    # Per-user identity is fetched per user.
    assert client.user_calls == 2


async def test_cached_snapshot_reused_within_ttl():
    authz, client = _make(FakeClock())
    await authz.ensure_snapshot("apuser@example.org")
    await authz.ensure_snapshot("apuser@example.org")
    assert client.user_calls == 1  # second call served from cache


async def test_snapshot_recomputed_after_ttl():
    clock = FakeClock()
    authz, client = _make(clock)
    await authz.ensure_snapshot("apuser@example.org")
    clock.advance(301)
    await authz.ensure_snapshot("apuser@example.org")
    assert client.user_calls == 2


async def test_stale_grace_serves_cached_on_fetch_failure():
    clock = FakeClock()
    authz, client = _make(clock)
    await authz.ensure_snapshot("apuser@example.org")
    clock.advance(400)          # past TTL, within grace
    client.fail = True
    snap = await authz.ensure_snapshot("apuser@example.org")
    assert snap.stale is True
    assert snap.is_error is False
    assert snap.allows("Erp.BO.APInvoiceSvc") is True   # served from cache


async def test_fail_closed_past_stale_grace():
    clock = FakeClock()
    authz, client = _make(clock)
    await authz.ensure_snapshot("apuser@example.org")
    clock.advance(3601)         # past grace (measured from successful compute)
    client.fail = True
    snap = await authz.ensure_snapshot("apuser@example.org")
    assert snap.is_error is True
    assert snap.allows("Erp.BO.APInvoiceSvc") is False


async def test_unknown_user_fails_closed_immediately_no_grace():
    authz = _authorizer(FakeClock())
    snap = await authz.ensure_snapshot("ghost@example.org")
    assert snap.is_error is True
    assert snap.allows("Ice.BO.CompanySvc") is False


async def test_disabled_user_fails_closed():
    authz = _authorizer(FakeClock())
    snap = await authz.ensure_snapshot("olduser@example.org")
    assert snap.is_error is True


# ====================================================================== #
# evict / inject / refresh_tenant
# ====================================================================== #

async def test_evict_drops_cached_snapshot():
    authz = _authorizer(FakeClock())
    await authz.ensure_snapshot("apuser@example.org")
    authz.evict("apuser@example.org")
    assert authz.get_snapshot("apuser@example.org") is None


async def test_evict_all_drops_everything():
    authz = _authorizer(FakeClock())
    await authz.ensure_snapshot("apuser@example.org")
    await authz.ensure_snapshot("enguser@example.org")
    authz.evict_all()
    assert authz.get_snapshot("apuser@example.org") is None
    assert authz.get_snapshot("enguser@example.org") is None


def test_inject_snapshot_seam_for_offline_harness():
    authz = _authorizer(FakeClock())
    marker = object()
    authz.inject_snapshot("someone@example.com", marker)
    assert authz.get_snapshot("someone@example.com") is marker


async def test_refresh_tenant_forces_menu_refetch():
    authz, client = _make(FakeClock())
    await authz.ensure_snapshot("apuser@example.org")
    assert client.menu_calls == 1
    await authz.refresh_tenant()
    authz.evict_all()
    await authz.ensure_snapshot("apuser@example.org")
    assert client.menu_calls == 2


# ====================================================================== #
# explain() — the menu-derived reasoning chain surfaced by epicor_my_access
# ()
# ====================================================================== #

async def test_explain_returns_menu_derived_reasoning_chain():
    authz = _authorizer(FakeClock())
    await authz.ensure_snapshot("apuser@example.org")
    rep = authz.explain("apuser@example.org")

    assert rep["epicor_user_id"] == "apuser"
    assert rep["security_mgr"] is False
    assert rep["stale"] is False
    # APInvoiceSvc + APAdjustmentSvc + VendorSvc + baseline CompanySvc.
    assert rep["allowed_service_count"] == 4
    assert rep["allowed_menu_count"] >= 2

    services = {s["service_id"]: s for s in rep["services"]}
    grant = services["Erp.BO.APInvoiceSvc"]
    granted_by = grant["granted_by"]
    assert any(
        g["menu_id"] == "AP0100" and g["sec_code"] == "APSEC"
        and "APP" in g["matched_principals"]
        for g in granted_by
    )


async def test_explain_of_security_mgr_flags_bypass():
    authz = _authorizer(FakeClock())
    await authz.ensure_snapshot("adminuser@example.org")
    rep = authz.explain("adminuser@example.org")
    assert rep["epicor_user_id"] == "adminuser"
    assert rep["security_mgr"] is True

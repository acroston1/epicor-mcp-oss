"""Cold-start + single-flight behavior of the menu authorizer.

The failure mode pinned against: under tenant-first ordering a SecurityMgr's
FIRST tool call after connect pays the whole tenant crawl before ``fetch_user``
ever runs. A slow crawl can time out at the client, and — with no single-flight and no useful cached answer — every
retry re-pays the crawl.  The refusal would read
``'no snapshot (epicor unreachable: )'`` because an httpx timeout ``str()``\\ s
to the empty string.

Required behavior:

* USER-FIRST: ``fetch_user`` runs before any tenant fetch.  SecurityMgr ->
  allow-all with ZERO tenant calls; unknown/disabled -> fail closed with ZERO
  tenant calls; only a scoped identity pays the crawl (computed as before).
* SINGLE-FLIGHT: concurrent ``ensure_snapshot`` for ONE e-mail share one
  ``fetch_user`` + one tenant crawl; concurrent calls for DIFFERENT scoped
  e-mails share ONE tenant crawl (the ``_get_tenant`` lock + TTL cache); a
  failed build releases the flight so the NEXT call re-fetches; a cancelled
  awaiter (the client-timeout scenario) does NOT cancel the shared build.
* REASON TEXT: every ``epicor unreachable`` reason carries the exception TYPE,
  so an exception whose ``str()`` is empty still renders diagnosably.

All seams injected — no network, no SQLite, no real clock.
"""

from __future__ import annotations

import asyncio

import pytest

from epicor_mcp.rbac.menu_authz import MenuAuthorizer

from fixtures.authz.fakes import FakeClock, FakeStore, ident, mrow, srow

# --- shared tenant (same shape as tests/test_menu_authz.py) ---------------- #

MENUS = [
    mrow("AP0100", sec_code="APSEC", program="Erp.UI.APInvoiceEntry"),
    mrow("PO0100", sec_code="POSEC", program="Erp.UI.POEntry"),
]
SECURITY = [srow("APSEC", entry_list="APP"), srow("POSEC", entry_list="PURCH")]

ADMIN = "adminuser@example.org"
SCOPED = "apuser@example.org"
SCOPED2 = "enguser@example.org"
GHOST = "ghost@example.org"
OLD = "olduser@example.org"


class GatedClient:
    """FakeAuthzClient variant with hold-gates + injectable exceptions.

    ``user_gate`` / ``tenant_gate`` (``asyncio.Event``) let a test hold a build
    IN FLIGHT so concurrency is real, not a scheduling accident.  ``user_exc``
    / ``tenant_exc`` raise on the next matching fetch (settable per test, and
    clearable to model recovery).
    """

    def __init__(
        self,
        *,
        identities: dict | None = None,
        menus: list | None = None,
        security: list | None = None,
        user_gate: asyncio.Event | None = None,
        tenant_gate: asyncio.Event | None = None,
        user_exc: BaseException | None = None,
        tenant_exc: BaseException | None = None,
    ) -> None:
        self.identities = identities or {}
        self.menus = menus if menus is not None else list(MENUS)
        self.security = security if security is not None else list(SECURITY)
        self.user_gate = user_gate
        self.tenant_gate = tenant_gate
        self.user_exc = user_exc
        self.tenant_exc = tenant_exc
        self.user_calls = 0
        self.menu_calls = 0
        self.security_calls = 0

    async def fetch_user(self, email: str):
        self.user_calls += 1
        if self.user_gate is not None:
            await self.user_gate.wait()
        if self.user_exc is not None:
            raise self.user_exc
        return self.identities.get(email.lower())

    async def fetch_menus(self):
        self.menu_calls += 1
        if self.tenant_gate is not None:
            await self.tenant_gate.wait()
        if self.tenant_exc is not None:
            raise self.tenant_exc
        return list(self.menus)

    async def fetch_security_rows(self):
        self.security_calls += 1
        if self.tenant_exc is not None:
            raise self.tenant_exc
        return list(self.security)

    async def aclose(self) -> None:
        pass


def _identities() -> dict:
    return {
        ADMIN.lower(): ident("adminuser", groups=("SysAdmin",), security_mgr=True),
        SCOPED.lower(): ident("apuser", groups=("APP",)),
        SCOPED2.lower(): ident("enguser", groups=("PURCH",)),
        OLD.lower(): ident("olduser", groups=("APP",), disabled=True),
    }


def _store() -> FakeStore:
    return FakeStore(
        menu_services={
            "AP0100": {"Erp.BO.APInvoiceSvc"},
            "PO0100": {"Erp.BO.POSvc"},
        },
        baseline={"Ice.BO.CompanySvc"},
    )


def _make(client: GatedClient | None = None, clock: FakeClock | None = None):
    client = client or GatedClient(identities=_identities())
    authz = MenuAuthorizer(
        client,
        _store(),
        ttl_seconds=300,
        stale_grace_seconds=3600,
        clock=clock or FakeClock(),
    )
    return authz, client


async def _drain(loop_ticks: int = 10) -> None:
    """Let background flights finish (all fakes complete in a few ticks)."""
    for _ in range(loop_ticks):
        await asyncio.sleep(0)


# ====================================================================== #
# USER-FIRST ordering
# ====================================================================== #

async def test_security_mgr_short_circuits_with_zero_tenant_calls():
    """The cold-start fix itself: an admin's first call never crawls the
    tenant.  fetch_menus / fetch_security_rows are NEVER called."""
    authz, client = _make()
    snap = await authz.ensure_snapshot(ADMIN)
    assert snap.security_mgr is True
    assert snap.allow_all is True
    assert snap.allows("Erp.BO.AnythingSvc") is True
    assert client.user_calls == 1
    assert client.menu_calls == 0
    assert client.security_calls == 0


async def test_unknown_user_fails_closed_with_zero_tenant_calls():
    authz, client = _make()
    snap = await authz.ensure_snapshot(GHOST)
    assert snap.is_error is True
    assert snap.allows("Ice.BO.CompanySvc") is False
    assert client.menu_calls == 0
    assert client.security_calls == 0


async def test_disabled_user_fails_closed_with_zero_tenant_calls():
    authz, client = _make()
    snap = await authz.ensure_snapshot(OLD)
    assert snap.is_error is True
    assert client.menu_calls == 0
    assert client.security_calls == 0


async def test_scoped_user_pays_the_crawl_and_computes_as_before():
    """The scoped path is UNCHANGED: tenant fetched once, grants computed off
    launchable menus x SecCode x structural map, baseline added."""
    authz, client = _make()
    snap = await authz.ensure_snapshot(SCOPED)
    assert snap.is_error is False
    assert snap.allows("Erp.BO.APInvoiceSvc") is True   # APSEC via APP group
    assert snap.allows("Erp.BO.POSvc") is False         # POSEC needs PURCH
    assert snap.allows("Ice.BO.CompanySvc") is True     # baseline
    assert client.user_calls == 1
    assert client.menu_calls == 1
    assert client.security_calls == 1


# ====================================================================== #
# Single-flight
# ====================================================================== #

async def test_concurrent_same_email_share_one_build():
    """Two concurrent ensure_snapshot for ONE e-mail -> one fetch_user + one
    tenant crawl, and both callers get the SAME snapshot object."""
    gate = asyncio.Event()
    authz, client = _make(GatedClient(identities=_identities(), user_gate=gate))
    t1 = asyncio.ensure_future(authz.ensure_snapshot(SCOPED))
    t2 = asyncio.ensure_future(authz.ensure_snapshot(SCOPED))
    await asyncio.sleep(0)  # both callers join the flight while it is held
    gate.set()
    s1, s2 = await asyncio.gather(t1, t2)
    assert s1 is s2
    assert client.user_calls == 1
    assert client.menu_calls == 1
    assert client.security_calls == 1


async def test_concurrent_distinct_scoped_emails_share_one_tenant_crawl():
    """Two DIFFERENT scoped users warming simultaneously: two identity fetches
    (per-user by nature) but ONE shared tenant crawl (_get_tenant lock + TTL)."""
    gate = asyncio.Event()
    authz, client = _make(GatedClient(identities=_identities(), user_gate=gate))
    t1 = asyncio.ensure_future(authz.ensure_snapshot(SCOPED))
    t2 = asyncio.ensure_future(authz.ensure_snapshot(SCOPED2))
    await asyncio.sleep(0)
    gate.set()
    s1, s2 = await asyncio.gather(t1, t2)
    assert s1.allows("Erp.BO.APInvoiceSvc") is True
    assert s2.allows("Erp.BO.POSvc") is True
    assert client.user_calls == 2
    assert client.menu_calls == 1
    assert client.security_calls == 1


async def test_failed_build_releases_flight_and_next_call_refetches():
    """A failed build must not pin its failure: the flight registry clears and
    a cached ERROR snapshot never counts as fresh, so the very next call
    re-fetches — the opposite of a no-retry thrash."""
    authz, client = _make(
        GatedClient(identities=_identities(), user_exc=RuntimeError("boom"))
    )
    snap1 = await authz.ensure_snapshot(SCOPED)
    assert snap1.is_error is True
    assert authz._flights == {}          # flight released
    client.user_exc = None               # Epicor recovers
    snap2 = await authz.ensure_snapshot(SCOPED)
    assert snap2.is_error is False
    assert snap2.allows("Erp.BO.APInvoiceSvc") is True
    assert client.user_calls == 2        # the retry actually re-fetched


async def test_cancelled_awaiter_does_not_cancel_the_shared_build():
    """The measured scenario: call 1 times out at the CLIENT (its awaiter is
    cancelled) while the build is in flight.  The build must survive, complete
    in the background, and be reused — user_calls stays 1."""
    gate = asyncio.Event()
    authz, client = _make(GatedClient(identities=_identities(), user_gate=gate))
    t1 = asyncio.ensure_future(authz.ensure_snapshot(SCOPED))
    await asyncio.sleep(0)  # flight created, held at the user gate
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1
    gate.set()
    snap = await authz.ensure_snapshot(SCOPED)  # joins/uses the surviving build
    assert snap.is_error is False
    assert snap.allows("Erp.BO.APInvoiceSvc") is True
    assert client.user_calls == 1


async def test_flight_registry_is_empty_after_successful_build():
    authz, _client = _make()
    await authz.ensure_snapshot(SCOPED)
    await _drain()
    assert authz._flights == {}


# ====================================================================== #
# Single-flight must not outlive an admin eviction
# ====================================================================== #

async def test_evict_is_not_undone_by_a_build_that_was_already_running():
    """``/admin/authz/evict/{email}`` exists for INSTANT propagation of an
    Epicor security change.  A build started before the evict must neither be
    joined by the next caller (it read the PRE-change identity) nor land on
    top of the eviction afterwards (which would serve pre-change authorization
    from cache for the full TTL)."""
    gate = asyncio.Event()
    authz, client = _make(GatedClient(identities=_identities(), user_gate=gate))
    stale_build = asyncio.ensure_future(authz.ensure_snapshot(SCOPED))
    await asyncio.sleep(0)  # in flight, held at the user gate

    authz.evict(SCOPED)                      # the admin's eviction lands here
    assert authz._flights == {}, "the pre-evict build stayed joinable"

    gate.set()
    await stale_build                        # it still answers its own awaiter
    await _drain()
    assert authz.get_snapshot(SCOPED) is None, (
        "a build that started before the evict re-populated the cache"
    )
    # ...and the next call rebuilds from scratch rather than reusing it.
    fresh = await authz.ensure_snapshot(SCOPED)
    assert fresh.is_error is False
    assert client.user_calls == 2


# ====================================================================== #
# Type-prefixed unreachable reasons
# ====================================================================== #

async def test_unreachable_reason_carries_type_for_empty_message_exception():
    """A bare asyncio.TimeoutError str()s to '' — the exact live rendering bug
    ('epicor unreachable: )').  The type name must survive."""
    authz, _client = _make(
        GatedClient(identities=_identities(), user_exc=asyncio.TimeoutError())
    )
    snap = await authz.ensure_snapshot(SCOPED)
    assert snap.is_error is True
    assert "TimeoutError" in snap.error
    assert snap.error.startswith("epicor unreachable: ")


async def test_tenant_failure_reason_also_carries_type():
    authz, _client = _make(
        GatedClient(identities=_identities(), tenant_exc=asyncio.TimeoutError())
    )
    snap = await authz.ensure_snapshot(SCOPED)
    assert snap.is_error is True
    assert "TimeoutError" in snap.error


async def test_tenant_failure_still_serves_stale_within_grace():
    """Stale-grace semantics for scoped users are UNCHANGED by the reordering:
    a tenant-fetch failure with a good cached snapshot inside the grace window
    serves it stale, never an error."""
    clock = FakeClock()
    client = GatedClient(identities=_identities())
    authz, _ = _make(client, clock)
    good = await authz.ensure_snapshot(SCOPED)
    assert good.is_error is False
    clock.advance(400)  # past TTL 300, well inside grace 3600
    client.tenant_exc = asyncio.TimeoutError()
    snap = await authz.ensure_snapshot(SCOPED)
    assert snap.stale is True
    assert snap.is_error is False
    assert snap.allows("Erp.BO.APInvoiceSvc") is True


# --------------------------------------------------------------------------- #
# A persistent identity ANSWER caches; only a fetch FAILURE retries
# --------------------------------------------------------------------------- #

async def test_an_unknown_user_answer_is_cached_for_the_ttl():
    """Unknown/disabled are real ANSWERS — live
    Epicor said so — and cache for the normal TTL, so a persistent identity
    state does not pay a live fetch_user on EVERY request. Only the
    "epicor unreachable" fetch-failure snapshot (``fetch_failed=True``) is
    excluded from freshness — that always-retry half is pinned separately by
    ``test_a_failed_build_releases_the_flight_and_the_next_call_refetches``."""
    clock = FakeClock()
    authz, client = _make(clock=clock)

    await authz.ensure_snapshot(GHOST)
    await authz.ensure_snapshot(GHOST)
    assert client.user_calls == 1  # second call served from cache

    clock.advance(301)  # past the 300 s TTL
    snap = await authz.ensure_snapshot(GHOST)
    assert client.user_calls == 2  # expiry re-fetches like any other snapshot
    assert snap.is_error is True
    assert snap.fetch_failed is False  # an answer, not a failure to answer

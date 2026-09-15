"""MenuAuthorizer — menu-derived authorization snapshots.

Ties the runtime fetch tier (:class:`EpicorAuthzClient`) and the structural map
(:class:`MenuMapStore`) together into a per-user :class:`UserAuthzSnapshot`
consumed by the (synchronous, network-free) RBAC enforcer.

Two halves:

* **Pure translation** — :meth:`MenuAuthorizer.compute_snapshot` turns
  ``(identity, live menu rows, live security rows, structural map)`` into a
  snapshot.  A SecurityMgr becomes an allow-all sentinel; a disabled/unknown user
  becomes an error snapshot denying everything; a launchable (enabled + has a
  program) menu whose SecCode the user passes contributes its mapped services;
  the justified baseline is always added; grants carry the granting menu, its
  SecCode, and the matched principals.
* **Async fetch + cache** — :meth:`ensure_snapshot` (awaited in middleware) and
  :meth:`get_snapshot` (sync, enforcer hot path).  Per-user TTL, tenant menus /
  security fetched once and shared under an ``asyncio.Lock`` (stampede guard),
  stale-grace on fetch failure, fail-closed past grace and for unknown/disabled
  users.

  **USER-FIRST ordering:** ``ensure_snapshot``
  resolves the identity (``fetch_user``) BEFORE any tenant crawl.  The failure
  mode it prevents: under tenant-first ordering a SecurityMgr's FIRST tool call
  pays the whole tenant sweep before ``fetch_user`` ever runs. A slow crawl
  can time out at the client, and every retry re-pays the crawl. Instead a
  SecurityMgr short-circuits to allow-all with ZERO tenant fetches, unknown/disabled users fail closed with ZERO tenant fetches, and
  only a scoped (real, non-SecurityMgr) identity pays the crawl.  Builds are
  SINGLE-FLIGHT per e-mail (concurrent calls await one shared build, shielded
  so a disconnected caller cannot kill it; a failed build releases the flight
  so the next call retries fresh), and the tenant crawl itself is serialized
  under the existing lock + TTL cache, so two scoped users warming
  concurrently share one crawl.  ``evict``/``evict_all`` drop the in-flight
  build as well as the cached snapshot and bump a generation counter, so an
  admin propagating an Epicor security change can neither be joined to a
  pre-change build nor have one land on top of the eviction afterwards.

``evaluate_sec_code`` is the pure disallow-wins set engine; ``'*'`` is ignored, a blank SecCode / missing row is an
unlocked ALLOW.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Sequence

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Snapshot value objects
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GrantMenu:
    """One menu that grants a service to the user, with the reasoning bits."""

    menu_id: str
    menu_desc: str
    sec_code: str
    matched_principals: tuple[str, ...]


@dataclass
class ServiceGrant:
    """Why a single service is allowed: the menus that granted it (+ baseline)."""

    service_id: str
    menus: list[GrantMenu] = field(default_factory=list)
    baseline: bool = False


@dataclass
class UserAuthzSnapshot:
    """The cached, per-user authorization decision surface the enforcer reads.

    Duck-compatible with the test ``FakeSnapshot`` — the enforcer only touches
    ``allows()`` / ``security_mgr`` / ``allow_all`` / ``is_error`` / ``stale`` /
    ``error``.  The remaining fields power ``explain()`` (epicor_my_access).
    """

    email: str
    user_id: str | None = None
    groups: tuple[str, ...] = ()
    security_mgr: bool = False
    allow_all: bool = False
    is_error: bool = False
    stale: bool = False
    error: str = ""
    allowed_services: frozenset[str] = frozenset()
    grants: dict[str, ServiceGrant] = field(default_factory=dict)
    allowed_menu_ids: tuple[str, ...] = ()
    computed_at: float = 0.0
    #: True ONLY for "epicor unreachable" snapshots — a failure to ANSWER, as
    #: opposed to an unknown/disabled user, which is a real answer from live
    #: Epicor. The freshness gate keys on this: fetch failures always retry,
    #: while a persistent identity state caches for
    #: the normal TTL so it does not pay a live fetch_user on every request.
    fetch_failed: bool = False

    def allows(self, service_id: str) -> bool:
        if self.is_error:
            return False
        if self.allow_all:
            return True
        return service_id in self.allowed_services


# --------------------------------------------------------------------------- #
# Authorizer
# --------------------------------------------------------------------------- #

class MenuAuthorizer:
    """Computes, caches, and serves per-user :class:`UserAuthzSnapshot`\\ s."""

    def __init__(
        self,
        client,
        store,
        *,
        ttl_seconds: float = 300.0,
        stale_grace_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._store = store
        self._ttl = float(ttl_seconds)
        self._grace = float(stale_grace_seconds)
        self._clock = clock

        self._snapshots: dict[str, UserAuthzSnapshot] = {}
        self._tenant_menus: Sequence | None = None
        self._tenant_security: Sequence | None = None
        self._tenant_fetched_at: float | None = None
        self._lock = asyncio.Lock()
        #: Per-email single-flight registry: lowercased e-mail -> the ONE
        #: in-progress build Task.  Entries are removed by a done-callback the
        #: moment the build finishes (success OR failure), so a failed build
        #: never pins its failure — the next call starts a fresh build.
        self._flights: dict[str, asyncio.Task] = {}
        #: Bumped by every eviction.  A build stamps the generation it STARTED
        #: in and refuses to write its result once that number has moved —
        #: without it, a build that began before an admin evict lands after it
        #: and re-populates the cache with pre-change authorization for a full
        #: TTL, silently undoing the "instant propagation" the endpoint
        #: promises.  Eviction also drops the in-flight entry, so the next
        #: caller starts a FRESH build instead of joining the pre-evict one
        #: (that join is what single-flight would otherwise have added on top
        #: of the pre-existing clobber).
        self._generation: int = 0

    # ------------------------------------------------------------------ #
    # Pure SecCode engine (menu authorization predicates)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _split_principals(field_value: str | None) -> set[str]:
        """EntryList / NoEntryList: comma-delimited, trimmed, ``'*'`` ignored."""
        out: set[str] = set()
        for part in (field_value or "").split(","):
            p = part.strip()
            if p and p != "*":
                out.add(p)
        return out

    @classmethod
    def _evaluate_verbose(
        cls, row, user_id: str, groups: tuple[str, ...]
    ) -> tuple[bool, list[str]]:
        """Return ``(access, matched_allow_principals)`` for one security row.

        ``row is None`` (blank SecCode / no Security record) => unlocked => ALLOW
        with no matched principal.  Otherwise the disallow-wins set engine:

            allowed    = AllowAll   OR (user_id or any group) in EntryList
            disallowed = DisallowAll OR (user_id or any group) in NoEntryList
            access     = allowed AND NOT disallowed
        """
        if row is None:
            return True, []
        principals = {user_id} | set(groups)
        entry = cls._split_principals(getattr(row, "entry_list", ""))
        noentry = cls._split_principals(getattr(row, "no_entry_list", ""))
        allow_all = bool(getattr(row, "allow_all", False))
        disallow_all = bool(getattr(row, "disallow_all", False))

        matched_allow = sorted(principals & entry)
        allowed = allow_all or bool(matched_allow)
        disallowed = disallow_all or bool(principals & noentry)
        return (allowed and not disallowed), matched_allow

    @classmethod
    def evaluate_sec_code(cls, row, user_id: str, groups: Iterable[str] = ()) -> bool:
        """Pure disallow-wins access decision for one security row (no SecurityMgr
        bypass — that is a snapshot-level concern)."""
        decision, _ = cls._evaluate_verbose(row, user_id, tuple(groups))
        return decision

    # ------------------------------------------------------------------ #
    # Pure snapshot compute
    # ------------------------------------------------------------------ #

    def _error_snapshot(self, email: str, user_id: str | None, reason: str) -> UserAuthzSnapshot:
        return UserAuthzSnapshot(
            email=email,
            user_id=user_id,
            is_error=True,
            error=reason,
            computed_at=self._clock(),
        )

    def compute_snapshot(
        self,
        email: str,
        identity,
        menu_rows: Sequence,
        security_rows: Sequence,
    ) -> UserAuthzSnapshot:
        """Translate live identity + tenant rows + structural map into a snapshot."""
        now = self._clock()

        if identity is None:
            return self._error_snapshot(email, None, "no Epicor user with that email")
        if getattr(identity, "disabled", False):
            return self._error_snapshot(email, identity.user_id, "Epicor user is disabled")

        # SecurityMgr bypasses all menu security — no structural map required.
        if getattr(identity, "security_mgr", False):
            return UserAuthzSnapshot(
                email=email,
                user_id=identity.user_id,
                groups=tuple(identity.groups),
                security_mgr=True,
                allow_all=True,
                computed_at=now,
            )

        # Every other decision needs the structural map; missing => fail closed.
        if not self._store.is_loaded():
            return self._error_snapshot(
                email, identity.user_id, "menu->service map not loaded"
            )

        user_id = identity.user_id
        groups = tuple(identity.groups)
        sec_by_code = {s.sec_code: s for s in security_rows}

        allowed_menu_ids: list[str] = []
        grant_menu_by_id: dict[str, GrantMenu] = {}
        for m in menu_rows:
            launchable = bool(getattr(m, "enabled", True)) and bool(
                (getattr(m, "program", "") or "").strip()
            )
            if not launchable:
                continue
            sec_code = (getattr(m, "sec_code", "") or "").strip()
            row = sec_by_code.get(sec_code) if sec_code else None
            decision, matched = self._evaluate_verbose(row, user_id, groups)
            if not decision:
                continue
            allowed_menu_ids.append(m.menu_id)
            grant_menu_by_id[m.menu_id] = GrantMenu(
                menu_id=m.menu_id,
                menu_desc=getattr(m, "menu_desc", "") or m.menu_id,
                sec_code=sec_code,
                matched_principals=tuple(matched),
            )

        grants: dict[str, ServiceGrant] = {}
        for menu_id in allowed_menu_ids:
            for svc in self._store.services_for_menus([menu_id]):
                grant = grants.get(svc)
                if grant is None:
                    grant = ServiceGrant(service_id=svc)
                    grants[svc] = grant
                grant.menus.append(grant_menu_by_id[menu_id])

        allowed_services: set[str] = set(grants)
        for svc in self._store.baseline():
            allowed_services.add(svc)
            grant = grants.get(svc)
            if grant is None:
                grants[svc] = ServiceGrant(service_id=svc, baseline=True)
            else:
                grant.baseline = True

        return UserAuthzSnapshot(
            email=email,
            user_id=user_id,
            groups=groups,
            allowed_services=frozenset(allowed_services),
            grants=grants,
            allowed_menu_ids=tuple(allowed_menu_ids),
            computed_at=now,
        )

    # ------------------------------------------------------------------ #
    # Async fetch + cache
    # ------------------------------------------------------------------ #

    async def _get_tenant(self, now: float, *, force: bool = False) -> tuple[Sequence, Sequence]:
        """Fetch (and cache, TTL-shared) the tenant menu + security rows."""
        async with self._lock:
            fresh = (
                self._tenant_fetched_at is not None
                and (now - self._tenant_fetched_at) <= self._ttl
                and self._tenant_menus is not None
            )
            if fresh and not force:
                return self._tenant_menus, self._tenant_security  # type: ignore[return-value]
            self._store.maybe_reload()
            menus = await self._client.fetch_menus()
            security = await self._client.fetch_security_rows()
            self._tenant_menus = menus
            self._tenant_security = security
            self._tenant_fetched_at = now
            return menus, security

    def _is_fresh(self, snap: UserAuthzSnapshot, now: float) -> bool:
        return (now - snap.computed_at) <= self._ttl

    @staticmethod
    def _exc_text(exc: BaseException) -> str:
        """``f"{type}: {exc}"`` — never the bare ``str(exc)``.

        Without the type name a timeout renders as ``'epicor unreachable: )'``
        because an httpx/asyncio timeout ``str()``\\ s to the EMPTY string.  The
        type name is the part that survives an empty message.
        """
        return f"{type(exc).__name__}: {exc}"

    def _fetch_failure(
        self,
        email: str,
        cached: UserAuthzSnapshot | None,
        now: float,
        exc: BaseException,
    ) -> UserAuthzSnapshot:
        """Stale-grace on any LIVE fetch failure (identity OR tenant); past
        grace, or with no good cached snapshot, fail closed with a
        type-prefixed reason.

        PURE — it computes the snapshot and never writes the cache.  Every
        write funnels through :meth:`_build_snapshot`'s single generation-
        guarded site, so an evict cannot be undone by a build that was already
        running when it arrived.
        """
        reason = self._exc_text(exc)
        if (
            cached is not None
            and not cached.is_error
            and (now - cached.computed_at) <= self._grace
        ):
            logger.warning(
                "authz fetch failed for %s (%s) — serving cached snapshot "
                "STALE (age %.0fs, within %.0fs grace)",
                email, reason, now - cached.computed_at, self._grace,
            )
            return replace(cached, stale=True)
        logger.error(
            "authz fetch failed for %s (%s) — no snapshot within grace, "
            "failing closed",
            email, reason,
        )
        return replace(
            self._error_snapshot(email, None, f"epicor unreachable: {reason}"),
            fetch_failed=True,
        )

    async def _build_snapshot(
        self, key: str, email: str, generation: int
    ) -> UserAuthzSnapshot:
        """One snapshot build: identity FIRST, tenant crawl only if needed.

        USER-FIRST ordering: ``fetch_user`` runs before any tenant
        fetch, so a SecurityMgr (allow-all — the row arguments are ignored by
        ``compute_snapshot``'s SecurityMgr branch) and an unknown/disabled user
        (fail-closed) both resolve with ZERO tenant calls.  Only a scoped
        identity pays the tenant crawl.

        ONE cache write, at the end, guarded by *generation* — the eviction
        counter as of the moment this flight was REGISTERED, which is why the
        caller passes it in rather than the body reading it: a Task does not
        execute its first line until the loop schedules it, so a build created
        one tick before an ``evict`` would otherwise read the POST-evict value
        and defeat the guard. A build that started before an ``evict`` must
        still ANSWER its own awaiters (that call is already in flight and
        cannot be un-asked) but must not re-populate the cache the admin just
        cleared.
        """
        now = self._clock()
        cached = self._snapshots.get(key)

        try:
            identity = await self._client.fetch_user(email)
        except Exception as exc:  # identity fetch failure -> stale grace
            return self._cache_snapshot(
                key, self._fetch_failure(email, cached, now, exc), generation
            )

        if (
            identity is None
            or getattr(identity, "disabled", False)
            or getattr(identity, "security_mgr", False)
        ):
            # All three branches of compute_snapshot ignore the row arguments:
            # unknown/disabled -> error snapshot, SecurityMgr -> allow-all
            # sentinel.  Empty sequences, NO tenant fetch — that locality is
            # the whole cold-start fix for an admin's first call.
            return self._cache_snapshot(
                key, self.compute_snapshot(email, identity, (), ()), generation
            )

        try:
            menus, security = await self._get_tenant(now)
        except Exception as exc:  # tenant fetch failure -> stale grace
            return self._cache_snapshot(
                key, self._fetch_failure(email, cached, now, exc), generation
            )

        return self._cache_snapshot(
            key, self.compute_snapshot(email, identity, menus, security), generation
        )

    def _cache_snapshot(
        self, key: str, snap: UserAuthzSnapshot, generation: int
    ) -> UserAuthzSnapshot:
        """Cache *snap* unless an eviction happened while it was being built."""
        if generation == self._generation:
            self._snapshots[key] = snap
        else:
            logger.info(
                "authz snapshot for %s completed across an eviction — answering "
                "the in-flight caller but NOT re-populating the cache",
                key,
            )
        return snap

    async def ensure_snapshot(self, email: str) -> UserAuthzSnapshot:
        """Return a fresh cached snapshot, recomputing from LIVE when expired.

        Identity is fetched FIRST (see :meth:`_build_snapshot`).  On a fetch
        failure, serves the cached snapshot with ``stale=True`` while it is
        within the stale-grace window; past grace (or with no cache) it returns
        a fail-closed error snapshot.  Unknown/disabled users fail closed
        immediately with no grace (they are not fetch failures).

        SINGLE-FLIGHT: concurrent calls for one e-mail await ONE shared build
        (shielded, so a caller that disconnects mid-build cannot cancel the
        build out from under the other awaiters — the build completes and
        caches in the background, which is what makes "retry shortly" honest).
        A cached FETCH-FAILURE snapshot never counts as fresh: "epicor
        unreachable" is a failure to answer, not an answer, and serving it from
        cache for the TTL would be a no-retry thrash.  An
        unknown or disabled user, by contrast, IS an answer — live Epicor said
        so — and caches for the normal TTL, which is what keeps a persistent
        identity state from paying a live fetch_user on every request.  The
        flight registry is cleared on completion, so the next call
        after a failed build re-fetches.
        """
        key = email.lower()
        now = self._clock()
        cached = self._snapshots.get(key)
        if (
            cached is not None
            and not cached.fetch_failed
            and self._is_fresh(cached, now)
        ):
            return cached

        flight = self._flights.get(key)
        if flight is None or flight.done():
            flight = asyncio.ensure_future(
                self._build_snapshot(key, email, self._generation)
            )
            self._flights[key] = flight

            def _release(task: asyncio.Task, k: str = key) -> None:
                # Identity-guarded: only remove OUR entry.  A done flight can
                # linger for one loop tick before this callback runs; if a new
                # build has already replaced it, popping blindly would drop the
                # NEW flight and re-open the stampede.
                if self._flights.get(k) is task:
                    self._flights.pop(k, None)

            flight.add_done_callback(_release)
        return await asyncio.shield(flight)

    def get_snapshot(self, email: str):
        """Sync hot-path read of the cached snapshot (``None`` if never ensured)."""
        return self._snapshots.get(email.lower())

    # ------------------------------------------------------------------ #
    # Admin / test seams
    # ------------------------------------------------------------------ #

    def evict(self, email: str) -> None:
        """Drop one user's cached snapshot (forces recompute next ensure).

        Drops the in-flight build too: joining it would authorize the next
        request off the identity/menus read BEFORE the change the admin is
        propagating, which is the one thing this endpoint exists to prevent.
        The bumped generation stops that build writing its result afterwards.
        """
        self._snapshots.pop(email.lower(), None)
        self._flights.pop(email.lower(), None)
        self._generation += 1

    def evict_all(self) -> None:
        """Drop every cached user snapshot (tenant menu/security cache is kept)."""
        self._snapshots.clear()
        self._flights.clear()
        self._generation += 1

    def inject_snapshot(self, email: str, snapshot) -> None:
        """Test-harness seam: seed a snapshot directly (offline, no fetch)."""
        self._snapshots[email.lower()] = snapshot

    async def refresh_tenant(self) -> None:
        """Force a re-fetch of the shared tenant menu + security rows."""
        await self._get_tenant(self._clock(), force=True)

    # ------------------------------------------------------------------ #
    # Explain (epicor_my_access reasoning chain)
    # ------------------------------------------------------------------ #

    def explain(self, email: str) -> dict:
        """The menu-derived reasoning chain for one user's cached snapshot."""
        snap = self.get_snapshot(email)
        if snap is None:
            return {"email": email, "error": "no snapshot computed for this user"}

        services = []
        for svc in sorted(snap.allowed_services):
            grant = snap.grants.get(svc)
            granted_by = []
            if grant is not None:
                for gm in grant.menus:
                    granted_by.append(
                        {
                            "menu_id": gm.menu_id,
                            "menu_desc": gm.menu_desc,
                            "sec_code": gm.sec_code,
                            "matched_principals": list(gm.matched_principals),
                        }
                    )
                if grant.baseline:
                    granted_by.append({"baseline": True})
            services.append({"service_id": svc, "granted_by": granted_by})

        return {
            "email": snap.email,
            "epicor_user_id": snap.user_id,
            "groups": list(snap.groups),
            "security_mgr": snap.security_mgr,
            "allow_all": snap.allow_all,
            "is_error": snap.is_error,
            "error": snap.error,
            "stale": snap.stale,
            "computed_at": snap.computed_at,
            "age_seconds": self._clock() - snap.computed_at,
            "allowed_service_count": len(snap.allowed_services),
            "allowed_menu_count": len(snap.allowed_menu_ids),
            "services": services,
        }

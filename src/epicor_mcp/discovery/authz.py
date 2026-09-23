'Table-level authorization — a GATE, default deny: scoped users reach only menu-mapped tables.'

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)

__all__ = [
    "TableAuthorizer",
    "AuthzScope",
    "ScopeState",
    "TABLE_AUTHZ_MODES",
    "normalize_table",
]

TABLE_AUTHZ_MODES = ("boost", "gate", "off")


def normalize_table(name: str) -> str:
    """``"Erp.JobHead"`` / ``"[Erp].[JobHead]"`` / ``"JOBHEAD "`` -> ``"jobhead"``.

    THE one comparison spelling (design point 7). Scope membership is stored and
    tested exclusively through this function — :meth:`AuthzScope.scoped` runs it
    over every stored name and :meth:`AuthzScope.allows` runs it over every
    queried name, so no consumer ever re-implements the normalisation.
    """
    bare = (name or "").strip().rsplit(".", 1)[-1]
    return bare.strip().strip("[]").lower()


class ScopeState(enum.Enum):
    """The explicit three-state. A snapshot FAILURE (UNAVAILABLE) and a
    SecurityMgr (UNLIMITED) would both be ``tables=None`` in a two-state model —
    indistinguishable, which under a gate is a fail-open. Here they cannot be
    confused: the state is carried explicitly and :meth:`AuthzScope.__post_init__`
    refuses the combinations that would blur them."""

    UNLIMITED = "unlimited"      # SecurityMgr / allow_all / mode=off: nothing filtered
    SCOPED = "scoped"            # menu-chain tables only (default deny)
    UNAVAILABLE = "unavailable"  # identity/snapshot failure: fail CLOSED in gate mode


@dataclass(frozen=True)
class AuthzScope:
    """What one identity may reach, plus why — served in tool responses so a
    surprising refusal or ranking is diagnosable without a second call."""

    email: str
    state: ScopeState
    #: Lower-cased bare table names — present IFF ``state is SCOPED``. Enforced
    #: in ``__post_init__`` so the old ``None``-means-three-things bug is
    #: unrepresentable: UNLIMITED and UNAVAILABLE both carry ``None`` and are
    #: told apart by ``state``, never by this field.
    tables: frozenset[str] | None
    reason: str
    security_mgr: bool = False
    service_count: int = 0

    def __post_init__(self) -> None:
        if (self.state is ScopeState.SCOPED) != (self.tables is not None):
            raise ValueError(
                f"AuthzScope state={self.state.value!r} is inconsistent with "
                f"tables={'a set' if self.tables is not None else None!r} — "
                "SCOPED carries a frozenset, UNLIMITED/UNAVAILABLE carry None"
            )

    # -- sanctioned constructors -------------------------------------------- #
    @classmethod
    def unlimited(cls, email: str, reason: str, *, security_mgr: bool = False) -> "AuthzScope":
        return cls(email, ScopeState.UNLIMITED, None, reason, security_mgr=security_mgr)

    @classmethod
    def scoped(
        cls, email: str, tables: Any, reason: str, *, service_count: int = 0
    ) -> "AuthzScope":
        return cls(
            email,
            ScopeState.SCOPED,
            frozenset(normalize_table(t) for t in tables),
            reason,
            service_count=service_count,
        )

    @classmethod
    def unavailable(cls, email: str, reason: str) -> "AuthzScope":
        return cls(email, ScopeState.UNAVAILABLE, None, reason)

    # -- predicates ---------------------------------------------------------- #
    @property
    def active(self) -> bool:
        """True IFF SCOPED — kept because ``discovery/tools.py`` branches on it
        today (``scope.active and mode == "gate"`` -> filter; ``… == "boost"``
        -> boost). UNLIMITED and UNAVAILABLE are both inactive there, which
        preserves the pre-gate behaviour of those call sites until they grow
        their own UNAVAILABLE refusal branch."""
        return self.state is ScopeState.SCOPED

    @property
    def is_unlimited(self) -> bool:
        return self.state is ScopeState.UNLIMITED

    @property
    def is_unavailable(self) -> bool:
        return self.state is ScopeState.UNAVAILABLE

    def allows(self, name: str) -> bool:
        'May this identity read *name*? The ONE membership test (point 7).'
        if self.state is ScopeState.UNLIMITED:
            return True
        if self.state is ScopeState.UNAVAILABLE:
            return False
        norm = normalize_table(name)
        if norm in self.tables:  # type: ignore[operator]
            return True
        if norm.endswith("_ud"):
            # Strip ONLY when the remainder is non-empty: a name that IS the
            # bare suffix ("_UD", "Erp._UD") must keep failing on its own
            # membership, never re-test the empty string (which could match a
            # malformed scope entry and fail open).
            parent = norm[: -len("_ud")]
            if parent:
                return parent in self.tables  # type: ignore[operator]
        return False

    def note(self, mode: str = "") -> str:
        """Human-readable line for the ``authz`` response block. *mode* tailors
        the verb; the zero-argument call (today's ``tools.py``) stays valid and
        mode-neutral."""
        if self.state is ScopeState.UNAVAILABLE:
            return f"Table authorization could not be established ({self.reason})."
        if self.state is ScopeState.UNLIMITED:
            return f"No table restriction for {self.email} ({self.reason})."
        verb = {
            "gate": "are the only tables this surface serves",
            "boost": "are ranked higher — nothing is hidden",
        }.get(mode, "define this caller's table scope")
        return (
            f"Tables {self.email} can reach through the Epicor menu "
            f"{verb} ({len(self.tables or ())} tables via "
            f"{self.service_count} business objects)."
        )


class TableAuthorizer:
    """email -> :class:`AuthzScope`, via the menu chain + the baseline.

    Failure paths return UNAVAILABLE — never an empty set (which would read as
    "the chain worked and found nothing") and never ``None`` tables under a
    success state (which would read as unlimited: the exact fail-open the
    three-state scope exists to remove).
    """

    def __init__(
        self,
        menu_authorizer: Any,
        service_index: Any,
        *,
        mode: str = "gate",
        dev_identity: str = "",
        snapshot_wait_s: float = 15.0,
    ) -> None:
        self._authorizer = menu_authorizer
        self._index = service_index
        # An unrecognised mode value falls back to GATE, not boost: a typo'd
        # env var must not silently un-gate the surface.
        self._mode = mode if mode in TABLE_AUTHZ_MODES else "gate"
        self._dev_identity = (dev_identity or "").strip()
        #: Bound the request-path wait because a cold scope build may need
        #: substantial metadata retrieval. Return authorization_unavailable
        #: before a long build exhausts the client's transport timeout,
        #: while the shielded build keeps running in the background —
        #: which is precisely what makes that envelope's "continues building in
        #: the background, retry shortly" wording TRUE.
        self._snapshot_wait_s = max(1.0, float(snapshot_wait_s))
        #: SESSION-PINNED rather than TTL-based: the
        #: first successful scope per lowercased e-mail lives for the process
        #: lifetime. No timestamps on purpose — refresh is evict or restart.
        self._cache: dict[str, AuthzScope] = {}

    @property
    def mode(self) -> str:
        return self._mode

    def resolve_identity(self, session_email: str = "") -> str:
        """Configured dev identity > session principal. NO caller argument.

        ``for_email`` was removed: in gate mode
        an explicit argument OUTRANKED the session, so any tool caller could
        pick whose authorization applied to them — an identity-spoofing
        vector. Identity now comes only from the connection session (the
        bearer token) or from server-side configuration
        (``EPICOR_MCP_DEV_IDENTITY``). The dev identity outranks the session
        because under ``dev_mode`` the session is a constant (the first
        ``users.json`` entry) and would make every caller look like the same
        person. It is NOT gated on ``dev_mode``, so while it is set every
        bearer-authenticated caller is scoped as that one identity and the gate
        is vacuous — it must stay empty anywhere real sessions exist.
        """
        return self._dev_identity or (session_email or "").strip()

    async def scope_for(self, email: str) -> AuthzScope:
        if self._mode == "off":
            # UNLIMITED, not UNAVAILABLE, and deliberately so: ``off`` is the
            # operator's rollback switch, and rollback must degrade to NO gating
            # everywhere — including in a consumer that checks only the scope
            # state and forgets to check the mode. UNAVAILABLE here would turn
            # the kill switch into a deny-everyone switch.
            return AuthzScope.unlimited(email, "table authz disabled (mode=off)")
        if not (email or "").strip():
            return AuthzScope.unavailable(email, "no identity supplied")
        if self._authorizer is None or self._index is None:
            return AuthzScope.unavailable(
                email, "menu authorizer or service index unavailable"
            )

        key = email.strip().lower()
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        try:
            snap = await asyncio.wait_for(
                self._authorizer.ensure_snapshot(email),
                timeout=self._snapshot_wait_s,
            )
        except asyncio.TimeoutError:
            # wait_for cancels OUR await, not the build: ensure_snapshot's
            # asyncio.shield keeps the shared flight alive (pinned by
            # test_cancelled_awaiter_does_not_cancel_the_shared_build), so the
            # honest report is "still building", not "failed". Never cached.
            logger.info(
                "table authz snapshot for %s still building after %.0fs — "
                "returning retryable unavailable; build continues",
                email, self._snapshot_wait_s,
            )
            return AuthzScope.unavailable(
                email,
                f"authorization snapshot still building (waited "
                f"{self._snapshot_wait_s:.0f}s; it continues in the background)",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("table authz snapshot failed for %s: %s", email, exc)
            return AuthzScope.unavailable(
                email, f"snapshot failed: {type(exc).__name__}"
            )

        scope = self._project(email, snap)
        # UNAVAILABLE is NEVER cached (design point 5): pinning a failure would
        # freeze a transient snapshot error into a permanent refusal. Only a
        # computed answer — UNLIMITED or SCOPED — is worth pinning.
        if not scope.is_unavailable:
            self._cache[key] = scope
        return scope

    def evict(self, email: str) -> bool:
        """Drop one pinned scope so the next request recomputes. Returns whether
        anything was pinned — the admin endpoint can report a no-op honestly."""
        return self._cache.pop((email or "").strip().lower(), None) is not None

    def evict_all(self) -> int:
        """Drop every pinned scope; returns how many were dropped."""
        n = len(self._cache)
        self._cache.clear()
        return n

    def _project(self, email: str, snap: Any) -> AuthzScope:
        """Snapshot -> scope, via ``service_index.get_entity_sets``."""
        if snap is None or getattr(snap, "is_error", False):
            return AuthzScope.unavailable(
                email, f"no snapshot ({getattr(snap, 'error', 'unknown')})"
            )
        if getattr(snap, "security_mgr", False) or getattr(snap, "allow_all", False):
            return AuthzScope.unlimited(
                email, "SecurityMgr — every table is reachable", security_mgr=True
            )

        services = sorted(getattr(snap, "allowed_services", ()) or ())
        if not services:
            # A SUCCESSFUL snapshot that grants no menus is an authorization
            # ANSWER, not a failure: no tables (default deny). Cached.
            return AuthzScope.scoped(
                email,
                frozenset(),
                "menu chain resolved zero services — no tables",
            )

        tables: set[str] = set()
        for sid in services:
            try:
                for entity in self._index.get_entity_sets(sid) or ():
                    tables.add(entity.lower())
                    # Entity sets are frequently the PLURAL collection while the
                    # DataSet table is singular (Customers/Customer). Carry both;
                    # a spurious extra name costs nothing under the gate's
                    # bare-name comparison, a missing one costs the right answer.
                    if entity.endswith("s"):
                        tables.add(entity[:-1].lower())
            except Exception:  # noqa: BLE001
                continue
        if not tables:
            # Services exist but the index projected NOTHING — that is an index
            # anomaly (stale/partial service_index), not an answer about the
            # user. UNAVAILABLE so it fails closed AND is retried next request
            # instead of pinning a wrongly-tiny scope for the process lifetime.
            return AuthzScope.unavailable(
                email, f"no tables resolved from {len(services)} allowed services"
            )
        return AuthzScope.scoped(
            email,
            frozenset(tables),
            "menu chain",
            service_count=len(services),
        )

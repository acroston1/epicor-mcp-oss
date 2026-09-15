"""In-memory fake seams for the menu-authz suite — constructor-injection only.

These are duck-typed on purpose. The build agents' real classes
(``EpicorAuthzClient``, ``MenuMapStore``, ``UserAuthzSnapshot`` …) must work
through plain attribute/method access — no ``isinstance`` gates — so these
lightweight stands-in exercise exactly the contract the enforcer / authorizer
depend on, without pulling in network or SQLite.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Iterable

from epicor_mcp.rbac.enforcer import AccessLevel
from epicor_mcp.rbac.user_map import UserProfile


# --------------------------------------------------------------------------- #
# Duck row / identity builders
# --------------------------------------------------------------------------- #

def ident(
    user_id: str,
    *,
    groups: Iterable[str] = (),
    disabled: bool = False,
    security_mgr: bool = False,
    email: str = "",
    name: str = "",
    duplicate: bool = False,
    candidates: Iterable[str] = (),
) -> SimpleNamespace:
    """A resolved Epicor identity (shape of ``EpicorUserIdentity``)."""
    return SimpleNamespace(
        user_id=user_id,
        name=name or user_id,
        email=email or f"{user_id}@example.org",
        groups=tuple(groups),
        disabled=disabled,
        security_mgr=security_mgr,
        duplicate=duplicate,
        candidates=tuple(candidates),
    )


def mrow(
    menu_id: str,
    *,
    sec_code: str = "",
    program: str = "",
    menu_desc: str = "",
    parent_menu_id: str = "",
    enabled: bool = True,
    hidden: bool = False,
) -> SimpleNamespace:
    """A live menu row (shape of ``MenuRow``)."""
    return SimpleNamespace(
        menu_id=menu_id,
        parent_menu_id=parent_menu_id,
        menu_desc=menu_desc or menu_id,
        sec_code=sec_code,
        program=program,
        enabled=enabled,
        hidden=hidden,
    )


def srow(
    sec_code: str = "",
    *,
    allow_all: bool = False,
    disallow_all: bool = False,
    entry_list: str = "",
    no_entry_list: str = "",
) -> SimpleNamespace:
    """A live security row (shape of ``SecurityRow``)."""
    return SimpleNamespace(
        sec_code=sec_code,
        allow_all=allow_all,
        disallow_all=disallow_all,
        entry_list=entry_list,
        no_entry_list=no_entry_list,
    )


# --------------------------------------------------------------------------- #
# Fake clock (drives TTL / stale-grace deterministically)
# --------------------------------------------------------------------------- #

class FakeClock:
    """Monotonic-ish clock the tests advance by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


# --------------------------------------------------------------------------- #
# Fake authz client (no network)
# --------------------------------------------------------------------------- #

class FakeAuthzClient:
    """Stands in for ``EpicorAuthzClient``.

    ``fail=True`` makes every fetch raise, to exercise stale-grace / fail-closed.
    Counters let tests assert tenant-fetch sharing (menus/security fetched once
    per refresh window, not per user).
    """

    def __init__(
        self,
        *,
        identities: dict[str, SimpleNamespace | None] | None = None,
        menus: list[SimpleNamespace] | None = None,
        security: list[SimpleNamespace] | None = None,
    ) -> None:
        self.identities = identities or {}
        self.menus = menus or []
        self.security = security or []
        self.fail = False
        self.user_calls = 0
        self.menu_calls = 0
        self.security_calls = 0
        self.closed = False

    async def fetch_user(self, email: str) -> SimpleNamespace | None:
        self.user_calls += 1
        if self.fail:
            raise RuntimeError("epicor unreachable (fetch_user)")
        return self.identities.get(email.lower())

    async def fetch_menus(self) -> list[SimpleNamespace]:
        self.menu_calls += 1
        if self.fail:
            raise RuntimeError("epicor unreachable (fetch_menus)")
        return list(self.menus)

    async def fetch_security_rows(self) -> list[SimpleNamespace]:
        self.security_calls += 1
        if self.fail:
            raise RuntimeError("epicor unreachable (fetch_security_rows)")
        return list(self.security)

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Fake menu-map store (SQLite-free)
# --------------------------------------------------------------------------- #

class FakeStore:
    """Stands in for ``MenuMapStore``. ``loaded=False`` => fail closed."""

    def __init__(
        self,
        *,
        menu_services: dict[str, set[str]] | None = None,
        baseline: Iterable[str] = (),
        loaded: bool = True,
    ) -> None:
        self._map = {k: set(v) for k, v in (menu_services or {}).items()}
        self._baseline = set(baseline)
        self.loaded = loaded

    def is_loaded(self) -> bool:
        return self.loaded

    def maybe_reload(self) -> bool:
        return False

    def services_for_menus(self, menu_ids: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for mid in menu_ids:
            out |= self._map.get(mid, set())
        return out

    def baseline(self) -> set[str]:
        return set(self._baseline)


# --------------------------------------------------------------------------- #
# Fake snapshot + authorizer for the enforcer tests
# --------------------------------------------------------------------------- #

class FakeSnapshot:
    """Duck of ``UserAuthzSnapshot`` — only what the enforcer reads."""

    def __init__(
        self,
        *,
        allowed: Iterable[str] = (),
        security_mgr: bool = False,
        allow_all: bool = False,
        is_error: bool = False,
        stale: bool = False,
        error: str = "",
    ) -> None:
        self._allowed = set(allowed)
        self.security_mgr = security_mgr
        self.allow_all = allow_all or security_mgr
        self.is_error = is_error
        self.stale = stale
        self.error = error

    def allows(self, service_id: str) -> bool:
        if self.is_error:
            return False
        if self.allow_all:
            return True
        return service_id in self._allowed


class FakeAuthorizer:
    """Duck of ``MenuAuthorizer`` — the sync ``get_snapshot`` hot path only."""

    def __init__(self, snapshots: dict[str, FakeSnapshot] | None = None) -> None:
        self._snap = snapshots or {}

    def get_snapshot(self, user_id: str):
        return self._snap.get(user_id.lower()) or self._snap.get(user_id)

    def inject_snapshot(self, email: str, snap: FakeSnapshot) -> None:
        self._snap[email.lower()] = snap


# --------------------------------------------------------------------------- #
# Fake index / user-map for the enforcer (legacy department path)
# --------------------------------------------------------------------------- #

class FakeIndex:
    """Duck of ``ServiceIndex`` for enforcer department checks."""

    def __init__(
        self,
        *,
        dept_services: dict[str, set[str]] | None = None,
        existing_services: Iterable[str] = (),
    ) -> None:
        self._dept = {k: set(v) for k, v in (dept_services or {}).items()}
        self._exists = set(existing_services)

    def service_exists(self, service_id: str) -> bool:
        return service_id in self._exists

    def get_department_services(self, dept: str) -> set[str]:
        return self._dept.get(dept, set())


class FakeUserMap:
    """Duck of ``UserMap`` for enforcer resolution + key selection."""

    READ_KEY = "READ-KEY"
    WRITE_KEY = "WRITE-KEY"
    BAQ_KEY = "BAQ-KEY"

    def __init__(self, users: dict[str, UserProfile]) -> None:
        self._users = {k.lower(): v for k, v in users.items()}

    def get_user(self, user_id: str) -> UserProfile | None:
        return self._users.get(user_id.lower())

    def get_department_key(self, department: str, baq: bool = False) -> str | None:
        return self.READ_KEY

    def get_read_key(self) -> str | None:
        return self.READ_KEY

    def get_write_key(self) -> str | None:
        return self.WRITE_KEY

    def get_baq_key(self) -> str | None:
        return self.BAQ_KEY


def profile(
    user_id: str,
    *,
    department: str = "Finance",
    extra: Iterable[str] = (),
    access: AccessLevel = AccessLevel.READ_ONLY,
    can_write_baqs: bool = False,
) -> UserProfile:
    """Build a real ``UserProfile`` for enforcer tests."""
    return UserProfile(
        user_id=user_id.lower(),
        epicor_username=user_id,
        department=department,
        access_level=access,
        environment="live",
        display_name=user_id,
        extra_departments=list(extra),
        can_write_baqs=can_write_baqs,
    )

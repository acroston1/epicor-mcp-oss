"""Async, LIVE-only Epicor reader for menu-derived RBAC.

This is the runtime fetch tier of the two-tier authz design: it answers the
three questions the :class:`~epicor_mcp.rbac.menu_authz.MenuAuthorizer` needs to
compute a snapshot, all against **LIVE** and strictly read-only (GET only):

  * ``fetch_user(email)``          -> the resolved :class:`EpicorUserIdentity`
    (UserID, groups, disabled/security-mgr flags) or ``None``.
  * ``fetch_menus()``              -> every :class:`MenuRow` (MenuID -> SecCode /
    Program), paged.
  * ``fetch_security_rows()``      -> every :class:`SecurityRow`
    (SecCode -> AllowAll / DisallowAll / EntryList / NoEntryList), paged.

The reader preserves these Epicor menu-security behaviors:

  * ``UserFileSvc.GetRows`` requires ALL THREE whereClauses — the two unused ones
    must still be present as empty strings, else Epicor 400s.
  * ``GroupList`` is TILDE-delimited; ``EntryList`` / ``NoEntryList`` are kept as
    the raw comma-delimited strings (the evaluation engine splits them).
  * Duplicate email -> pick enabled-first, then lowest UserID case-insensitively.
  * Menu / Security reads page ``absolutePage`` until a short page.
  * Single quotes in the email filter are SQL-escaped.

The client is constructed with the **LIVE base URL passed in** (never derived
from ``settings.environment``) and accepts an injected ``httpx.AsyncClient`` so
tests can drive it with ``httpx.MockTransport`` and no socket is ever opened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# GetRows page size for the tenant-wide menu / security sweeps.  pageSize=0 400s
# in the original deployment, so this is always a positive value.
_PAGE_SIZE = 1000


def _more_pages(body: dict) -> bool:
    """Read Epicor's ``parameters.morePages`` flag from a GetRows response.

    The flag rides a ``parameters`` object; Epicor puts it at the top level and
    also mirrors it under ``returnObj`` — check both so we are robust to either
    JSON path.
    """
    for container in (body, body.get("returnObj", {})):
        if isinstance(container, dict):
            params = container.get("parameters")
            if isinstance(params, dict) and "morePages" in params:
                return bool(params["morePages"])
    return False


# --------------------------------------------------------------------------- #
# Frozen result dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EpicorUserIdentity:
    """A resolved Epicor identity (one row of Ice.BO.UserFile, chosen
    deterministically when an email maps to several)."""

    user_id: str
    name: str
    email: str
    groups: tuple[str, ...]
    disabled: bool
    security_mgr: bool
    duplicate: bool = False
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class MenuRow:
    """One Ice.BO.Menu row — the menu's SecCode + launching program."""

    menu_id: str
    parent_menu_id: str
    menu_desc: str
    sec_code: str
    program: str
    enabled: bool
    hidden: bool


@dataclass(frozen=True)
class SecurityRow:
    """One Ice.BO.Security row.  ``entry_list`` / ``no_entry_list`` stay raw
    comma-delimited strings; the evaluation engine owns the splitting."""

    sec_code: str
    allow_all: bool
    disallow_all: bool
    entry_list: str
    no_entry_list: str


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class EpicorAuthzClient:
    """Read-only async Epicor client for the authz fetch tier.

    Parameters
    ----------
    base_url:
        The **LIVE** OData base, e.g.
        ``https://.../YourInstance/api/v2/odata/YOUR_COMPANY/`` (must end with ``/``).
    username, password:
        Epicor service-account credentials for the password-grant token.
    api_key:
        The ``X-API-Key`` (an admin read key is sufficient — reads only).
    client:
        An optional pre-built ``httpx.AsyncClient`` (test-injection seam).  When
        omitted one is created bound to ``base_url``.
    timeout:
        Per-request timeout in seconds. The default allows for slow security
        pages so a healthy tenant is not prematurely reported as unreachable.
        An httpx timeout can have an empty string representation, so callers
        should preserve the exception type in diagnostics.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not base_url.endswith("/"):
            base_url = base_url + "/"
        self._base_url = base_url
        self._username = username
        self._password = password
        self._api_key = api_key
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._token: str | None = None

    # -- lifecycle ---------------------------------------------------------- #

    async def aclose(self) -> None:
        """Close the underlying httpx client (whether owned or injected)."""
        await self._client.aclose()

    async def __aenter__(self) -> "EpicorAuthzClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    # -- auth --------------------------------------------------------------- #

    @property
    def _token_url(self) -> str:
        """Derive the SaaS-root TokenResource endpoint from the OData base."""
        root = self._base_url.split("api/v2/odata/")[0]
        return root + "TokenResource.svc/"

    async def _get_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        resp = await self._client.post(
            self._token_url,
            data={
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        token = resp.json().get("AccessToken")
        if not token:
            raise httpx.HTTPError("Epicor token response carried no AccessToken")
        self._token = token
        return token

    async def _headers(self, force_token: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._get_token(force=force_token)}",
            "X-API-Key": self._api_key,
            "Accept": "application/json",
        }

    # -- raw GetRows -------------------------------------------------------- #

    async def _get_page(
        self,
        service: str,
        table: str,
        *,
        where: str = "",
        page: int = 1,
        page_size: int = _PAGE_SIZE,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[list[dict], bool]:
        """One page of ``Ice.BO.<service>/GetRows`` -> ``(rows, more_pages)``.

        Retries once on 401/403 with a forced token refresh (read-only GET).
        ``more_pages`` is Epicor's authoritative ``parameters.morePages`` flag —
        NOT short-page detection: MenuSvc returns variable-length pages, so a
        "len < pageSize => done" loop silently truncates the sweep.
        """
        params: dict[str, object] = {
            "whereClause" + table: where,
            "pageSize": page_size,
            "absolutePage": page,
        }
        if extra_params:
            params.update(extra_params)

        for attempt in range(2):
            resp = await self._client.get(
                f"Ice.BO.{service}/GetRows",
                params=params,
                headers=await self._headers(force_token=(attempt == 1)),
                timeout=self._timeout,
            )
            if resp.status_code in (401, 403) and attempt == 0:
                continue
            resp.raise_for_status()
            body = resp.json()
            return_obj = body.get("returnObj", {}) or {}
            rows = return_obj.get(table, []) or []
            more = _more_pages(body)
            return rows, more
        resp.raise_for_status()  # pragma: no cover
        return [], False  # pragma: no cover

    async def _get_rows(self, service: str, table: str, **kwargs) -> list[dict]:
        """A single GetRows page's rows (used for small, filtered reads)."""
        rows, _ = await self._get_page(service, table, **kwargs)
        return rows

    async def _get_all_rows(self, service: str, table: str) -> list[dict]:
        """Page ``absolutePage`` from 1 while ``parameters.morePages`` is truthy."""
        out: list[dict] = []
        page = 1
        while True:
            rows, more = await self._get_page(service, table, page=page)
            out.extend(rows)
            if not more:
                break
            page += 1
        return out

    @staticmethod
    def _sql_escape(value: str) -> str:
        return value.replace("'", "''")

    # -- public fetches ----------------------------------------------------- #

    async def fetch_user(self, email: str) -> EpicorUserIdentity | None:
        """Resolve ``email`` to a single Epicor identity, or ``None``.

        Duplicate-email rule (live-verified): when several UserFile rows share an
        address, pick in this order — (1) the enabled row whose UserID matches the
        email LOCAL-PART case-insensitively (the human account rather than a
        kiosk account that shares an address), (2) any enabled row, (3) the lowest UserID case-insensitively.
        A *disabled* local-part row does not win rule 1 — it falls through.
        ``duplicate`` / ``candidates`` surface the collision so callers can flag it.
        """
        where = f"EMailAddress = '{self._sql_escape(email)}'"
        rows = await self._get_rows(
            "UserFileSvc",
            "UserFile",
            where=where,
            page_size=200,
            extra_params={"whereClauseUserComp": "", "whereClauseUserCompExt": ""},
        )
        if not rows:
            return None

        candidates = tuple(sorted(r.get("UserID", "") for r in rows))
        local_part = email.split("@")[0].lower()

        def _rank(r: dict) -> tuple:
            uid = r.get("UserID") or ""
            disabled = bool(r.get("UserDisabled"))
            local_match = (not disabled) and uid.lower() == local_part
            return (0 if local_match else 1, disabled, uid.lower())

        chosen = sorted(rows, key=_rank)[0]
        group_list = chosen.get("GroupList") or ""
        return EpicorUserIdentity(
            user_id=chosen.get("UserID", ""),
            name=chosen.get("Name", "") or chosen.get("UserID", ""),
            email=chosen.get("EMailAddress", "") or email,
            groups=tuple(g for g in group_list.split("~") if g),
            disabled=bool(chosen.get("UserDisabled")),
            security_mgr=bool(chosen.get("SecurityMgr")),
            duplicate=len(rows) > 1,
            candidates=candidates,
        )

    async def fetch_menus(self) -> list[MenuRow]:
        """Every Ice.BO.Menu row, paged."""
        rows = await self._get_all_rows("MenuSvc", "Menu")
        return [
            MenuRow(
                menu_id=r.get("MenuID", ""),
                parent_menu_id=r.get("ParentMenuID", "") or "",
                menu_desc=r.get("MenuDesc", "") or "",
                sec_code=(r.get("SecCode") or "").strip(),
                program=(r.get("Program") or r.get("ProgramKinetic") or ""),
                enabled=bool(r.get("MenuEnabled", True)),
                hidden=bool(r.get("Hidden", False)),
            )
            for r in rows
        ]

    async def fetch_security_rows(self) -> list[SecurityRow]:
        """Every Ice.BO.Security row, paged."""
        rows = await self._get_all_rows("SecuritySvc", "Security")
        return [
            SecurityRow(
                sec_code=r.get("SecCode", "") or "",
                allow_all=bool(r.get("AllowAll")),
                disallow_all=bool(r.get("DisallowAll")),
                entry_list=r.get("EntryList") or "",
                no_entry_list=r.get("NoEntryList") or "",
            )
            for r in rows
        ]

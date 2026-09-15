"""EpicorAuthzClient (LIVE-only reads, prototype-faithful).

Uses httpx.MockTransport (respx is not installed in the shared venv; MockTransport
needs no extra dependency).  Pins, against a fake Epicor:

  * frozen result dataclasses ``EpicorUserIdentity`` / ``MenuRow`` / ``SecurityRow``;
  * UserFileSvc.GetRows carries ALL THREE whereClauses (else Epicor 400s);
  * GroupList is tilde-split; EntryList stays a raw string on SecurityRow;
  * SQL single-quotes are escaped in the email whereClause;
  * duplicate email -> (1) the enabled row whose UserID equals the email
    local-part (case-insensitive), then (2) enabled rows, then (3) lowest UserID
    case-insensitive;
  * menus/security page via absolutePage, driven by the response's
    parameters.morePages flag (continue while truthy, stop when falsy) — NOT
    short-page detection (MenuSvc returns variable-length pages with
    morePages=True mid-stream).

The client is constructed with an INJECTED ``httpx.AsyncClient`` so no socket is
ever opened.
"""

from __future__ import annotations

import json

import httpx
import pytest

from epicor_mcp.rbac.epicor_authz import (
    EpicorAuthzClient,
    EpicorUserIdentity,
    MenuRow,
    SecurityRow,
)

from fixtures.authz import getrows_envelope, menu_row, security_row, userfile_row

BASE = "https://epicor.example.com/YourInstance/api/v2/odata/DEMO/"


def _merge_params(request: httpx.Request) -> dict:
    """GetRows may be issued as GET (query) or POST (body); merge both."""
    params = dict(request.url.params)
    if request.content:
        try:
            body = json.loads(request.content)
            if isinstance(body, dict):
                params = {**body, **params}
        except (ValueError, TypeError):
            pass
    return params


def _client(handler) -> EpicorAuthzClient:
    transport = httpx.MockTransport(handler)
    injected = httpx.AsyncClient(transport=transport, base_url=BASE)
    return EpicorAuthzClient(
        base_url=BASE,
        username="svc",
        password="pw",
        api_key="key",
        client=injected,
    )


# --------------------------------------------------------------------------- #
# fetch_user
# --------------------------------------------------------------------------- #

async def test_fetch_user_parses_identity_and_tilde_splits_groups():
    def handler(request):
        path = request.url.path
        if "TokenResource" in path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        if "UserFileSvc" in path:
            rows = [userfile_row("apuser", "apuser@example.org",
                                 group_list="APP~PURCH~ENG")]
            return httpx.Response(200, json=getrows_envelope("UserFile", rows))
        return httpx.Response(200, json=getrows_envelope("UserFile", []))

    ident = await _client(handler).fetch_user("apuser@example.org")
    assert isinstance(ident, EpicorUserIdentity)
    assert ident.user_id == "apuser"
    assert tuple(ident.groups) == ("APP", "PURCH", "ENG")
    assert ident.disabled is False
    assert ident.security_mgr is False


async def test_fetch_user_sends_all_three_whereclauses_and_escapes_quotes():
    captured: dict = {}

    def handler(request):
        path = request.url.path
        if "TokenResource" in path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        if "UserFileSvc" in path:
            captured["params"] = _merge_params(request)
            return httpx.Response(200, json=getrows_envelope("UserFile", []))
        return httpx.Response(200, json=getrows_envelope("UserFile", []))

    await _client(handler).fetch_user("o'brien@example.org")
    params = captured["params"]
    # All three whereClauses must be present (the two unused ones as empty strings).
    assert "whereClauseUserFile" in params
    assert "whereClauseUserComp" in params
    assert "whereClauseUserCompExt" in params
    # SQL-escaped apostrophe.
    assert "o''brien@example.org" in params["whereClauseUserFile"]
    assert "EMailAddress" in params["whereClauseUserFile"]


async def test_fetch_user_unknown_email_returns_none():
    def handler(request):
        if "TokenResource" in request.url.path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        return httpx.Response(200, json=getrows_envelope("UserFile", []))

    assert await _client(handler).fetch_user("ghost@example.com") is None


async def test_fetch_user_security_mgr_flag_surfaces():
    def handler(request):
        if "TokenResource" in request.url.path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        rows = [userfile_row("adminuser", "adminuser@example.org",
                             group_list="SysAdmin", security_mgr=True)]
        return httpx.Response(200, json=getrows_envelope("UserFile", rows))

    ident = await _client(handler).fetch_user("adminuser@example.org")
    assert ident.security_mgr is True


# --------------------------------------------------------------------------- #
# duplicate-email rule: (1) enabled row whose UserID ==
# email local-part (case-insensitive), then (2) enabled rows, then (3) lowest
# UserID case-insensitive. The local-part tier keeps a real human from being
# shadowed by a kiosk/service account that reuses their email.
# --------------------------------------------------------------------------- #

async def test_duplicate_email_prefers_local_part_match_over_lower_kiosk_id():
    # Synthetic kiosk IDs sort before the human ID; the email local-part
    # match must still select the human and preserve the human group set.
    def handler(request):
        if "TokenResource" in request.url.path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        rows = [
            userfile_row("01Kiosk1", "sampleuser@example.org", group_list="SITE_A"),
            userfile_row("01Kiosk2", "sampleuser@example.org", group_list="SITE_A"),
            userfile_row("01Kiosk3", "sampleuser@example.org", group_list="SITE_A"),
            userfile_row("sampleuser", "sampleuser@example.org",
                         group_list="SITE_A~APP~ARP~GLP~PRP"),
        ]
        return httpx.Response(200, json=getrows_envelope("UserFile", rows))

    ident = await _client(handler).fetch_user("sampleuser@example.org")
    assert ident.user_id == "sampleuser"       # local-part match wins over lower kiosk id
    assert ident.duplicate is True
    assert "APP" in ident.groups           # got the human's full group set


async def test_duplicate_email_local_part_match_is_case_insensitive():
    # UserID casing need not match the address casing.
    def handler(request):
        if "TokenResource" in request.url.path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        rows = [
            userfile_row("01Kiosk1", "SampleUser@example.org", group_list="SITE_A"),
            userfile_row("SAMPLEUSER", "SampleUser@example.org", group_list="SITE_A~APP"),
        ]
        return httpx.Response(200, json=getrows_envelope("UserFile", rows))

    ident = await _client(handler).fetch_user("SampleUser@example.org")
    assert ident.user_id == "SAMPLEUSER"


async def test_duplicate_email_prefers_enabled_over_lower_disabled_id():
    def handler(request):
        if "TokenResource" in request.url.path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        rows = [
            userfile_row("aaa", "dup@example.com", group_list="A", disabled=True),
            userfile_row("zzz", "dup@example.com", group_list="Z", disabled=False),
        ]
        return httpx.Response(200, json=getrows_envelope("UserFile", rows))

    ident = await _client(handler).fetch_user("dup@example.com")
    assert ident.user_id == "zzz"          # enabled wins over lower-but-disabled
    assert ident.duplicate is True
    assert set(ident.candidates) == {"aaa", "zzz"}


async def test_duplicate_email_lowest_userid_among_enabled():
    def handler(request):
        if "TokenResource" in request.url.path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        rows = [
            userfile_row("Zeta", "dup2@example.com", group_list="Z"),
            userfile_row("alpha", "dup2@example.com", group_list="A"),
        ]
        return httpx.Response(200, json=getrows_envelope("UserFile", rows))

    ident = await _client(handler).fetch_user("dup2@example.com")
    assert ident.user_id == "alpha"        # case-insensitive lowest
    assert ident.duplicate is True


# --------------------------------------------------------------------------- #
# fetch_menus / fetch_security_rows — paging + row mapping
# --------------------------------------------------------------------------- #

async def test_fetch_menus_pages_while_moreflag_true_not_short_page():
    # Variable-length pages can report morePages=True mid-stream. Continue
    # while parameters.morePages is truthy and stop only when it is falsy.
    # Every page below is SHORT relative to any real pageSize, so a short-page
    # ("len < pageSize => done") loop stops after page 1 and loses pages 2-4.
    pages: dict[int, tuple[list, bool]] = {
        1: ([menu_row("MP1a", sec_code="S", program="Erp.UI.X"),
             menu_row("MP1b", sec_code="S", program="Erp.UI.X"),
             menu_row("MP1c", sec_code="S", program="Erp.UI.X")], True),
        2: ([menu_row("MP2", sec_code="S", program="Erp.UI.X")], True),   # short, more to come
        3: ([menu_row("MP3a", sec_code="S", program="Erp.UI.X"),
             menu_row("MP3b", sec_code="S", program="Erp.UI.X")], True),
        4: ([menu_row("MFINAL", sec_code="S", program="Erp.UI.X")], False),  # last page
    }
    pages_seen: list[int] = []

    def handler(request):
        if "TokenResource" in request.url.path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        params = _merge_params(request)
        page = int(params.get("absolutePage", 1))
        pages_seen.append(page)
        rows, more = pages.get(page, ([], False))
        return httpx.Response(
            200, json=getrows_envelope("Menu", rows, more_pages=more)
        )

    menus = await _client(handler).fetch_menus()
    ids = {m.menu_id for m in menus}

    # Drove on morePages: all four pages fetched, and it STOPPED once the flag
    # went false (no page 5).
    assert sorted(set(pages_seen)) == [1, 2, 3, 4]
    # Every row across the variable-length pages was collected (short-page
    # detection would have returned only page 1's three rows).
    assert len(menus) == 7
    assert {"MP2", "MP3a", "MP3b", "MFINAL"} <= ids
    assert isinstance(menus[0], MenuRow)
    assert menus[0].sec_code == "S"
    assert menus[0].program == "Erp.UI.X"


async def test_fetch_security_rows_maps_flags_and_lists():
    def handler(request):
        if "TokenResource" in request.url.path:
            return httpx.Response(200, json={"AccessToken": "tok"})
        rows = [
            security_row("APSEC", entry_list="APP,jsmith", no_entry_list="baddie"),
            security_row("OPEN", allow_all=True),
        ]
        return httpx.Response(200, json=getrows_envelope("Security", rows))

    secs = await _client(handler).fetch_security_rows()
    by_code = {s.sec_code: s for s in secs}
    assert isinstance(by_code["APSEC"], SecurityRow)
    assert by_code["APSEC"].entry_list == "APP,jsmith"   # raw string preserved
    assert by_code["APSEC"].no_entry_list == "baddie"
    assert by_code["APSEC"].allow_all is False
    assert by_code["OPEN"].allow_all is True


async def test_dataclasses_are_frozen():
    ident = EpicorUserIdentity(
        user_id="u", name="U", email="u@example.com", groups=("APP",),
        disabled=False, security_mgr=False,
    )
    with pytest.raises(Exception):
        ident.user_id = "other"  # type: ignore[misc]

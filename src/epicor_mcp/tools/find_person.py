"""Tool: epicor_find_person

Search for people by name in Epicor.  Searches both UserFile (system users
with email addresses) and EmpBasic (all employees) to maximize coverage.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)


def _norm_name(s: str) -> str:
    """Case/whitespace-insensitive name key ('jAnE dOe' == 'JANE DOE')."""
    return " ".join((s or "").lower().split())


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_find_person`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_find_person(
        search_name: str,
        top: int = 20,
    ) -> str:
        """Resolve ANY person/user/employee reference in Epicor — names,
        login/user IDs, or employee numbers — to a cross-referenced identity.

        Call this FIRST whenever a question mentions a person or account
        (e.g. "service-account", "Example User", "employee 5101") BEFORE filtering ERP
        columns. Each match returns up to: ``name``, ``email``, ``user_id``
        (Epicor login, for CreatedBy/ApprovedBy/EntryPerson/DcdUserID-style
        columns) and ``emp_id`` (employee number, for EmployeeNum-style
        columns on LaborDtl etc.). Use the RIGHT id for the column you
        filter — never put a login string in an EmployeeNum column.

        Searches Epicor system users (UserFile: exact UserID + name) and
        employees (EmpBasic: exact EmpID, exact DcdUserID login link, +
        name), including accounts with no email address.

        Examples
        --------
        - ``epicor_find_person(search_name="service-account")``   (login id)
        - ``epicor_find_person(search_name="5101")``    (employee number)
        - ``epicor_find_person(search_name="Example User")``

        Parameters
        ----------
        search_name : str
            Name (full or partial), Epicor user/login ID, or employee
            number.  Case-insensitive.
        top : int, optional
            Maximum results to return, 1--50 (default ``20``).
        """
        try:
            session = get_current_session()

            baq_result = rbac.check_baq_access(session.user_id)
            if not baq_result.allowed:
                return json.dumps({"error": baq_result.message})
            api_key = baq_result.api_key or ""

            top = max(1, min(top, 50))
            search = search_name.strip()
            if not search:
                return json.dumps({"error": "search_name cannot be empty"})
            q = search.replace("'", "''")  # OData single-quote escape
            single_token = " " not in search

            matches: list[dict] = []

            def _merge(name: str, email: str, user_id: str = "",
                       emp_id: str = "", source: str = "") -> None:
                """Add a hit, merging with an existing entry that shares an
                email, user_id, or emp_id (the UserFile<->EmpBasic link)."""
                name = (name or "").strip()
                email = (email or "").strip()
                user_id = (user_id or "").strip()
                emp_id = (emp_id or "").strip()
                # Skip inactive (Epicor convention: name prefixed "XX")
                if name.startswith("XX"):
                    return
                for m in matches:
                    # Cross-source name link: same display name where one
                    # side has only a login and the other only an employee
                    # number (tenants often leave DcdUserID blank). Guarded
                    # one-sided so two same-named employees never merge.
                    name_link = (
                        name
                        and _norm_name(m.get("name", "")) == _norm_name(name)
                        and (
                            (m.get("user_id") and not m.get("emp_id") and emp_id)
                            or (m.get("emp_id") and not m.get("user_id") and user_id)
                        )
                    )
                    if (
                        (user_id and m.get("user_id", "").lower() == user_id.lower())
                        or (email and m.get("email", "").lower() == email.lower())
                        or (emp_id and m.get("emp_id") == emp_id)
                        or name_link
                    ):
                        for key, val in (("name", name), ("email", email),
                                         ("user_id", user_id), ("emp_id", emp_id)):
                            if val and not m.get(key):
                                m[key] = val
                        return
                entry = {"name": name, "source": source}
                if email:
                    entry["email"] = email
                if user_id:
                    entry["user_id"] = user_id
                if emp_id:
                    entry["emp_id"] = emp_id
                matches.append(entry)

            # --- Source 1: UserFile (system users incl. service accounts) ---
            # Exact-login leg matters: accounts like 'service-account' don't contain
            # their login in the display Name, and service/kiosk accounts
            # often have NO email — they are still valid resolutions.
            try:
                uf_filter = f"contains(Name, '{q}')"
                if single_token:
                    uf_filter = f"UserID eq '{q}' or {uf_filter}"
                uf_resp = await client.get(
                    "Ice.BO.UserFileSvc/UserFiles",
                    api_key,
                    params={
                        "$filter": uf_filter,
                        "$select": "UserID,Name,EMailAddress",
                        "$top": top,
                        "$orderby": "Name",
                    },
                )
                for r in uf_resp.get("value", []):
                    _merge(
                        r.get("Name"), r.get("EMailAddress"),
                        user_id=r.get("UserID"), source="UserFile",
                    )
            except Exception:
                logger.warning("UserFile search failed, continuing with EmpBasic", exc_info=True)

            # --- Source 2: EmpBasic (employees; DcdUserID = login link) ---
            try:
                parts = search.split()
                if single_token:
                    emp_filter = (
                        f"EmpID eq '{q}' or DcdUserID eq '{q}' "
                        f"or contains(Name, '{q}')"
                    )
                else:
                    first = parts[0].replace("'", "''")
                    last = parts[-1].replace("'", "''")
                    emp_filter = (
                        f"(contains(FirstName, '{first}') and "
                        f"contains(LastName, '{last}'))"
                    )
                # Cross-link: also pull employee rows for the user logins
                # found above, so one call returns user_id<->emp_id pairs.
                uids = [m["user_id"].replace("'", "''")
                        for m in matches if m.get("user_id")][:5]
                if uids:
                    link = " or ".join(f"DcdUserID eq '{u}'" for u in uids)
                    emp_filter = f"({emp_filter}) or {link}"

                emp_resp = await client.get(
                    "Erp.BO.EmpBasicSvc/EmpBasics",
                    api_key,
                    params={
                        "$filter": emp_filter,
                        "$select": "EmpID,FirstName,LastName,Name,EMailAddress,DcdUserID",
                        "$top": top,
                        "$orderby": "LastName,FirstName",
                    },
                )
                for r in emp_resp.get("value", []):
                    _merge(
                        r.get("Name"), r.get("EMailAddress"),
                        user_id=r.get("DcdUserID"),
                        emp_id=r.get("EmpID"), source="Employee",
                    )

                # --- Name-link pass: login hits with no employee number ---
                # Some tenants leave EmpBasic.DcdUserID blank, so the login
                # link above can miss; an exact-name query still pairs
                # UserFile 'jAnE dOe' with EmpBasic 'JANE DOE' (SQL collation is
                # case-insensitive). One extra call for up to 3 hits.
                unlinked = [m for m in matches
                            if m.get("user_id") and not m.get("emp_id")
                            and m.get("name")][:3]
                if unlinked:
                    name_filter = " or ".join(
                        "Name eq '{}'".format(m["name"].replace("'", "''"))
                        for m in unlinked
                    )
                    link_resp = await client.get(
                        "Erp.BO.EmpBasicSvc/EmpBasics",
                        api_key,
                        params={
                            "$filter": name_filter,
                            "$select": "EmpID,Name,EMailAddress,DcdUserID",
                            "$top": 10,
                        },
                    )
                    for r in link_resp.get("value", []):
                        _merge(
                            r.get("Name"), r.get("EMailAddress"),
                            user_id=r.get("DcdUserID"),
                            emp_id=r.get("EmpID"), source="Employee",
                        )
            except Exception:
                    logger.warning("EmpBasic search failed", exc_info=True)

            matches = matches[:top]
            result: dict = {
                "matches": matches,
                "match_count": len(matches),
            }
            if matches:
                result["usage"] = (
                    "user_id = Epicor login (filter CreatedBy/ApprovedBy/"
                    "EntryPerson/DcdUserID columns); emp_id = employee "
                    "number (filter EmployeeNum columns, e.g. LaborDtl). "
                    "Use the id matching the column type — never a login "
                    "string in EmployeeNum."
                )
            else:
                result["note"] = (
                    f"No people found matching '{search_name}'. "
                    "Try a different spelling or a shorter search term."
                )

            return format_response(result, records_key=None)

        except Exception:
            logger.exception("epicor_find_person failed")
            return json.dumps(
                {"error": f"Person search for '{search_name}' failed. Check the name and try again."}
            )

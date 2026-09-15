"""Shared synthetic fixtures for the menu-derived RBAC suite.

Everything here is hermetic: raw Epicor-GetRows-shaped dicts, tmp
``menu_security.db`` builders, and in-memory fake seams (client / clock).
No network, no auth-weakening env flags. The shapes match the
menu-map database writer and authorization client interfaces.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Raw Epicor GetRows payload shapes.  GetRows returns:
#   {"returnObj": {"<TableName>": [ {row}, {row}, ... ]}}
# The authz client parses exactly this shape (prototype-faithful).
# --------------------------------------------------------------------------- #


def getrows_envelope(
    table: str,
    rows: list[dict[str, Any]],
    *,
    more_pages: bool = False,
    page_size: int | None = None,
    absolute_page: int | None = None,
) -> dict[str, Any]:
    """Wrap ``rows`` in the returnObj/<table> envelope GetRows emits.

    Epicor GetRows echoes a ``parameters`` object (sibling of ``returnObj``)
    carrying ``morePages`` — the authoritative "more to fetch?" flag. Paging is
    driven by that flag, NOT by short-page detection (MenuSvc returns
    variable-length pages). ``more_pages`` defaults to
    False so single-page fixtures naturally signal "stop". The flag is mirrored
    under ``returnObj`` too so the reader is robust to either JSON path.
    """
    params: dict[str, Any] = {"morePages": bool(more_pages)}
    if page_size is not None:
        params["pageSize"] = page_size
    if absolute_page is not None:
        params["absolutePage"] = absolute_page
    return {
        "returnObj": {table: rows, "parameters": dict(params)},
        "parameters": dict(params),
    }


def userfile_row(
    user_id: str,
    email: str,
    *,
    group_list: str = "",
    disabled: bool = False,
    security_mgr: bool = False,
    name: str = "",
) -> dict[str, Any]:
    """A single Ice.BO.UserFile row. GroupList is TILDE-delimited (Epicor)."""
    return {
        "UserID": user_id,
        "Name": name or user_id,
        "EMailAddress": email,
        "GroupList": group_list,
        "UserDisabled": disabled,
        "SecurityMgr": security_mgr,
    }


def menu_row(
    menu_id: str,
    *,
    sec_code: str = "",
    program: str = "",
    menu_desc: str = "",
    parent_menu_id: str = "",
    enabled: bool = True,
    hidden: bool = False,
) -> dict[str, Any]:
    """A single Ice.BO.Menu row."""
    return {
        "MenuID": menu_id,
        "ParentMenuID": parent_menu_id,
        "MenuDesc": menu_desc or menu_id,
        "SecCode": sec_code,
        "Program": program,
        "MenuEnabled": enabled,
        "Hidden": hidden,
    }


def security_row(
    sec_code: str,
    *,
    allow_all: bool = False,
    disallow_all: bool = False,
    entry_list: str = "",
    no_entry_list: str = "",
) -> dict[str, Any]:
    """A single Ice.BO.Security row. Entry/NoEntry lists are COMMA-delimited."""
    return {
        "SecCode": sec_code,
        "AllowAll": allow_all,
        "DisallowAll": disallow_all,
        "EntryList": entry_list,
        "NoEntryList": no_entry_list,
    }


# --------------------------------------------------------------------------- #
# A coherent tenant used across compute-snapshot / enforcer tests.
#
#   Menus:
#     AP0100  SecCode APSEC  program Erp.UI.APInvoiceEntry
#     AP0200  SecCode APSEC  program Erp.UI.APAdjustmentEntry
#     PO0100  SecCode POSEC  program Erp.UI.POEntry
#   Security:
#     APSEC   positive allow-list, EntryList "APP"   (AP group only)
#     POSEC   positive allow-list, EntryList "PURCH" (Purchasing group only)
#   Menu -> service mapping (menu_security.db):
#     AP0100 -> Erp.BO.APInvoiceSvc
#     AP0200 -> Erp.BO.APAdjustmentSvc, Erp.BO.VendorSvc
#     PO0100 -> Erp.BO.POSvc
#   Baseline: Ice.BO.CompanySvc (justified core-context grant)
# --------------------------------------------------------------------------- #

TENANT_MENUS: list[dict[str, Any]] = [
    menu_row("AP0100", sec_code="APSEC", program="Erp.UI.APInvoiceEntry",
             menu_desc="AP Invoice Entry"),
    menu_row("AP0200", sec_code="APSEC", program="Erp.UI.APAdjustmentEntry",
             menu_desc="AP Adjustment Entry"),
    menu_row("PO0100", sec_code="POSEC", program="Erp.UI.POEntry",
             menu_desc="Purchase Order Entry"),
    # Launchable-but-unmapped: has a program but no service rows in the map db.
    menu_row("QA0100", sec_code="", program="Erp.UI.SomeUnmappedEntry",
             menu_desc="Unmapped Entry"),
    # Non-launchable: no program -> must NOT contribute grants even if allowed.
    menu_row("HDR0100", sec_code="", program="", menu_desc="Header (no program)"),
]

TENANT_SECURITY: list[dict[str, Any]] = [
    security_row("APSEC", entry_list="APP"),
    security_row("POSEC", entry_list="PURCH"),
]

# menu_id -> list of (service_id, source)
TENANT_MENU_SERVICES: dict[str, list[tuple[str, str]]] = {
    "AP0100": [("Erp.BO.APInvoiceSvc", "metafx")],
    "AP0200": [("Erp.BO.APAdjustmentSvc", "metafx"), ("Erp.BO.VendorSvc", "metafx")],
    "PO0100": [("Erp.BO.POSvc", "metafx")],
    # QA0100 intentionally has no mapping (unmapped program).
}

TENANT_BASELINE: list[tuple[str, str]] = [
    ("Ice.BO.CompanySvc", "core company context — every user needs it"),
]

# Synthetic users representing the authorization scenarios.
USER_SECMGR = userfile_row(
    "adminuser", "adminuser@example.org",
    group_list="APP~SysAdmin", security_mgr=True, name="Alex Admin",
)
USER_AP = userfile_row(
    "apuser", "apuser@example.org",
    group_list="APP", name="Pat Accounts",
)
USER_ENG = userfile_row(
    "enguser", "enguser@example.org",
    group_list="ENG", name="Erin Engineer",
)
USER_DISABLED = userfile_row(
    "olduser", "olduser@example.org",
    group_list="APP", disabled=True,
)


# --------------------------------------------------------------------------- #
# menu_security.db builder.
#
# Uses rbac.menu_map_builder.write_menu_map_db when available, with an inline
# schema fallback for isolated fixture use.
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE menus (
    menu_id        TEXT PRIMARY KEY,
    parent_menu_id TEXT,
    menu_desc      TEXT,
    sec_code       TEXT,
    program        TEXT,
    app_id         TEXT,
    enabled        INTEGER,
    hidden         INTEGER,
    map_strategy   TEXT,
    map_confidence REAL
);
CREATE TABLE menu_services (
    menu_id    TEXT,
    service_id TEXT,
    source     TEXT,
    PRIMARY KEY (menu_id, service_id)
);
CREATE INDEX idx_menu_services_service ON menu_services (service_id);
CREATE TABLE baseline_services (
    service_id    TEXT PRIMARY KEY,
    justification TEXT
);
CREATE TABLE unmapped_programs (
    program         TEXT PRIMARY KEY,
    menu_count      INTEGER,
    sample_menu_ids TEXT
);
"""


def build_menu_security_db(
    path: Path,
    *,
    menus: Iterable[dict[str, Any]] = TENANT_MENUS,
    menu_services: dict[str, list[tuple[str, str]]] | None = None,
    baseline: Iterable[tuple[str, str]] = TENANT_BASELINE,
    meta: dict[str, str] | None = None,
) -> Path:
    """Write a menu_security.db at ``path`` and return it.

    Routes through ``rbac.menu_map_builder.write_menu_map_db`` when that module
    is available so the fixture exercises the production writer; otherwise uses
    the inline synthetic schema.
    """
    menu_services = TENANT_MENU_SERVICES if menu_services is None else menu_services

    try:  # prefer the real writer once build agents ship it
        from epicor_mcp.rbac import menu_map_builder  # type: ignore

        write = getattr(menu_map_builder, "write_menu_map_db", None)
        if write is not None:
            ms_rows = [
                (mid, sid, src)
                for mid, pairs in menu_services.items()
                for (sid, src) in pairs
            ]
            write(
                path,
                menus=[_menu_map_record(m, menu_services) for m in menus],
                menu_services=ms_rows,
                baseline_services=list(baseline),
                unmapped_programs=[],
                meta=meta or {"built_at": "test", "coverage_pct": "1.0"},
            )
            return path
    except Exception:
        # Real builder not present / different signature — fall back inline.
        pass

    return _build_inline(path, menus, menu_services, baseline, meta)


def _menu_map_record(
    m: dict[str, Any], menu_services: dict[str, list[tuple[str, str]]]
) -> dict[str, Any]:
    """Shape one menu row for the builder write fn."""
    mid = m["MenuID"]
    strategy = "metafx" if menu_services.get(mid) else "unmapped"
    return {
        "menu_id": mid,
        "parent_menu_id": m.get("ParentMenuID", ""),
        "menu_desc": m.get("MenuDesc", ""),
        "sec_code": m.get("SecCode", ""),
        "program": m.get("Program", ""),
        "app_id": "",
        "enabled": bool(m.get("MenuEnabled", True)),
        "hidden": bool(m.get("Hidden", False)),
        "map_strategy": strategy,
        "map_confidence": 1.0 if strategy == "metafx" else 0.0,
    }


def _build_inline(
    path: Path,
    menus: Iterable[dict[str, Any]],
    menu_services: dict[str, list[tuple[str, str]]],
    baseline: Iterable[tuple[str, str]],
    meta: dict[str, str] | None,
) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        for m in menus:
            rec = _menu_map_record(m, menu_services)
            conn.execute(
                "INSERT INTO menus (menu_id, parent_menu_id, menu_desc, sec_code, "
                "program, app_id, enabled, hidden, map_strategy, map_confidence) "
                "VALUES (:menu_id, :parent_menu_id, :menu_desc, :sec_code, :program, "
                ":app_id, :enabled, :hidden, :map_strategy, :map_confidence)",
                rec,
            )
        for mid, pairs in menu_services.items():
            for sid, src in pairs:
                conn.execute(
                    "INSERT OR IGNORE INTO menu_services (menu_id, service_id, source) "
                    "VALUES (?, ?, ?)",
                    (mid, sid, src),
                )
        for sid, just in baseline:
            conn.execute(
                "INSERT OR REPLACE INTO baseline_services (service_id, justification) "
                "VALUES (?, ?)",
                (sid, just),
            )
        for k, v in (meta or {"built_at": "test", "coverage_pct": "1.0"}).items():
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (k, str(v))
            )
        conn.commit()
    finally:
        conn.close()
    return path

"""MenuMapStore (structural menu->service map) + the builder's
atomic write function.

The store is the nightly-built, mtime-hot-reloaded SQLite half of the two-tier
design: it answers "which services do these menus touch?" and "what's the
justified baseline?", and reports ``is_loaded() == False`` (=> fail closed) when
the db is missing.  ``write_menu_map_db`` is the atomic writer both the nightly
builder and the test fixtures use; this pins the shared database schema.
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from epicor_mcp.rbac.menu_map_store import MenuMapStore

from fixtures.authz import build_menu_security_db


# --------------------------------------------------------------------------- #
# MenuMapStore
# --------------------------------------------------------------------------- #

def test_store_loads_and_reports_loaded(tmp_path):
    db = build_menu_security_db(tmp_path / "menu_security.db")
    store = MenuMapStore(db)
    assert store.is_loaded() is True


def test_services_for_menus_unions_mapped_services(tmp_path):
    db = build_menu_security_db(tmp_path / "menu_security.db")
    store = MenuMapStore(db)
    services = store.services_for_menus(["AP0100", "AP0200"])
    assert services == {
        "Erp.BO.APInvoiceSvc",
        "Erp.BO.APAdjustmentSvc",
        "Erp.BO.VendorSvc",
    }


def test_services_for_unmapped_menu_is_empty(tmp_path):
    db = build_menu_security_db(tmp_path / "menu_security.db")
    store = MenuMapStore(db)
    assert store.services_for_menus(["QA0100"]) == set()


def test_baseline_returns_justified_services(tmp_path):
    db = build_menu_security_db(tmp_path / "menu_security.db")
    store = MenuMapStore(db)
    assert store.baseline() == {"Ice.BO.CompanySvc"}


def test_missing_db_is_not_loaded_and_fails_closed(tmp_path):
    store = MenuMapStore(tmp_path / "does_not_exist.db")
    assert store.is_loaded() is False
    # Fail closed: no services, no baseline, rather than raising.
    assert store.services_for_menus(["AP0100"]) == set()
    assert store.baseline() == set()


def test_maybe_reload_picks_up_a_rebuild(tmp_path):
    path = tmp_path / "menu_security.db"
    build_menu_security_db(path)
    store = MenuMapStore(path)
    assert store.services_for_menus(["AP0100"]) == {"Erp.BO.APInvoiceSvc"}

    # Nightly rebuild remaps AP0100 to a different service.
    build_menu_security_db(
        path,
        menu_services={"AP0100": [("Erp.BO.RemappedSvc", "override")]},
    )
    os.utime(path, (time.time() + 10, time.time() + 10))  # ensure newer mtime

    assert store.maybe_reload() is True
    assert store.services_for_menus(["AP0100"]) == {"Erp.BO.RemappedSvc"}


# --------------------------------------------------------------------------- #
# write_menu_map_db — atomic writer and shared schema
# --------------------------------------------------------------------------- #

def test_write_menu_map_db_creates_plan_schema(tmp_path):
    from epicor_mcp.rbac.menu_map_builder import write_menu_map_db

    path = tmp_path / "built.db"
    write_menu_map_db(
        path,
        menus=[{
            "menu_id": "AP0100", "parent_menu_id": "", "menu_desc": "AP Invoice",
            "sec_code": "APSEC", "program": "Erp.UI.APInvoiceEntry", "app_id": "",
            "enabled": True, "hidden": False, "map_strategy": "metafx",
            "map_confidence": 1.0,
        }],
        menu_services=[("AP0100", "Erp.BO.APInvoiceSvc", "metafx")],
        baseline_services=[("Ice.BO.CompanySvc", "core context")],
        unmapped_programs=[("Erp.UI.Mystery", 3, "M1,M2,M3")],
        meta={"built_at": "2026-07-08", "coverage_pct": "0.91"},
    )

    conn = sqlite3.connect(str(path))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"meta", "menus", "menu_services", "baseline_services",
                "unmapped_programs"} <= tables

        cols = {r[1] for r in conn.execute("PRAGMA table_info(menus)")}
        assert {"menu_id", "parent_menu_id", "menu_desc", "sec_code", "program",
                "app_id", "enabled", "hidden", "map_strategy",
                "map_confidence"} <= cols

        assert conn.execute(
            "SELECT service_id FROM menu_services WHERE menu_id='AP0100'"
        ).fetchone()[0] == "Erp.BO.APInvoiceSvc"
        assert conn.execute(
            "SELECT justification FROM baseline_services WHERE service_id='Ice.BO.CompanySvc'"
        ).fetchone()[0] == "core context"
    finally:
        conn.close()


def test_written_db_is_readable_by_store(tmp_path):
    from epicor_mcp.rbac.menu_map_builder import write_menu_map_db

    path = tmp_path / "built2.db"
    write_menu_map_db(
        path,
        menus=[{
            "menu_id": "AP0100", "parent_menu_id": "", "menu_desc": "AP Invoice",
            "sec_code": "APSEC", "program": "Erp.UI.APInvoiceEntry", "app_id": "",
            "enabled": True, "hidden": False, "map_strategy": "metafx",
            "map_confidence": 1.0,
        }],
        menu_services=[("AP0100", "Erp.BO.APInvoiceSvc", "metafx")],
        baseline_services=[("Ice.BO.CompanySvc", "core context")],
    )
    store = MenuMapStore(path)
    assert store.is_loaded() is True
    assert store.services_for_menus(["AP0100"]) == {"Erp.BO.APInvoiceSvc"}
    assert store.baseline() == {"Ice.BO.CompanySvc"}

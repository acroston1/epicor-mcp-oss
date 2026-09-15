"""audit.db migration: authz decision columns + shadow-divergence table.

On init the AuditLogger must, idempotently:
  * ALTER an existing (old-schema) ``audit_log`` to add ``authz_source`` /
    ``authz_reason`` when absent (PRAGMA table_info gate), and
  * create the ``authz_shadow_divergence`` table that powers the shadow report.

It must also accept the two new keyword args on ``log`` and expose
``log_shadow_divergence`` for the shadow path.  All against a LOCAL temp db;
no real audit.db is touched.
"""

from __future__ import annotations

import sqlite3

import pytest

from epicor_mcp.audit import AuditLogger

# The pre-migration audit_log schema (no authz_* columns).
_OLD_SCHEMA = """
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    user_email  TEXT    NOT NULL,
    department  TEXT    NOT NULL DEFAULT '',
    tool_name   TEXT    NOT NULL,
    arguments   TEXT    NOT NULL DEFAULT '{}',
    status      TEXT    NOT NULL,
    duration_ms REAL,
    error       TEXT    NOT NULL DEFAULT ''
);
"""


def _cols(path, table):
    conn = sqlite3.connect(str(path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _tables(path):
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    finally:
        conn.close()


def _seed_old_db(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO audit_log (timestamp, user_email, tool_name, status) "
            "VALUES ('2026-07-07T00:00:00', 'old@example.com', 'epicor_read', 'success')"
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_adds_authz_columns_to_existing_table(tmp_path):
    db = tmp_path / "audit.db"
    _seed_old_db(db)
    assert "authz_source" not in _cols(db, "audit_log")

    AuditLogger(db)  # constructing must migrate in place

    cols = _cols(db, "audit_log")
    assert "authz_source" in cols
    assert "authz_reason" in cols


def test_migration_preserves_existing_rows(tmp_path):
    db = tmp_path / "audit.db"
    _seed_old_db(db)
    AuditLogger(db)
    conn = sqlite3.connect(str(db))
    try:
        (n,) = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    finally:
        conn.close()
    assert n == 1  # old row survived the ALTER


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "audit.db"
    _seed_old_db(db)
    AuditLogger(db).close()
    # Second construction must not raise (columns already present).
    AuditLogger(db).close()
    assert "authz_source" in _cols(db, "audit_log")


def test_shadow_divergence_table_created(tmp_path):
    db = tmp_path / "audit.db"
    AuditLogger(db)
    assert "authz_shadow_divergence" in _tables(db)
    cols = _cols(db, "authz_shadow_divergence")
    assert {"timestamp", "user", "service", "tool",
            "dept_decision", "menu_decision", "reason"} <= cols


def test_log_persists_authz_source_and_reason(tmp_path):
    db = tmp_path / "audit.db"
    audit = AuditLogger(db)
    audit.log(
        user_email="apuser@example.org",
        department="AP",
        tool_name="epicor_read",
        arguments={"service": "Erp.BO.APInvoiceSvc"},
        status="success",
        authz_source="menu",
        authz_reason="allowed via menu AP0100 (SecCode APSEC)",
    )
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT authz_source, authz_reason FROM audit_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "menu"
    assert "AP0100" in row[1]


def test_log_shadow_divergence_writes_row(tmp_path):
    db = tmp_path / "audit.db"
    audit = AuditLogger(db)
    audit.log_shadow_divergence(
        user="apuser@example.org",
        service="Erp.BO.APInvoiceSvc",
        tool="epicor_read",
        dept_decision=True,
        menu_decision=False,
        reason="legacy allowed via empty-department fallback; menu denied",
    )
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT user, service, tool, dept_decision, menu_decision, reason "
            "FROM authz_shadow_divergence ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "apuser@example.org"
    assert row[1] == "Erp.BO.APInvoiceSvc"
    assert row[2] == "epicor_read"

"""The SSO-disabled server records every tool call in the local audit log.

Synthetic configuration, unreachable Epicor URL, no network. The log lands in
the test's temporary directory through ``audit_log_path``.
"""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from tests.fixtures.oss_server import server_settings


def _rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT user_email, tool_name, status, error FROM audit_log ORDER BY id").fetchall()


def _readonly(tmp_path, **overrides):
    from epicor_mcp.server import _build_readonly_server
    settings = server_settings(tmp_path, auth_mode="none", discovery_index_path=tmp_path / "missing-schema",
                               docs_db_path=tmp_path / "missing-docs.db", **overrides)
    return settings, _build_readonly_server(settings)


async def test_every_tool_call_is_audited_including_refused_arguments(tmp_path):
    settings, (mcp, runtime, resources) = _readonly(tmp_path)
    try:
        assert mcp.epicor_audit_logger is not None
        await mcp._tool_manager.call_tool("epicor_help", {"query": "anything"})
        await mcp._tool_manager.call_tool("epicor_tables", {"query": ["not", "a", "string"]})
        rows = _rows(settings.audit_log_path)
        assert [(row[1], row[2]) for row in rows] == [("epicor_help", "success"), ("epicor_tables", "error")]
        assert rows[1][3], "the refusal reason is recorded, not just the status"
    finally:
        await runtime.client.close()
        for resource in resources:
            resource.close()


def test_http_calls_are_attributed_to_the_shared_principal_and_health_reports_it(tmp_path):
    from epicor_mcp.server import create_app
    settings = server_settings(tmp_path, auth_mode="none", discovery_index_path=tmp_path / "missing-schema",
                               docs_db_path=tmp_path / "missing-docs.db")
    app = create_app(settings)
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="https://mcp.example.org") as client:
        assert client.get("/health").json()["audit_log"] is True
        result = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "epicor_help", "arguments": {"query": "anything"}}},
                             headers=headers)
        assert result.status_code == 200, result.text
    rows = _rows(settings.audit_log_path)
    assert rows == [("shared-read-only", "epicor_help", "success", "")]


def test_audit_can_be_switched_off_and_health_says_so(tmp_path):
    from epicor_mcp.server import create_app
    settings = server_settings(tmp_path, auth_mode="none", audit_log_enabled=False,
                               discovery_index_path=tmp_path / "missing-schema", docs_db_path=tmp_path / "missing-docs.db")
    app = create_app(settings)
    with TestClient(app, base_url="https://mcp.example.org") as client:
        assert client.get("/health").json()["audit_log"] is False
    assert not settings.audit_log_path.exists()

"""A connection or credential failure is not a SQL error.

A wrong Epicor URL, an unreachable host, or a rejected service account / API key
fails the FIRST Epicor call, ``ParseFromSQL``. Reported as ``sql_parse_error``
("Epicor refused this statement at parse time") a model rewrites valid SQL in a
loop and an operator debugging a new install looks in the wrong place. These
tests pin distinct, terminal envelopes that name the configuration to check.
"""

from __future__ import annotations

import asyncio

import pytest

from epicor_mcp.sql.adhoc import EXECUTE_PATH, run_sql
from tests.wedge_fixtures import FakeEpicorError, MockEpicorClient, load, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"
SQL = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]"

UNREACHABLE = [
    (0, "HTTP transport error: [Errno -2] Name or service not known"),
    (408, "Request timed out after 30s: https://example.invalid/api/v2/odata/DEMO/x"),
]
REJECTED = [
    (401, "HTTP 401: Unauthorized"),
    (403, "HTTP 403: Access scope does not allow this service"),
]


def call(sql: str, client: MockEpicorClient) -> dict:
    return asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE))


@pytest.mark.parametrize(("status", "message"), UNREACHABLE)
def test_unreachable_epicor_at_parse_is_a_connection_error(status, message):
    client = MockEpicorClient(parse_error=FakeEpicorError(message, status))
    out = call(SQL, client)
    assert out["success"] is False
    assert out["error"] == "epicor_unreachable"
    assert out["terminal"] is True
    assert "not a problem with your SQL" in out["message"]
    assert "Epicor URL" in out["message"]
    assert out["detail"]["stage"] == "parse"
    assert out["detail"]["status"] == status
    assert f"{BASE}/{EXECUTE_PATH}" not in client.paths


@pytest.mark.parametrize(("status", "message"), REJECTED)
def test_rejected_credentials_at_parse_are_an_auth_error(status, message):
    client = MockEpicorClient(parse_error=FakeEpicorError(message, status))
    out = call(SQL, client)
    assert out["success"] is False
    assert out["error"] == "epicor_auth_error"
    assert out["terminal"] is True
    assert "not a problem with your SQL" in out["message"]
    assert "API key" in out["message"]
    assert "access scope" in out["message"]
    assert out["detail"]["stage"] == "parse"
    assert f"{BASE}/{EXECUTE_PATH}" not in client.paths


def test_a_genuine_parse_400_is_still_a_sql_parse_error():
    client = MockEpicorClient(
        parse_error=FakeEpicorError("SQL cannot be parsed: Incorrect syntax near 'x'.", 400)
    )
    out = call(SQL, client)
    assert out["error"] == "sql_parse_error"
    assert "Incorrect syntax near 'x'." in out["message"]


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [*((s, m, "epicor_unreachable") for s, m in UNREACHABLE),
     *((s, m, "epicor_auth_error") for s, m in REJECTED)],
)
def test_the_same_failures_at_execute_are_classified_the_same_way(status, message, expected):
    sql, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute([]),
        execute_error=FakeEpicorError(message, status),
    )
    out = call(sql, client)
    assert out["error"] == expected
    assert out["terminal"] is True
    assert out["detail"]["stage"] == "execute"

"""Regression coverage: test baq order by blame."""
from __future__ import annotations

import asyncio
import json

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools.baq import _do_run

# Reuse the params-test fixtures: a BAQ whose definition carries two mandatory
# params (FromDate/ToDate) and two result columns.
from tests.test_baq_params import (  # noqa: F401
    BAQ, _FakeRBAC, _FakeClient, _SESSION, _GETBYID_OBJ,
)


class _BadOrderByClient(_FakeClient):
    """Data GET always 400s with the property-not-found mask naming the bad
    order_by column; GetByID (POST) still serves the real definition."""

    async def get(self, url, api_key, params=None):
        self.gets.append((url, dict(params or {})))
        if "AUTO-" in url:
            raise EpicorError(status_code=404,
                              message="Dynamic query is not found")
        raise EpicorError(
            status_code=400,
            message="Could not find a property named 'NoSuchCol' on type "
                    "'Epicor.QueryItem'.")


def _run_do(**over):
    kw = dict(session=_SESSION, rbac=_FakeRBAC(), client=_BadOrderByClient(),
              baq=BAQ, where="", limit=50, cursor="", params=None)
    kw.update(over)
    return json.loads(asyncio.run(_do_run(**kw)))


def test_bad_order_by_blamed_on_order_by_not_params():
    # A bad order_by on a BAQ with unsupplied mandatory params must be blamed
    # on order_by, not reported as baq_needs_params.
    out = _run_do(order_by="NoSuchCol desc")
    assert out["error"] == "baq_bad_order_by"
    assert "NoSuchCol" in out["message"]
    assert "order_by" in out["message"]
    # Names the real result columns to sort by instead.
    assert out["valid"]["columns"] == ["PartTran_PartNum", "Calculated_SumTranQty"]
    # Retry drops the bad sort rather than re-sending it.
    assert out["retry_with"]["order_by"] == ""


def test_bad_order_by_blamed_even_with_params_supplied():
    # Params satisfied so `unsupplied` is empty — pre-fix this fell through to
    # baq_run_failed blaming a `where` that was never passed.
    out = _run_do(order_by="NoSuchCol desc",
                  params={"FromDate": "2025-01-01", "ToDate": "2026-07-14"})
    assert out["error"] == "baq_bad_order_by"
    assert "NOT a `where` or parameter problem" in out["message"]


def test_valid_order_by_column_is_not_falsely_blamed():
    # A real result column as order_by must NOT trip the order_by blame — the
    # failure then belongs to the normal param/where path.
    out = _run_do(order_by="PartTran_PartNum desc")
    assert out["error"] != "baq_bad_order_by"


def test_run_failure_without_where_does_not_blame_where():
    # No where, no order_by, params satisfied, run still fails -> baq_run_failed
    # but the message must not point at a filter that was never sent, and
    # retry_with must not echo an empty where.
    out = _run_do(params={"FromDate": "2025-01-01", "ToDate": "2026-07-14"})
    assert out["error"] == "baq_run_failed"
    assert "`where` filter is the usual cause" not in out["message"]
    assert "where" not in out["retry_with"]


def test_run_failure_with_where_still_blames_filter():
    # Regression guard: when a where IS passed, the filter guidance stays.
    out = _run_do(where="PartTran_PartNum eq 'X'",
                  params={"FromDate": "2025-01-01", "ToDate": "2026-07-14"})
    assert out["error"] == "baq_run_failed"
    assert "`where` filter is the usual cause" in out["message"]
    assert out["retry_with"]["where"] == "PartTran_PartNum eq 'X'"

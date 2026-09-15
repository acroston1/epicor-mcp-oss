"""epicor_baq create: the under-specified-create guard and `order_by` (Area 3).

An unsupported ``query='SELECT ...'`` argument must be rejected before
FastMCP can discard it and leave an under-specified create request. Blank
``fields`` triggers automatic field selection, and blank ``where`` emits no
WHERE clause; together they can produce an unintended unfiltered join.

The existing ``missing_tables`` guard did NOT and could not catch this — tables
were supplied. The hole is blank fields AND blank where. The guard must also sit
ahead of ``create_baq``, which DeleteByID's the target id BEFORE parsing: an
under-specified retry destroys a previously-good BAQ first.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from epicor_mcp.tools import baq as baq_mod
from epicor_mcp.tools.baq import _compose_baq_sql, _do_create, _resolve_order_terms
from tests.test_baq_aggregate import PODETAIL, POHEADER


class _Index:
    """Resolves only the synthetic purchasing-table fixtures."""

    _TABLES = {
        "erp.poheader": POHEADER,
        "erp.podetail": PODETAIL,
    }

    def search_tables(self, term, limit=5):
        t = self._TABLES.get(term.strip().lower())
        return [{"full_name": t["full_name"], "table_name": t["table_name"]}] if t else []


def _run(**kw):
    args = dict(session=None, rbac=None, client=None, baq_index=None,
                baq="X", tables="Erp.POHeader", fields="", where="", limit=50)
    args.update(kw)
    return json.loads(asyncio.run(_do_create(**args)))


@pytest.fixture
def resolved(monkeypatch):
    """Short-circuit table resolution so these tests exercise the guard only."""
    async def _no_run(**kw):
        return {"records": [], "record_count": 0}
    monkeypatch.setattr(baq_mod, "run_baq_impl", _no_run)

    def _fake(_index, tables):
        names = [t.strip().lower() for t in str(tables).split(",") if t.strip()]
        out = [{"erp.poheader": POHEADER, "erp.podetail": PODETAIL}[n]
               for n in names]
        return out, [], []
    monkeypatch.setattr(baq_mod, "_resolve_tables", _fake)


def test_blank_fields_and_blank_where_is_refused(resolved, monkeypatch):
    """An under-specified request must be rejected before create_baq runs."""
    called = []
    async def _boom(**kw):
        called.append(kw)
        raise AssertionError("create_baq must not be reached")
    monkeypatch.setattr(baq_mod, "create_baq", _boom)

    out = _run(baq="Example_Inventory_Query",
               tables="Erp.POHeader, Erp.PODetail")
    assert out["error"] == "underspecified_create"
    assert not called
    # A caller who genuinely wanted the auto-picked columns confirms in ONE hop.
    assert out["retry_with"]["fields"]
    assert out["valid"]["columns"]


def test_blank_fields_with_a_where_still_works(resolved, monkeypatch):
    """Deliberately NOT over-tightened: blank fields + a where is a legitimate
    'give me the useful columns for these rows'."""
    seen = {}
    async def _ok(**kw):
        seen.update(kw)
        return {"baq_id": "AUTO-X", "records": []}
    monkeypatch.setattr(baq_mod, "create_baq", _ok)
    out = _run(where="PONum = 5")
    assert out.get("error") != "underspecified_create"
    assert seen["sql"].lower().count("where") == 1


def test_guard_runs_before_the_destructive_pre_delete(resolved, monkeypatch):
    """create_baq DeleteByID's the target id before parsing, so reaching it at
    all with an under-specified create wipes a good BAQ."""
    async def _raise(**kw):
        raise AssertionError("reached create_baq")
    monkeypatch.setattr(baq_mod, "create_baq", _raise)
    assert _run()["error"] == "underspecified_create"


def test_caller_description_beats_the_auto_composed_one(resolved, monkeypatch):
    seen = {}
    async def _ok(**kw):
        seen.update(kw)
        return {"baq_id": "AUTO-X", "records": []}
    monkeypatch.setattr(baq_mod, "create_baq", _ok)
    _run(fields="PONum", description="Open POs for the buyer review")
    assert seen["description"] == "Open POs for the buyer review"


def test_blank_description_keeps_the_auto_composed_default(resolved, monkeypatch):
    seen = {}
    async def _ok(**kw):
        seen.update(kw)
        return {"baq_id": "AUTO-X", "records": []}
    monkeypatch.setattr(baq_mod, "create_baq", _ok)
    _run(fields="PONum")
    assert seen["description"].startswith("Auto-composed:")


# --------------------------------------------------------------------------- #
# order_by on the composer
# --------------------------------------------------------------------------- #

def test_order_by_emits_a_qualified_trailing_clause():
    terms, err = _resolve_order_terms(
        "OrderDate desc", [POHEADER], [("POHeader", "PONum")], [], [])
    assert err is None
    sql, _joins = _compose_baq_sql([POHEADER], [("POHeader", "PONum")], "",
                                   None, None, terms)
    assert sql.strip().endswith("order by [POHeader].[OrderDate] desc")


def test_order_by_an_aggregate_output_alias_uses_the_alias_form():
    aggs = [{"fn": "count", "alias": "POHeader", "real": "PONum",
             "out": "POCount"}]
    terms, err = _resolve_order_terms("POCount desc", [POHEADER], [], aggs, [])
    assert err is None and terms == [("[POCount]", "desc")]


def test_order_by_after_group_by(resolved=None):
    aggs = [{"fn": "count", "alias": "POHeader", "real": "PONum",
             "out": "POCount"}]
    terms, _ = _resolve_order_terms("POCount desc", [POHEADER], [], aggs, [])
    sql, _j = _compose_baq_sql([POHEADER], [("POHeader", "BuyerID")], "",
                               aggs, None, terms)
    assert sql.index("group by") < sql.index("order by")


def test_unresolvable_order_column_is_an_error_not_a_dropped_sort():
    terms, err = _resolve_order_terms("Frobnicate desc", [POHEADER], [], [], [])
    assert terms == [] and err["error"] == "unknown_columns"


def test_expression_order_by_is_refused_with_the_alias_route():
    _t, err = _resolve_order_terms("sum(x) desc", [POHEADER], [], [], [])
    assert err["error"] == "order_expression_unsupported"


def test_no_order_by_leaves_the_sql_byte_identical():
    plain, _a = _compose_baq_sql([POHEADER], [("POHeader", "PONum")], "")
    with_none, _b = _compose_baq_sql([POHEADER], [("POHeader", "PONum")], "",
                                     None, None, [])
    assert plain == with_none
    assert "order by" not in plain

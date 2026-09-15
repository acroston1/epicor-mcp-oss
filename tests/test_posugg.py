"""Unit tests for the NEW-vs-CHANGE PO-suggestion recognizer (``_posugg.py``).

The crux: "PO change suggestions" used to word-match "po" and resolve to
POHeader, returning purchase orders (200 of them, with a reflex OpenOrder=true
filter that only POHeader has). These tests pin the routing, the OpenOrder
scrub, the DueDate-desc default order, and the disambiguation note — no live
Epicor (``run_odata``/``run_getrows`` are monkeypatched).
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.tools import _posugg
from epicor_mcp.tools._posugg import (
    _strip_open,
    detect_po_sugg,
    po_suggestions,
)


class _FakeRBAC:
    def __init__(self, deny=()):
        self._deny = set(deny)

    def check_access(self, user_id, service_id):
        if service_id in self._deny:
            return (False, f"no access to {service_id}")
        return (True, "")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key="K")


_SESSION = types.SimpleNamespace(user_id="tester")
_INDEX = object()
_CLIENT = object()


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Recognition (pure)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("target,expected", [
    ("PO change suggestions", "change"),
    ("po change suggestion", "change"),
    ("purchase order change suggestions", "change"),
    ("reschedule PO suggestions", "change"),
    ("sugpochg", "change"),
    ("PO suggestions", "new"),
    ("new po suggestions", "new"),
    ("buy suggestions", "new"),
    ("sugpodtl", "new"),
    # Not a suggestion ask — left to the normal resolver:
    ("open POs", None),
    ("purchase orders", None),
    ("po changes", None),
    ("show me open jobs", None),
])
def test_detect_po_sugg(target, expected):
    assert detect_po_sugg(target) == expected


@pytest.mark.parametrize("where,cleaned,dropped_present", [
    ("OpenOrder = true", "", True),
    ("OpenOrder = true and VendorID = 'ABC'", "VendorID = 'ABC'", True),
    ("DueDate > '2026-01-01' and OpenOrder = true", "DueDate > '2026-01-01'", True),
    ("OpenLine = true", "", True),
    ("PartNum = 'ABC'", "PartNum = 'ABC'", False),
    ("", "", False),
])
def test_strip_open(where, cleaned, dropped_present):
    out, dropped = _strip_open(where)
    assert out == cleaned
    assert (dropped is not None) == dropped_present


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _patch(monkeypatch, rows=None, capture=None):
    rows = rows if rows is not None else []

    async def _fake_odata(client, service, entity, api_key, **kw):
        if capture is not None:
            capture.update({"service": service, "entity": entity,
                            "orderby": kw.get("orderby"),
                            "filter": kw.get("filter")})
        return json.dumps({"records": rows})

    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        raise AssertionError("should not fall back to GetRows in this test")

    monkeypatch.setattr(_posugg, "run_odata", _fake_odata)
    monkeypatch.setattr(_posugg, "run_getrows", _fake_getrows)


def test_change_routes_to_chg_bo_and_scrubs_openorder(monkeypatch):
    cap = {}
    _patch(monkeypatch, rows=[{"PONum": 100, "POLine": 1, "DueDate": "2026-08-01"}],
           capture=cap)
    out = json.loads(_run(po_suggestions(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        kind="change", target="PO change suggestions",
        where="OpenOrder = true", limit=25)))
    # Routed to the CHANGE business object, not POHeader. The OData segment is
    # the plural POSuggChgs (SugPOChg is only the GetRows DataSet table name).
    assert cap["service"] == "Erp.BO.POSuggChgSvc"
    assert cap["entity"] == "POSuggChgs"
    # No server-side order — this BO 500s on any $orderby / GetRows "By".
    assert cap["orderby"] in ("", None)
    # The reflex OpenOrder filter was dropped (SugPOChg has no such column).
    assert cap["filter"] in ("", None)
    assert out["resolved"]["service"] == "Erp.BO.POSuggChgSvc"
    assert out["resolved"]["entity_set"] == "SugPOChg"
    assert out["row_count"] == 1
    # Note distinguishes the BO and points at the NEW one, and flags the drop.
    assert "CHANGE" in out["note"] and "POSuggSvc" in out["note"]
    assert "Dropped" in out["note"]


def test_rows_sorted_soonest_due_first(monkeypatch):
    # Unordered rows come back sorted ascending by DueDate; blanks sort last.
    rows = [
        {"PONum": 3, "DueDate": "2026-09-01"},
        {"PONum": 1, "DueDate": "2026-07-15"},
        {"PONum": 9, "DueDate": ""},
        {"PONum": 2, "DueDate": "2026-08-10"},
    ]
    _patch(monkeypatch, rows=rows)
    out = json.loads(_run(po_suggestions(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        kind="change", target="PO change suggestions", where="", limit=10)))
    assert [r["PONum"] for r in out["records"]] == [1, 2, 3, 9]


def test_new_routes_to_new_bo(monkeypatch):
    cap = {}
    _patch(monkeypatch, rows=[{"PONUM": 5}], capture=cap)
    out = json.loads(_run(po_suggestions(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        kind="new", target="PO suggestions", where="", limit=25)))
    assert cap["service"] == "Erp.BO.POSuggSvc"
    assert cap["entity"] == "SugPoDtl"
    assert cap["orderby"] in ("", None)
    assert out["resolved"]["service"] == "Erp.BO.POSuggSvc"
    # Note points the other way — at the CHANGE BO.
    assert "POSuggChgSvc" in out["note"]


def test_real_filter_preserved(monkeypatch):
    cap = {}
    _patch(monkeypatch, rows=[], capture=cap)
    _run(po_suggestions(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        kind="change", target="PO change suggestions",
        where="VendorName = 'Acme' and OpenOrder = true", limit=25))
    # The genuine predicate survives; only OpenOrder is scrubbed.
    assert "Acme" in (cap["filter"] or "")
    assert "open" not in (cap["filter"] or "").lower()


def test_access_denied(monkeypatch):
    _patch(monkeypatch)
    out = json.loads(_run(po_suggestions(
        _CLIENT, _INDEX, _FakeRBAC(deny=("Erp.BO.POSuggChgSvc",)), _SESSION,
        kind="change", target="PO change suggestions", where="", limit=25)))
    assert out["error"] == "access_denied"


def test_getrows_fallback_on_odata_error(monkeypatch):
    from epicor_mcp.epicor_client.error_handler import EpicorError

    async def _boom(*a, **k):
        raise EpicorError(500, "unexpected internal problem")

    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        assert service == "Erp.BO.POSuggChgSvc"
        return json.dumps({"records": [{"PONum": 1}]})

    monkeypatch.setattr(_posugg, "run_odata", _boom)
    monkeypatch.setattr(_posugg, "run_getrows", _fake_getrows)
    out = json.loads(_run(po_suggestions(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        kind="change", target="PO change suggestions", where="", limit=25)))
    assert out["row_count"] == 1

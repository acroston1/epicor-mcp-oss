"""Unit tests for the vendor/customer contacts read in ``read.py``.

The crux: VENDOR contacts ride the parent's GetByID dataset (VendCnt child),
but CUSTOMER contacts do NOT (the CustomerSvc GetByID has no CustCnt child at
all) — they come from the dedicated ``Erp.BO.CustCntSvc/CustCnts`` collection.
These tests pin both fetch strategies without live Epicor: ``run_getrows``
(parent name→number lookup + the customer contact collection) and
``client.post`` (vendor GetByID) are faked.
"""

from __future__ import annotations

import asyncio
import json
import types

from epicor_mcp.tools import read as readmod
from epicor_mcp.tools.read import _read_contacts


class _FakeRBAC:
    def __init__(self, deny=()):
        self._deny = set(deny)

    def check_access(self, user_id, service_id):
        if service_id in self._deny:
            return (False, f"no access to {service_id}")
        return (True, "")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key="K")


class _FakeClient:
    """Fakes only ``post`` (used by the vendor GetByID strategy)."""

    def __init__(self, getbyid_dataset):
        self._ds = getbyid_dataset

    async def post(self, path, api_key, json_body=None):
        return {"returnObj": self._ds}


_SESSION = types.SimpleNamespace(user_id="tester")
_INDEX = object()


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Customer contacts — the fixed `collection` strategy
# --------------------------------------------------------------------------- #

def test_customer_contacts_use_custcnt_collection(monkeypatch):
    calls = {}

    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        calls.setdefault("services", []).append((service, entity, kw.get("filter")))
        # First call = parent name→number lookup on Customers; second = contacts.
        if entity == "Customers":
            return json.dumps({"records": [{"CustNum": 58, "CustID": "EXAMPLE",
                                            "Name": "Example Customer Inc"}]})
        return json.dumps({"records": [
            {"CustNum": 58, "ConNum": 1, "Name": "SAMPLE CONTACT",
             "PhoneNum": "555-0100", "EMailAddress": ""},
            {"CustNum": 58, "ConNum": 3, "Name": "Accounting",
             "EMailAddress": "accounting@example.com", "PhoneNum": "555-0100"},
        ]})

    monkeypatch.setattr(readmod, "run_getrows", _fake_getrows)

    out = json.loads(_run(_read_contacts(
        _FakeClient({}), _INDEX, _FakeRBAC(), _SESSION,
        "Erp.BO.CustomerSvc", "CustCnt",
        target="customer contacts for Example Customer", where="", fields="",
        limit=25, count_only=False)))

    assert out["row_count"] == 2
    # Fetched from the dedicated contact service, not CustomerSvc/GetByID.
    assert out["resolved"]["service"] == "Erp.BO.CustCntSvc"
    assert out["resolved"]["via"] == "collection"
    assert out["resolved"]["CustNum"] == 58
    # The contact fetch filtered by the customer number.
    assert ("Erp.BO.CustCntSvc", "CustCnts", "CustNum eq 58") in calls["services"]
    names = {r.get("Name") for r in out["rows"]}
    assert "SAMPLE CONTACT" in names and "Accounting" in names


def test_customer_contacts_direct_custnum_in_where(monkeypatch):
    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        assert entity == "CustCnts"        # no parent lookup needed
        return json.dumps({"records": [{"CustNum": 58, "ConNum": 1, "Name": "X"}]})

    monkeypatch.setattr(readmod, "run_getrows", _fake_getrows)
    out = json.loads(_run(_read_contacts(
        _FakeClient({}), _INDEX, _FakeRBAC(), _SESSION,
        "Erp.BO.CustomerSvc", "CustCnt",
        target="customer contacts", where="CustNum = 58", fields="",
        limit=25, count_only=False)))
    assert out["row_count"] == 1
    assert out["resolved"]["via"] == "collection"


def test_customer_contacts_denied_when_no_custcnt_access(monkeypatch):
    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        return json.dumps({"records": [{"CustNum": 58, "Name": "Example Customer"}]})

    monkeypatch.setattr(readmod, "run_getrows", _fake_getrows)
    out = json.loads(_run(_read_contacts(
        _FakeClient({}), _INDEX, _FakeRBAC(deny=("Erp.BO.CustCntSvc",)), _SESSION,
        "Erp.BO.CustomerSvc", "CustCnt",
        target="customer contacts for Example Customer", where="", fields="",
        limit=25, count_only=False)))
    assert out["error"] == "access_denied"


# --------------------------------------------------------------------------- #
# Vendor contacts — unchanged `getbyid` strategy still works
# --------------------------------------------------------------------------- #

def test_vendor_contacts_use_getbyid_child(monkeypatch):
    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        # Parent name→number lookup only.
        return json.dumps({"records": [{"VendorNum": 368, "VendorID": "EXAMPLE",
                                        "Name": "Example Customer"}]})

    monkeypatch.setattr(readmod, "run_getrows", _fake_getrows)
    client = _FakeClient({"VendCnt": [
        {"VendorNum": 368, "ConNum": 1, "Name": "Jane Vendor",
         "EmailAddress": "jane@example.com"},
    ]})
    out = json.loads(_run(_read_contacts(
        client, _INDEX, _FakeRBAC(), _SESSION,
        "Erp.BO.VendorSvc", "VendCnt",
        target="vendor contacts for Example Customer", where="", fields="",
        limit=25, count_only=False)))
    assert out["row_count"] == 1
    assert out["resolved"]["service"] == "Erp.BO.VendorSvc"
    assert out["resolved"]["via"] == "GetByID"
    assert out["rows"][0]["Name"] == "Jane Vendor"

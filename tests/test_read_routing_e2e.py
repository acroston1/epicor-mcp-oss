"""End-to-end mock-client reads through the registered ``epicor_read``.

Proves the wiring, not just the helpers: that the date-column type set actually
reaches `sql_to_odata` at the call site, that a boolean mis-correction never
reaches the wire, and that a GetRows generic-500 falls back to the plural OData
collection exactly once. No live Epicor — the client is a stub that records
every request it is handed.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools import read as read_mod

APOLOGY = ("We apologize, but an unexpected internal problem occurred. "
           "Correlation ID: abc-123")


class _Idx:
    def __init__(self, fields, entity_sets=None, hosts=None):
        self._fields = fields          # (service, entity) -> [{field_name, field_type}]
        self._sets = entity_sets or {}
        self._hosts = hosts or {}

    def get_fields(self, service, entity_set):
        return self._fields.get((service, entity_set), [])

    def get_field_types(self, service, entity_set):
        return {r["field_name"]: r.get("field_type") or ""
                for r in self.get_fields(service, entity_set)}

    def get_entity_sets(self, service):
        return self._sets.get(service, [])

    def services_for_entity(self, entity_set):
        return self._hosts.get(entity_set.strip().lower(), [])

    def search_services(self, raw, limit=5):
        return []

    def find_field_owners(self, name, limit=6):
        out = []
        for (svc, ent), rows in self._fields.items():
            if any(r["field_name"].lower() == name.lower() for r in rows):
                out.append({"service_id": svc, "entity_set_name": ent,
                            "field_type": ""})
        return out[:limit]


class _RBAC:
    def check_access(self, user_id, service_id):
        return (True, "")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key="K")


class _Client:
    def __init__(self, get_result=None, post_result=None):
        self._get = get_result
        self._post = post_result
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    async def get(self, url, api_key, params=None):
        self.gets.append((url, dict(params or {})))
        if isinstance(self._get, Exception):
            raise self._get
        return self._get if self._get is not None else {"value": []}

    async def post(self, url, api_key, json_body=None):
        self.posts.append((url, dict(json_body or {})))
        if isinstance(self._post, Exception):
            raise self._post
        return self._post if self._post is not None else {"returnObj": {}}


class _Server:
    """Captures the function `register` decorates."""

    def __init__(self):
        self.fn = None

    def tool(self, **kwargs):
        def deco(fn):
            self.fn = fn
            return fn
        return deco


def _make(index, client, monkeypatch):
    monkeypatch.setattr(read_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="tester"))
    srv = _Server()
    read_mod.register(srv, index, _RBAC(), client)
    return srv.fn


def _clear_caches():
    from epicor_mcp.tools.query import _DATE_COLS_CACHE
    from epicor_mcp.tools.read import _SEARCH_SVC_CACHE
    _DATE_COLS_CACHE.clear()
    _SEARCH_SVC_CACHE.clear()


PARTTRAN_FIELDS = [
    {"field_name": "PartNum", "field_type": "Edm.String"},
    {"field_name": "TranDate", "field_type": "Edm.DateTimeOffset"},
    {"field_name": "TranQty", "field_type": "Edm.Double"},
]


def test_read_passes_date_columns(monkeypatch):
    """The quoted ISO date must reach the wire UNQUOTED with Z."""
    _clear_caches()
    idx = _Idx(
        fields={("Erp.BO.PartTranSvc", "PartTran"): PARTTRAN_FIELDS},
        entity_sets={"Erp.BO.PartTranSvc": ["PartTran", "PartTrans"]},
        hosts={"parttran": [{"service_id": "Erp.BO.PartTranSvc",
                             "entity_set_name": "PartTran"}]},
    )
    client = _Client(get_result={"value": [{"PartNum": "X"}]})
    fn = _make(idx, client, monkeypatch)

    asyncio.run(fn(target="PartTran", where="TranDate >= '2025-07-20'"))

    assert client.gets, "no OData request was issued"
    flt = client.gets[0][1]["$filter"]
    assert flt == "TranDate ge 2025-07-20T00:00:00Z"
    assert "'2025-07-20'" not in flt


def test_read_leaves_string_column_date_quoted(monkeypatch):
    """Type-driven, not shape-driven — end to end."""
    _clear_caches()
    idx = _Idx(
        fields={("Erp.BO.PartTranSvc", "PartTran"): PARTTRAN_FIELDS},
        entity_sets={"Erp.BO.PartTranSvc": ["PartTran"]},
        hosts={"parttran": [{"service_id": "Erp.BO.PartTranSvc",
                             "entity_set_name": "PartTran"}]},
    )
    client = _Client(get_result={"value": []})
    fn = _make(idx, client, monkeypatch)

    asyncio.run(fn(target="PartTran", where="PartNum = '2025-07-20'"))
    assert client.gets[0][1]["$filter"] == "PartNum eq '2025-07-20'"


PART_FIELDS = [
    {"field_name": "PartNum", "field_type": "Edm.String"},
    {"field_name": "PartDescription", "field_type": "Edm.String"},
    {"field_name": "HasOnHandQty", "field_type": "Edm.Boolean"},
]


def test_onhandqty_not_corrected_to_boolean(monkeypatch):
    """No GetRows POST at all — the bad whereClause is never built."""
    _clear_caches()
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "Part"): PART_FIELDS,
                ("Erp.BO.PartSvc", "PartWhse"): [
                    {"field_name": "OnHandQty", "field_type": "Edm.Double"}]},
        entity_sets={"Erp.BO.PartSvc": ["Part", "Parts", "PartWhse"]},
    )
    client = _Client()
    fn = _make(idx, client, monkeypatch)

    out = json.loads(asyncio.run(
        fn(target="Erp.BO.PartSvc/Part", where="OnHandQty > 0")))

    assert out["error"] == "unknown_columns"
    assert client.posts == [] and client.gets == []
    assert "HasOnHandQty" not in json.dumps(out.get("retry_with", {}))
    # And it names where OnHandQty actually lives.
    assert "OnHandQty" in out["valid"]["column_lives_on"]
    assert ("Erp.BO.PartSvc/PartWhse"
            in out["valid"]["column_lives_on"]["OnHandQty"])


def test_description_correction_still_reaches_the_client(monkeypatch):
    """The load-bearing substring rule is untouched by the type veto."""
    _clear_caches()
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "Part"): PART_FIELDS},
        entity_sets={"Erp.BO.PartSvc": ["Part", "Parts"]},
    )
    client = _Client(get_result={"value": [{"PartNum": "X"}]})
    fn = _make(idx, client, monkeypatch)

    asyncio.run(fn(target="Erp.BO.PartSvc/Part", where="Description = 'WIDGET'"))
    # PartSvc is heavy, so the corrected read goes out via GetRows.
    assert client.posts, "the corrected read never went out"
    assert "PartDescription" in json.dumps(client.posts[0][1])


def test_getrows_500_falls_back_to_odata_plural(monkeypatch):
    """One GetRows POST, then exactly one OData GET on the plural collection."""
    _clear_caches()
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "Part"): PART_FIELDS},
        entity_sets={"Erp.BO.PartSvc": ["Part", "Parts"]},
    )
    client = _Client(
        get_result={"value": [{"PartNum": "X"}]},
        post_result=EpicorError(500, APOLOGY),
    )
    monkeypatch.setattr(read_mod, "is_heavy", lambda svc: True)
    fn = _make(idx, client, monkeypatch)

    out = json.loads(asyncio.run(fn(target="Erp.BO.PartSvc/Part")))

    assert len(client.posts) == 1
    assert len(client.gets) == 1
    assert client.gets[0][0] == "Erp.BO.PartSvc/Parts"
    assert "Parts" in out["assumptions"]["route_via"]


def test_getrows_500_no_plural_collection_no_retry(monkeypatch):
    """No plural -> zero OData GETs and the INV-1 envelope survives."""
    _clear_caches()
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "Part"): PART_FIELDS},
        entity_sets={"Erp.BO.PartSvc": ["Part"]},
    )
    client = _Client(post_result=EpicorError(500, APOLOGY))
    monkeypatch.setattr(read_mod, "is_heavy", lambda svc: True)
    fn = _make(idx, client, monkeypatch)

    out = json.loads(asyncio.run(fn(target="Erp.BO.PartSvc/Part")))

    assert len(client.posts) == 1
    assert client.gets == []
    assert out["error"] == "upstream_error"
    assert "Neither OData nor GetRows worked" not in out["message"]
    assert "abc-123" in out["detail"]["message"]
    assert "abc-123" not in out["message"]


def test_odata_error_is_classified_not_swallowed(monkeypatch):
    """The blanket except used to eat this and return a fixed string."""
    _clear_caches()
    idx = _Idx(
        fields={("Erp.BO.PartTranSvc", "PartTran"): PARTTRAN_FIELDS},
        entity_sets={"Erp.BO.PartTranSvc": ["PartTran"]},
        hosts={"parttran": [{"service_id": "Erp.BO.PartTranSvc",
                             "entity_set_name": "PartTran"}]},
    )
    client = _Client(get_result=EpicorError(
        400, "A binary operator with incompatible types was detected. Found "
             "operand types 'Edm.DateTimeOffset' and 'Edm.String'"))
    fn = _make(idx, client, monkeypatch)

    out = json.loads(asyncio.run(
        fn(target="PartTran", where="TranQty gt 0")))

    assert out["error"] == "filter_type_mismatch"
    assert out["detail"]["message"]
    assert out["message"] != (
        "epicor_read failed — check target, fields, and where syntax.")


def test_partwhse_resolves_to_part_service(monkeypatch):
    """Defect-2 end to end: an exact table name no longer errors."""
    _clear_caches()
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "PartWhse"): [
            {"field_name": "PartNum", "field_type": "Edm.String"},
            {"field_name": "OnHandQty", "field_type": "Edm.Double"}]},
        entity_sets={"Erp.BO.PartSvc": ["Part", "PartWhse"]},
        hosts={"partwhse": [{"service_id": "Erp.BO.PartSvc",
                             "entity_set_name": "PartWhse"}]},
    )
    client = _Client(get_result={"value": [{"PartNum": "X", "OnHandQty": 3}]})
    fn = _make(idx, client, monkeypatch)

    out = json.loads(asyncio.run(fn(target="PartWhse")))
    assert "error" not in out
    assert out["resolved"]["service"] == "Erp.BO.PartSvc"
    assert out["resolved"]["entity_set"] == "PartWhse"

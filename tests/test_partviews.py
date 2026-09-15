"""Unit tests for the part-view reads folded into ``epicor_read``.

Covers resolution (part/plant/job), the INV-1 error envelope (missing part,
access denied, Epicor method-security 401 → access_denied), and the routing
decision (time phase → GoProcessTimePhase method-POST; BOM → the part's most
recent job method via JobHead GetRows + JobMtls/JobOpers OData). No live
Epicor — the client and GetRows are faked.
"""

from __future__ import annotations

import asyncio
import json
import re
import types

import pytest

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools import _partviews
from epicor_mcp.tools._partviews import (
    detect_part_view,
    read_bom,
    read_timephase,
    resolve_part,
    _rank_jobs,
    _resolve_plant,
)


@pytest.fixture
def configured_plants(monkeypatch):
    """Explicit synthetic installation; production ships without a plant map."""
    from epicor_mcp.tools import _tenant, read

    plants = {"101": "North Works", "202": "South Works", "303": "West Works"}
    monkeypatch.setattr(_tenant, "PLANTS", plants)
    monkeypatch.setattr(_partviews, "PLANTS", plants)
    monkeypatch.setattr(read, "PLANTS", plants)
    return plants


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _FakeRBAC:
    def __init__(self, allowed=True, api_key="K"):
        self._allowed = allowed
        self._api_key = api_key
        self.checked: list[str] = []

    def check_access(self, user_id, service_id):
        self.checked.append(service_id)
        return (self._allowed, "" if self._allowed else f"no access to {service_id}")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key=self._api_key)


class _FakeClient:
    """Fakes both post() (time phase) and get() (job collections).

    Collection rows carry a JobNum; get() filters them by the JobNum in the
    request's ``$filter`` so the candidate-probing logic is actually exercised.
    """

    def __init__(self, post_result=None, timephase_by_plant=None,
                 job_methods=None):
        self._post_result = post_result or {}
        self._tp_by_plant = timephase_by_plant  # plant code -> [TimePhas rows]
        self._job_methods = job_methods or {}   # jobnum -> {"JobMtl":[], "JobOper":[]}
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    async def post(self, url, api_key, json_body=None):
        self.posts.append((url, json_body))
        if isinstance(self._post_result, Exception):
            raise self._post_result
        if self._tp_by_plant is not None and url.endswith("/GoProcessTimePhase"):
            plant = (json_body or {}).get("plant", "")
            return {"returnObj": {"TimePhas": self._tp_by_plant.get(plant, [])}}
        if url.endswith("/GetByID"):   # JobEntrySvc job method
            jn = (json_body or {}).get("jobNum")
            m = self._job_methods.get(jn, {})
            return {"returnObj": {"JobHead": [{"JobNum": jn}],
                                  "JobMtl": m.get("JobMtl", []),
                                  "JobOper": m.get("JobOper", [])}}
        return self._post_result

    async def get(self, url, api_key, params=None):
        self.gets.append((url, params))
        return {"value": []}


_SESSION = types.SimpleNamespace(user_id="tester")


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Pure recognition / resolution
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("target,kind", [
    ("time phase for part WIDGET-100", "timephase"),
    ("time-phased requirements for ABC-123", "timephase"),
    ("timephase 100-500 at West Works", "timephase"),
    ("BOM for part WIDGET-9", "bom"),
    ("bill of materials for 12345", "bom"),
    ("components of ASSY-1", "bom"),
    ("open purchase orders", None),
    ("top parts by sales in 2025", None),
    ("part 6205", None),          # a bare part with no view word must NOT trigger
    # The model's fallback of targeting the table/service directly must route
    # to the intent helper, not fall through to a broken raw read (audit).
    ("Erp.BO.TimePhasSvc/TimePhas", "timephase"),
    ("PartDtl", "timephase"),
    ("Erp.BO.PartDtlSvc/PartDtl", "timephase"),
    ("ITEM-123 method of manufacture", "bom"),
    ("routing operations for ITEM-123", "bom"),
])
def test_detect_part_view(target, kind):
    assert detect_part_view(target) == kind


def test_resolve_part_from_where_and_target():
    assert resolve_part("PartNum = 'WIDGET-100'", "time phase") == "WIDGET-100"
    assert resolve_part("", "bom for part WIDGET-9") == "WIDGET-9"
    assert resolve_part("MtlPartNum eq 'RAW-1'", "") == "RAW-1"
    assert resolve_part("", "time phase") is None


def test_resolve_plant(configured_plants):
    assert _resolve_plant("Plant eq '303'", "") == "303"
    assert _resolve_plant("", "timephase 100 at West Works") == "303"
    assert _resolve_plant("", "bom for X") == ""


def test_rank_jobs_completed_first_then_open_then_planning():
    # A future-dated unreleased MRP job must NOT outrank a completed job that
    # actually ran, even though its StartDate is newest.
    jobs = [
        {"JobNum": "RAN-OLD", "JobReleased": True, "JobClosed": True, "StartDate": "2026-01-01"},
        {"JobNum": "RAN-NEW", "JobReleased": True, "JobClosed": True, "StartDate": "2026-06-01"},
        {"JobNum": "OPEN", "JobReleased": True, "JobClosed": False, "StartDate": "2026-12-01"},
        {"JobNum": "MRP", "JobReleased": False, "JobClosed": False, "StartDate": "2027-02-01"},
    ]
    ranked = [j["JobNum"] for j in _rank_jobs(jobs)]
    assert ranked == ["RAN-NEW", "RAN-OLD", "OPEN", "MRP"]
    assert _rank_jobs([]) == []


# --------------------------------------------------------------------------- #
# Time phase (method-POST path)
# --------------------------------------------------------------------------- #

def test_timephase_success_routes_to_goprocess():
    client = _FakeClient(post_result={"returnObj": {"TimePhas": [
        {"PartNum": "X", "DueDate": "2026-07-10", "RequiredQty": 5,
         "BalanceQty": -5, "SourceName": "Job 123", "Ignored": "drop me"},
    ]}})
    rbac = _FakeRBAC()
    out = json.loads(_run(read_timephase(
        client, rbac, _SESSION,
        where="PartNum = 'X' and Plant = '101'", target="time phase",
        fields="", limit=20)))

    assert client.posts[0][0].endswith("/GoProcessTimePhase")
    assert client.posts[0][1]["partNum"] == "X"
    assert client.posts[0][1]["plant"] == "101"
    assert rbac.checked == [_partviews.TIMEPHASE_SERVICE]
    assert "error" not in out
    assert out["row_count"] == 1
    assert out["resolved"]["via"] == "GoProcessTimePhase"
    assert "Ignored" not in out["records"][0]           # projection drops it
    assert out["records"][0]["SourceName"] == "Job 123"


def test_timephase_no_part_returns_envelope():
    out = json.loads(_run(read_timephase(
        _FakeClient(), _FakeRBAC(), _SESSION,
        where="", target="time phase", fields="", limit=20)))
    assert out["error"] == "need_part"


def test_timephase_rbac_denied():
    client = _FakeClient()
    out = json.loads(_run(read_timephase(
        client, _FakeRBAC(allowed=False), _SESSION,
        where="PartNum = 'X'", target="time phase", fields="", limit=20)))
    assert out["error"] == "access_denied"
    assert client.posts == []


def test_timephase_access_scope_denial_maps_to_access_denied(configured_plants):
    # Epicor's "Access denied (…GoProcessTimePhase)" comes from the read-only
    # Access Scope (Get*-only), not the user's rights — surface it as a terminal
    # access problem naming the real cause, not a retryable query failure.
    client = _FakeClient(post_result=EpicorError(
        401, "Access denied (Erp.BO.TimePhas.GoProcessTimePhase)."))
    out = json.loads(_run(read_timephase(
        client, _FakeRBAC(), _SESSION,
        where="PartNum = 'X'", target="time phase", fields="", limit=20)))
    assert out["error"] == "access_denied"
    assert "access scope" in out["message"].lower()


def test_timephase_empty_is_terminal(configured_plants):
    out = json.loads(_run(read_timephase(
        _FakeClient(post_result={"returnObj": {"TimePhas": []}}),
        _FakeRBAC(), _SESSION,
        where="PartNum = 'X'", target="time phase", fields="", limit=20)))
    assert out["row_count"] == 0
    assert "complete answer" in out["summary"]


def test_timephase_no_plant_iterates_plants_and_merges(configured_plants):
    # GoProcessTimePhase is per-plant and returns [] for an empty plant, so with
    # no plant given the helper must query every configured plant and merge the
    # non-empty ones — here only plant 101 has data.
    client = _FakeClient(timephase_by_plant={
        "101": [{"PartNum": "X", "Plant": "101", "RequiredQty": 5,
                "SourceName": "Job 1"}],
    })
    out = json.loads(_run(read_timephase(
        client, _FakeRBAC(), _SESSION,
        where="PartNum = 'X'", target="time phase", fields="", limit=20)))
    # It probed every plant (empty plant string is never sent) and merged.
    sent = {p[1]["plant"] for p in client.posts}
    assert sent == set(configured_plants) and "" not in sent
    assert out["row_count"] == 1
    assert out["records"][0]["Plant"] == "101"
    assert "101" in out["resolved"]["Plant"]


def test_timephase_without_a_configured_or_explicit_plant_refuses(monkeypatch):
    monkeypatch.setattr(_partviews, "PLANTS", {})
    client = _FakeClient()
    out = json.loads(_run(read_timephase(
        client, _FakeRBAC(), _SESSION, where="PartNum = 'X'",
        target="time phase", fields="", limit=20)))
    assert out["error"] == "plant_required"
    assert "EPICOR_MCP_PLANTS" in out["message"]
    assert client.posts == []


def test_timephase_explicit_plant_single_call():
    client = _FakeClient(timephase_by_plant={
        "303": [{"PartNum": "X", "Plant": "303"}],
        "101": [{"PartNum": "X", "Plant": "101"}],
    })
    out = json.loads(_run(read_timephase(
        client, _FakeRBAC(), _SESSION,
        where="PartNum = 'X' and Plant = '303'", target="time phase",
        fields="", limit=20)))
    assert [p[1]["plant"] for p in client.posts] == ["303"]   # only the one plant
    assert out["row_count"] == 1
    assert out["records"][0]["Plant"] == "303"


# --------------------------------------------------------------------------- #
# BOM = job method
# --------------------------------------------------------------------------- #

def _patch_jobhead(monkeypatch, jobs):
    async def _fake_getrows(client, index, service, entity_set, api_key, **kw):
        _fake_getrows.seen = {"service": service, "entity_set": entity_set,
                              "filter": kw.get("filter")}
        return json.dumps({"records": jobs})
    monkeypatch.setattr(_partviews, "run_getrows", _fake_getrows)
    return _fake_getrows


def test_bom_probes_past_empty_planning_job_to_one_with_materials(monkeypatch):
    # Newest job by StartDate is an unreleased MRP job with NO method; the
    # helper must skip it and land on the completed job whose GetByID has one.
    gr = _patch_jobhead(monkeypatch, [
        {"JobNum": "MRP-99", "PartNum": "ASSY-1", "JobReleased": False,
         "JobClosed": False, "StartDate": "2027-02-01"},
        {"JobNum": "RAN-1", "PartNum": "ASSY-1", "JobReleased": True,
         "JobClosed": True, "StartDate": "2026-06-01"},
    ])
    client = _FakeClient(job_methods={
        # only RAN-1 has a loaded method (MRP-99 GetByID returns empty)
        "RAN-1": {"JobMtl": [{"JobNum": "RAN-1", "AssemblySeq": 0, "MtlSeq": 10,
                              "PartNum": "RAW-1", "QtyPer": 2}],
                  "JobOper": [{"JobNum": "RAN-1", "AssemblySeq": 0, "OprSeq": 10,
                               "OpCode": "SAW"}]},
    })
    rbac = _FakeRBAC()
    out = json.loads(_run(read_bom(
        client, object(), rbac, _SESSION,
        where="PartNum = 'ASSY-1'", target="bom", fields="", limit=20)))

    assert gr.seen["service"] == _partviews.JOB_SERVICE
    assert gr.seen["entity_set"] == "JobHead"
    assert "PartNum eq 'ASSY-1'" in gr.seen["filter"]
    assert rbac.checked == [_partviews.JOB_SERVICE]
    # Method pulled via GetByID (not the OData collections).
    assert all(u.endswith("/GetByID") for u, _ in client.posts)
    # Completed RAN-1 is ranked ahead of the newer MRP-99 and has a method.
    assert out["resolved"]["JobNum"] == "RAN-1"
    assert out["row_count"] == 1
    assert out["materials"][0]["PartNum"] == "RAW-1"
    assert out["operations"][0]["OpCode"] == "SAW"
    assert "job method" in out["note"]


def test_bom_explicit_jobnum_skips_part_lookup(monkeypatch):
    gr = _patch_jobhead(monkeypatch, [])   # must NOT be consulted
    client = _FakeClient(job_methods={
        "J-9": {"JobMtl": [{"JobNum": "J-9", "AssemblySeq": 0, "MtlSeq": 10}],
                "JobOper": []},
    })
    out = json.loads(_run(read_bom(
        client, object(), _FakeRBAC(), _SESSION,
        where="JobNum = 'J-9'", target="bom", fields="", limit=20)))
    assert not hasattr(gr, "seen")          # JobHead lookup skipped
    assert out["resolved"]["JobNum"] == "J-9"
    assert out["row_count"] == 1


def test_bom_no_job_method_does_not_claim_no_engineering_bom(monkeypatch):
    _patch_jobhead(monkeypatch, [])
    out = json.loads(_run(read_bom(
        _FakeClient(), object(), _FakeRBAC(), _SESSION,
        where="PartNum = 'PURCHASED-1'", target="bom", fields="", limit=20)))
    assert out["terminal"] is False
    assert out["row_count"] == 0
    assert "No job method" in out["summary"]
    assert "Engineering BOMs may still exist" in out["summary"]
    assert "engineering method" in out["stop_hint"]


def test_bom_no_part_returns_envelope():
    out = json.loads(_run(read_bom(
        _FakeClient(), object(), _FakeRBAC(), _SESSION,
        where="", target="bill of materials", fields="", limit=20)))
    assert out["error"] == "need_part"


def test_bom_rbac_denied(monkeypatch):
    gr = _patch_jobhead(monkeypatch, [])
    out = json.loads(_run(read_bom(
        _FakeClient(), object(), _FakeRBAC(allowed=False), _SESSION,
        where="PartNum = 'X'", target="bom", fields="", limit=20)))
    assert out["error"] == "access_denied"
    assert not hasattr(gr, "seen")          # never looked up jobs


# --------------------------------------------------------------------------- #
# Dedicated epicor_time_phase tool
# --------------------------------------------------------------------------- #

def _register_time_phase(client, rbac):
    """Register epicor_time_phase on a capture shim and return its coroutine."""
    from epicor_mcp.tools import time_phase
    captured = {}

    class _Shim:
        def tool(self, *a, **k):
            def deco(fn):
                captured["fn"] = fn
                return fn
            return deco

    time_phase.register(_Shim(), object(), rbac, client)
    return captured["fn"]


def test_time_phase_tool_delegates_to_core(monkeypatch, configured_plants):
    import epicor_mcp.tools.time_phase as tp_mod
    monkeypatch.setattr(tp_mod, "get_current_session", lambda: _SESSION)
    client = _FakeClient(timephase_by_plant={
        "101": [{"PartNum": "X", "Plant": "101", "BalanceQty": 5}],
    })
    fn = _register_time_phase(client, _FakeRBAC())
    out = json.loads(_run(fn(part="X")))
    assert out["row_count"] == 1
    assert out["records"][0]["Plant"] == "101"
    # No plant given → iterated every plant (never an empty plant string).
    sent = {p[1]["plant"] for p in client.posts}
    assert sent == set(configured_plants) and "" not in sent


def test_time_phase_tool_requires_part(monkeypatch):
    import epicor_mcp.tools.time_phase as tp_mod
    monkeypatch.setattr(tp_mod, "get_current_session", lambda: _SESSION)
    fn = _register_time_phase(_FakeClient(), _FakeRBAC())
    out = json.loads(_run(fn(part="   ")))
    assert out["error"] == "need_part"

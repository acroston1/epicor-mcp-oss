"""Unit tests for the planner→jobs recognizer folded into ``epicor_read``.

Covers recognition ("jobs for planner X" vs the roster vs neither), planner-code
and name extraction, the casual-name fuzzy match ("Taylor Morgan" → the
Person-master "Taylor M" → Plan2), the open/closed filter, and the
not-found / ambiguous / need_planner envelopes. No live Epicor — ``run_odata``
(Person master) and ``run_getrows`` (JobHead) are monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.tools import _planner
from epicor_mcp.tools._planner import (
    _extract_planner_term,
    _match_by_name,
    _open_filter,
    detect_planner_jobs,
    detect_planner_roster,
    planner_jobs,
    planner_roster,
)


class _FakeRBAC:
    def __init__(self, allowed=True, api_key="K", deny=()):
        self._allowed = allowed
        self._api_key = api_key
        self._deny = set(deny)

    def check_access(self, user_id, service_id):
        if service_id in self._deny or not self._allowed:
            return (False, f"no access to {service_id}")
        return (True, "")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key=self._api_key)


_SESSION = types.SimpleNamespace(user_id="tester")
_INDEX = object()
_CLIENT = object()

# Synthetic Person roster: abbreviated and full names, an inactive entry,
# and non-natural PersonID ordering.
_PEOPLE = [
    {"PersonID": "Plan6", "Name": "Alex J", "InActive": False, "EMailAddress": ""},
    {"PersonID": "Plan2", "Name": "Taylor M", "InActive": False, "EMailAddress": "tm@example.org"},
    {"PersonID": "Plan10", "Name": "Robin Chen", "InActive": True, "EMailAddress": ""},
    {"PersonID": "Plan16", "Name": "Jamie Reed", "InActive": False, "EMailAddress": ""},
    {"PersonID": "Plan5", "Name": "Casey N", "InActive": False, "EMailAddress": ""},
]


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Recognition (pure)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("target,expected", [
    ("open jobs for planner Taylor Morgan", True),
    ("what jobs are assigned to planner Jamie Reed", True),
    ("jobs for planner Plan16", True),
    ("jobs planned by Chris", True),
    ("planner Plan2 jobs", True),
    # Not a planner-jobs request:
    ("show me open jobs", False),
    ("list the planners", False),           # roster, not jobs
    ("who is planner Plan16", False),        # roster/lookup, no job word
    ("trend yield for part X over 3 months", False),
])
def test_detect_planner_jobs(target, expected):
    assert detect_planner_jobs(target) is expected


@pytest.mark.parametrize("target,expected", [
    ("list the planners", True),
    ("who are the planners", True),
    ("show planners", True),
    ("who is planner Plan16", True),
    ("planner list", True),
    # Not a roster request:
    ("jobs for planner Taylor Morgan", False),   # has a job word
    ("open jobs", False),
])
def test_detect_planner_roster(target, expected):
    assert detect_planner_roster(target) is expected


@pytest.mark.parametrize("target,code,name", [
    ("open jobs for planner Taylor Morgan", "", "Taylor Morgan"),
    ("jobs for planner Plan16", "Plan16", ""),
    ("planner plan #2 jobs", "Plan2", ""),
    ("jobs planned by Chris", "", "Chris"),
    ("who is planner Plan10", "Plan10", ""),
])
def test_extract_planner_term(target, code, name):
    c, n = _extract_planner_term(target)
    assert c == code
    assert n == name


@pytest.mark.parametrize("target,expected", [
    ("open jobs for planner X", "JobClosed eq false"),
    ("active jobs for planner X", "JobClosed eq false"),
    ("closed jobs for planner X", "JobClosed eq true"),
    ("jobs for planner X", ""),
])
def test_open_filter(target, expected):
    assert _open_filter(target) == expected


# --------------------------------------------------------------------------- #
# Casual-name fuzzy match (the crux of the fix)
# --------------------------------------------------------------------------- #

def test_match_full_name_to_first_plus_initial():
    # "Taylor Morgan" must resolve to the Person-master "Taylor M".
    m = _match_by_name("taylor morgan", _PEOPLE)
    assert [p["PersonID"] for p in m] == ["Plan2"]


def test_match_exact_name():
    m = _match_by_name("jamie reed", _PEOPLE)
    assert [p["PersonID"] for p in m] == ["Plan16"]


def test_match_first_name_only_can_be_unique():
    m = _match_by_name("alex", _PEOPLE)
    assert [p["PersonID"] for p in m] == ["Plan6"]


def test_match_unknown_returns_empty():
    assert _match_by_name("zaphod beeblebrox", _PEOPLE) == []


# --------------------------------------------------------------------------- #
# Orchestration — run_odata (People) + run_getrows (JobHead) faked
# --------------------------------------------------------------------------- #

def _patch(monkeypatch, people=_PEOPLE, jobs=None):
    jobs = jobs if jobs is not None else []

    async def _fake_odata(client, service, entity, api_key, **kw):
        return json.dumps({"records": people})

    async def _fake_getrows(client, index, service, entity, api_key, **kw):
        return json.dumps({"records": jobs})

    monkeypatch.setattr(_planner, "run_odata", _fake_odata)
    monkeypatch.setattr(_planner, "run_getrows", _fake_getrows)


def test_planner_jobs_by_name_bridges_to_code(monkeypatch):
    jobs = [
        {"JobNum": "J1", "PersonID": "Plan2", "PersonIDName": "Taylor M", "JobClosed": False},
        {"JobNum": "J2", "PersonID": "Plan2", "PersonIDName": "Taylor M", "JobClosed": False},
    ]
    _patch(monkeypatch, jobs=jobs)
    out = json.loads(_run(planner_jobs(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        target="open jobs for planner Taylor Morgan")))
    assert "error" not in out
    assert out["resolved"]["planner_code"] == "Plan2"
    assert out["resolved"]["filter"] == "PersonID eq 'Plan2' and JobClosed eq false"
    assert out["row_count"] == 2
    assert "Plan2" in out["summary"] and "Taylor M" in out["summary"]
    # The note names the RIGHT column so the model doesn't fall back to PlanUserID.
    assert "PlanUserID" in out["note"]


def test_planner_jobs_by_code(monkeypatch):
    _patch(monkeypatch, jobs=[{"JobNum": "J9", "PersonID": "Plan16"}])
    out = json.loads(_run(planner_jobs(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION, target="jobs for planner Plan16")))
    assert out["resolved"]["planner_code"] == "Plan16"
    assert out["resolved"]["filter"] == "PersonID eq 'Plan16'"


def test_planner_jobs_unknown_returns_roster(monkeypatch):
    _patch(monkeypatch)
    out = json.loads(_run(planner_jobs(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        target="jobs for planner Zaphod Beeblebrox")))
    assert out["error"] == "planner_not_found"
    codes = [p["planner_code"] for p in out["valid"]["planners"]]
    assert "Plan2" in codes
    assert "Plan10" not in codes           # inactive planners are hidden


def test_planner_jobs_bad_code_returns_roster(monkeypatch):
    _patch(monkeypatch)
    out = json.loads(_run(planner_jobs(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION, target="jobs for planner Plan999")))
    assert out["error"] == "planner_not_found"


def test_planner_jobs_access_denied(monkeypatch):
    _patch(monkeypatch)
    out = json.loads(_run(planner_jobs(
        _CLIENT, _INDEX, _FakeRBAC(allowed=False), _SESSION,
        target="jobs for planner Taylor Morgan")))
    assert out["error"] == "access_denied"


def test_planner_jobs_job_service_denied_after_resolve(monkeypatch):
    # PersonSvc allowed (resolves the planner) but JobEntrySvc denied.
    _patch(monkeypatch, jobs=[{"JobNum": "J1"}])
    out = json.loads(_run(planner_jobs(
        _CLIENT, _INDEX, _FakeRBAC(deny=("Erp.BO.JobEntrySvc",)), _SESSION,
        target="jobs for planner Taylor Morgan")))
    assert out["error"] == "access_denied"


def test_planner_roster_lists_active(monkeypatch):
    _patch(monkeypatch)
    out = json.loads(_run(planner_roster(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION, target="list the planners")))
    codes = [p["planner_code"] for p in out["planners"]]
    assert codes == ["Plan2", "Plan5", "Plan6", "Plan16"]   # natural sort, active only


def test_planner_roster_single_by_code(monkeypatch):
    _patch(monkeypatch)
    out = json.loads(_run(planner_roster(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION, target="who is planner Plan16")))
    assert out["planner"]["planner_code"] == "Plan16"
    assert out["planner"]["name"] == "Jamie Reed"

"""Unit tests for the production-yield trend engine folded into ``epicor_read``.

Covers recognition ("trend yield over N months"), part extraction, the pure
monthly-bucket + ratio math, the need_part / no-jobs / access_denied
envelopes, and the two-fetch orchestration (JobHead good vs JobOper scrap).
No live Epicor — ``run_getrows`` is monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.tools import _yield
from epicor_mcp.tools._yield import (
    _bucket_yield,
    _extract_part,
    detect_yield_trend,
    yield_trend,
)


class _FakeRBAC:
    def __init__(self, allowed=True, api_key="K"):
        self._allowed = allowed
        self._api_key = api_key

    def check_access(self, user_id, service_id):
        return (self._allowed, "" if self._allowed else f"no access to {service_id}")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key=self._api_key)


_SESSION = types.SimpleNamespace(user_id="tester")
_INDEX = object()
_CLIENT = object()


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Recognition + part extraction (pure)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("target,expected", [
    ("trend the production yield for the Example 520 chassis over the last 3 months", True),
    ("production yield by month for part X", True),
    ("scrap rate trend for PART-200", True),
    ("yield history for part ABC", True),
    ("scrap trended monthly", True),
    # Not a trend request:
    ("production yield for part PART-300", False),   # no time signal
    ("open jobs", False),
    ("time phase for part 6205", False),
    ("top parts by sales in 2025", False),
    ("BOM for part WIDGET-9", False),
])
def test_detect_yield_trend(target, expected):
    assert detect_yield_trend(target) is expected


@pytest.mark.parametrize("target,where,expected", [
    ("trend yield", "PartNum = 'PART-300'", "PART-300"),
    ("trend yield for PART-200 over 3 months", "", "PART-200"),
    ("yield for D7890-001 by month", "", "D7890-001"),
    # A description with no part-shaped token yields nothing (must ask).
    ("trend the production yield for the Example 520 chassis over 3 months", "", ""),
    # "3-months" / "last-3" must NOT be mistaken for a part number.
    ("trend yield over the last-3 months", "", ""),
])
def test_extract_part(target, where, expected):
    assert _extract_part(target, where) == expected


# --------------------------------------------------------------------------- #
# Pure bucket + ratio math
# --------------------------------------------------------------------------- #

def test_bucket_yield_math():
    jobs = [
        {"JobNum": "J1", "JobCompletionDate": "2026-05-10T00:00:00", "QtyCompleted": 90},
        {"JobNum": "J2", "JobCompletionDate": "2026-05-20T00:00:00", "QtyCompleted": 80},
        {"JobNum": "J3", "JobCompletionDate": "2026-06-05T00:00:00", "QtyCompleted": 50},
    ]
    scrap = {"J1": 10.0, "J2": 20.0, "J3": 50.0}
    out = _bucket_yield(jobs, scrap, "JobCompletionDate")
    assert [b["month"] for b in out] == ["2026-05", "2026-06"]
    may = out[0]
    assert may["jobs"] == 2 and may["good_qty"] == 170.0 and may["scrap_qty"] == 30.0
    assert may["yield_pct"] == 85.0          # 170 / 200
    jun = out[1]
    assert jun["yield_pct"] == 50.0          # 50 / 100


def test_bucket_yield_zero_total_is_none_not_crash():
    jobs = [{"JobNum": "J9", "JobCompletionDate": "2026-05-01", "QtyCompleted": 0}]
    out = _bucket_yield(jobs, {}, "JobCompletionDate")
    assert out[0]["yield_pct"] is None       # 0 good + 0 scrap → undefined, not /0


def test_bucket_yield_skips_undated_rows():
    jobs = [{"JobNum": "J1", "JobCompletionDate": None, "QtyCompleted": 10}]
    assert _bucket_yield(jobs, {}, "JobCompletionDate") == []


# --------------------------------------------------------------------------- #
# Envelopes
# --------------------------------------------------------------------------- #

def test_yield_trend_need_part_when_only_description():
    out = json.loads(_run(yield_trend(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        target="trend the production yield for the Example 520 chassis over 3 months",
        where="")))
    assert out["error"] == "need_part"
    # Hands back the exact "resolve the part first" recovery path (INV-1).
    assert out["retry_with"]["target"] == "Part"


def test_yield_trend_access_denied():
    out = json.loads(_run(yield_trend(
        _CLIENT, _INDEX, _FakeRBAC(allowed=False), _SESSION,
        target="trend yield over 3 months", where="PartNum = 'X-1'")))
    assert out["error"] == "access_denied"


# --------------------------------------------------------------------------- #
# Full orchestration (JobHead good vs JobOper scrap), run_getrows faked
# --------------------------------------------------------------------------- #

def _fake_getrows_factory(jobhead_rows, joboper_rows):
    async def _fake(client, index, service, entity, api_key, **kw):
        rows = jobhead_rows if entity == "JobHead" else joboper_rows
        return json.dumps({"records": rows})
    return _fake


def test_yield_trend_end_to_end(monkeypatch):
    jobhead = [
        {"JobNum": "J1", "JobCompletionDate": "2026-05-10T00:00:00", "QtyCompleted": 90, "ProdQty": 100},
        {"JobNum": "J2", "JobCompletionDate": "2026-05-20T00:00:00", "QtyCompleted": 80, "ProdQty": 100},
        {"JobNum": "J3", "JobCompletionDate": "2026-06-05T00:00:00", "QtyCompleted": 50, "ProdQty": 100},
    ]
    joboper = [
        {"JobNum": "J1", "ScrapQty": 4}, {"JobNum": "J1", "ScrapQty": 6},
        {"JobNum": "J2", "ScrapQty": 20},
        {"JobNum": "J3", "ScrapQty": 50},
    ]
    monkeypatch.setattr(_yield, "run_getrows",
                        _fake_getrows_factory(jobhead, joboper))

    out = json.loads(_run(yield_trend(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        target="trend production yield over the last 3 months",
        where="PartNum = 'PART-300'")))

    assert "error" not in out
    assert out["resolved"]["entity_set"] == "JobHead+JobOper"
    assert out["resolved"]["part"] == "PART-300"
    months = {b["month"]: b for b in out["records"]}
    assert months["2026-05"]["yield_pct"] == 85.0    # (90+80)/(170+30)
    assert months["2026-06"]["yield_pct"] == 50.0    # 50/(50+50)
    # Overall in the summary: 220 good / 300 total.
    assert "73.33%" in out["summary"]
    # The metric formula is stated so the ratio is never a silent guess.
    assert "JobOper.ScrapQty" in out["metric"]


def test_yield_trend_no_jobs_is_terminal(monkeypatch):
    monkeypatch.setattr(_yield, "run_getrows", _fake_getrows_factory([], []))
    out = json.loads(_run(yield_trend(
        _CLIENT, _INDEX, _FakeRBAC(), _SESSION,
        target="trend yield over 3 months", where="PartNum = 'NOPE-1'")))
    assert out["records"] == []
    assert "no yield to trend" in out["summary"].lower()
    assert "do not retry" in out["stop_hint"].lower()

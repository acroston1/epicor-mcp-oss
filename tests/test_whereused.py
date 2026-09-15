"""Unit tests for the part where-used recognizer folded into ``epicor_read``.

Covers recognition ("used to make" / "where used" / filter-driven MtlPartNum,
vs the downward-BOM look-alikes), part resolution from where/target, the
per-plant dedupe of ``GetPartWhereUsed`` rows, and the need_part / terminal-empty
/ access-denied envelopes. No live Epicor — the client's method-POST is faked.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools._whereused import (
    _dedupe,
    _resolve_wu_part,
    detect_where_used,
    where_used,
)


class _FakeRBAC:
    def __init__(self, allowed=True, api_key="K"):
        self._allowed = allowed
        self._api_key = api_key

    def check_access(self, user_id, service_id):
        return (True, "") if self._allowed else (False, f"no access to {service_id}")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key=self._api_key)


class _FakeClient:
    """Captures the GetPartWhereUsed POST and returns a canned dataset."""

    def __init__(self, rows=None, error=None):
        self._rows = rows if rows is not None else []
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, api_key, json_body=None):
        self.calls.append((url, json_body or {}))
        if self._error:
            raise self._error
        return {"returnObj": {"PartWhereUsed": self._rows,
                              "PartRefDesWhereUsed": [], "ExtensionTables": []}}


_SESSION = types.SimpleNamespace(user_id="tester")


def _row(parent="ASSY-100", rev="A", mtlseq=10, alt="", qty=2.0):
    return {
        "Company": "DEMO", "PartNum": parent, "RevisionNum": rev,
        "MtlSeq": mtlseq, "MtlPartNum": "PART-100", "QtyPer": qty,
        "AltMethod": alt, "TypeDesc": "Mtl",
        "PartNumPartDescription": "Example assembly bracket",
        "OpCode": "CUT", "RowMod": "",
    }


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Recognition (pure)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("target,where,expected", [
    # Representative natural-language requests:
    ("what is part PART-100 used to make", "", True),
    ("part where used", "PartNum = 'PART-100'", True),
    ("where used PART-100", "", True),
    ("WhereUsed", "PartNum = 'PART-100'", True),
    ("Erp.BO.PartSvc/WhereUsed", "PartNum = 'PART-100'", True),
    ("material where used", "Material = 'PART-100'", True),
    ("PartMtl", "MtlPartNum = 'PART-100'", True),      # filter says where-used
    ("PartMtl", "Material = 'PART-100'", True),
    # Other natural upward phrasings:
    ("what does PART-100 go into", "", True),
    ("what uses part PART-100", "", True),
    ("which parts use PART-100", "", True),
    ("parents of part PART-100", "", True),
    ("what assembly is PART-100 used in", "", True),   # strong? no — but no job word
    ("what is PART-100 used to build", "", True),
    # NOT where-used (downward BOM / job-scoped / unrelated):
    ("PartMtl", "PartNum = 'PART-100'", False),        # downward by parent
    ("BOM for part PART-100", "", False),
    ("materials used in job JOB-100", "", False),       # job-scoped = downward
    ("open POs", "", False),
    ("", "PartNum = 'X'", False),
])
def test_detect_where_used(target, where, expected):
    assert detect_where_used(target, where) is expected


def test_used_to_make_beats_job_scope_guard():
    # The strong form stays where-used even when a job is mentioned.
    assert detect_where_used("what is PART-100 used to make on job 123", "") is True


@pytest.mark.parametrize("where,target,part", [
    ("PartNum = 'PART-100'", "part where used", "PART-100"),
    ("MtlPartNum = 'PART-100'", "PartMtl", "PART-100"),
    ("Material = 'PART-100'", "material where used", "PART-100"),
    ("", "what is part PART-100 used to make", "PART-100"),
    ("", "where used PART-100", "PART-100"),
    ("", "part where used", None),
])
def test_resolve_wu_part(where, target, part):
    assert _resolve_wu_part(where, target) == part


def test_dedupe_collapses_repeats_and_projects():
    rows = [_row(), _row(), _row(),                      # synthetic per-plant triplicate
            _row(parent="ASSY-200", rev="B", mtlseq=20)]
    out = _dedupe(rows)
    assert len(out) == 2
    assert out[0]["PartNum"] == "ASSY-100"
    assert "SysRowID" not in out[0] and "RowMod" not in out[0]
    assert out[0]["PartNumPartDescription"].startswith("Example")


# --------------------------------------------------------------------------- #
# Orchestration — client method-POST faked
# --------------------------------------------------------------------------- #

def test_where_used_happy_path():
    client = _FakeClient(rows=[_row(), _row(), _row()])
    out = json.loads(_run(where_used(
        client, _FakeRBAC(), _SESSION,
        target="what is part PART-100 used to make")))
    assert "error" not in out
    assert out["row_count"] == 1
    assert out["records"][0]["PartNum"] == "ASSY-100"
    assert "ASSY-100" in out["summary"] and "used to make" in out["summary"]
    assert "stop_hint" in out
    # One call, to the right method, with the right part.
    (url, body), = client.calls
    assert url.endswith("Erp.BO.PartSvc/GetPartWhereUsed")
    assert body["whereUsedPartNum"] == "PART-100"


def test_where_used_part_from_where_clause():
    client = _FakeClient(rows=[_row()])
    out = json.loads(_run(where_used(
        client, _FakeRBAC(), _SESSION,
        target="part where used", where="PartNum = 'PART-100'")))
    assert out["resolved"]["PartNum"] == "PART-100"


def test_where_used_empty_is_terminal_not_error():
    client = _FakeClient(rows=[])
    out = json.loads(_run(where_used(
        client, _FakeRBAC(), _SESSION, target="where used 9999-NOPE")))
    assert "error" not in out
    assert out["terminal"] is True
    assert out["row_count"] == 0
    assert "do NOT hunt" in out["stop_hint"]


def test_where_used_need_part_envelope():
    client = _FakeClient()
    out = json.loads(_run(where_used(
        client, _FakeRBAC(), _SESSION, target="part where used")))
    assert out["error"] == "need_part"
    assert client.calls == []                    # never hit Epicor


def test_where_used_access_denied():
    client = _FakeClient()
    out = json.loads(_run(where_used(
        client, _FakeRBAC(allowed=False), _SESSION,
        target="where used PART-100")))
    assert out["error"] == "access_denied"
    assert client.calls == []


def test_where_used_epicor_error_becomes_envelope():
    client = _FakeClient(error=EpicorError(status_code=500, message="boom"))
    out = json.loads(_run(where_used(
        client, _FakeRBAC(), _SESSION, target="where used PART-100")))
    assert out["error"] == "whereused_failed"
    assert "boom" in out["message"]


def test_where_used_respects_limit():
    rows = [_row(parent=f"P{i}", mtlseq=i) for i in range(30)]
    client = _FakeClient(rows=rows)
    out = json.loads(_run(where_used(
        client, _FakeRBAC(), _SESSION,
        target="where used PART-100", limit=5)))
    assert out["row_count"] == 5
    assert "30 part(s)" in out["summary"]

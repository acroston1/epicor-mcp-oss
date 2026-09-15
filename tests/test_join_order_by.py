"""Caller `order_by` on the parent/child JOIN path (item 5).

The regression these lock down: `order_by` was the ONE join argument that was
neither validated nor auto-corrected before the scan. `fields` and `where` are
both resolved (and BAQ-alias-corrected) up front at the call site, but
`order_by` was passed through raw and only checked AFTER the join had
materialised. A mistyped sort column must be rejected before any scan.

Every test here asserts on whether the join was AWAITED, not just on the
payload: the whole point is that the bad cases now cost ~0 requests.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.tools import read as read_mod
from tests.test_read_routing_e2e import _Client, _Idx, _RBAC, _Server


PART_FIELDS = [
    {"field_name": "PartNum", "field_type": "Edm.String"},
    {"field_name": "PartDescription", "field_type": "Edm.String"},
    {"field_name": "ClassID", "field_type": "Edm.String"},
    {"field_name": "HasOnHandQty", "field_type": "Edm.Boolean"},
]
PARTWHSE_FIELDS = [
    {"field_name": "PartNum", "field_type": "Edm.String"},
    {"field_name": "WarehouseCode", "field_type": "Edm.String"},
    {"field_name": "OnHandQty", "field_type": "Edm.Double"},
]


class _Join:
    """Records whether the join engine ran, and with what."""

    def __init__(self, payload=None):
        self.calls: list[dict] = []
        self.payload = payload if payload is not None else {
            "records": [{"PartNum": "A", "OnHandQty": 1.0}],
            "record_count": 1,
        }

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(self.payload)


def _make(monkeypatch, join, idx=None, client=None):
    monkeypatch.setattr(read_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="tester"))
    monkeypatch.setattr(read_mod, "_capture_children_fn",
                        lambda index, rbac, client: join)
    idx = idx or _Idx(
        fields={("Erp.BO.PartSvc", "Part"): PART_FIELDS,
                ("Erp.BO.PartSvc", "PartWhse"): PARTWHSE_FIELDS},
        entity_sets={"Erp.BO.PartSvc": ["Part", "Parts", "PartWhse"]},
    )
    srv = _Server()
    read_mod.register(srv, idx, _RBAC(), client or _Client())
    return srv.fn


def _run(fn, **kw):
    return json.loads(asyncio.run(fn(**kw)))


# --------------------------------------------------------------------------- #
# Pre-fetch validation — the bad cases must cost ZERO join requests
# --------------------------------------------------------------------------- #

def test_unknown_order_column_errors_without_scanning(monkeypatch):
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse",
               where="PartNum = 'A'", order_by="NoSuchColumn desc")

    assert out["error"] == "unknown_columns"
    assert not join.calls, "the join ran before the sort column was checked"
    # INV-1: the envelope reports BOTH sides of the join, so the model can see
    # which table it should have named. (`column_help` returns the curated set
    # plus close matches, not every column — same shape the sibling
    # fields/where envelopes use.)
    assert set(out["valid"]) == {"Part", "PartWhse"}
    assert out["valid"]["PartWhse"]["total_columns"] == 3


def test_expression_order_by_refused_without_scanning(monkeypatch):
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse",
               where="PartNum = 'A'", order_by="OnHandQty * 2 desc")

    assert out["error"] == "order_expression_unsupported"
    assert not join.calls
    # Names the supported path rather than just refusing.
    assert out["retry_with"]["aggregate"]


def test_baq_alias_order_column_is_corrected(monkeypatch):
    """The corrector already fixes this form in `fields`/`where`; order_by was
    the one argument it never saw."""
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse",
               where="PartNum = 'A'", order_by="PartWhse_OnHandQty desc")

    assert join.calls, "a correctable sort column should not block the read"
    assert join.calls[0]["order_by"] == "OnHandQty desc"
    assert out["assumptions"]["order_corrected"] == {
        "PartWhse_OnHandQty": "OnHandQty"}


def test_order_column_is_never_corrected_to_a_boolean(monkeypatch):
    """`_correct_column`'s substring rule reaches OnHandQty -> HasOnHandQty.

    A boolean ranking still returns plausible rows, so unlike a bad filter
    nothing downstream catches it — refuse instead of guessing.
    """
    join = _Join()
    idx = _Idx(
        fields={("Erp.BO.PartSvc", "Part"): PART_FIELDS,
                ("Erp.BO.PartSvc", "PartWhse"): [
                    {"field_name": "PartNum", "field_type": "Edm.String"}]},
        entity_sets={"Erp.BO.PartSvc": ["Part", "Parts", "PartWhse"]},
    )
    fn = _make(monkeypatch, join, idx=idx)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse",
               where="PartNum = 'A'", order_by="OnHandQty desc")

    assert out["error"] == "unknown_columns"
    assert not join.calls


def test_valid_order_column_outside_the_projection_is_added(monkeypatch):
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse", where="PartNum = 'A'",
               fields="PartNum", order_by="WarehouseCode asc")

    assert join.calls
    sel = (join.calls[0]["parent_select"] + ","
           + join.calls[0]["child_select"])
    assert "WarehouseCode" in sel
    assert "WarehouseCode" in out["assumptions"]["order_column_added"]


# --------------------------------------------------------------------------- #
# The unbounded-scan refusal — the actual latency fix
# --------------------------------------------------------------------------- #

def test_unbounded_global_sort_is_refused_up_front(monkeypatch):
    """No parent-side condition => ranking means paging every Part page. The
    scan cannot complete, so the 82s answer was a partial ranking wearing a
    top-N's clothes."""
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse",
               where="OnHandQty > 0", order_by="OnHandQty desc")

    assert out["error"] == "order_scan_unbounded"
    assert not join.calls
    # Must name the supported paths, not merely refuse.
    msg = out["message"]
    assert "where" in msg and "group_by" in msg


def test_a_parent_side_condition_keeps_the_full_scan(monkeypatch):
    """Negative case: bounded scans are unchanged, still stop_after_child_rows=0
    so the caller sort ranks the WHOLE set."""
    join = _Join()
    fn = _make(monkeypatch, join)
    _run(fn, target="Erp.BO.PartSvc/PartWhse",
         where="ClassID = 'X' and OnHandQty > 0", order_by="OnHandQty desc")

    assert join.calls
    assert join.calls[0]["stop_after_child_rows"] == 0
    assert join.calls[0]["order_by"] == "OnHandQty desc"


def test_no_order_by_still_stops_early(monkeypatch):
    """The early stop is what keeps an unordered child listing at ~17s; the
    refusal above must not have disturbed it."""
    join = _Join()
    fn = _make(monkeypatch, join)
    _run(fn, target="Erp.BO.PartSvc/PartWhse", where="OnHandQty > 0", limit=25)

    assert join.calls
    assert join.calls[0]["stop_after_child_rows"] == 25


# --------------------------------------------------------------------------- #
# _augment — the engine's ordering meta must reach `resolved`
# --------------------------------------------------------------------------- #

def test_augment_lifts_order_meta_into_resolved():
    raw = json.dumps({
        "records": [{"PartNum": "A"}],
        "record_count": 1,
        "order": "OnHandQty desc",
        "order_source": "caller",
    })
    out = json.loads(read_mod._augment(
        raw, {"service": "Erp.BO.PartSvc", "entity_set": "Part"},
        limit=100, skip=0, cursor_ctx={}, paginated=False))

    assert out["resolved"]["order"] == "OnHandQty desc"
    assert out["resolved"]["order_source"] == "caller"


def test_augment_labels_an_incomplete_ranking():
    """The engine already knew the ranking was partial; the label just never
    reached the field a weak model reads."""
    raw = json.dumps({
        "records": [{"PartNum": "A"}],
        "record_count": 1,
        "order": "OnHandQty desc",
        "order_source": "caller",
        "order_warning": "the scan did NOT reach the end of the set",
    })
    out = json.loads(read_mod._augment(
        raw, {"service": "Erp.BO.PartSvc", "entity_set": "Part"},
        limit=100, skip=0, cursor_ctx={}, paginated=False))

    assert out["summary"].startswith("INCOMPLETE RANKING:")
    assert out["resolved"]["order_warning"]


def test_augment_leaves_an_ordinary_payload_alone():
    raw = json.dumps({"records": [{"PartNum": "A"}], "record_count": 1})
    out = json.loads(read_mod._augment(
        raw, {"service": "Erp.BO.PartSvc", "entity_set": "Part"},
        limit=100, skip=0, cursor_ctx={}, paginated=False))

    assert "order" not in out["resolved"]
    assert not out.get("summary", "").startswith("INCOMPLETE RANKING")


# --------------------------------------------------------------------------- #
# unknown_columns on the JOIN path carries the same side attribution as the
# plain path — INV-1 is a UNIFORM shape, not a per-branch one. (Both tables'
# `valid` blocks are preserved; the attribution rides at the top level.)
# --------------------------------------------------------------------------- #

def test_join_field_error_reports_the_where_clean(monkeypatch):
    """A bad projection column must not implicate a valid filter.

    On the join path the where is routed AFTER the fields are partitioned, so
    the fields envelope used to return before the filter was ever looked at —
    the BATCH-100 failure shape, one branch over.
    """
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse",
               where="PartNum = 'A'", fields="PartNum,WarehouseCode,Frobnicate")

    assert out["error"] == "unknown_columns"
    assert not join.calls, "the join ran before the columns were checked"
    assert out["unknown_by_argument"] == {"fields": ["Frobnicate"]}
    assert out["validated_clean"] == ["where"]
    assert out["retry_with"]["where"] == "PartNum = 'A'"
    survivors = [f.strip() for f in out["retry_with"]["fields"].split(",")]
    assert set(survivors) == {"PartNum", "WarehouseCode"}
    # Both tables' column help is still served, unchanged.
    assert set(out["valid"]) == {"Part", "PartWhse"}


def test_join_where_error_reports_the_fields_clean(monkeypatch):
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse",
               where="Frobnicate = 'A'", fields="PartNum,WarehouseCode")

    assert out["error"] == "unknown_columns"
    assert not join.calls
    assert out["unknown_by_argument"] == {"where": ["Frobnicate"]}
    assert out["validated_clean"] == ["fields"]
    assert out["retry_with"]["fields"] == "PartNum,WarehouseCode"


def test_join_order_by_error_reports_where_and_fields_clean(monkeypatch):
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse", where="PartNum = 'A'",
               fields="PartNum,WarehouseCode", order_by="NoSuchColumn desc")

    assert out["error"] == "unknown_columns"
    assert not join.calls
    assert out["unknown_by_argument"] == {"order_by": ["NoSuchColumn"]}
    assert set(out["validated_clean"]) == {"where", "fields"}
    assert set(out["valid"]) == {"Part", "PartWhse"}


def test_join_field_error_still_probes_a_broken_where(monkeypatch):
    """The clean bill must be EARNED, not assumed.

    The fields envelope fires before `_route_conjuncts` runs, so without the
    explicit probe a genuinely broken filter would be reported as valid — a
    confident half-truth, worse than the ambiguity it replaced.
    """
    join = _Join()
    fn = _make(monkeypatch, join)
    out = _run(fn, target="Erp.BO.PartSvc/PartWhse",
               where="Bogusly = 'A'", fields="PartNum,Frobnicate")

    assert out["error"] == "unknown_columns"
    assert not join.calls
    assert out["unknown_by_argument"] == {
        "fields": ["Frobnicate"], "where": ["Bogusly"]}
    assert "validated_clean" not in out

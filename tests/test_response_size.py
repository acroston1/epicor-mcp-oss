"""Response size: curated defaults, the column cap, and truncation honesty (Area 4).

Two response-size regressions:

1. The SearchSvc fast-route rewrites `service` to the
   ``<Entity>SearchSvc`` twin BEFORE fields resolve, so the curated-default
   lookup — keyed on the PARENT service — MISSED and every column shipped.
   This can exhaust the inline response budget even on a modest row count.
2. The formatter caps rows to keep=40 but left ``record_count`` set from the
   PRE-truncation list, so ``epicor_read`` computed has_more from the truncated
   rows (40 >= 100 is False), suppressed next_cursor, and incorrectly called
   the truncated rows "the complete result" with a "do NOT re-run" hint.
"""

from __future__ import annotations

import json
import types

import pytest

from epicor_mcp.response.formatter import truncate_and_summarize
from epicor_mcp.tools._inline_schema import CURATED_BY_ENTITY, FIELD_OVERRIDES
from epicor_mcp.tools._resolve import _CAP_THRESHOLD, resolve_fields
from epicor_mcp.tools.read import _augment


class _Idx:
    def __init__(self, cols):
        self._cols = cols

    def get_fields(self, service, entity_set):
        return [{"field_name": c, "field_type": "Edm.String"} for c in self._cols]


ORDERDTL_REAL = FIELD_OVERRIDES[("Erp.BO.SalesOrderSvc", "OrderDtl")]


# --------------------------------------------------------------------------- #
# C1 — curated defaults must survive the SearchSvc / alias-service swap
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("twin,entity", [
    ("Erp.BO.OrderDtlSearchSvc", "OrderDtl"),
    ("Erp.BO.JobOperSearchSvc", "JobOper"),
    ("Erp.BO.JobMtlSearchSvc", "JobMtl"),
    ("Erp.BO.ShipDtlSearchSvc", "ShipDtl"),
    # Not SearchSvc twins at all — alias services hosting a curated table.
    ("Erp.BO.ARInvSearchSvc", "InvcHead"),
    ("Erp.BO.SalesOrdHedDtlSvc", "OrderHed"),
])
def test_curated_defaults_survive_the_service_swap(twin, entity):
    curated = CURATED_BY_ENTITY[entity]
    idx = _Idx(curated + [f"Filler{i}" for i in range(300)])
    res = resolve_fields(idx, twin, entity, "")
    assert res["fields"] == curated
    assert res["default_used"] is True
    assert not res.get("capped")


def test_a_curated_column_missing_from_the_twin_is_dropped_not_projected():
    """The intersection guard: a bad fallback degrades to FEWER columns, never
    to an invalid $select."""
    idx = _Idx(ORDERDTL_REAL[:3])
    res = resolve_fields(idx, "Erp.BO.OrderDtlSearchSvc", "OrderDtl", "")
    assert res["fields"] == ORDERDTL_REAL[:3]


def test_curated_entity_names_are_distinct():
    """The entity-name fallback rests on this. If FIELD_OVERRIDES ever gains
    two services curating the SAME entity name differently, one silently wins."""
    names = [ent for (_svc, ent) in FIELD_OVERRIDES]
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# C4 — the announced column cap for uncurated wide entities
# --------------------------------------------------------------------------- #

def test_wide_uncurated_entity_is_capped_and_says_so():
    idx = _Idx(["WidgetNum", "WidgetID"] + [f"Col{i}Date" for i in range(310)])
    res = resolve_fields(idx, "Erp.BO.WidgetSvc", "Widget", "")
    assert res["capped"] is True
    assert res["total_columns"] == 312
    assert 0 < len(res["fields"]) < 312
    # MUST stay default_used: read.py substitutes rollup_cols only when this is
    # True. A cap masquerading as an explicit field list projects a group_by
    # column away and the whole rollup collapses into ONE NULL BUCKET stamped
    # complete=true.
    assert res["default_used"] is True


def test_the_cap_is_inert_below_the_threshold():
    """The median entity is 18 columns; capping it would be pure loss."""
    idx = _Idx([f"Col{i}" for i in range(_CAP_THRESHOLD)])
    res = resolve_fields(idx, "Erp.BO.WidgetSvc", "Widget", "")
    assert not res.get("capped")
    assert res["fields"] == []


def test_explicit_fields_are_never_capped():
    idx = _Idx([f"Col{i}" for i in range(312)])
    asked = ",".join(f"Col{i}" for i in range(25))
    res = resolve_fields(idx, "Erp.BO.WidgetSvc", "Widget", asked)
    assert len(res["fields"]) == 25
    assert not res.get("capped")


# --------------------------------------------------------------------------- #
# C2 — truncation must never read as complete
# --------------------------------------------------------------------------- #

def _settings():
    return types.SimpleNamespace(
        response_truncate_keep=40, response_max_bytes=700_000,
        response_stats_top_k=5)


def test_truncation_keeps_the_payload_self_consistent():
    result = {"records": [{"PartNum": f"P{i}"} for i in range(853)],
              "record_count": 853}
    out = truncate_and_summarize(
        result, records_key="records", actual_bytes=1_982_000,
        settings=_settings())
    assert out["record_count"] == 40 == len(out["records"])
    assert out["returned_record_count"] == 40
    assert out["original_record_count"] == 853
    assert out["rows_dropped_for_size"] == 813


def test_truncated_read_never_claims_a_complete_result():
    payload = json.dumps({
        "records": [{"PartNum": f"P{i}"} for i in range(40)],
        "record_count": 40, "truncated": True,
        "original_record_count": 853, "returned_record_count": 40,
    })
    out = json.loads(_augment(
        payload, {"service": "Erp.BO.PartSvc", "entity_set": "PartWhse"},
        limit=100, skip=0, cursor_ctx={"target": "PartWhse"}, paginated=True))
    assert "complete result" not in out["summary"]
    assert out["summary"].startswith("INCOMPLETE")
    assert "853" in out["summary"]
    # ...and must not tell the model to stop asking.
    assert "do NOT re-run" not in out["stop_hint"]


def test_truncation_no_longer_deletes_the_cursor():
    """40 >= 100 is False, so byte-truncation used to suppress next_cursor."""
    payload = json.dumps({
        "records": [{"PartNum": f"P{i}"} for i in range(40)],
        "record_count": 40, "truncated": True,
        "original_record_count": 100, "returned_record_count": 40,
    })
    out = json.loads(_augment(
        payload, {"service": "Erp.BO.PartSvc", "entity_set": "PartWhse"},
        limit=100, skip=0, cursor_ctx={"target": "PartWhse"}, paginated=True))
    assert out.get("next_cursor")


def test_an_ordinary_short_page_is_unchanged():
    """Guard on `truncated` keeps non-truncated paging byte-identical."""
    payload = json.dumps({"records": [{"P": 1}], "record_count": 1})
    out = json.loads(_augment(
        payload, {"service": "S", "entity_set": "E"},
        limit=100, skip=0, cursor_ctx={}, paginated=True))
    assert "next_cursor" not in out
    assert "complete result" in out["summary"]


# --------------------------------------------------------------------------- #
# C3 — stop teaching the model a parameter that does not exist
# --------------------------------------------------------------------------- #

def test_size_guidance_names_fields_not_select():
    """The server's own oversize note told the model to pass `select=` — an
    argument epicor_read then silently discarded, so the read fell back to the
    same default set and was oversized again. A closed loop, and the causal
    source of the 168 dropped `select` arguments."""
    import epicor_mcp.response.formatter as fmt
    src = open(fmt.__file__).read()
    guidance = [fmt._OVERSIZE_GUIDANCE]
    for text in guidance:
        assert 'select="' not in text
        assert 'fields="' in text
    # No truncation/oversize message may advertise the removed earlier tool.
    assert 'select="col1,col2,..."' not in src

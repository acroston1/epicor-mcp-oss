"""Unit tests for fail-soft resolution in epicor_read.

The core behavior change: a projection (`fields`) or an ambiguous `target` no
longer HARD-ERRORS — the read returns best-effort DATA plus an `assumptions`
note, so the model doesn't burn a round-trip correcting a name. Covers the
confident-column auto-corrector and the `_augment` assumptions surfacing.
"""

from __future__ import annotations

import json

import pytest

from tests.test_partviews import configured_plants

from epicor_mcp.tools.read import (
    _augment,
    _correct_column,
    _resolve_site_filter,
    _rewrite_filter_columns,
)


# --------------------------------------------------------------------------- #
# _resolve_site_filter — place name/abbrev/wrong-column -> real Plant code
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("where,expect_frag,expect_note", [
    ("Plant = 'North Works'", "Plant = '101'", True),          # name -> code
    ("Plant eq 'No'", "Plant eq '101'", True),            # abbrev -> code
    ("SiteID = '303'", "Plant = '303'", True),             # wrong column -> Plant
    ("SiteID = 'North Works'", "Plant = '101'", True),         # wrong col + name
    ("Plant = '101'", "Plant = '101'", False),             # valid code -> untouched
    ("PartNum = 'WIDGET-1'", "PartNum = 'WIDGET-1'", False),     # unrelated -> untouched
])
def test_resolve_site_filter(configured_plants, where, expect_frag, expect_note):
    out, notes = _resolve_site_filter(where)
    assert expect_frag in out
    assert bool(notes) is expect_note


# --------------------------------------------------------------------------- #
# _search_service_for — heavy BO read -> fast <Entity>SearchSvc twin
# --------------------------------------------------------------------------- #

class _FakeIdx:
    def __init__(self, m):
        self.m = m

    def get_entity_sets(self, svc):
        return self.m.get(svc, [])


def test_search_service_for():
    from epicor_mcp.tools.read import _SEARCH_SVC_CACHE, _search_service_for
    _SEARCH_SVC_CACHE.clear()
    idx = _FakeIdx({"Erp.BO.JobOperSearchSvc": ["JobOper"],
                    "Erp.BO.JobMtlSearchSvc": ["JobMtl"]})
    assert _search_service_for(idx, "JobOper") == "Erp.BO.JobOperSearchSvc"
    _SEARCH_SVC_CACHE.clear()
    assert _search_service_for(idx, "JobMtl") == "Erp.BO.JobMtlSearchSvc"
    _SEARCH_SVC_CACHE.clear()
    # No twin service in the index -> empty (keep the original routing).
    assert _search_service_for(idx, "OrderHed") == ""
    _SEARCH_SVC_CACHE.clear()
    assert _search_service_for(idx, "") == ""


def test_resolve_site_filter_does_not_touch_wrong_numeric_code():
    # The model already mis-translated North Works->303; we can't know that. A valid
    # code is left as-is (only the description/instructions prevent that).
    out, notes = _resolve_site_filter("Plant = '303'")
    assert out == "Plant = '303'" and not notes

_COLS = ["PartNum", "PartDescription", "OrderQty", "CustNum", "OpCode",
         "ResourceGrpID", "DueDate"]


# --------------------------------------------------------------------------- #
# _correct_column — auto-correct only when high-confidence, else None (drop)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad,expected", [
    ("Description", "PartDescription"),        # sub-string
    ("ResourceGroupID", "ResourceGrpID"),      # difflib ratio
    ("partnum", "PartNum"),                     # case variant
    ("OrderDtl_PartNum", "PartNum"),            # BAQ alias — strip Table_
    ("OrderHed_CustNum", "CustNum"),            # BAQ alias
    ("Widget", None),                           # no match -> drop
    ("Customer", None),                         # not a confident hit -> drop
])
def test_correct_column(bad, expected):
    assert _correct_column(bad, _COLS) == expected


def test_correct_column_empty_cols():
    assert _correct_column("PartNum", []) is None


def test_rewrite_filter_columns():
    flt = "OrderDtl_PartNum eq 'X' and OrderHed_CustNum eq 5"
    out = _rewrite_filter_columns(
        flt, {"OrderDtl_PartNum": "PartNum", "OrderHed_CustNum": "CustNum"})
    assert out == "PartNum eq 'X' and CustNum eq 5"


# --------------------------------------------------------------------------- #
# _augment — assumptions ride back and lead the summary
# --------------------------------------------------------------------------- #

def _raw(rows):
    return json.dumps({"records": rows, "record_count": len(rows)})


def test_augment_surfaces_assumptions():
    resolved = {"service": "Erp.BO.PartSvc", "entity_set": "Part",
                "fields": ["PartNum"]}
    soft = {
        "target_assumed": {"used": "Erp.BO.PartSvc/Part",
                           "for_phrase": "parts", "alternatives": []},
        "fields_corrected": {"Description": "PartDescription"},
        "fields_dropped": ["Customer"],
    }
    out = _augment(_raw([{"PartNum": "WIDGET-100"}]), resolved,
                   limit=100, skip=0, cursor_ctx={}, paginated=True,
                   assumptions=soft)
    payload = json.loads(out)
    # data still returned
    assert payload["row_count"] == 1
    assert payload["records"] == [{"PartNum": "WIDGET-100"}]
    # assumptions rode back AND lead the summary so the model reports them
    assert payload["assumptions"] == soft
    assert payload["summary"].startswith("ASSUMPTIONS")
    assert "PartDescription" in payload["summary"]
    assert "Customer" in payload["summary"]


def test_augment_no_assumptions_is_clean():
    resolved = {"service": "Erp.BO.PartSvc", "entity_set": "Part",
                "fields": ["PartNum"]}
    out = _augment(_raw([{"PartNum": "X"}]), resolved,
                   limit=100, skip=0, cursor_ctx={}, paginated=True,
                   assumptions=None)
    payload = json.loads(out)
    assert "assumptions" not in payload
    assert not payload["summary"].startswith("ASSUMPTIONS")


# =========================================================================== #
# Adversarial-review regressions. Each one is a SILENT WRONG
# ANSWER the engine used to produce: a confident value with no error attached.
# =========================================================================== #

def test_correct_column_refuses_an_ambiguous_substring():
    """A short generic name must NOT be rewritten to an arbitrary column.

    `_correct_column` returned the FIRST valid column containing the bad name,
    in index (alphabetical) order, BEFORE difflib was consulted. Against the
    real Erp.BO.PartSvc/Part column list that meant Class -> AttrClassID
    (real: ClassID), Weight -> CNWeight (real: NetWeight), Cost -> CostMethod
    (a string code, not a number). Each produced 0 rows stamped "complete
    result" — a confidently wrong empty answer with no error.
    """
    from epicor_mcp.tools.read import _correct_column

    cols = ["AttrClassID", "ClassID", "CNWeight", "NetWeight",
            "CostMethod", "StdMaterialCost", "PartNum"]
    # Several columns contain "class"/"weight" -> fall through to difflib,
    # which scores the real answer far above the incidental match.
    assert _correct_column("Class", cols) == "ClassID"
    # "Weight" scores IDENTICALLY against CNWeight and NetWeight, so there is
    # no confident fix at all — the honest outcome is an unknown_columns
    # envelope listing both, not an arbitrary pick by list order.
    assert _correct_column("Weight", cols) is None
    # "Cost" matches CostMethod AND StdMaterialCost; neither is confident.
    assert _correct_column("Cost", cols) is None


def test_correct_column_keeps_the_unique_substring_rule():
    """The rule this exists for — a single unambiguous match — still fires."""
    from epicor_mcp.tools.read import _correct_column

    cols = ["PartNum", "PartDescription", "ClassID"]
    assert _correct_column("Description", cols) == "PartDescription"
    assert _correct_column("OrderDtl_PartNum", cols) == "PartNum"


def test_boolean_correction_vetoed_for_eq_against_a_number():
    """The veto must key on the LITERAL, not the operator.

    Vetoing only gt/lt/ge/le left the identical dead end one keystroke away:
    `OnHandQty eq 0` -> `HasOnHandQty eq 0` compares an Edm.Boolean to an
    integer, the same Epicor type error the veto was written to kill.
    """
    from epicor_mcp.tools.read import _correction_type_ok

    types_ = {"HasOnHandQty": "Edm.Boolean"}
    assert not _correction_type_ok(
        "OnHandQty", "HasOnHandQty", "OnHandQty eq 0", types_)
    assert not _correction_type_ok(
        "OnHandQty", "HasOnHandQty", "OnHandQty gt 0", types_)
    # A genuine boolean comparison is still legitimate.
    assert _correction_type_ok(
        "OnHandQty", "HasOnHandQty", "OnHandQty eq true", types_)
    # Non-boolean fixes are untouched.
    assert _correction_type_ok(
        "Description", "PartDescription", "Description eq 'X'",
        {"PartDescription": "Edm.String"})


def test_filter_column_rewrite_never_touches_a_quoted_literal():
    """A corrected column name must not be substituted INSIDE a string.

    The user asked for parts whose DESCRIPTION contains 'Weight'; the bare
    whole-word rewrite searched for 'CNWeight' instead and returned 0 rows,
    reported as a complete result.
    """
    from epicor_mcp.tools.read import _rewrite_filter_columns

    out = _rewrite_filter_columns(
        "contains(PartDescription,'Weight') and Weight gt 5",
        {"Weight": "CNWeight"})
    assert out == "contains(PartDescription,'Weight') and CNWeight gt 5"


def test_site_filter_preserves_a_correct_plant1_column(configured_plants):
    """`Plant1` is the REAL code column on Erp.BO.PlantSvc/Plants.

    repl() already classified it as correct (`col.lower() not in ('plant',
    'plant1')`) yet every return path emitted the literal 'Plant' — silently,
    with no entry in `assumptions` — turning a valid filter into
    unknown_columns on the one entity where `Plant` does not exist.
    """
    from epicor_mcp.tools.read import _resolve_site_filter

    assert _resolve_site_filter("Plant1 = '101'") == ("Plant1 = '101'", {})
    # The wrong names are still corrected, and still reported.
    where, notes = _resolve_site_filter("SiteID = 'North Works'")
    assert where == "Plant = '101'"
    assert notes


def test_aggregated_partial_scan_is_never_called_complete():
    """A rollup over a truncated scan must not be presented as a total.

    `_finish_paged_aggregation` sets complete=false + a scan-ceiling note when
    the 20-page ceiling is hit; `_augment` used to overwrite the headline with
    "This is the complete result for this query", and the summary is emitted
    FIRST in the ordered payload — so the model reported a materially
    understated total as final.
    """
    import json as _json

    from epicor_mcp.tools.read import _augment

    raw = _json.dumps({
        "records": [{"InventoryValue": 123.0}],
        "record_count": 1,
        "complete": False,
        "note": "Scanned 20000 rows across 20 pages (scan ceiling); "
                "results may be partial",
    })
    out = _json.loads(_augment(
        raw, {"service": "Erp.BO.PartTranSvc", "entity_set": "PartTran"},
        limit=100, skip=0, cursor_ctx={}, paginated=False))

    assert out["summary"].startswith("INCOMPLETE")
    assert "complete result" not in out["summary"]
    assert "scan ceiling" in out["summary"]


@pytest.mark.parametrize("rows", [[], [{"PartNum": "ASSY-100", "MtlPartNum": "RAW-100"}]])
def test_partmtl_reports_the_observed_query_without_tenant_assumptions(rows):
    """Engineering methods may be populated in an operator's environment.

    A bounded PartMtl read describes exactly its rows. Neither an empty nor a
    populated result should invent a tenant-wide rule or redirect to jobs.
    """
    out = json.loads(_augment(
        _raw(rows), {"service": "Erp.BO.BomSearchSvc", "entity_set": "PartMtl"},
        limit=100, skip=0, cursor_ctx={}, paginated=True))

    assert out["records"] == rows
    assert out["row_count"] == len(rows)
    assert "complete result for this query" in out["summary"]
    assert "PartMtl" in out["summary"]
    assert "retry_with" not in out
    assert "NOT the answer" not in out["summary"]
    assert "does NOT answer" not in out["stop_hint"]


def test_a_genuinely_complete_read_still_says_so():
    """Fence: the honesty fixes must not make every result look partial."""
    import json as _json

    from epicor_mcp.tools.read import _augment

    raw = _json.dumps({"records": [{"PartNum": "X"}], "record_count": 1})
    out = _json.loads(_augment(
        raw, {"service": "Erp.BO.PartSvc", "entity_set": "Part"},
        limit=100, skip=0, cursor_ctx={}, paginated=True))
    assert "This is the complete result" in out["summary"]

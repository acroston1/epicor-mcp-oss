"""Regression coverage: test baq order and projection."""

from __future__ import annotations

import asyncio
import json

import pytest

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools import baq as baq_mod
from epicor_mcp.tools.baq import (
    _compose_baq_sql,
    _do_create,
    _project_baq_records,
    _resolve_order_terms,
    _split_sql_order_terms,
)
from tests.test_baq_aggregate import PODETAIL, POHEADER
from tests.test_baq_create_guard import resolved  # noqa: F401 — fixture


def _run(**kw):
    args = dict(session=None, rbac=None, client=None, baq_index=None,
                baq="X", tables="Erp.POHeader", fields="PONum", where="PONum = 5",
                limit=50)
    args.update(kw)
    return json.loads(asyncio.run(_do_create(**args)))


# --------------------------------------------------------------------------- #
# Defect 1 — a saved-but-unrunnable BAQ must not report success
# --------------------------------------------------------------------------- #

def test_saved_but_unrunnable_baq_reports_failure(resolved, monkeypatch):  # noqa: F811
    async def _ok(**kw):
        return {"success": True, "baq_id": "AUTO-X"}

    async def _boom(**kw):
        raise EpicorError(400, "bad request")

    monkeypatch.setattr(baq_mod, "create_baq", _ok)
    monkeypatch.setattr(baq_mod, "run_baq_impl", _boom)

    out = _run()
    assert out["success"] is False, "create_baq's success:True must not win"
    assert out["error"] == "baq_saved_but_run_failed"
    # The id survives so the caller can retry or delete rather than re-create.
    assert out["baq_id"] == "AUTO-X"
    assert out["run_error"]


def test_create_error_branch_also_reports_failure(resolved, monkeypatch):  # noqa: F811
    """Latent twin of the same dict-spread bug — harmless only because
    create_baq's error dicts happen to carry no `success` key today."""
    async def _err(**kw):
        return {"success": True, "error": "parse_failed"}

    monkeypatch.setattr(baq_mod, "create_baq", _err)
    out = _run()
    assert out["success"] is False


# --------------------------------------------------------------------------- #
# Defect 2 — an alias/measure sort must never reach the persisted SQL
# --------------------------------------------------------------------------- #

def test_alias_order_term_is_peeled_off_the_sql():
    """`[POCount] desc` parses and then 400s on every run of the saved BAQ."""
    safe, client = _split_sql_order_terms(
        [("[POHeader].[OrderDate]", "desc"), ("[POCount]", "desc")])
    assert safe == [("[POHeader].[OrderDate]", "desc")]
    assert client == [("[POCount]", "desc")]


def test_composed_sql_never_orders_by_a_bare_alias():
    aggs = [{"fn": "count", "alias": "POHeader", "real": "PONum",
             "out": "POCount"}]
    terms, err = _resolve_order_terms("POCount desc", [POHEADER], [], aggs, [])
    assert err is None
    sql_order, client_order = _split_sql_order_terms(terms)
    sql, _j = _compose_baq_sql([POHEADER], [("POHeader", "BuyerID")], "",
                               aggs, None, sql_order)
    assert "order by" not in sql, "a bare alias in order by kills the BAQ"
    assert client_order, "...so it must be sorted client-side instead"


def test_real_column_order_still_reaches_the_sql():
    """The one form that actually runs must not be collateral damage."""
    terms, err = _resolve_order_terms(
        "OrderDate desc", [POHEADER], [("POHeader", "PONum")], [], [])
    sql_order, client_order = _split_sql_order_terms(terms)
    assert client_order == []
    sql, _j = _compose_baq_sql([POHEADER], [("POHeader", "PONum")], "",
                               None, None, sql_order)
    assert sql.strip().endswith("order by [POHeader].[OrderDate] desc")


def test_multi_key_order_on_real_columns_is_preserved():
    terms, err = _resolve_order_terms(
        "OrderDate desc, PONum asc", [POHEADER],
        [("POHeader", "OrderDate"), ("POHeader", "PONum")], [], [])
    assert err is None
    sql, _j = _compose_baq_sql([POHEADER], [("POHeader", "PONum")], "",
                               None, None, terms)
    assert sql.strip().endswith(
        "order by [POHeader].[OrderDate] desc, [POHeader].[PONum] asc")


def test_client_sorted_create_is_honest_about_the_saved_baq(resolved, monkeypatch):  # noqa: F811
    """Both halves must be stated: these ROWS are sorted, the SAVED BAQ is not
    — a later action='run' returns them unordered."""
    async def _ok(**kw):
        return {"success": True, "baq_id": "AUTO-X"}

    async def _run_baq(**kw):
        # Multi-digit on purpose: a lexicographic key ranks 95 above 1000, so
        # these values fail unless the sort really is numeric.
        return {"records": [{"POCount": 95}, {"POCount": 1000},
                            {"POCount": 900}],
                "record_count": 3}

    monkeypatch.setattr(baq_mod, "create_baq", _ok)
    monkeypatch.setattr(baq_mod, "run_baq_impl", _run_baq)

    out = _run(fields="BuyerID, count(PONum) as POCount", order_by="POCount desc")
    assert [r["POCount"] for r in out["records"]] == [1000, 900, 95]
    # The note must land in a STRUCTURED place. Falling back to json.dumps(out)
    # let this pass even when the honesty note landed nowhere addressable.
    assert "order" in out.get("resolved", {}), out.get("resolved")
    note = out["resolved"]["order"]
    assert "client-side" in note
    assert "NOT" in note and "action='run'" in note


# --------------------------------------------------------------------------- #
# Item 4 — `select` must actually PRUNE, not merely be announced
# --------------------------------------------------------------------------- #

_ROWS = [{"DMRNum": 1, "JobNum": "J1", "PartNum": "P", "Qty": 3}]


def test_projection_prunes_and_counts_honestly():
    rows, notes = _project_baq_records(_ROWS, "DMRNum,JobNum")
    assert set(rows[0]) == {"DMRNum", "JobNum"}
    # The announced count must match what was actually shipped.
    assert "2 of 4" in notes["projected"]


def test_projection_is_case_insensitive_but_never_guesses():
    rows, notes = _project_baq_records(_ROWS, "dmrnum")
    assert set(rows[0]) == {"DMRNum"}


def test_projection_resolves_the_baq_alias_field_shape():
    rows, _n = _project_baq_records(
        [{"DMRHead_DMRNum": 1, "DMRHead_JobNum": "J1"}], "DMRNum")
    assert set(rows[0]) == {"DMRHead_DMRNum"}


def test_unknown_field_keeps_the_rows_and_names_the_real_columns():
    """The rows are already fetched and valid, and BAQ Alias_Field columns are
    unguessable — a terminal envelope would cost a round trip for data in hand."""
    rows, notes = _project_baq_records(_ROWS, "DMRNum,Nonexistent")
    assert set(rows[0]) == {"DMRNum"}
    assert "Nonexistent" in notes["unknown_fields"]
    assert "Qty" in notes["valid_columns"]


def test_all_fields_unknown_shows_everything_and_says_so():
    rows, notes = _project_baq_records(_ROWS, "Nope,AlsoNope")
    assert rows is _ROWS
    assert "NO" in notes["unknown_fields"]
    assert "projected" not in notes


def test_blank_fields_is_a_no_op():
    rows, notes = _project_baq_records(_ROWS, "")
    assert rows is _ROWS and notes == {}


def test_ambiguous_case_insensitive_hit_is_not_guessed():
    """Picking one of two same-name-different-case columns is the silent wrong
    guess this workstream exists to kill."""
    rows, notes = _project_baq_records([{"Qty": 1, "qty": 2}], "QTY")
    assert "unknown_fields" in notes

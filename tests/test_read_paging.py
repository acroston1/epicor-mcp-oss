"""Unit tests for epicor_read paging honesty.

Covers the `top` alias for `limit` (silently-dropped top=1000 previously fell
back to a 20-row default page that read as the complete answer), the raised
default page size, and the _augment summary/stop_hint contract: a full page
must say INCOMPLETE loudly, a partial page may claim completeness, and a
trimmed join must not claim completeness. No live Epicor needed.
"""

from __future__ import annotations

import json

from epicor_mcp.tools.read import (
    _DEFAULT_LIMIT,
    _augment,
    _effective_limit,
)


# --------------------------------------------------------------------------- #
# _effective_limit — limit / top alias / cursor / default precedence
# --------------------------------------------------------------------------- #

def test_explicit_limit_wins():
    assert _effective_limit(50, 1000) == 50


def test_top_alias_used_when_limit_unset():
    assert _effective_limit(0, 1000) == 1000


def test_cursor_limit_used_when_neither_set():
    assert _effective_limit(0, 0, cursor_limit=200) == 200


def test_default_when_nothing_set():
    assert _effective_limit(0, 0) == _DEFAULT_LIMIT


def test_default_is_not_the_old_conservative_20():
    assert _DEFAULT_LIMIT >= 100


# --------------------------------------------------------------------------- #
# _augment — full-page vs partial-page honesty
# --------------------------------------------------------------------------- #

_RESOLVED = {"service": "Erp.BO.PartSvc", "entity_set": "Parts"}


def _rows(n):
    return [{"PartNum": f"P{i}"} for i in range(n)]


def _augmented(records, *, limit, paginated=True, trim_to=None):
    raw = json.dumps({"records": records, "record_count": len(records)})
    return json.loads(_augment(
        raw, dict(_RESOLVED), limit=limit, skip=0,
        cursor_ctx={"target": "Part", "limit": limit},
        paginated=paginated, trim_to=trim_to,
    ))


def test_full_page_says_incomplete_and_offers_cursor():
    out = _augmented(_rows(20), limit=20)
    assert "next_cursor" in out
    assert out["summary"].startswith("INCOMPLETE")
    assert "Do NOT present this page as the full list" in out["summary"]
    # stop_hint must instruct continued paging, not "you're done".
    assert "next_cursor" in out["stop_hint"]
    assert "answer the read" not in out["stop_hint"]


def test_partial_page_claims_complete_and_stops():
    out = _augmented(_rows(7), limit=20)
    assert "next_cursor" not in out
    assert "complete result" in out["summary"]
    assert "Present them to the user now" in out["stop_hint"]


def test_trimmed_join_never_claims_complete():
    # Join path: paginated=False, rows over trim_to get cut with limit_trim.
    out = _augmented(_rows(30), limit=10, paginated=False, trim_to=10)
    assert out["row_count"] == 10
    assert "limit_trim" in out
    assert out["summary"].startswith("INCOMPLETE")
    assert "complete result" not in out["summary"]


def test_error_payload_untouched_by_paging_summary():
    raw = json.dumps({"error": "unknown_columns", "records": []})
    out = json.loads(_augment(
        raw, dict(_RESOLVED), limit=20, skip=0, cursor_ctx={},
        paginated=True,
    ))
    assert "summary" not in out
    assert "next_cursor" not in out

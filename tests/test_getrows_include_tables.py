"""Opt-in sibling tables on the GetRows path (``include_tables``).

``_build_getrows_where`` hard-codes ``whereClause{X} = "1=0"`` for every
non-target table. That default is correct — it is what keeps an ordinary
single-table read small — but it makes every sibling child table structurally
unreachable, and an ``*Attch`` attachment table has NO other route (the plural
OData collection ``Erp.BO.APInvoiceSvc/APInvHedAttches`` 500s, the singular
segment 404s, and ``Ice.BO.AttachmentSvc/DownloadFile`` is 401 under the
read-only Access Scope).

These tests pin the contract of the opt-in:

* default path (no ``include_tables``) is BYTE-IDENTICAL to before,
* an opted-in sibling gets ``""`` and everything else still gets ``1=0``,
* EVERY DataSet table still gets a param — omit one and Epicor 400s with
  "Parameter whereClauseX is not found in the input object",
* an unknown table name surfaces an INV-1 envelope instead of silently
  no-opping (which would read as "there are no attachments").

Mock client only; no live Epicor.
"""

from __future__ import annotations

import asyncio
import json

from epicor_mcp.tools._engine import (
    _build_getrows_where,
    extract_getrows_tables,
    odata_to_sql,
    resolve_include_tables,
    run_getrows,
)

SVC = "Erp.BO.APInvoiceSvc"

# A miniature APInvoiceSvc DataSet. "APInvoices" is the plural OData collection
# — the index has no FIELDS for it, so it must never become a whereClause param.
_TABLES = ["APInvHed", "APInvHedAttch", "APInvDtl", "APInvExp", "APInvSched"]


class _Idx:
    def get_entity_sets(self, service):
        if service != SVC:
            return []
        return [*_TABLES, "APInvoices"]

    def get_fields(self, service, entity_set):
        if service != SVC or entity_set not in _TABLES:
            return []
        return [{"field_name": "Company", "field_type": "Edm.String"},
                {"field_name": "InvoiceNum", "field_type": "Edm.String"},
                {"field_name": "GroupID", "field_type": "Edm.String"}]

    def get_field_types(self, service, entity_set):
        return {f["field_name"]: f["field_type"]
                for f in self.get_fields(service, entity_set)}

    def find_field_owners(self, name, limit=6):
        return []


class _Client:
    def __init__(self, post_result=None):
        self._post = post_result
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, api_key, json_body=None):
        self.posts.append((url, dict(json_body or {})))
        return self._post if self._post is not None else {"returnObj": {}}


FILTER = "GroupID eq 'BATCH-100'"


# --- (a) default path: 1=0 everywhere but the target -----------------------

def test_default_body_is_unchanged():
    body, target = _build_getrows_where(_Idx(), SVC, "APInvHed", FILTER, "")

    assert target == "APInvHed"
    assert body == {
        "whereClauseAPInvHed": odata_to_sql(FILTER),
        "whereClauseAPInvHedAttch": "1=0",
        "whereClauseAPInvDtl": "1=0",
        "whereClauseAPInvExp": "1=0",
        "whereClauseAPInvSched": "1=0",
    }
    assert body["whereClauseAPInvHed"]  # the target really carries the filter


def test_plural_collection_never_becomes_a_where_clause():
    body, _ = _build_getrows_where(_Idx(), SVC, "APInvHed", FILTER, "")
    assert "whereClauseAPInvoices" not in body


# --- (b) opt-in: "" for the named siblings, 1=0 for the rest ---------------

def test_include_tables_opts_named_siblings_in():
    body, target = _build_getrows_where(
        _Idx(), SVC, "APInvHed", FILTER, "",
        include_tables={"APInvHedAttch", "APInvDtl"},
    )

    assert target == "APInvHed"
    assert body["whereClauseAPInvHed"] == odata_to_sql(FILTER)
    # "" (not "1=0") is the whole fix — that is what returns the child rows
    # scoped to the matched parents.
    assert body["whereClauseAPInvHedAttch"] == ""
    assert body["whereClauseAPInvDtl"] == ""
    # Everything NOT named keeps the small-response default.
    assert body["whereClauseAPInvExp"] == "1=0"
    assert body["whereClauseAPInvSched"] == "1=0"


def test_include_tables_is_case_and_plural_tolerant():
    resolved, err = resolve_include_tables(
        _Idx(), SVC, ["apinvhedattch", "APInvHedAttches"])
    assert err is None
    assert resolved == {"APInvHedAttch"}

    body, _ = _build_getrows_where(
        _Idx(), SVC, "APInvHed", FILTER, "", include_tables={"apinvhedattch"})
    assert body["whereClauseAPInvHedAttch"] == ""


def test_target_table_in_include_tables_keeps_the_filter():
    """Naming the target itself must not blank its whereClause."""
    body, _ = _build_getrows_where(
        _Idx(), SVC, "APInvHed", FILTER, "",
        include_tables={"APInvHed", "APInvHedAttch"},
    )
    assert body["whereClauseAPInvHed"] == odata_to_sql(FILTER)
    assert body["whereClauseAPInvHedAttch"] == ""


# --- (c) every DataSet table is still present ------------------------------

def test_every_dataset_table_still_gets_a_param():
    expected = {f"whereClause{t}" for t in _TABLES}
    plain, _ = _build_getrows_where(_Idx(), SVC, "APInvHed", FILTER, "")
    opted, _ = _build_getrows_where(
        _Idx(), SVC, "APInvHed", FILTER, "",
        include_tables={"APInvHedAttch"},
    )
    # Missing one => Epicor 400 "Parameter whereClauseX is not found in the
    # input object". The opt-in must not shrink the param set.
    assert set(plain) == expected
    assert set(opted) == expected


# --- (d) unknown name: INV-1 envelope, never a silent no-op ----------------

def test_unknown_include_table_returns_inv1_envelope():
    resolved, err = resolve_include_tables(
        _Idx(), SVC, ["APInvHedAttch", "APInvHeadAttach"])

    assert err is not None
    assert err["error"] == "unknown_include_tables"
    assert "APInvHeadAttach" in err["message"]
    # INV-1: hand back the CORRECT names, not just the rejected one.
    assert err["valid"]["tables"] == _TABLES
    assert err["valid"]["did_you_mean"]["APInvHeadAttach"]
    # Hard rejects echo what DID map.
    assert "APInvHedAttch" in err["retry_with"]["include_tables"]
    assert resolved == {"APInvHedAttch"}


def test_unindexed_service_says_so_instead_of_blaming_the_name():
    """An empty valid.tables would be a dead end — name the real cause."""
    resolved, err = resolve_include_tables(
        _Idx(), "Erp.BO.NotIndexedSvc", ["APInvHedAttch"])

    assert resolved == set()
    assert err["error"] == "include_tables_unavailable"
    assert "service index" in err["message"]


def test_run_getrows_rejects_unknown_include_table_before_posting():
    client = _Client()
    out = json.loads(asyncio.run(run_getrows(
        client, _Idx(), SVC, "APInvHed", "K",
        filter=FILTER, select="", orderby="", top=10,
        count_only=False, format="json",
        include_tables=["NoSuchTable"],
    )))

    assert out["error"] == "unknown_include_tables"
    assert out["valid"]["tables"] == _TABLES
    # The reject short-circuits: no wasted round trip against Epicor.
    assert client.posts == []


# --- run_getrows plumbing --------------------------------------------------

def _rows():
    return {"returnObj": {
        "APInvHed": [{"InvoiceNum": "INV-100", "GroupID": "BATCH-100"}],
        "APInvHedAttch": [{"InvoiceNum": "INV-100", "XFileRefNum": 91,
                           "FileName": r"F:\Accounting-Example\AP\a.pdf"}],
        "APInvDtl": [{"InvoiceNum": "INV-100"}],
    }}


def test_run_getrows_default_request_and_response_unchanged():
    client = _Client(_rows())
    out = json.loads(asyncio.run(run_getrows(
        client, _Idx(), SVC, "APInvHed", "K",
        filter=FILTER, select="", orderby="", top=10,
        count_only=False, format="json")))

    url, body = client.posts[0]
    assert url == f"{SVC}/GetRows"
    assert body == {
        "pageSize": 10, "absolutePage": 1,
        "whereClauseAPInvHed": odata_to_sql(FILTER),
        "whereClauseAPInvHedAttch": "1=0",
        "whereClauseAPInvDtl": "1=0",
        "whereClauseAPInvExp": "1=0",
        "whereClauseAPInvSched": "1=0",
    }
    # No opt-in => no new key in the response.
    assert "related_tables" not in out
    assert out["record_count"] == 1


def test_run_getrows_returns_the_opted_in_sibling_rows():
    client = _Client(_rows())
    out = json.loads(asyncio.run(run_getrows(
        client, _Idx(), SVC, "APInvHed", "K",
        filter=FILTER, select="", orderby="", top=10,
        count_only=False, format="json",
        include_tables={"APInvHedAttch"},
    )))

    _, body = client.posts[0]
    assert body["whereClauseAPInvHedAttch"] == ""
    assert body["whereClauseAPInvDtl"] == "1=0"

    # Fetching the sibling and then throwing it away in extraction would make
    # the whole opt-in pointless.
    attch = out["related_tables"]["APInvHedAttch"]
    assert attch[0]["FileName"].endswith("a.pdf")
    # The target table's own rows are untouched by the opt-in.
    assert out["records"][0]["InvoiceNum"] == "INV-100"
    # A table that was NOT opted in never rides along.
    assert "APInvDtl" not in out["related_tables"]


# --- extraction helper -----------------------------------------------------

def test_extract_getrows_tables_never_guesses():
    resp = _rows()
    assert extract_getrows_tables(resp, set()) == {}
    # Present but empty stays [] (an honest "no attachments"), and a table the
    # response never carried is simply absent — no "first non-empty list"
    # fallback, which for a named sibling is wrong by construction.
    resp["returnObj"]["APInvHedAttch"] = []
    got = extract_getrows_tables(resp, {"APInvHedAttch", "APInvMsc"})
    assert got == {"APInvHedAttch": []}

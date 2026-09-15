"""INV-1 on the execution path: an EpicorError never loses its message.

`run_odata` makes its client call with no try/except, and the only
`except EpicorError` in read.py handled three cases and re-raised the rest —
straight into the function-wide blanket `except Exception`, which returned a
FIXED generic string and discarded `exc.message`. That produced an asymmetry:
the same target + same `where` returned a bare `read_failed` on the plain path
and the real "incompatible types" text on the paged path.

Also covers the type-guard on `_correct_column` (OnHandQty -> HasOnHandQty, a
boolean) that manufactured an un-executable GetRows whereClause.
"""

from __future__ import annotations

import json

import pytest

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools._resolve import epicor_error_envelope

APOLOGY = ("We apologize, but an unexpected internal problem occurred. "
           "Correlation ID: 6f1c2a30-1111-4b8e-9a55-abcdef012345")


def _env(msg, status=400, **kw):
    return epicor_error_envelope(
        EpicorError(status, msg),
        service="Erp.BO.PartSvc", entity_set="Part", **kw)


def test_unknown_property_error_serves_columns():
    env = _env("Could not find a property named 'AvgCost' on type "
               "'Erp.PartWhseFullSearchListItem'",
               valid_columns=["PartNum", "AvgCost2", "PartDescription"])
    assert env["error"] == "unknown_columns"
    assert "AvgCost" in env["valid"]["did_you_mean"]
    assert env["retry_with"]
    assert "Could not find a property named 'AvgCost'" in env["detail"]["message"]


def test_datetime_type_mismatch_envelope():
    env = _env("A binary operator with incompatible types was detected. Found "
               "operand types 'Edm.DateTimeOffset' and 'Edm.String' for "
               "operator kind 'GreaterThanOrEqual'",
               where="TranDate >= '2025-07-20'")
    assert env["error"] == "filter_type_mismatch"
    assert "2025-07-20T00:00:00Z" in env["retry_with"]["where"]
    assert "'2025-07-20'" not in env["retry_with"]["where"]


def test_generic_apology_offers_alternatives():
    env = _env(APOLOGY, status=500,
               alternatives=["Erp.BO.PartWhseFullSearchSvc/PartWhseSearch"])
    assert env["error"] == "upstream_error"
    assert env["valid"]["alternatives"]


def test_filter_syntax_error_echoes_attempted_filter():
    env = _env("Syntax error at position 11 in 'UnitPrice * 2 gt 100'",
               odata_filter="UnitPrice * 2 gt 100",
               where="UnitPrice * 2 > 100")
    assert env["error"] == "filter_rejected"
    assert env["valid"]["attempted_filter"] == "UnitPrice * 2 gt 100"
    # Must NOT claim arithmetic is unsupported — mul/div/add/sub ARE supported.
    assert "not supported" not in env["message"].lower()


@pytest.mark.parametrize("msg", [
    "Could not find a property named 'X' on type 'Y'",
    "A binary operator with incompatible types was detected. Found operand "
    "types 'Edm.DateTimeOffset' and 'Edm.String'",
    APOLOGY,
    "Syntax error at position 12",
    "something entirely unclassifiable",
])
def test_every_envelope_carries_detail(msg):
    """The INV-1 regression guard: no bare reason code, ever."""
    env = _env(msg)
    assert set(env) >= {"error", "message", "detail"}
    assert env["detail"]["message"]
    assert env["message"]


def test_detail_omits_exc_details_and_clips():
    exc = EpicorError(500, "boom " + "x" * 5000,
                      {"Authorization": "Bearer SECRET",
                       "x-api-key": "KEYVALUE"})
    blob = json.dumps(epicor_error_envelope(
        exc, service="S", entity_set="E"))
    assert "SECRET" not in blob and "KEYVALUE" not in blob
    assert len(json.loads(blob)["detail"]["message"]) <= 600


def test_correlation_id_not_in_headline_message():
    env = _env(APOLOGY, status=500)
    assert "6f1c2a30" not in env["message"]
    assert "6f1c2a30" in env["detail"]["message"]


def test_attempted_path_is_honest():
    """A heavy service goes straight to GetRows — never claim OData was tried."""
    env = _env("boom", status=500, attempted=("getrows",))
    assert "Neither OData nor GetRows worked" not in env["message"]
    assert "getrows" in env["message"]


# --------------------------------------------------------------------------- #
# _correction_type_ok — the silent OnHandQty -> HasOnHandQty rewrite
# --------------------------------------------------------------------------- #

TYPES = {"HasOnHandQty": "Edm.Boolean", "PartDescription": "Edm.String"}


def test_relational_op_against_boolean_is_vetoed():
    from epicor_mcp.tools.read import _correction_type_ok
    assert not _correction_type_ok(
        "OnHandQty", "HasOnHandQty", "OnHandQty gt 0", TYPES)


@pytest.mark.parametrize("flt", ["HasOnHandQty eq true", "OnHandQty eq true"])
def test_equality_against_boolean_still_allowed(flt):
    """The guard is narrow: only gt/lt/ge/le against Edm.Boolean is nonsense."""
    from epicor_mcp.tools.read import _correction_type_ok
    assert _correction_type_ok("OnHandQty", "HasOnHandQty", flt, TYPES)


def test_description_correction_survives_guard():
    """Regression guard on the load-bearing substring rule."""
    from epicor_mcp.tools.read import _correct_column, _correction_type_ok
    fix = _correct_column("Description", ["PartNum", "PartDescription"])
    assert fix == "PartDescription"
    assert _correction_type_ok(
        "Description", fix, "Description eq 'WIDGET'", TYPES)


def test_part_has_no_searchsvc_twin():
    """Pins the DISPROVEN hypothesis so nobody 'fixes' the fast-path gate."""
    from epicor_mcp.tools.read import _SEARCH_SVC_CACHE, _search_service_for

    class _Idx:
        def get_entity_sets(self, svc):
            return {"Erp.BO.JobOperSearchSvc": ["JobOper"]}.get(svc, [])

    _SEARCH_SVC_CACHE.clear()
    assert _search_service_for(_Idx(), "Part") == ""
    # The existing fast path still swaps — Changes 1-4 didn't disturb it.
    assert _search_service_for(_Idx(), "JobOper") == "Erp.BO.JobOperSearchSvc"
    _SEARCH_SVC_CACHE.clear()


# --------------------------------------------------------------------------- #
# column_help — "not here" is only half the answer
# --------------------------------------------------------------------------- #

def test_unknown_column_envelope_names_owning_entity():
    from epicor_mcp.tools._resolve import column_help

    class _Idx:
        def find_field_owners(self, name, limit=6):
            assert name == "OnHandQty"
            return [
                {"service_id": "Erp.BO.PartWhseFullSearchSvc",
                 "entity_set_name": "PartWhseSearch", "field_type": "Edm.Double"},
                {"service_id": "Erp.BO.PartSvc",
                 "entity_set_name": "PartWhse", "field_type": "Edm.Double"},
            ]

    got = column_help("Erp.BO.PartSvc", "Part",
                      ["PartNum", "HasOnHandQty"], ["OnHandQty"], index=_Idx())
    lives = got["column_lives_on"]["OnHandQty"]
    assert lives[0] == "Erp.BO.PartWhseFullSearchSvc/PartWhseSearch"
    assert "Erp.BO.PartSvc/PartWhse" in lives


def test_column_help_without_index_is_unchanged():
    """The kwarg is optional — non-read callers are unaffected."""
    from epicor_mcp.tools._resolve import column_help
    got = column_help("S", "E", ["PartNum"], ["Nope"])
    assert "column_lives_on" not in got
    assert set(got) == {"columns", "did_you_mean", "total_columns"}


def test_find_field_owners_ranking_prefers_searchsvc():
    from epicor_mcp.index.service_index import ServiceIndex
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fields (service_id TEXT, entity_set_name TEXT, "
                 "field_name TEXT, field_type TEXT, nullable INT, description TEXT)")
    conn.executemany(
        "INSERT INTO fields VALUES (?,?,?,?,0,'')",
        [("Erp.BO.PartSvc", "PartWhse", "OnHandQty", "Edm.Double"),
         ("Erp.BO.PartWhseSearchSvc", "PartWhseSearch", "OnHandQty", "Edm.Double")])
    conn.commit()

    idx = ServiceIndex.__new__(ServiceIndex)
    idx._conn = conn
    # Do NOT stub `_dict_rows`: replacing it with a local lambda meant the
    # production row-mapping helper never executed, so only the SQL string and
    # the sort key were under test. The real one runs against the real cursor.

    owners = idx.find_field_owners("onhandqty")  # case-insensitive
    assert owners[0]["service_id"] == "Erp.BO.PartWhseSearchSvc"
    assert len(idx.find_field_owners("OnHandQty", limit=1)) == 1


# =========================================================================== #
# INV-1 uniformity: BOTH callers of epicor_error_envelope must serve schema.
# =========================================================================== #

def test_getrows_unknown_column_envelope_serves_the_real_columns():
    """The earlier rejected-names-only bug, reintroduced on the GetRows path only.

    run_getrows called epicor_error_envelope WITHOUT valid_columns, so an
    'unknown_columns' classification shipped valid.columns=[],
    did_you_mean={col: []} and a factually false total_columns=0 — while the
    message told the model to "see valid.did_you_mean / valid.columns". The
    read.py caller already passed them, so the shape was non-uniform between
    the two callers of the same classifier.
    """
    import asyncio
    import json as _json
    import types as _types

    from epicor_mcp.epicor_client.error_handler import EpicorError
    from epicor_mcp.tools._engine import run_getrows

    fields = [
        {"field_name": "PartNum", "field_type": "Edm.String"},
        {"field_name": "PartDescription", "field_type": "Edm.String"},
        {"field_name": "HasOnHandQty", "field_type": "Edm.Boolean"},
    ]

    class _Idx2:
        def get_fields(self, s, e):
            return fields

        def get_field_types(self, s, e):
            return {f["field_name"]: f["field_type"] for f in fields}

        def get_entity_sets(self, s):
            return ["Part"] if s == "Erp.BO.PartSvc" else []

        def find_field_owners(self, name, limit=6):
            return []

    exc = EpicorError(
        400, "Could not find a property named 'OnHandQty' on type 'Erp.Part'")

    class _C:
        async def post(self, url, api_key, json_body=None):
            raise exc

    out = _json.loads(asyncio.run(run_getrows(
        _C(), _Idx2(), "Erp.BO.PartSvc", "Part", "K",
        filter="OnHandQty gt 0", select="", orderby="", top=10,
        count_only=False, format="json")))

    assert out["error"] == "unknown_columns"
    valid = out["valid"]
    # The message promises these; they must actually carry content.
    assert valid["columns"], "valid.columns was empty — the message lies"
    assert "PartNum" in valid["columns"]
    assert valid["total_columns"] == len(fields)
    assert valid["total_columns"] != 0


# =========================================================================== #
# unknown_columns must say WHICH SIDE broke.
#
# A call often carries BOTH a `where` and `fields`, and an envelope that names
# the columns without naming the ARGUMENT misleads the model: a perfect
# `GroupID = 'BATCH-100'` filter plus six `fields`, two of which don't exist,
# comes back "unknown_columns"; the model concludes group filtering is
# impossible and falls back to a 20-call per-invoice loop.
# =========================================================================== #

import types  # noqa: E402

from epicor_mcp.tools import read as read_mod  # noqa: E402

APINVHED_FIELDS = [
    {"field_name": "InvoiceNum", "field_type": "Edm.String"},
    {"field_name": "VendorNum", "field_type": "Edm.Int32"},
    {"field_name": "GroupID", "field_type": "Edm.String"},
    {"field_name": "DocInvoiceAmt", "field_type": "Edm.Decimal"},
    {"field_name": "DocTaxAmt", "field_type": "Edm.Decimal"},
    {"field_name": "InvoiceDate", "field_type": "Edm.DateTimeOffset"},
]

AV_FIELDS = ("InvoiceNum,VendorNum,DocInvoiceAmt,DocMiscAmt,"
             "DocFreightAmt,DocTaxAmt")
AV_WHERE = "GroupID = 'BATCH-100'"


class _EIdx:
    def __init__(self, fields, entity_sets, hosts):
        self._fields, self._sets, self._hosts = fields, entity_sets, hosts

    def get_fields(self, service, entity_set):
        return self._fields.get((service, entity_set), [])

    def get_field_types(self, service, entity_set):
        return {r["field_name"]: r.get("field_type") or ""
                for r in self.get_fields(service, entity_set)}

    def get_entity_sets(self, service):
        return self._sets.get(service, [])

    def services_for_entity(self, entity_set):
        return self._hosts.get(entity_set.strip().lower(), [])

    def search_services(self, raw, limit=5):
        return []

    def find_field_owners(self, name, limit=6):
        return []


class _ERBAC:
    def check_access(self, user_id, service_id):
        return (True, "")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key="K")


class _EClient:
    def __init__(self):
        self.gets: list = []
        self.posts: list = []

    async def get(self, url, api_key, params=None):
        self.gets.append((url, dict(params or {})))
        return {"value": []}

    async def post(self, url, api_key, json_body=None):
        self.posts.append((url, dict(json_body or {})))
        return {"returnObj": {}}


class _ESrv:
    def __init__(self):
        self.fn = None

    def tool(self, **kwargs):
        def deco(fn):
            self.fn = fn
            return fn
        return deco


def _apinv_read(monkeypatch, **kw):
    """Drive the REGISTERED epicor_read against a stub APInvoiceSvc."""
    from epicor_mcp.tools.query import _DATE_COLS_CACHE
    from epicor_mcp.tools.read import _SEARCH_SVC_CACHE
    _DATE_COLS_CACHE.clear()
    _SEARCH_SVC_CACHE.clear()
    idx = _EIdx(
        fields={("Erp.BO.APInvoiceSvc", "APInvHed"): APINVHED_FIELDS},
        entity_sets={"Erp.BO.APInvoiceSvc": ["APInvHed", "APInvHeds"]},
        hosts={"apinvhed": [{"service_id": "Erp.BO.APInvoiceSvc",
                             "entity_set_name": "APInvHed"}]},
    )
    monkeypatch.setattr(read_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="tester"))
    srv = _ESrv()
    client = _EClient()
    read_mod.register(srv, idx, _ERBAC(), client)
    import asyncio
    return client, json.loads(asyncio.run(srv.fn(target="APInvHed", **kw)))


def test_valid_where_with_unknown_fields_blames_fields_and_clears_the_where(monkeypatch):
    """A valid `where` plus two unknown `fields`: blame `fields`, keep `where`."""
    client, out = _apinv_read(monkeypatch, where=AV_WHERE, fields=AV_FIELDS)

    assert out["error"] == "unknown_columns"
    # 1) WHICH SIDE, with the offending names.
    assert out["unknown_by_argument"] == {
        "fields": ["DocMiscAmt", "DocFreightAmt"]}
    # 2) The other side is explicitly reported CLEAN — the one sentence that
    #    that keeps the model from abandoning a valid filter.
    assert out["validated_clean"] == ["where"]
    assert "2 of 6 `fields`" in out["message"]
    assert "`where`" in out["message"] and "VALID" in out["message"]
    # 3) A runnable next call: the surviving good fields + the unchanged where.
    retry = out["retry_with"]
    assert retry["where"] == AV_WHERE
    survivors = [f.strip() for f in retry["fields"].split(",")]
    assert survivors == ["InvoiceNum", "VendorNum", "DocInvoiceAmt",
                         "DocTaxAmt"]
    # 4) INV-1 payload kept.
    assert out["valid"]["columns"]
    assert "DocMiscAmt" in out["valid"]["did_you_mean"]
    # 5) NOT fail-soft: the bad fields are refused, not silently dropped, and
    #    nothing reached the wire.
    assert "records" not in out
    assert client.gets == [] and client.posts == []


def test_bad_where_column_alone_reports_fields_clean(monkeypatch):
    """The mirror image: the projection is fine, the filter is not."""
    _client, out = _apinv_read(
        monkeypatch, where="Frobnicate = 'X'",
        fields="InvoiceNum,VendorNum")

    assert out["error"] == "unknown_columns"
    assert out["unknown_by_argument"] == {"where": ["Frobnicate"]}
    assert out["validated_clean"] == ["fields"]
    # The good projection rides back untouched, so the retry is the same call
    # with one filter column fixed.
    assert out["retry_with"]["fields"] == "InvoiceNum,VendorNum"


def test_both_sides_bad_claims_neither_is_clean(monkeypatch):
    """No half-truths: when both broke, nothing gets a clean bill."""
    _client, out = _apinv_read(
        monkeypatch, where="Frobnicate = 'X'", fields=AV_FIELDS)

    assert out["error"] == "unknown_columns"
    by = out["unknown_by_argument"]
    assert by["fields"] == ["DocMiscAmt", "DocFreightAmt"]
    assert by["where"] == ["Frobnicate"]
    assert "validated_clean" not in out
    assert "VALID" not in out["message"]


def test_a_correctable_where_typo_is_not_reported_as_broken(monkeypatch):
    """A name the corrector repairs a moment later is not a caller error.

    The BAQ-alias form is silently fixed on the read path, so listing it as an
    unknown column would send the model to "fix" a clause that already works.
    """
    _client, out = _apinv_read(
        monkeypatch, where="APInvHed_InvoiceNum = '123'", fields=AV_FIELDS)

    assert out["unknown_by_argument"] == {
        "fields": ["DocMiscAmt", "DocFreightAmt"]}
    assert out["validated_clean"] == ["where"]
    assert out["retry_with"]["where"] == "APInvHed_InvoiceNum = '123'"


def test_retry_fields_are_the_RESOLVED_names_not_the_raw_string(monkeypatch):
    """"Hard rejects echo what DID map" — built post-resolution, not from raw.

    `_argguard` shipped the raw-input version of this bug once already: a
    retry_with built from the caller's own text under a message promising the
    arguments that DID map.
    """
    _client, out = _apinv_read(
        monkeypatch, where=AV_WHERE,
        fields="invoicenum, DocMiscAmt, groupid")

    assert out["error"] == "unknown_columns"
    survivors = [f.strip() for f in out["retry_with"]["fields"].split(",")]
    assert survivors == ["InvoiceNum", "GroupID"], "raw casing was echoed back"


def test_order_by_failure_clears_both_where_and_fields(monkeypatch):
    """Uniform shape on the sort path too — the other two sides passed."""
    _client, out = _apinv_read(
        monkeypatch, where=AV_WHERE, fields="InvoiceNum,VendorNum",
        order_by="Frobnicate desc")

    assert out["error"] == "unknown_columns"
    assert out["unknown_by_argument"] == {"order_by": ["Frobnicate"]}
    assert set(out["validated_clean"]) == {"where", "fields"}
    assert out["retry_with"]["where"] == AV_WHERE


def test_rollup_sort_key_error_blames_order_by_alone(monkeypatch):
    """A rollup's group_by/aggregate legitimately NAME the raw column the sort
    key names, so attribution there would blame a perfectly valid spec."""
    _client, out = _apinv_read(
        monkeypatch, where=AV_WHERE, group_by="VendorNum",
        aggregate="sum(DocInvoiceAmt) as Total", order_by="InvoiceDate desc")

    assert out["error"] == "unknown_columns"
    assert out["unknown_by_argument"] == {"order_by": ["InvoiceDate"]}
    assert "are not part of this rollup" in out["message"]
    # The rollup's own key space, and a runnable replacement sort.
    assert out["valid"]["columns"] == ["VendorNum", "Total"]
    assert out["retry_with"]["order_by"] == "Total desc"


def test_a_value_that_looks_like_a_column_never_blames_the_where():
    """Quoted literals are masked BEFORE tokenising.

    Without the mask, `where="GroupID = 'DocMiscAmt'"` would report the filter
    as the offending side and send the model to fix a perfectly good clause.
    """
    from epicor_mcp.tools._resolve import attribute_unknown_columns

    attr = attribute_unknown_columns(
        ["DocMiscAmt"],
        {"where": "GroupID = 'DocMiscAmt'", "fields": AV_FIELDS})
    assert attr["by_argument"] == {"fields": ["DocMiscAmt"]}
    assert attr["clean"] == ["where"]


def test_a_column_named_like_an_operator_still_gets_attributed():
    """`Total`, `Length`, `Date` and `Added` are REAL Epicor columns.

    The operator/function stop-list makes the "N of M" count honest; letting it
    decide ATTRIBUTION would drop those names from the token list and hand
    their argument a false clean bill — the exact failure mode this fix exists
    to remove.
    """
    from epicor_mcp.tools._resolve import attribute_unknown_columns

    attr = attribute_unknown_columns(
        ["Total"], {"where": "PartNum eq 'A'", "fields": "PartNum, Total"})
    assert attr["by_argument"] == {"fields": ["Total"]}
    assert attr["clean"] == ["where"]
    assert attr["totals"]["fields"] == 2


def test_an_unvalidated_argument_is_never_called_clean():
    """`clean` covers only what the caller actually passed for validation."""
    from epicor_mcp.tools._resolve import attribute_unknown_columns

    attr = attribute_unknown_columns(["Bogus"], {"where": "", "fields": "Bogus"})
    assert attr["clean"] == []          # a blank `where` is not a clean `where`
    assert attr["by_argument"] == {"fields": ["Bogus"]}


def test_unplaced_names_are_still_reported():
    """A name that matches no argument must not vanish from the message."""
    from epicor_mcp.tools._resolve import unknown_columns_envelope

    env = unknown_columns_envelope(
        target="Erp.BO.PartSvc/Part", unknown=["Injected"],
        arguments={"where": "PartNum eq 'A'"},
        valid={"columns": ["PartNum"]})
    assert env["unknown_by_argument"] == {"unplaced": ["Injected"]}
    assert "Injected" in env["message"]
    assert env["validated_clean"] == ["where"]


def test_epicor_reported_column_is_attributed_to_its_side():
    """The server-reported miss gets the same treatment as the pre-flight one."""
    env = _env("Could not find a property named 'DocMiscAmt' on type "
               "'Erp.APInvHed'",
               valid_columns=["InvoiceNum", "GroupID"],
               where=AV_WHERE, fields=AV_FIELDS)
    assert env["error"] == "unknown_columns"
    assert env["unknown_by_argument"] == {"fields": ["DocMiscAmt"]}
    assert env["validated_clean"] == ["where"]
    assert env["detail"]["message"]     # INV-1: the cause is still intact

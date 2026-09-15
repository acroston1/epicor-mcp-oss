"""Unit tests for BAQ execution-parameter support in ``epicor_baq``.

Covers the new `params` channel end to end at the unit level: query-string
merging in ``run_baq``, the ``baq_needs_params`` INV-1 envelope when a
mandatory parameter is missing (including AUTO- retry masking, where
the 400 hides behind a chained 404), graceful degradation when the definition
can't be read, and the saved-BAQ ``schema`` action. No live Epicor — the
client and saved-BAQ responses are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools import _baq_helpers
from epicor_mcp.tools.baq import (
    _coerce_baq_params,
    _describe_saved_baq,
    _do_run,
    _do_schema,
)

BAQ = "DEMO-CPM-PartUsage2"

_MANDATORY_MSG = (
    "Parameter 'ToDate' is configured as mandatory but no value is specified"
)

_GETBYID_OBJ = {
    "QueryParameter": [
        {"ParameterID": "FromDate", "ParameterType": "date", "SkipIfEmpty": False},
        {"ParameterID": "ToDate", "ParameterType": "date", "SkipIfEmpty": False},
    ],
    "QueryField": [
        {"FieldName": "PartNum", "Alias": "PartTran_PartNum"},
        {"FieldName": "SumTranQty", "Alias": "Calculated_SumTranQty"},
    ],
}

_ROWS = [{"PartTran_PartNum": "PART-100", "Calculated_SumTranQty": 12.0}]


class _FakeRBAC:
    def check_baq_access(self, user_id):
        return types.SimpleNamespace(allowed=True, api_key="BAQKEY", message="")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key="SVCKEY")


class _FakeClient:
    """Mimics live BaqSvc: parameterized BAQ 400s without its parameters, and
    the AUTO- spelling of a real id 404s (which is what masks the 400)."""

    def __init__(self, getbyid_obj=_GETBYID_OBJ, getbyid_error=None):
        self._getbyid_obj = getbyid_obj
        self._getbyid_error = getbyid_error
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    async def get(self, url, api_key, params=None):
        params = params or {}
        self.gets.append((url, dict(params)))
        if "AUTO-" in url:
            raise EpicorError(status_code=404,
                              message=f"Dynamic query is not found AUTO-{BAQ}")
        if "FromDate" in params and "ToDate" in params:
            return {"value": _ROWS}
        raise EpicorError(status_code=400, message=_MANDATORY_MSG)

    async def post(self, url, api_key, json_body=None):
        self.posts.append((url, json_body or {}))
        if self._getbyid_error:
            raise self._getbyid_error
        return {"returnObj": self._getbyid_obj}


_SESSION = types.SimpleNamespace(user_id="tester")


def _run(coro):
    return asyncio.run(coro)


def _do_run_kwargs(**over):
    kw = dict(session=_SESSION, rbac=_FakeRBAC(), client=_FakeClient(),
              baq=BAQ, where="", limit=50, cursor="", params=None)
    kw.update(over)
    return kw


# --------------------------------------------------------------------------- #
# params plumbing (pure-ish)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ({"FromDate": "2025-01-01"}, {"FromDate": "2025-01-01"}),
    ('{"ToDate": "2026-07-14"}', {"ToDate": "2026-07-14"}),   # JSON-string form
    ("not json", {}),
    ("", {}),
    (None, {}),
])
def test_coerce_baq_params(raw, expected):
    assert _coerce_baq_params(raw) == expected


def test_run_baq_merges_params_into_query_string():
    client = _FakeClient()
    result = _run(_baq_helpers.run_baq(
        session=_SESSION, rbac=_FakeRBAC(), client=client, baq_id=BAQ,
        top=5, baq_params={"FromDate": "2025-01-01", "ToDate": "2026-07-14",
                           "$top": 999,       # $-names are NOT parameters
                           "Empty": None}))
    assert result["records"] == _ROWS
    (url, sent), = client.gets
    assert url.endswith(f"BaqSvc/{BAQ}/Data")
    assert sent["FromDate"] == "2025-01-01"
    assert sent["ToDate"] == "2026-07-14"
    assert sent["$top"] == 5                   # params can't clobber $top
    assert sent["Empty"] == ""                 # None -> empty value, still sent


# --------------------------------------------------------------------------- #
# _do_run — the tool-level behavior
# --------------------------------------------------------------------------- #

def test_run_with_params_succeeds():
    client = _FakeClient()
    out = json.loads(_run(_do_run(**_do_run_kwargs(
        client=client,
        params={"FromDate": "2025-01-01", "ToDate": "2026-07-14"}))))
    assert "error" not in out
    assert out["records"] == _ROWS


def test_run_with_params_as_json_string_succeeds():
    out = json.loads(_run(_do_run(**_do_run_kwargs(
        params='{"FromDate": "2025-01-01", "ToDate": "2026-07-14"}'))))
    assert "error" not in out


def test_run_without_params_serves_the_parameter_list():
    # Epicor's 400 is masked by the AUTO- retry's 404; the envelope
    # must still be baq_needs_params (not baq_not_found), with the FULL
    # parameter list and a copyable retry template.
    client = _FakeClient()
    out = json.loads(_run(_do_run(**_do_run_kwargs(client=client))))
    assert out["error"] == "baq_needs_params"
    assert "FromDate" in out["message"] and "ToDate" in out["message"]
    names = [p["name"] for p in out["valid"]["parameters"]]
    assert names == ["FromDate", "ToDate"]
    assert all(p["mandatory"] for p in out["valid"]["parameters"])
    assert out["retry_with"]["params"] == {"FromDate": "<date>",
                                           "ToDate": "<date>"}
    # The message must teach the params-vs-`where` channel distinction.
    assert "NOT in `where`" in out["message"]


def test_run_param_error_with_where_still_serves_params():
    # The common mistake: the parameter fed through `where`.
    out = json.loads(_run(_do_run(**_do_run_kwargs(
        where="ToDate = '2026-07-14'"))))
    assert out["error"] == "baq_needs_params"


def test_property_not_found_mask_still_serves_params():
    # A parameter guessed into `where` fails
    # with "Could not find a property named 'ToDate'..." — a FILTER error
    # message masking a PARAMETER problem. The definition check must see
    # through it.
    class _PropertyErrClient(_FakeClient):
        async def get(self, url, api_key, params=None):
            self.gets.append((url, dict(params or {})))
            if "AUTO-" in url:
                raise EpicorError(status_code=404,
                                  message="Dynamic query is not found")
            raise EpicorError(
                status_code=400,
                message="Could not find a property named 'ToDate' on type "
                        "'Epicor.QueryItem'.")
    out = json.loads(_run(_do_run(**_do_run_kwargs(
        client=_PropertyErrClient(), where="ToDate = '2026-07-14'"))))
    assert out["error"] == "baq_needs_params"
    assert [p["name"] for p in out["valid"]["parameters"]] == [
        "FromDate", "ToDate"]


def test_filter_error_with_params_supplied_blames_filter_with_columns():
    # Parameters satisfied, filter genuinely bad -> baq_run_failed, with the
    # definition's result columns served for the retry.
    class _BadFilterClient(_FakeClient):
        async def get(self, url, api_key, params=None):
            self.gets.append((url, dict(params or {})))
            if "AUTO-" in url:
                raise EpicorError(status_code=404,
                                  message="Dynamic query is not found")
            raise EpicorError(
                status_code=400,
                message="Could not find a property named 'BogusCol' on type "
                        "'Epicor.QueryItem'.")
    out = json.loads(_run(_do_run(**_do_run_kwargs(
        client=_BadFilterClient(), where="BogusCol = 'x'",
        params={"FromDate": "2025-01-01", "ToDate": "2026-07-14"}))))
    assert out["error"] == "baq_run_failed"
    assert out["valid"]["columns"] == ["PartTran_PartNum",
                                       "Calculated_SumTranQty"]


def test_needs_params_degrades_without_definition_access():
    # GetByID unavailable -> still the right error code, generic template.
    client = _FakeClient(getbyid_error=EpicorError(status_code=403,
                                                   message="denied"))
    out = json.loads(_run(_do_run(**_do_run_kwargs(client=client))))
    assert out["error"] == "baq_needs_params"
    assert out["retry_with"]["params"] == {"<ParameterID>": "<value>"}
    assert "valid" not in out


def test_missing_baq_still_terminal():
    class _NotFoundClient(_FakeClient):
        async def get(self, url, api_key, params=None):
            raise EpicorError(status_code=404,
                              message="Dynamic query is not found NOPE")
    out = json.loads(_run(_do_run(**_do_run_kwargs(
        client=_NotFoundClient(), baq="NOPE"))))
    assert out["error"] == "baq_not_found"
    assert out["terminal"] is True


# --------------------------------------------------------------------------- #
# _describe_saved_baq + schema action
# --------------------------------------------------------------------------- #

def test_describe_saved_baq_shape():
    desc = _run(_describe_saved_baq(
        session=_SESSION, rbac=_FakeRBAC(), client=_FakeClient(), baq_id=BAQ))
    assert desc["parameters"][0] == {"name": "FromDate", "type": "date",
                                     "mandatory": True}
    assert desc["columns"] == ["PartTran_PartNum", "Calculated_SumTranQty"]


def test_describe_saved_baq_tries_fallback_key():
    class _FirstKeyFails(_FakeClient):
        async def post(self, url, api_key, json_body=None):
            if api_key == "BAQKEY":
                raise EpicorError(status_code=403, message="denied")
            return await super().post(url, api_key, json_body)
    desc = _run(_describe_saved_baq(
        session=_SESSION, rbac=_FakeRBAC(), client=_FirstKeyFails(),
        baq_id=BAQ))
    assert desc is not None


class _FakeBaqIndex:
    def get_table(self, name):
        if name == "Erp.APInvHed":
            return {"full_name": "Erp.APInvHed", "description": "AP invoices"}
        return None

    def get_fields(self, name):
        return [{"field_name": "InvoiceNum", "data_type": "nvarchar"}]


def test_schema_action_describes_saved_baq():
    out = json.loads(_run(_do_schema(
        baq_index=_FakeBaqIndex(), tables="", baq=BAQ,
        session=_SESSION, rbac=_FakeRBAC(), client=_FakeClient())))
    assert out["baq"] == BAQ
    assert [p["name"] for p in out["parameters"]] == ["FromDate", "ToDate"]
    assert out["result_columns"] == ["PartTran_PartNum", "Calculated_SumTranQty"]
    assert '"FromDate"' in out["hint"] and "params=" in out["hint"]


def test_schema_action_dictionary_table_unchanged():
    out = json.loads(_run(_do_schema(
        baq_index=_FakeBaqIndex(), tables="Erp.APInvHed", baq="",
        session=_SESSION, rbac=_FakeRBAC(), client=_FakeClient())))
    assert out["mode"] == "schema"
    assert out["tables"][0]["full_name"] == "Erp.APInvHed"

"""Regression coverage: wedge fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "parsed_ds"

#: Arrays ParseFromSQL returns. The wedge only reads the first eight, which is
#: what the fixtures carry.
_DS_ARRAYS = (
    "QuerySubQuery", "QueryTable", "QueryField", "QuerySortBy", "QueryWhereItem",
    "QueryRelation", "QueryRelationField", "QueryGroupBy",
)


def load(name: str) -> tuple[str, dict[str, Any]]:
    """Return ``(sql, runtime_ds)`` for a captured fixture."""
    data = json.loads((FIXTURE_DIR / f"{name}.json").read_text())
    return data["sql"], data["ds"]


def names() -> list[str]:
    return sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))


def as_designer(runtime_ds: Mapping[str, Any]) -> dict[str, Any]:
    """Runtime tableset -> the ``*Designer`` tableset ParseFromSQL actually returns."""
    out: dict[str, Any] = {"DynamicQueryDesigner": [{"QueryID": "AdHocV3", "RowMod": "A"}]}
    for array in _DS_ARRAYS:
        out[f"{array}Designer"] = list(runtime_ds.get(array) or [])
    return out


def getbyid_returnobj(
    *,
    query_id: str = "AUTO-test",
    description: str = "a saved BAQ",
    display_phrase: str = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]",
    tables: list[dict] | None = None,
    fields: list[dict] | None = None,
    parameters: list[dict] | None = None,
) -> dict:
    """A ``DynamicQuerySvc/GetByID`` ``returnObj``, in Epicor's RUNTIME naming.

    Real GetByID returns the runtime array names (``QueryTable``, not
    ``QueryTableDesigner``). Both the deny-list and the cost governor fall back
    to the ``*Designer`` spelling, so the fixture pins the shape that actually
    arrives rather than the one the parse returns.
    """
    return {
        "DynamicQuery": [
            {
                "QueryID": query_id,
                "Description": description,
                "DisplayPhrase": display_phrase,
            }
        ],
        "QueryTable": (
            tables
            if tables is not None
            else [
                {
                    "TableID": "P",
                    "DBSchemaName": "Erp",
                    "DBTableName": "Part",
                    "TableType": "DB",
                }
            ]
        ),
        "QueryField": (
            fields
            if fields is not None
            else [
                {
                    "TableID": "P",
                    "DBSchemaName": "Erp",
                    "DBTableName": "Part",
                    "FieldName": "PartNum",
                    "Alias": "PN",
                    "DataType": "nvarchar",
                }
            ]
        ),
        "QueryParameter": list(parameters or []),
    }


class MockEpicorClient:
    """Records every call and replays canned responses. No network, ever.

    ``calls`` is an ORDERED log of ``(url, body-or-params)``, which is what lets
    a test assert the two properties the save path's safety rests on: that
    ``DeleteByID`` never precedes validation, that ``GetByID`` always precedes
    ``BaqSvc/…/Data`` — and that a refusal made ZERO calls at all. The
    ``AssertionError`` on an unrecognised URL is load-bearing for that last one:
    without it an unexpected call would silently return ``{}`` and read as a
    clean pass.
    """

    def __init__(
        self,
        *,
        parse_ds: Mapping[str, Any] | None = None,
        parse_error: Exception | None = None,
        execute_response: Mapping[str, Any] | None = None,
        execute_error: Exception | None = None,
        analyze_messages: list[str] | None = None,
        execute_delay_s: float = 0.0,
        # --- the save / saved-BAQ path (baq_ops) -------------------------
        save_parse_ds: Mapping[str, Any] | None = None,
        save_parse_error: Exception | None = None,
        getbyid_obj: Mapping[str, Any] | None = None,
        getbyid_missing: bool = False,
        getbyid_error: Exception | None = None,
        designer_getbyid_missing: bool = False,
        delete_error: Exception | None = None,
        update_error: Exception | None = None,
        baq_data_rows: list[dict] | None = None,
        baq_data_error: Exception | None = None,
        call_method_responses: Mapping[str, Any] | None = None,
    ) -> None:
        self.parse_ds = parse_ds
        self.parse_error = parse_error
        self.execute_response = execute_response
        self.execute_error = execute_error
        self.analyze_messages = analyze_messages or []
        self.execute_delay_s = execute_delay_s
        self.save_parse_ds = save_parse_ds
        self.save_parse_error = save_parse_error
        self.getbyid_obj = getbyid_obj
        self.getbyid_missing = getbyid_missing
        self.getbyid_error = getbyid_error
        self.designer_getbyid_missing = designer_getbyid_missing
        self.delete_error = delete_error
        self.update_error = update_error
        self.baq_data_rows = baq_data_rows
        self.baq_data_error = baq_data_error
        self.call_method_responses = dict(call_method_responses or {})
        self.calls: list[tuple[str, dict]] = []

    @property
    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    def called(self, fragment: str) -> bool:
        return any(fragment in p for p in self.paths)

    def count(self, fragment: str) -> int:
        return sum(1 for p in self.paths if fragment in p)

    def index_of(self, fragment: str) -> int:
        for i, path in enumerate(self.paths):
            if fragment in path:
                return i
        raise AssertionError(f"no call matching {fragment!r}; saw {self.paths}")

    def body_for(self, fragment: str) -> dict:
        for path, body in self.calls:
            if fragment in path:
                return body
        raise AssertionError(f"no call matching {fragment!r}; saw {self.paths}")

    def last_body_for(self, fragment: str) -> dict:
        for path, body in reversed(self.calls):
            if fragment in path:
                return body
        raise AssertionError(f"no call matching {fragment!r}; saw {self.paths}")

    async def post(self, url: str, api_key: str, json_body: dict | None = None) -> dict:
        import asyncio

        body = json_body or {}
        self.calls.append((url, body))
        if "ParseFromSQL" in url:
            # The SAVE path posts the full 26-field template with every
            # ``*Designer`` array present; the ad-hoc pipe posts a one-key ds
            # holding only ``DynamicQueryDesigner``. That difference is the
            # discriminator, so a test can make the save's parse fail while the
            # ad-hoc parse that produced the rows still succeeds.
            is_save = len(body.get("ds") or {}) > 1
            if is_save:
                if self.save_parse_error:
                    raise self.save_parse_error
                return {
                    "parameters": {
                        "ds": as_designer(self.save_parse_ds or self.parse_ds or {})
                    }
                }
            if self.parse_error:
                raise self.parse_error
            return {"parameters": {"ds": as_designer(self.parse_ds or {})}}
        if "BAQDesignerSvc/GetByID" in url:
            # The DESIGNER's read, used only to snapshot a definition about to
            # be overwritten. It returns the `*Designer` naming — which is the
            # whole point: `BAQDesignerSvc/Update` cannot take the RUNTIME
            # tableset `DynamicQuerySvc/GetByID` hands back, so a restore built
            # from that one is inert.
            if self.designer_getbyid_missing:
                raise FakeEpicorError(
                    f"Query is not found {body.get('queryID')}", 404
                )
            base = self.getbyid_obj or getbyid_returnobj(
                query_id=str(body.get("queryID") or "AUTO-test")
            )
            return {"returnObj": as_designer(base)}
        if "DynamicQuerySvc/GetByID" in url:
            if self.getbyid_error:
                raise self.getbyid_error
            if self.getbyid_missing:
                raise FakeEpicorError(
                    f"Dynamic query is not found {body.get('queryID')}", 404
                )
            obj = self.getbyid_obj
            if obj is None:
                obj = getbyid_returnobj(query_id=str(body.get("queryID") or "AUTO-test"))
            return {"returnObj": dict(obj)}
        if "Execute" in url:
            if self.execute_delay_s:
                await asyncio.sleep(self.execute_delay_s)
            if self.execute_error:
                raise self.execute_error
            return self.execute_response or {
                "returnObj": {"Results": [], "Errors": [], "ExecutionInfo": []}
            }
        if "Analyze" in url:
            return {"parameters": {"errorMessages": list(self.analyze_messages)}}
        if "BAQDesignerSvc/DeleteByID" in url:
            if self.delete_error:
                raise self.delete_error
            return {}
        if "BAQDesignerSvc/Update" in url:
            if self.update_error:
                raise self.update_error
            return {"parameters": {"ds": dict(body.get("ds") or {})}}
        raise AssertionError(f"unexpected call to {url}")

    async def get(self, url: str, api_key: str, params: dict | None = None) -> dict:
        self.calls.append((url, dict(params or {})))
        if "/Data" in url:
            if self.baq_data_error:
                raise self.baq_data_error
            return {"value": list(self.baq_data_rows or [])}
        raise AssertionError(f"unexpected GET to {url}")

    async def call_method(
        self,
        base_url: str,
        service: str,
        method: str,
        api_key: str,
        params: dict | None = None,
    ) -> dict:
        key = f"{service}/{method}"
        self.calls.append((f"{base_url}/{key}", dict(params or {})))
        if key in self.call_method_responses:
            response = self.call_method_responses[key]
            if isinstance(response, Exception):
                raise response
            return dict(response)
        raise AssertionError(f"unexpected call_method to {key}")

    async def close(self) -> None:  # pragma: no cover - parity with EpicorClient
        return None


class FakeEpicorError(Exception):
    """Stands in for ``EpicorError`` — same duck type the pipe reads."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def ok_execute(rows: list[dict], *, sql_ms: str = "12.5") -> dict:
    return {
        "returnObj": {
            "Results": rows,
            "Errors": [],
            "ExecutionInfo": [{"Name": "ExecutionTime", "Value": sql_ms}],
        }
    }

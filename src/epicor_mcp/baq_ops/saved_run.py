"""Read and authorize a saved BAQ definition before executing it.

A BAQ id alone says nothing about the tables it reads. Execution therefore
follows this order: reject unsupported paging, check the runtime budget, read
``DynamicQuerySvc/GetByID``, validate the definition shape, enforce the denylist
and any supplied table scope, coerce query parameters, then call
``BaqSvc/{id}/Data`` under the inflight limit and timeout.

The shape guard requires at least one ``QueryTable`` or ``QueryTableDesigner``
row with ``TableType == 'DB'`` and a nonempty ``DBTableName``. Missing arrays
cannot count as an allowed definition: an empty ``Denial`` is false, so relying
on the denylist alone would let an unreadable definition execute.

Saved queries can contain parameters and cross-subquery references that cannot
be attributed to a physical column. Those unresolved references are allowed
on this path, while explicit denied tables/columns and evaluator failures still
refuse execution. The SSO-disabled runtime also supplies the operator's table
whitelist. SSO saved reads follow Epicor's grant model rather than the separate
menu-derived table scope.

The shape-based cost guard is omitted because saved query parameters need not
appear as literal filters in the definition. The session budget, shared
concurrency bound and wall timeout still apply. Parameter metadata is advisory;
Epicor remains responsible for evaluating the saved query's inputs.

Public saved-BAQ paging is unsupported beyond page 1. Sending an unverified
``$skip`` could relabel repeated first-page rows as a later page; the ad-hoc
TOP-based reachability check cannot establish saved-query paging semantics.
The internal ``allow_paging`` option is reserved for controlled verification.
See ``tests/test_saved_baq_run.py`` for shape, denial and paging coverage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Mapping

from epicor_mcp.sql import denylist as denylistmod
from epicor_mcp.sql.adhoc import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    _KEYSET_RECIPE,
    _columns_of,
    rows_to_tsv,
)
from epicor_mcp.sql.envelope import error_envelope
from epicor_mcp.sql.governor import CostGovernor, GovernorPolicy, timeout_envelope
from epicor_mcp.sql.next_step import annotate_next_step

logger = logging.getLogger(__name__)

__all__ = [
    "run_saved_baq",
    "describe_saved_baq",
    "coerce_baq_params",
    "paging_unsupported_envelope",
    "GETBYID_PATH",
]

GETBYID_PATH = "Ice.BO.DynamicQuerySvc/GetByID"

#: Columns `BaqSvc` appends to a result that are NOT columns of the BAQ. They
#: are row handles for Epicor's own grid, synthesised per page, and they answer
#: no business question. Dropped only when the BAQ's own declared result columns
#: do not contain the name — a BAQ that really selects `RowIdent` keeps it.
_TRANSPORT_COLUMNS = frozenset({"RowIdent", "SysRowID", "RowMod"})


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def coerce_baq_params(params: Any) -> tuple[dict[str, Any], str]:
    """``(params, problem)`` — a dict, a JSON-object string, or a named problem.

    Port of ``tools/baq.py::_coerce_baq_params`` with ONE change: the legacy helper returns an
    empty dict for anything it cannot read, which silently drops the caller's
    parameters and then blames the BAQ for needing them. Here the shape that
    could not be read comes back as *problem*, and the caller turns it into an
    INV-1 refusal — never silently drop an argument.
    """
    if params is None:
        return {}, ""
    if isinstance(params, Mapping):
        return dict(params), ""
    if isinstance(params, str):
        if not params.strip():
            return {}, ""
        try:
            parsed = json.loads(params)
        except ValueError:
            return {}, "a string that is not JSON"
        if isinstance(parsed, dict):
            return dict(parsed), ""
        return {}, f"a JSON {type(parsed).__name__}, not a JSON object"
    return {}, f"a {type(params).__name__}"


def describe_saved_baq(obj: Mapping[str, Any]) -> dict[str, Any]:
    """The BAQ's execution parameters and result columns, from a GetByID returnObj.

    Pure — the definition is already in hand, so this costs nothing. ``mandatory``
    is Epicor's ``not SkipIfEmpty`` (the same heuristic as ``tools/baq.py``)
    and is used ONLY to annotate and to attribute a failure, never to refuse; see
    :func:`run_saved_baq`.
    """
    parameters = [
        {
            "name": str(p.get("ParameterID")),
            "type": str(p.get("ParameterType") or "string"),
            "mandatory": not p.get("SkipIfEmpty", False),
        }
        for p in (obj.get("QueryParameter") or obj.get("QueryParameterDesigner") or [])
        if isinstance(p, Mapping) and p.get("ParameterID")
    ]
    columns = [
        str(f.get("Alias") or f.get("FieldName"))
        for f in (obj.get("QueryField") or obj.get("QueryFieldDesigner") or [])
        if isinstance(f, Mapping) and (f.get("Alias") or f.get("FieldName"))
    ]
    phrase = ""
    rows = obj.get("DynamicQuery") or obj.get("DynamicQueryDesigner") or []
    if rows and isinstance(rows[0], Mapping):
        phrase = str(rows[0].get("DisplayPhrase") or "")
    return {"parameters": parameters, "columns": columns, "display_phrase": phrase}


def _params_example(parameters: list[dict[str, Any]]) -> dict[str, str]:
    return {p["name"]: f"<{p.get('type', 'value')}>" for p in parameters}


def _db_table_rows(ds: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = ds.get("QueryTable") or ds.get("QueryTableDesigner") or []
    return [
        r
        for r in rows
        if isinstance(r, Mapping)
        and str(r.get("TableType") or "").strip() == "DB"
        and str(r.get("DBTableName") or "").strip()
    ]


def paging_unsupported_envelope(baq_id: str, page: int, page_size: int) -> dict[str, Any]:
    """``page > 1`` on a saved BAQ. Zero Epicor calls; the recovery is named."""
    return error_envelope(
        "saved_baq_paging_unsupported",
        (
            f"page={page} is not available for a saved BAQ. This server sends only `$top` "
            "to BaqSvc — `$skip` has never been measured against it, and the sibling "
            "execution endpoint SILENTLY IGNORES paging settings it does not recognise, "
            "which would hand you page 1's rows labelled page "
            f"{page}. Rather than return a wrong answer: read page 1 with a larger "
            "page_size (max 1000); or, if the BAQ takes parameters, narrow it with "
            "`params`; or re-express the question as SQL through `sql`, where keyset "
            f"paging works — {_KEYSET_RECIPE}."
        ),
        evidence="tools/_baq_helpers.py sends $top only; adhoc.py records "
        "the measured silent-ignore of unrecognised paging settings on the sibling endpoint",
        retry_with={"saved_baq": baq_id, "page_size": min(1000, max(page_size, 1000))},
        detail={"stage": "paging", "saved_baq": baq_id, "page": page},
        terminal=False,
    )


# --------------------------------------------------------------------------- #
# the runner
# --------------------------------------------------------------------------- #


async def run_saved_baq(
    *,
    client: Any,
    api_key: str,
    base_url: str,
    baq_id: str,
    params: Any = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    page: int = 1,
    governor: CostGovernor | None = None,
    session_id: str = "anonymous",
    max_bytes: int = 700_000,
    allow_paging: bool = False,
    table_scope: Any = None,
) -> dict[str, Any]:
    """Execute saved BAQ *baq_id*. Returns the ad-hoc success shape, or INV-1.

    *allow_paging* is a TEST SEAM and production must never set it. There is no
    ``$skip``, so a ``page > 1`` fetch re-reads page 1 — which is precisely why
    the refusal exists. The switch is here only so the completeness
    belt-and-braces (``empty_later_page``, and the EMPTY PAGE summary branch)
    stays reachable by a test until the ``$skip`` probe lands and paging can be
    turned on for real.
    """
    started = time.monotonic()
    governor = governor or CostGovernor(GovernorPolicy())
    policy = governor.policy
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    if page_size <= 0:
        page_size = DEFAULT_PAGE_SIZE
    page_size = min(page_size, MAX_PAGE_SIZE)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1

    baq_id = str(baq_id or "").strip()

    def _charge(result: dict[str, Any]) -> dict[str, Any]:
        """Every exit path is charged, refusals included — a refusal that cost an
        Epicor call still cost it, and one that cost nothing charges ~0."""
        governor.record(session_id, time.monotonic() - started)
        return result

    # --- 1. paging, refused locally --------------------------------------
    if page > 1 and not allow_paging:
        return _charge(paging_unsupported_envelope(baq_id, page, page_size))

    # --- 2. session budget ------------------------------------------------
    budget = governor.check_budget(session_id)
    if budget is not None:
        return _charge(budget)

    baq_params, problem = coerce_baq_params(params)
    if problem:
        return _charge(
            error_envelope(
                "invalid_params_type",
                (
                    f"`params` must be a JSON object keyed by the BAQ's own ParameterID "
                    f"(for example {{\"FromDate\": \"2026-01-01\"}}); this call sent "
                    f"{problem}. Nothing was run and nothing was dropped silently."
                ),
                retry_with={"saved_baq": baq_id, "params": {}},
                detail={"stage": "params"},
            )
        )

    assumptions: dict[str, Any] = {}

    # --- 3. GetByID -------------------------------------------------------
    parse_started = time.monotonic()
    definition: Mapping[str, Any] | None = None
    first_error: Exception | None = None
    actual_id = baq_id
    for candidate in ([baq_id] if baq_id.startswith("AUTO-") else [baq_id, f"AUTO-{baq_id}"]):
        try:
            response = await client.post(
                f"{base_url}/{GETBYID_PATH}", api_key, json_body={"queryID": candidate}
            )
        except Exception as exc:  # noqa: BLE001
            if first_error is None:
                first_error = exc
            continue
        obj = response.get("returnObj") if isinstance(response, Mapping) else None
        if isinstance(obj, Mapping) and (
            obj.get("DynamicQuery") or obj.get("DynamicQueryDesigner")
        ):
            definition = obj
            actual_id = candidate
            break
    parse_ms = (time.monotonic() - parse_started) * 1000

    if definition is None:
        # The AUTO- retry's own 404 must NEVER mask the first spelling's real
        # error — the legacy tools/baq.py shows that exact masking: a bad
        # request against an EXISTING BAQ came back as "AUTO-<id> not found",
        # sending the model off to invent another id.
        detail: dict[str, Any] = {"stage": "saved_baq_resolve", "tried": [baq_id]}
        if not baq_id.startswith("AUTO-"):
            detail["tried"].append(f"AUTO-{baq_id}")
        if first_error is not None:
            detail["status"] = getattr(first_error, "status_code", None)
            detail["message"] = str(getattr(first_error, "message", first_error))[:1200]
            detail["message_is_from"] = detail["tried"][0]
        return _charge(
            error_envelope(
                "baq_not_found",
                (
                    f"No saved BAQ '{baq_id}' exists in Epicor"
                    + (
                        f" (the AUTO-{baq_id} spelling was tried too)."
                        if not baq_id.startswith("AUTO-")
                        else "."
                    )
                    + " Inventing another id is not a recovery — ask the user for the exact "
                    "BAQ id, or answer the question with `sql` instead."
                ),
                detail=detail,
                terminal=True,
            )
        )
    if actual_id != baq_id:
        assumptions.setdefault("saved_baq", {})["baq_id_corrected"] = {
            "submitted": baq_id,
            "used": actual_id,
            "why": "no BAQ of the submitted id exists; the AUTO- prefix resolved it",
        }

    described = describe_saved_baq(definition)

    # --- 4. SHAPE GUARD: fail CLOSED on a definition we cannot read -------
    if not _db_table_rows(definition):
        return _charge(
            error_envelope(
                "saved_baq_definition_unreadable",
                (
                    f"BAQ '{actual_id}' exists, but its definition carries no readable "
                    "database table (no QueryTable row with TableType 'DB' and a real "
                    "DBTableName). This server authorizes a saved BAQ by evaluating the "
                    "deny-list over the tables in its definition, so a definition it "
                    "cannot read cannot be authorized — and it is refused rather than "
                    "run. An updatable or external-datasource BAQ looks like this. Ask "
                    "for the data another way, or express the question as `sql`."
                ),
                evidence="denylist.check_parsed_ds returns an ALL-EMPTY Denial for an "
                "unrecognised tableset and an empty Denial is falsy (denylist.py), "
                "so without this guard the query would run UNGATED",
                detail={"stage": "shape_guard", "saved_baq": actual_id},
                terminal=True,
            )
        )

    # --- 5. ENFORCE the deny-list over Epicor's OWN resolved rows --------
    # `unattributed_denies=False`: the table and resolved-column deny-lists both
    # still run, unchanged — a saved BAQ over Erp.PREmpMas, or one selecting a
    # resolved LaborRate, is refused exactly as an ad-hoc statement is. What is
    # relaxed is the fail-closed rule for a reference this server could not place,
    # which has no threat model here: the caller sends an ID, not SQL, so nothing
    # they control can smuggle a column past it. It is the ORDINARY vocabulary of
    # a hand-authored BAQ — Query Parameters, `CurrentUserID`, cross-subquery
    # references Epicor renders with no TableID. These can appear in a saved
    # definition whose resolved tables and columns are all permitted. Same
    # reasoning as `check_cost` not applying here (saved parameters are query inputs).
    denial = denylistmod.check_parsed_ds(definition, unattributed_denies=False)
    if denial:
        env = denylistmod.denial_envelope(denial)
        env.setdefault("detail", {})["stage"] = "denylist"
        env["detail"]["source"] = "saved_baq"
        env["detail"]["saved_baq"] = actual_id
        return _charge(env)
    from epicor_mcp.sql.scope_gate import check_table_scope
    scope_refusal = check_table_scope(table_scope, definition)
    if scope_refusal is not None:
        scope_refusal.setdefault("detail", {}).update(source="saved_baq", saved_baq=actual_id)
        return _charge(scope_refusal)
    tables_read = denial.allowed_tables

    # --- 6. parameters: ADVISORY, never a pre-flight refusal -------------
    # `mandatory` is the heuristic `not SkipIfEmpty` and in the legacy tools it
    # is used ONLY to attribute a failure Epicor already returned. Nothing
    # evidences that SkipIfEmpty == False means Epicor refuses to run, so
    # promoting it to a refusal would blame the caller for something Epicor never
    # complained about — the false-blame class "don't blame params for a bad
    # order_by" exists to kill. Annotate, then run anyway.
    parameters = described["parameters"]
    unsupplied = [p for p in parameters if p["mandatory"] and p["name"] not in baq_params]
    if unsupplied:
        assumptions.setdefault("saved_baq", {})["parameters_unsupplied"] = {
            "parameters": unsupplied,
            "note": (
                "This BAQ declares parameters that were not supplied. They are inputs the "
                "QUERY needs, not a filter on the result, so if the run comes back wrong "
                "or empty, supply them rather than re-filtering."
            ),
            "retry_with": {"saved_baq": actual_id, "params": _params_example(unsupplied)},
        }

    # --- 7. execute -------------------------------------------------------
    query: dict[str, Any] = {"$top": max(1, min(page_size, MAX_PAGE_SIZE))}
    dropped_param_keys: list[str] = []
    for key, value in baq_params.items():
        # A `$`-prefixed key would let a parameter overwrite `$top` and silently
        # unbound the read. Dropped, and ANNOUNCED — never silently.
        if str(key).startswith("$"):
            dropped_param_keys.append(str(key))
            continue
        query[str(key)] = value
    if dropped_param_keys:
        assumptions.setdefault("saved_baq", {})["params_dropped"] = {
            "keys": dropped_param_keys,
            "why": "a $-prefixed key is an OData system option, not a BAQ ParameterID; "
            "allowing it would let a parameter overwrite the row bound",
        }

    exec_started = time.monotonic()
    try:
        async with governor.inflight():
            response = await asyncio.wait_for(
                client.get(f"{base_url}/BaqSvc/{actual_id}/Data", api_key, params=query),
                timeout=policy.execute_timeout_s,
            )
    except asyncio.TimeoutError:
        return _charge(timeout_envelope(policy.execute_timeout_s, described["display_phrase"]))
    except Exception as exc:  # noqa: BLE001
        detail = {
            "status": getattr(exc, "status_code", None),
            "message": str(getattr(exc, "message", exc))[:1200],
            "stage": "execute",
            "saved_baq": actual_id,
        }
        if unsupplied:
            # NOW the blame is earned: Epicor refused, and the definition already
            # in hand says which inputs were missing. The template is derived from
            # the definition rather than from un-masking two look-alike messages.
            return _charge(
                error_envelope(
                    "baq_needs_params",
                    (
                        f"BAQ '{actual_id}' failed to run and it declares parameters that "
                        "were not supplied: "
                        + ", ".join(
                            f"{p['name']} ({p['type']}, mandatory)" for p in unsupplied
                        )
                        + ". Pass them in `params` — a JSON object keyed by parameter name, "
                        "dates as YYYY-MM-DD. They are inputs the query needs to run at "
                        "all, not a filter on the result. Ask the user for values if the "
                        "question does not imply them."
                    ),
                    valid={"parameters": parameters},
                    retry_with={
                        "saved_baq": actual_id,
                        "params": _params_example(unsupplied),
                    },
                    detail={**detail, "stage": "params"},
                )
            )
        return _charge(
            error_envelope(
                "baq_run_failed",
                f"Saved BAQ '{actual_id}' failed to run: {detail['message']}. This server "
                "did not write this BAQ — the definition lives in Epicor and is fixed "
                "there, in the BAQ Designer.",
                valid={"columns": described["columns"]} if described["columns"] else None,
                detail=detail,
            )
        )
    exec_ms = (time.monotonic() - exec_started) * 1000

    records = response.get("value") if isinstance(response, Mapping) else None
    if not isinstance(records, list):
        records = []
    rows = [r for r in records if isinstance(r, Mapping)]
    columns = _columns_of(rows)
    # `RowIdent` is BaqSvc's own result-grid row handle, not a column of the
    # BAQ: it is appended to every saved-BAQ result and its values are
    # synthesised per page (`00000001-0000-…`, `00000002-0000-…`). It answers no
    # business question, it costs a 36-char GUID on EVERY row, and the ad-hoc
    # path does not return it. Carrying it here breaks the shape parity this
    # path is supposed to hold. Dropped, and ANNOUNCED in `assumptions` rather than
    # silently, per the module's own rule. Only dropped when the BAQ did not
    # select a column of that name itself.
    declared = {str(c) for c in (described.get("columns") or [])}
    dropped_transport = [
        c for c in columns if c in _TRANSPORT_COLUMNS and c not in declared
    ]
    if dropped_transport:
        columns = [c for c in columns if c not in dropped_transport]
        assumptions.setdefault("saved_baq", {})["transport_columns_dropped"] = {
            "columns": dropped_transport,
            "why": (
                "Epicor's BaqSvc appends these to every saved-BAQ result as row "
                "handles for its own grid; they are not columns of the BAQ and "
                "the ad-hoc path does not return them."
            ),
        }
    tsv = rows_to_tsv(rows, columns)

    dropped = 0
    if len(tsv.encode("utf-8")) > max_bytes:
        keep = len(rows)
        while keep > 0 and len(rows_to_tsv(rows[:keep], columns).encode("utf-8")) > max_bytes:
            keep = int(keep * 0.8) if keep > 10 else keep - 1
        dropped = len(rows) - keep
        rows = rows[:keep]
        tsv = rows_to_tsv(rows, columns)

    # ALL THREE completeness terms, exactly as `adhoc.run_sql` computes them. The
    # third is dead code while page > 1 is refused — that is the point: it is the
    # belt-and-braces for the day the $skip probe turns paging on, and a
    # completeness rule that has to be re-derived later is a completeness rule
    # that comes back wrong.
    full_page = len(rows) + dropped >= page_size
    empty_later_page = page > 1 and not rows and dropped == 0
    complete = not full_page and dropped == 0 and not empty_later_page

    if dropped:
        summary = (
            f"INCOMPLETE: {len(rows)} of {len(rows) + dropped} rows returned — the rest "
            "were dropped to stay under the response size cap. This BAQ's column list is "
            "fixed in its definition, so narrow it with `params` or read it in Epicor."
        )
    elif empty_later_page:
        summary = (
            f"EMPTY PAGE: page {page} returned 0 rows. That is NOT evidence that the "
            "result set ended — a later page can be empty because the row bound was "
            "reached, not because the data was. Re-read page 1."
        )
    elif full_page:
        summary = (
            f"INCOMPLETE: {len(rows)} rows, which is a FULL page (page_size={page_size}). "
            "There are almost certainly more. Raise page_size (max 1000) or narrow the "
            "BAQ with `params` — this server cannot page a saved BAQ, because `$skip` is "
            "unmeasured against BaqSvc and returning page 1 twice would be a wrong answer."
        )
    elif not rows:
        # NOT diagnosed, and the reason is stated rather than left as silence.
        # `diagnose_empty` decomposes the WHERE of a statement WE authored; this
        # SQL was written by somebody else and lives in Epicor.
        summary = (
            "0 rows. This server did not write this BAQ's SQL, so it does not diagnose "
            "the empty result — check the BAQ's parameters or its definition in Epicor."
        )
    else:
        summary = (
            f"{len(rows)} row(s) — a partial page, so this is the complete result for "
            "this BAQ."
        )

    result: dict[str, Any] = {
        "success": True,
        "columns": columns,
        "rows": tsv,
        "format": "tab-separated; the first line is the header; an empty field is null",
        "row_count": len(rows),
        "rows_dropped_for_size": dropped,
        "complete": complete,
        "terminal": complete,
        "summary": summary,
        "next_step": "",
        "tables_read": tables_read,














        "sql_executed": "",
        "saved_baq_id": actual_id,
        "assumptions": assumptions,
        "notes": [],
        # Epicor reports no ExecutionTime on BaqSvc, and `None` is how the ad-hoc
        # path already spells "Epicor did not tell us". `0` would be a claim.
        "sql_ms": None,
        "parse_ms": round(parse_ms, 1),
        "execute_ms": round(exec_ms, 1),
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    governor.record(session_id, time.monotonic() - started)
    return annotate_next_step(result)

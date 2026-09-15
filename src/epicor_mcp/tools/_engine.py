"""Legacy read engine — module-level query core lifted from ``query.py``.

Originally the OData and GetRows executors were nested inside ``query.register()``.
They already take everything (client, index, service, entity_set, api_key, and
keyword-only options) as explicit parameters and capture no closure, so they
lift to module scope unchanged. The legacy intent tools (``read``, ``act``,
``baq``) call these directly instead of re-implementing the routing:

    from epicor_mcp.tools._engine import (
        run_odata, run_getrows, validate_columns, suggest_entities,
        sql_to_odata, odata_to_sql, getrows_services,
    )

``query.py`` is left intact as an engine library and remains fully importable;
this module reuses its helper functions rather than duplicating them, so the
hint/validation logic stays single-sourced.
"""

from __future__ import annotations

import difflib
import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.response import format_response
from epicor_mcp.tools._aggregate import _warn_having_ignored, aggregate_records

# Reuse query.py's module-level helpers verbatim (single source of truth). These are
# all defined at module scope in query.py, so importing them here creates no
# closure and no circular import (query.py does not import _engine).
from epicor_mcp.tools._resolve import epicor_error_envelope, error_envelope
from epicor_mcp.tools.query import (
    _child_pivot_hint,
    date_columns_for,
    _children_tool_nudge,
    _extract_filter_identifiers,
    _getrows_services,
    _is_generic_epicor_apology,
    _name_resolution_hint,
    _odata_to_sql_where,
    _sql_to_odata_filter,
    _suggest_entity_sets,
    _validate_query_columns,
    _zero_result_hint,
)

if TYPE_CHECKING:
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex

logger = logging.getLogger(__name__)

# Shared runtime set of services whose OData entity-set GET is known to fail;
# the same object query.py mutates, so a discovery in either path sticks.
getrows_services = _getrows_services

# ---------------------------------------------------------------------------
# Public helper aliases (stable names for the intent tools)
# ---------------------------------------------------------------------------

# SQL-ish filter -> OData filter (input translation).
sql_to_odata = _sql_to_odata_filter
# OData filter -> Epicor whereClause SQL (GetRows path).
odata_to_sql = _odata_to_sql_where
# Pre-flight column validation; returns unknown/misspelled column names.
validate_columns = _validate_query_columns
# Closest entity-set names on a service to a requested one.
suggest_entities = _suggest_entity_sets
# Lowercased Edm date columns on an entity (type gate for date coercion).
date_columns_for = date_columns_for

# Re-exported hint helpers (used to enrich INV-1 envelopes and results).
children_tool_nudge = _children_tool_nudge
zero_result_hint = _zero_result_hint
name_resolution_hint = _name_resolution_hint
child_pivot_hint = _child_pivot_hint
is_generic_epicor_apology = _is_generic_epicor_apology


# ---------------------------------------------------------------------------
# OData executor — lifted UNCHANGED from query.register()._odata_query,
# renamed run_odata.
# ---------------------------------------------------------------------------

async def run_odata(
    client: "EpicorClient",
    service: str,
    entity_set: str,
    api_key: str,
    *,
    filter: str,
    select: str,
    orderby: str,
    top: int,
    skip: int = 0,
    expand: str,
    count_only: bool,
    group_by: str = "",
    aggregate: str = "",
    distinct: str = "",
    having: str = "",
    order_by: str = "",
    format: str,
) -> str:
    """Execute a direct OData GET on the entity set endpoint."""

    if count_only:
        count_url = f"{service}/{entity_set}/$count"
        count_params: dict[str, str] = {}
        if filter:
            count_params["$filter"] = filter
        response_raw = await client.get(count_url, api_key, params=count_params)
        if isinstance(response_raw, dict):
            count_value = response_raw.get("_raw", response_raw)
        else:
            count_value = response_raw
        try:
            count_int = int(str(count_value).strip())
        except (ValueError, TypeError):
            count_int = count_value
        count_payload: dict = {
            "count": count_int,
            "service": service,
            "entity_set": entity_set,
            "filter": filter or "(none)",
        }
        nudge = _children_tool_nudge(service, entity_set)
        if nudge:
            count_payload["efficiency_hint"] = nudge
        return format_response(count_payload, records_key=None)

    params: dict[str, str | int] = {"$top": top}
    if skip > 0:
        params["$skip"] = skip
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if orderby:
        params["$orderby"] = orderby
    if expand:
        params["$expand"] = expand

    url = f"{service}/{entity_set}"
    response = await client.get(url, api_key, params=params)

    records = response.get("value", response)
    record_count = len(records) if isinstance(records, list) else None

    if (group_by or aggregate or distinct) and isinstance(records, list):
        try:
            agg_result = aggregate_records(
                records,
                group_by=group_by,
                aggregate=aggregate,
                distinct=distinct,
                having=having,
                order_by=order_by,
            )
        except ValueError as ve:
            return json.dumps(error_envelope(
                "invalid_aggregate", str(ve)))
        if record_count == top:
            # top already caps at 1000 — telling the model to "raise top"
            # here was a dead end that drove tool-call thrash. Be honest.
            agg_result["note"] = f"Aggregated over the first {top} rows only."
        return format_response(agg_result, records_key="records", format=format)

    result: dict = {"records": records}
    if record_count is not None:
        result["record_count"] = record_count
    # A `having` with no group_by/aggregate never reaches aggregate_records
    # (the gate above requires one of the three), so it was dropped in total
    # silence and the FULL unfiltered page came back looking like the
    # thresholded answer -- and the parameter is persisted into cursor_ctx, so
    # every later page was silently unfiltered too.
    _warn_having_ignored(result, having, "plain")
    if record_count == top:
        result["note"] = (
            f"Result limited to {top} records. "
            "Increase 'top' or refine your filter to see more."
        )
    if record_count == 0:
        zero = _zero_result_hint(entity_set, filter)
        if zero:
            result["no_match_hint"] = zero
    nudge = _children_tool_nudge(service, entity_set)
    if nudge:
        result["efficiency_hint"] = nudge

    return format_response(result, records_key="records", format=format)


# ---------------------------------------------------------------------------
# GetRows internals — extracted from run_getrows so the paged aggregation
# scanner below can reuse them page-by-page. Pure refactor: run_getrows
# behavior is unchanged.
# ---------------------------------------------------------------------------

def _dataset_tables(index: "ServiceIndex", service: str) -> list[str]:
    """The service's primary-DataSet table names, in index order.

    GetRows takes a ``whereClause{Table}`` param for each table in the
    service's primary DataSet — those are the entity sets the index has
    FIELDS for. Plural-only OData collection names (e.g. "Vendors",
    "Customers", "Parts") come from a separate source and are NOT valid
    GetRows targets; including them causes Epicor to throw an internal error.
    """
    return [
        es for es in (index.get_entity_sets(service) or [])
        if index.get_fields(service, es)
    ]


def resolve_include_tables(
    index: "ServiceIndex",
    service: str,
    requested,
) -> tuple[set[str], dict | None]:
    """Map caller-requested sibling tables onto the service's DataSet tables.

    Returns ``(resolved, error)``. ``resolved`` holds the REAL table names
    (case corrected); ``error`` is an INV-1 envelope when a requested name is
    not a table on this service — an unknown name must never quietly no-op,
    because the caller would then get the ordinary ``1=0`` response and read it
    as "there are no attachments" instead of "you asked for the wrong table".
    """
    if isinstance(requested, str):
        requested = requested.split(",")
    wanted = [str(t).strip() for t in (requested or []) if str(t).strip()]
    if not wanted:
        return set(), None

    real_tables = _dataset_tables(index, service)
    if not real_tables:
        # The service has no indexed DataSet: there is nothing to validate
        # against, AND _build_getrows_where can then only emit the target's own
        # whereClause — so an "included" sibling would silently no-op. Say that
        # rather than blaming the caller's name against an empty valid.tables.
        return set(), error_envelope(
            "include_tables_unavailable",
            f"{service} has no DataSet tables in the service index, so sibling "
            "tables cannot be scoped into this GetRows call. Rebuild the index "
            "(build-index), or drop include_tables and read the sibling table "
            "as its own target.",
            retry_with={"include_tables": None},
        )
    by_lower = {t.lower(): t for t in real_tables}

    resolved: set[str] = set()
    unknown: list[str] = []
    for name in wanted:
        hit = by_lower.get(name.lower())
        if hit is None and name.lower().endswith("s"):
            # Tolerate the plural OData spelling ("APInvHedAttches"), the same
            # translation the target entity_set already gets below.
            for stripped in (name[:-1], name[:-2]):
                hit = by_lower.get(stripped.lower())
                if hit:
                    break
        if hit:
            resolved.add(hit)
        else:
            unknown.append(name)

    if not unknown:
        return resolved, None

    retry = set(resolved)
    did_you_mean: dict[str, list[str]] = {}
    for name in unknown:
        close = difflib.get_close_matches(name, real_tables, n=3, cutoff=0.6)
        if close:
            did_you_mean[name] = close
            retry.add(close[0])
    valid: dict = {"tables": real_tables}
    if did_you_mean:
        valid["did_you_mean"] = did_you_mean
    return resolved, error_envelope(
        "unknown_include_tables",
        f"{service} has no DataSet table named "
        f"{', '.join(repr(u) for u in unknown)}. Pick from valid.tables — "
        "include_tables names sibling tables of the SAME GetRows DataSet, "
        "which come back scoped to the matched parent rows.",
        valid=valid,
        retry_with={"include_tables": sorted(retry)} if retry else None,
    )


def _build_getrows_where(
    index: "ServiceIndex",
    service: str,
    entity_set: str,
    filter: str,
    orderby: str,
    *,
    include_tables: set[str] | None = None,
) -> tuple[dict, str]:
    """Build the per-table ``whereClause{Table}`` params for a GetRows call.

    Returns ``(where_body, target_table)`` — the caller adds ``pageSize`` /
    ``absolutePage`` per fetch. The target entity_set gets the (translated)
    filter; all other tables get ``1=0`` to exclude child data.

    ``include_tables`` is the narrow opt-in escape hatch: each named sibling
    table gets ``""`` instead of ``1=0``, which is what makes Epicor return
    those child rows scoped to the matched parents (this is the ONLY way to
    reach an ``*Attch`` attachment table — those are invisible over OData).
    Every DataSet table still gets a param either way: omit one and Epicor
    400s with "Parameter whereClauseX is not found in the input object".
    """
    where_clause = _odata_to_sql_where(filter)

    # Append ordering to the whereClause if provided
    if orderby:
        # Translate OData orderby ("OrderDate desc") to SQL ("By OrderDate desc")
        order_sql = orderby.strip()
        if not order_sql.lower().startswith("by "):
            order_sql = f"By {order_sql}"
        if where_clause:
            where_clause = f"({where_clause}) {order_sql}"
        else:
            where_clause = order_sql

    # GetRows takes whereClauseX params for each table in the
    # service's primary DataSet (see _dataset_tables).
    real_tables = _dataset_tables(index, service)

    # If the caller passed an OData URL/plural form (no fields in
    # the index), translate it to the corresponding DataSet table
    # name by stripping the plural suffix.
    target_table = entity_set
    if entity_set not in real_tables:
        for stripped in (
            entity_set[:-1] if entity_set.endswith("s") else None,
            entity_set[:-2] if entity_set.endswith("es") else None,
        ):
            if stripped and stripped in real_tables:
                target_table = stripped
                break

    # Case-correct the opt-in names here too, so a direct caller of this
    # helper gets the same tolerance resolve_include_tables provides.
    included = {t.lower() for t in (include_tables or ())}

    where_body: dict = {}
    matched_target = False
    for es in real_tables:
        if es == target_table:
            where_body[f"whereClause{es}"] = where_clause
            matched_target = True
        elif es.lower() in included:
            # "" (NOT "1=0") = return this sibling's rows for the matched
            # parents. Opt-in only: "1=0" stays the default because it is what
            # keeps an ordinary single-table read small.
            where_body[f"whereClause{es}"] = ""
        else:
            where_body[f"whereClause{es}"] = "1=0"

    if not matched_target:
        # Target table isn't in the index either — best-effort.
        where_body[f"whereClause{target_table}"] = where_clause

    return where_body, target_table


def _extract_getrows_records(response: dict, target_table: str) -> list:
    """Pull the target table's rows out of a GetRows response.

    Only fall through to "first non-empty list" when the requested
    entity_set isn't present in the dataset at all — do NOT fall
    through when the target table is present but empty, because
    that legitimately means "no matching rows" and falling through
    would silently return unrelated data (e.g. a TaxConnectStatus
    metadata row when QuoteDtl matched nothing).
    """
    data = response.get("returnObj", response)
    records: list = []
    if isinstance(data, dict):
        # GetRows returns rows keyed by DataSet table name (singular),
        # which is target_table after any plural→singular translation.
        if target_table in data:
            records = data.get(target_table) or []
        else:
            for key, value in data.items():
                if isinstance(value, list) and value:
                    records = value
                    break
    return records


def extract_getrows_tables(response: dict, tables) -> dict[str, list]:
    """Pull the NAMED sibling tables' rows out of a GetRows response.

    ``_extract_getrows_records`` deliberately returns the target table only, so
    a sibling opted in via ``include_tables`` would otherwise be fetched and
    then thrown away. A table present but empty comes back as ``[]`` (an honest
    "no attachments"); a table absent from the DataSet is omitted. There is no
    "first non-empty list" fallback here — for a named sibling that guess is
    wrong by construction.
    """
    if not tables:
        return {}
    data = response.get("returnObj", response)
    if not isinstance(data, dict):
        return {}
    out: dict[str, list] = {}
    for name in tables:
        rows = data.get(name)
        if isinstance(rows, list):
            out[name] = rows
    return out


def _project_select(records: list, select: str) -> list:
    """Client-side column projection (GetRows has no ``$select``)."""
    if not select:
        return records
    wanted = [c.strip() for c in select.split(",") if c.strip()]
    if not wanted:
        return records
    return [
        {k: row.get(k) for k in wanted}
        for row in records
        if isinstance(row, dict)
    ]


# ---------------------------------------------------------------------------
# GetRows executor — lifted UNCHANGED from query.register()._getrows_query,
# renamed run_getrows.
# ---------------------------------------------------------------------------

def alternative_targets(
    index: "ServiceIndex", service: str, entity_set: str, filter: str,
    limit: int = 6,
) -> list[str]:
    """Concrete ``Service/Entity`` targets to retry after an upstream failure.

    The ``upstream_error`` envelope's message tells the model to "try an
    alternative target (valid.alternatives)"; an empty list there is a promise
    with no content. Two sources, both real routes: the entity's
    ``<Entity>SearchSvc`` twin, and the other services that actually own the
    columns named in the filter. Shared so BOTH callers of
    ``epicor_error_envelope`` (this module's GetRows path and read.py's outer
    handler) emit the same shape.
    """
    alternatives: list[str] = []
    twin = f"Erp.BO.{entity_set}SearchSvc"
    try:
        if entity_set in (index.get_entity_sets(twin) or []):
            alternatives.append(f"{twin}/{entity_set}")
    except Exception:  # noqa: BLE001
        pass
    for col in _extract_filter_identifiers(filter or ""):
        try:
            owners = index.find_field_owners(col, limit=3) or []
        except Exception:  # noqa: BLE001
            owners = []
        for o in owners:
            tgt = f'{o["service_id"]}/{o["entity_set_name"]}'
            if tgt != f"{service}/{entity_set}" and tgt not in alternatives:
                alternatives.append(tgt)
    return alternatives[:limit]


async def run_getrows(
    client: "EpicorClient",
    index: "ServiceIndex",
    service: str,
    entity_set: str,
    api_key: str,
    *,
    filter: str,
    select: str,
    orderby: str,
    top: int,
    skip: int = 0,
    count_only: bool,
    group_by: str = "",
    aggregate: str = "",
    distinct: str = "",
    having: str = "",
    order_by: str = "",
    format: str,
    attempted: tuple = ("odata", "getrows"),
    include_tables=None,
) -> str:
    """Fall back to the service's GetRows method.

    Builds table-specific ``whereClause{Table}`` params from the
    service index.  The target entity_set gets the filter; all
    other tables get ``1=0`` to exclude child data.

    Epicor's GetRows does not support OData ``$select``, so when the
    caller passed one we project the requested columns client-side
    after extracting the rows.  Without this projection the response
    carries every field on the target table (often 500+), defeating
    the caller's intent and routinely blowing past the response-size
    budget.

    ``include_tables`` opts specific sibling tables of the same DataSet back
    IN (``""`` instead of ``1=0``) and returns their rows under
    ``related_tables`` — the only route to an ``*Attch`` attachment table,
    which is invisible over OData. Omit it and both the request body and the
    response are byte-identical to before.
    """
    include_resolved: set[str] = set()
    if include_tables:
        include_resolved, include_error = resolve_include_tables(
            index, service, include_tables
        )
        if include_error is not None:
            return json.dumps(include_error)

    where_body, target_table = _build_getrows_where(
        index, service, entity_set, filter, orderby,
        include_tables=include_resolved or None,
    )

    # absolutePage is 1-based and pages are sized by pageSize.
    # OData $skip is a row offset, so translate: page = floor(skip/top)+1.
    # When skip isn't an even multiple of top we still skip a few extra
    # rows client-side after the fetch.
    page = (skip // top) + 1 if skip > 0 else 1
    skip_remainder = skip - (page - 1) * top if skip > 0 else 0
    body: dict = {"pageSize": top, "absolutePage": page, **where_body}

    url = f"{service}/GetRows"
    try:
        response = await client.post(url, api_key, json_body=body)
    except EpicorError as exc:
        # INV-1: a bare {"error": <prose>} with no reason code, no `valid` and
        # no `retry_with` is exactly the dead end that drew six identical
        # retries. It also LIED — a heavy service routes straight to GetRows,
        # so "Neither OData nor GetRows worked" named a path never attempted.
        # `attempted` now carries what actually ran.
        alternatives = alternative_targets(index, service, entity_set, filter)
        # INV-1: the unknown_columns branch of epicor_error_envelope tells the
        # model to "see valid.did_you_mean / valid.columns". Without these it
        # shipped valid.columns=[], did_you_mean={col: []} and a false
        # total_columns=0 — the earlier rejected-names-only bug, pointing the model at
        # an empty list. `index` is already in scope here (used for the
        # alternatives above), and the read.py caller already passes these, so
        # the shape is now uniform across BOTH callers of the classifier.
        try:
            known_cols = [f["field_name"]
                          for f in (index.get_fields(service, entity_set) or [])]
        except Exception:  # noqa: BLE001 — the envelope is still worth sending
            known_cols = []
        payload = epicor_error_envelope(
            exc,
            service=service,
            entity_set=entity_set,
            odata_filter=filter,
            attempted=tuple(attempted),
            alternatives=alternatives,
            valid_columns=known_cols,
            index=index,
        )
        # The correlation GUID is operator data, not a model-fixable fact.
        # Leaving it in the headline made six identical responses look like six
        # different ones. epicor_error_envelope keeps it under detail.message.
        suggestions = _suggest_entity_sets(index, service, entity_set)
        if suggestions and entity_set not in (index.get_entity_sets(service) or []):
            payload["did_you_mean"] = suggestions
        # Empty-500 against a known child entity? Point at the parent.
        if _is_generic_epicor_apology(exc.message or ""):
            pivot = _child_pivot_hint(service, target_table, filter)
            if pivot is not None:
                payload["pivot_hint"] = pivot
            # Empty-500 because the header was filtered by a denormalized
            # customer/vendor name field — steer to name→CustNum resolution.
            resolve = _name_resolution_hint(entity_set, filter)
            if resolve is not None:
                payload["resolve_hint"] = resolve
        # An error on a header/detail table is the strongest "wrong path"
        # signal — surface the single-call join tool here too, not just on
        # success, so a failed probe redirects immediately.
        nudge = _children_tool_nudge(service, entity_set)
        if nudge:
            payload["efficiency_hint"] = nudge
        return json.dumps(payload)

    # Extract the target table's rows from the response dataset.
    records = _extract_getrows_records(response, target_table)

    # Trim the page-alignment remainder when skip isn't a multiple
    # of top (GetRows pages by absolutePage * pageSize).
    if skip_remainder > 0 and len(records) > skip_remainder:
        records = records[skip_remainder:]

    records = _project_select(records, select)

    record_count = len(records)

    if count_only:
        count_payload: dict = {
            "count": record_count,
            "service": service,
            "entity_set": entity_set,
            "filter": filter or "(none)",
            "note": "Count via GetRows (limited by pageSize).",
        }
        nudge = _children_tool_nudge(service, entity_set)
        if nudge:
            count_payload["efficiency_hint"] = nudge
        return format_response(count_payload, records_key=None)

    if group_by or aggregate or distinct:
        try:
            agg_result = aggregate_records(
                records,
                group_by=group_by,
                aggregate=aggregate,
                distinct=distinct,
                having=having,
                order_by=order_by,
            )
        except ValueError as ve:
            return json.dumps(error_envelope(
                "invalid_aggregate", str(ve)))
        if record_count == top:
            # top already caps at 1000 — telling the model to "raise top"
            # here was a dead end that drove tool-call thrash. Be honest.
            agg_result["note"] = f"Aggregated over the first {top} rows only."
        return format_response(agg_result, records_key="records", format=format)

    result: dict = {"records": records, "record_count": record_count}
    related = extract_getrows_tables(response, include_resolved - {target_table})
    if related:
        result["related_tables"] = related
    _warn_having_ignored(result, having, "plain")
    if record_count == top:
        result["note"] = (
            f"Result limited to {top} records. "
            "Increase 'top' or refine your filter to see more."
        )
    if record_count == 0:
        zero = _zero_result_hint(entity_set, filter)
        if zero:
            result["no_match_hint"] = zero
    nudge = _children_tool_nudge(service, entity_set)
    if nudge:
        result["efficiency_hint"] = nudge

    return format_response(result, records_key="records", format=format)


# ---------------------------------------------------------------------------
# Paged aggregation scanners — new here (not lifted from query.py).
#
# The single-page executors above roll group_by/aggregate up over at most ONE
# page of 1000 rows, so full-year rollups ("top parts by sales in 2025") were
# silently truncated at 1000 rows. These scanners loop pages, project the
# select columns per page to bound memory, then aggregate the accumulated set
# — and say honestly whether the scan exhausted the data ("complete") or hit
# the page ceiling.
# ---------------------------------------------------------------------------

# pageSize/$top floor when halving after a timeout.
_MIN_PAGE_SIZE = 250


def _is_timeout_error(exc: EpicorError) -> bool:
    """Timeout-looking EpicorError — the client maps httpx timeouts to 408."""
    return exc.status_code == 408 or "timed out" in (exc.message or "").lower()


def _finish_paged_aggregation(
    all_rows: list,
    *,
    group_by: str,
    aggregate: str,
    distinct: str,
    having: str = "",
    order_by: str = "",
    pages_scanned: int,
    complete: bool,
    error_note: str | None,
    format: str,
) -> str:
    """Aggregate the accumulated rows and stamp the scan metadata.

    Emits the same shape as the single-page aggregation branches plus
    ``scanned_rows`` / ``pages_scanned`` / ``complete``, and a ``note`` when
    the scan did NOT exhaust the data (ceiling hit or a page errored out).
    """
    try:
        agg_result = aggregate_records(
            all_rows,
            group_by=group_by,
            aggregate=aggregate,
            distinct=distinct,
            having=having,
            order_by=order_by,
        )
    except ValueError as ve:
        return json.dumps(error_envelope(
            "invalid_aggregate", str(ve)))
    agg_result["scanned_rows"] = len(all_rows)
    agg_result["pages_scanned"] = pages_scanned
    agg_result["complete"] = complete
    if not complete:
        agg_result["note"] = error_note or (
            f"Scanned {len(all_rows)} rows across {pages_scanned} pages "
            "(scan ceiling); results may be partial - narrow the filter "
            "(e.g. a date range) for exact figures."
        )
        if having and agg_result.get("having"):
            # A threshold over an incomplete scan is exactly the kind of
            # confidently-wrong short list this codebase exists to avoid:
            # groups that DO exceed the threshold can be missing entirely.
            agg_result["note"] += (
                f" The having filter ({agg_result['having']}) was applied to "
                "this PARTIAL scan, so qualifying rows may be missing "
                "— push the criteria into `where` to shrink the scan."
            )
    return format_response(agg_result, records_key="records", format=format)


def _paged_error_note(exc: EpicorError, scanned_rows: int, pages_scanned: int) -> str:
    """Note for a scan cut short by an Epicor error — names the error."""
    return (
        f"Scan stopped by an Epicor error after {scanned_rows} rows across "
        f"{pages_scanned} pages: {exc.message}. Results may be partial - "
        "narrow the filter (e.g. a date range) for exact figures."
    )


async def run_getrows_paged(
    client: "EpicorClient",
    index: "ServiceIndex",
    service: str,
    entity_set: str,
    api_key: str,
    *,
    filter: str,
    select: str,
    orderby: str,
    page_size: int = 1000,
    max_pages: int = 20,
    group_by: str,
    aggregate: str,
    distinct: str = "",
    having: str = "",
    order_by: str = "",
    format: str = "json",
) -> str:
    """Multi-page GetRows scan feeding a group_by/aggregate/distinct rollup.

    Loops ``absolutePage`` until a short page (data exhausted) or the
    ``max_pages`` ceiling, projecting ``select`` client-side per page before
    accumulating so memory stays bounded. On a timeout-looking page error the
    pageSize is halved (floor 250) and that page retried once — pages are
    addressed by row ``offset`` rather than a fixed page index so the
    absolutePage arithmetic survives the resize (a page-alignment remainder
    is trimmed client-side, the same trick run_getrows uses for skip). A page
    that still fails ends the scan with the rows already gathered rather than
    throwing them away.
    """
    page_size = max(1, min(1000, page_size))
    max_pages = max(1, min(50, max_pages))

    where_body, target_table = _build_getrows_where(
        index, service, entity_set, filter, orderby
    )
    url = f"{service}/GetRows"

    async def _fetch_page(offset: int, size: int) -> tuple[list, int]:
        """Fetch the rows starting at row ``offset``; returns (rows, raw_len).

        ``raw_len`` is the untrimmed page length — the short-page test must
        use it, not the trimmed count, or a remainder trim would read as
        end-of-data.
        """
        page = offset // size + 1
        remainder = offset - (page - 1) * size
        body: dict = {"pageSize": size, "absolutePage": page, **where_body}
        response = await client.post(url, api_key, json_body=body)
        records = _extract_getrows_records(response, target_table)
        raw_len = len(records)
        if remainder > 0:
            records = records[remainder:]
        return _project_select(records, select), raw_len

    all_rows: list = []
    pages_scanned = 0
    complete = False
    error_note: str | None = None

    while pages_scanned < max_pages:
        offset = len(all_rows)
        try:
            records, raw_len = await _fetch_page(offset, page_size)
        except EpicorError as exc:
            if not _is_timeout_error(exc):
                if pages_scanned == 0:
                    # Nothing gathered yet — surface the error itself.
                    return json.dumps({
                        "error": (
                            f"Paged scan of {service}/{entity_set} failed on "
                            f"the first page. GetRows error: {exc.message}"
                        )
                    })
                error_note = _paged_error_note(exc, len(all_rows), pages_scanned)
                break
            # Timeout: halve the page size (heavy services can't fill a
            # 1000-row page inside the 30s client timeout) and retry the
            # same offset once. The smaller size sticks for later pages.
            page_size = max(_MIN_PAGE_SIZE, page_size // 2)
            try:
                records, raw_len = await _fetch_page(offset, page_size)
            except EpicorError as exc2:
                if pages_scanned == 0:
                    return json.dumps({
                        "error": (
                            f"Paged scan of {service}/{entity_set} failed on "
                            f"the first page (after a pageSize={page_size} "
                            f"retry). GetRows error: {exc2.message}"
                        )
                    })
                error_note = _paged_error_note(exc2, len(all_rows), pages_scanned)
                break

        all_rows.extend(records)
        pages_scanned += 1
        if raw_len < page_size:
            # Short page — the data is exhausted.
            complete = True
            break

    return _finish_paged_aggregation(
        all_rows,
        group_by=group_by,
        aggregate=aggregate,
        distinct=distinct,
        having=having,
        order_by=order_by,
        pages_scanned=pages_scanned,
        complete=complete,
        error_note=error_note,
        format=format,
    )


async def run_odata_paged(
    client: "EpicorClient",
    service: str,
    entity_set: str,
    api_key: str,
    *,
    filter: str,
    select: str,
    orderby: str,
    page_size: int = 1000,
    max_pages: int = 20,
    expand: str = "",
    group_by: str,
    aggregate: str,
    distinct: str = "",
    having: str = "",
    order_by: str = "",
    format: str = "json",
) -> str:
    """Multi-page OData scan feeding a group_by/aggregate/distinct rollup.

    Same contract as run_getrows_paged, over ``$skip``/``$top`` instead of
    absolutePage — $skip is a plain row offset, so the timeout-halving needs
    no page arithmetic. ``$select`` projects server-side, but we project
    client-side too in case a service ignores it.
    """
    page_size = max(1, min(1000, page_size))
    max_pages = max(1, min(50, max_pages))

    url = f"{service}/{entity_set}"

    async def _fetch_page(offset: int, size: int) -> list:
        params: dict[str, str | int] = {"$top": size}
        if offset > 0:
            params["$skip"] = offset
        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = select
        if orderby:
            params["$orderby"] = orderby
        if expand:
            params["$expand"] = expand
        response = await client.get(url, api_key, params=params)
        records = response.get("value", response)
        if not isinstance(records, list):
            records = []
        return _project_select(records, select)

    all_rows: list = []
    pages_scanned = 0
    complete = False
    error_note: str | None = None

    while pages_scanned < max_pages:
        offset = len(all_rows)
        try:
            records = await _fetch_page(offset, page_size)
        except EpicorError as exc:
            if not _is_timeout_error(exc):
                if pages_scanned == 0:
                    return json.dumps({
                        "error": (
                            f"Paged scan of {service}/{entity_set} failed on "
                            f"the first page. OData error: {exc.message}"
                        )
                    })
                error_note = _paged_error_note(exc, len(all_rows), pages_scanned)
                break
            # Timeout: halve $top and retry the same offset once; the
            # smaller size sticks for later pages.
            page_size = max(_MIN_PAGE_SIZE, page_size // 2)
            try:
                records = await _fetch_page(offset, page_size)
            except EpicorError as exc2:
                if pages_scanned == 0:
                    return json.dumps({
                        "error": (
                            f"Paged scan of {service}/{entity_set} failed on "
                            f"the first page (after a $top={page_size} "
                            f"retry). OData error: {exc2.message}"
                        )
                    })
                error_note = _paged_error_note(exc2, len(all_rows), pages_scanned)
                break

        all_rows.extend(records)
        pages_scanned += 1
        if len(records) < page_size:
            # Short page — the data is exhausted.
            complete = True
            break

    return _finish_paged_aggregation(
        all_rows,
        group_by=group_by,
        aggregate=aggregate,
        distinct=distinct,
        having=having,
        order_by=order_by,
        pages_scanned=pages_scanned,
        complete=complete,
        error_note=error_note,
        format=format,
    )

"""Tool: epicor_query_with_children  (experimental, flag-gated)

Pull a parent entity set **and** one child entity set from the same Epicor
business object in a *single* paged ``GetRows`` call, join them client-side
on the parent key, and optionally roll the result up with the same
in-process aggregation the ``epicor_query`` tool uses.

Why this exists
---------------
Epicor's ``Erp.BO.*`` services do not expose queryable OData collections —
``epicor_query`` silently falls back to ``GetRows``, and that fallback sets
``whereClause{everyOtherTable} = "1=0"`` to avoid dragging in child data.
For a parent/child dataset that makes it **impossible** to bulk-pull the
child rows: zeroing the parent excludes all parents, and child rows only
come back under a matching parent. The practical fallout is that a question
like "open orders for plant 01 broken down by customer / month / part"
forces a ``get_record`` call *per order*: one round trip for every order
in scope.

This tool sets the whereClause on **both** the parent and the child table in
one ``GetRows`` body, pages on the parent (the verified paging axis), and
joins the two tables in memory. The rollup above collapses from one call
per order to a handful.

Scope / limits
--------------
- One parent + one child **in the same BO dataset** (OrderHed+OrderDtl,
  JobHead+JobOper, …). It does NOT join across services — e.g. customer
  *names* live in ``Erp.BO.CustomerSvc`` and need a separate lookup.
- Aggregation is client-side (GetRows cannot group server-side), so a very
  large result still materialises detail rows before rollup. For genuinely
  huge scans, narrow the filter (by site/date/customer) and page — this is
  the BO-only path; BAQs are intentionally not used here because most users
  lack BAQ access.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.response import format_response
from epicor_mcp.tools._aggregate import (
    _sort_key,
    aggregate_records,
    group_by_base_fields,
    parse_aggregates,
    resolve_sort_terms,
)
from epicor_mcp.tools._engine import _MIN_PAGE_SIZE, _is_timeout_error
from epicor_mcp.tools._inline_schema import order_terms_to_clause, parse_order_by
from epicor_mcp.tools.query import _CHILD_TO_PARENT_PIVOT, _odata_to_sql_where

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

# Hard caps so a runaway call can't page forever or blow the response budget.
_MAX_PAGE_SIZE = 1000
_MAX_PAGES = 50

# Epicor date fields come back as ISO datetimes ("2025-10-31T00:00:00"). To
# support "by month"/"by day" rollups without a BAQ, we derive sibling keys
# off any selected date column: <Field>_Month (YYYY-MM) and <Field>_Date
# (YYYY-MM-DD). group_by can then reference e.g. "OrderDate_Month".
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _add_date_buckets(row: dict) -> None:
    """In place: for every ISO-date string value, add _Month/_Date siblings."""
    for key, val in list(row.items()):
        if (
            isinstance(val, str)
            and _ISO_DATE_RE.match(val)
            and f"{key}_Month" not in row
        ):
            row[f"{key}_Year"] = val[:4]
            row[f"{key}_Month"] = val[:7]
            row[f"{key}_Date"] = val[:10]


_DESCRIPTION = """\
Pull a PARENT table plus ONE CHILD table from the same Epicor business object \
in a single paged call, join them, and optionally roll them up — without \
calling epicor_get_record once per record.

USE THIS (instead of looping epicor_get_record) whenever a question needs \
detail/line data for MANY parent records at once, e.g. "open sales orders for \
site 01 broken down by customer, month and part", "all open POs with their \
lines", "open jobs and their operations". One call here replaces hundreds of \
per-record fetches. Reach for it FIRST on any "broken down by / by customer / \
by month / by part / summarize / rollup" question — do NOT pre-probe the \
tables with epicor_query / epicor_describe_service first; the fields below are \
already correct, and on a wrong field name this tool returns the valid list.

WORKED EXAMPLE — "open sales orders from site 01 broken down by customer, \
month and part in Excel" answers in ONE call:
  epicor_query_with_children(
    service="Erp.BO.SalesOrderSvc",
    parent_entity="OrderHed", child_entity="OrderDtl",
    parent_filter="Plant eq '01' and OpenOrder eq true",
    child_filter="OpenLine eq true",
    parent_select="OrderNum,CustomerName,CustomerCustID,OrderDate",
    child_select="OrderLine,PartNum,LineDesc,OrderQty,ExtPriceDtl",
    group_by="CustomerName,OrderDate_Month,PartNum",
    aggregate="sum(ExtPriceDtl) as total, count(*)")
Note the facts baked in: a "site"/"plant" filter is Plant eq '<n>' (no Plant \
lookup needed); "open" = OpenOrder eq true on the header AND OpenLine eq true \
on the line; the customer name is CustomerName; the line value is ExtPriceDtl. \
Then write the joined records to Excel.

Supported parent→child pairs (same dataset, joined on the listed key):
  Erp.BO.SalesOrderSvc : OrderHed → OrderDtl | OrderRel        (OrderNum)
  Erp.BO.POSvc         : POHeader → PODetail | PORel           (PONum)
  Erp.BO.JobEntrySvc   : JobHead  → JobOper | JobMtl | JobAsmbl(JobNum)
  Erp.BO.ARInvoiceSvc  : InvcHead → InvcDtl                    (InvoiceNum)
  Erp.BO.QuoteSvc      : QuoteHed → QuoteDtl | QuoteQty        (QuoteNum)

Filters use OData syntax (e.g. "Plant eq '01' and OpenOrder eq true"). Put \
header conditions in parent_filter and line conditions in child_filter. \
Provide parent_select for the header columns you want carried onto each \
joined line (e.g. CustNum, OrderDate) — group_by can then reference them.

To summarise, pass group_by (e.g. "CustNum,PartNum") and aggregate (e.g. \
"sum(ExtPriceDtl) as total, count(*)"); the rollup runs over the joined \
lines. NOTE the extended-price field on order lines is ExtPriceDtl, not \
DocExtPrice (which is often blank).

For time rollups (by year / month / day): wrap a SELECTED date column in a \
bucketing function in group_by — year(OrderDate), quarter(OrderDate), \
month(OrderDate) (→YYYY-MM), day(OrderDate). So a "year-over-year sales" \
question is group_by="year(OrderDate)" with parent_select including OrderDate. \
Equivalent auto-derived companion keys also exist: <Field>_Year (YYYY), \
<Field>_Month (YYYY-MM), <Field>_Date (YYYY-MM-DD). Group on the raw date \
column only if you want per-exact-timestamp rows.

Parameters: service, parent_entity, child_entity (required); parent_filter, \
child_filter, parent_select, child_select, group_by, aggregate, page_size \
(default 1000), max_pages (optional). There is no "top" parameter — use \
page_size; the tool pages automatically.

Limits: one child table per call; same-BO only (cannot join customer names \
from CustomerSvc — look those up separately); aggregation is client-side."""


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the experimental ``epicor_query_with_children`` tool to *server*."""

    @server.tool(structured_output=False, description=_DESCRIPTION)
    async def epicor_query_with_children(
        service: str,
        parent_entity: str,
        child_entity: str,
        parent_filter: str = "",
        child_filter: str = "",
        parent_select: str = "",
        child_select: str = "",
        group_by: str = "",
        aggregate: str = "",
        having: str = "",
        order_by: str = "",
        page_size: int = 1000,
        max_pages: int = _MAX_PAGES,
        stop_after_child_rows: int = 0,
        format: str = "json",
    ) -> str:
        """Pull parent + one child in one paged GetRows call and join them.

        ``stop_after_child_rows`` (>0, raw listings only — ignored when
        group_by/aggregate is set): stop paging as soon as at least that many
        child rows have been collected, instead of walking every parent page.

        ``order_by`` / ``having`` are applied CLIENT-side over the joined rows
        (GetRows cannot sort a joined projection, and a child-table $orderby
        500s). They are accepted here rather than dropped because this is the
        MANDATORY route for every detail table in ``_CHILD_TO_PARENT_PIVOT`` —
        silently ignoring them returns join-scan order under a caller sort,
        which reads as the correct answer. When the scan did not complete, the
        sort is reported as partial rather than presented as a global top-N.
        """
        try:
            session = get_current_session()

            # --- RBAC ------------------------------------------------------
            allowed, msg = rbac.check_access(session.user_id, service)
            if not allowed:
                return json.dumps({"error": msg})
            svc_result = rbac.check_service_access(session.user_id, service)
            api_key = svc_result.api_key or ""

            # --- Resolve the dataset tables + join key ---------------------
            all_sets = index.get_entity_sets(service) or []
            real_tables = [es for es in all_sets if index.get_fields(service, es)]
            if parent_entity not in real_tables:
                return json.dumps({
                    "error": (
                        f"Parent entity '{parent_entity}' is not a real table on "
                        f"'{service}'."
                    ),
                    "available_tables": real_tables[:25],
                })
            if child_entity not in real_tables:
                return json.dumps({
                    "error": (
                        f"Child entity '{child_entity}' is not a real table on "
                        f"'{service}'."
                    ),
                    "available_tables": real_tables[:25],
                })

            pivot = _CHILD_TO_PARENT_PIVOT.get(child_entity)
            if pivot and pivot[0] == parent_entity:
                join_keys = list(pivot[1])
            else:
                # Fall back to columns present on BOTH tables that look like
                # the parent's identifying keys. Keep it conservative.
                # NOTE: index.get_fields returns a list of field-metadata DICTS,
                # not names — set() over them raises "unhashable type: 'dict'".
                # Reduce to the field-name strings before intersecting.
                pfields = {
                    f.get("field_name")
                    for f in (index.get_fields(service, parent_entity) or [])
                    if f.get("field_name")
                }
                cfields = {
                    f.get("field_name")
                    for f in (index.get_fields(service, child_entity) or [])
                    if f.get("field_name")
                }
                shared = pfields & cfields
                join_keys = [k for k in (f"{parent_entity[:-3]}Num", "OrderNum",
                                         "PONum", "JobNum", "InvoiceNum",
                                         "QuoteNum") if k in shared]
                if not join_keys:
                    return json.dumps({
                        "error": (
                            f"Could not determine a join key between "
                            f"'{parent_entity}' and '{child_entity}'. Pass a "
                            f"recognised parent/child pair."
                        ),
                        "shared_columns": sorted(shared)[:20],
                    })

            # --- Clamp paging knobs ----------------------------------------
            page_size = max(1, min(int(page_size), _MAX_PAGE_SIZE))
            max_pages = max(1, min(int(max_pages), _MAX_PAGES))

            psel = [c.strip() for c in parent_select.split(",") if c.strip()]
            csel = [c.strip() for c in child_select.split(",") if c.strip()]

            # --- Validate select columns against the REAL schema -----------
            # A wrong select column (e.g. CustName for CustomerName) silently
            # rides through: it lands on the joined row as None, so a group_by
            # on it collapses every customer into one bogus null bucket and the
            # caller re-runs the whole heavy join again and again. Catch the
            # bad name up front against the actual field list, with a
            # did_you_mean, so it's fixed in ONE retry — not six 5s re-fetches.
            parent_field_names = {
                f.get("field_name")
                for f in (index.get_fields(service, parent_entity) or [])
                if f.get("field_name")
            }
            child_field_names = {
                f.get("field_name")
                for f in (index.get_fields(service, child_entity) or [])
                if f.get("field_name")
            }

            def _unknown_selects(cols, valid, where):
                out = []
                for c in cols:
                    if c == "*" or c in valid:
                        continue
                    out.append({
                        "ref": c,
                        "where": where,
                        "did_you_mean": difflib.get_close_matches(
                            c, sorted(valid), n=4, cutoff=0.5
                        ),
                    })
                return out

            bad_select = (
                _unknown_selects(psel, parent_field_names, "parent_select")
                + _unknown_selects(csel, child_field_names, "child_select")
            )
            if bad_select:
                return json.dumps({
                    "error": (
                        "Unknown column(s) in parent_select/child_select: "
                        + ", ".join(b["ref"] for b in bad_select)
                        + ". Fix the names (see did_you_mean) and retry. "
                        "Caught before fetching."
                    ),
                    "unknown_columns": bad_select,
                })

            # --- Fail-fast group_by/aggregate validation -------------------
            # The rollup is validated again post-fetch, but doing it here too
            # — against the SCHEMA, before a 50-page GetRows scan — turns a
            # bad group_by into a millisecond error instead of a long
            # fetch followed by a reject. Build the set of columns the
            # joined rows WILL have: selected parent cols (or join keys),
            # selected child cols (or every child field), plus the derived
            # _Year/_Month/_Date date companions on each.
            if group_by or aggregate:
                expected = set(psel or join_keys)
                expected |= set(csel) if csel else child_field_names
                for col in list(expected):
                    expected |= {f"{col}_Year", f"{col}_Month", f"{col}_Date"}
                requested = group_by_base_fields(group_by)
                try:
                    for ag in parse_aggregates(aggregate):
                        if ag["field"] != "*":
                            requested.append(ag["field"])
                except ValueError as ve:
                    return json.dumps({"error": str(ve)})
                unknown = [c for c in requested if c not in expected]
                if unknown:
                    return json.dumps({
                        "error": (
                            f"Unknown field(s) in group_by/aggregate: "
                            f"{', '.join(unknown)}. Caught before fetching."
                        ),
                        "available_fields": sorted(expected),
                        "hint": (
                            "Use only fields the joined rows will carry. To "
                            "group a header column, add it to parent_select. "
                            "For time rollups wrap a selected date column: "
                            "group_by=\"year(OrderDate)\" (also month/quarter/"
                            "day), or use the OrderDate_Year / _Month / _Date "
                            "companions."
                        ),
                    })

            where_parent = _odata_to_sql_where(parent_filter)
            where_child = _odata_to_sql_where(child_filter)

            # --- Page on the PARENT (verified paging axis) -----------------
            # On a timeout-looking page error the page size is halved (floor
            # _MIN_PAGE_SIZE) and the SAME parent offset retried — heavy
            # header tables can't fill a 1000-row page inside the 30s client
            # timeout. Halving only happens while the new size still aligns
            # with the rows already fetched (offset % new_size == 0): a
            # misaligned re-fetch would re-pull parents we already hold and
            # DUPLICATE their child rows. A page that still fails ends the
            # scan with the rows already gathered (labeled partial) instead
            # of throwing them away.
            parent_rows: list[dict] = []
            child_rows: list[dict] = []
            pages_fetched = 0
            complete = False
            early_stop = False
            scan_error: str | None = None
            child_key_seen = False  # did the child table ever appear at all?
            while pages_fetched < max_pages:
                offset = len(parent_rows)
                page = offset // page_size + 1
                body: dict = {"pageSize": page_size, "absolutePage": page}
                for es in real_tables:
                    if es == parent_entity:
                        body[f"whereClause{es}"] = where_parent
                    elif es == child_entity:
                        body[f"whereClause{es}"] = where_child
                    else:
                        body[f"whereClause{es}"] = "1=0"

                try:
                    resp = await client.post(
                        f"{service}/GetRows", api_key, json_body=body
                    )
                except EpicorError as exc:
                    if _is_timeout_error(exc):
                        half = max(_MIN_PAGE_SIZE, page_size // 2)
                        if half < page_size and offset % half == 0:
                            page_size = half
                            continue  # retry the same offset, smaller page
                    if parent_rows or child_rows:
                        scan_error = (
                            f"GetRows failed on page {page}: {exc.message}. "
                            f"Stopped with the {len(parent_rows)} parent / "
                            f"{len(child_rows)} child row(s) already fetched "
                            "— results are PARTIAL."
                        )
                        break
                    return json.dumps({
                        "error": (
                            f"GetRows failed on {service} (page {page}): "
                            f"{exc.message}"
                        )
                    })

                data = resp.get("returnObj", resp) if isinstance(resp, dict) else resp
                if isinstance(data, dict) and child_entity in data:
                    child_key_seen = True
                page_parents = (data or {}).get(parent_entity, []) or []
                page_children = (data or {}).get(child_entity, []) or []
                parent_rows.extend(page_parents)
                child_rows.extend(page_children)
                pages_fetched += 1

                # Paging is parent-driven: a short parent page is the last one.
                if len(page_parents) < page_size:
                    complete = True
                    break
                # Raw listings can stop as soon as enough child rows are in
                # hand — never cut a rollup short this way (its total would
                # silently be wrong).
                if (stop_after_child_rows > 0
                        and not (group_by or aggregate)
                        and len(child_rows) >= stop_after_child_rows):
                    early_stop = True
                    break
            truncated = (
                not complete and not early_stop and scan_error is None
                and pages_fetched >= max_pages
            )

            # --- Build the parent lookup + join ----------------------------
            # Resolve the join columns SEPARATELY on each side. Epicor detail
            # tables don't always spell the key the same as the header — e.g.
            # POHeader.PONum vs PODetail.PONUM (all caps). A literal
            # row.get("PONum") on the child then returns None for every line,
            # so nothing joins and the tool silently returns empty. Match the
            # actual column name case-insensitively against each row's keys.
            _MISSING = object()

            def _resolve_keycols(rows: list[dict]) -> list[str | None]:
                if not rows:
                    return list(join_keys)
                lower = {k.lower(): k for k in rows[0]}
                return [lower.get(jk.lower()) for jk in join_keys]

            parent_keycols = _resolve_keycols(parent_rows)
            child_keycols = _resolve_keycols(child_rows)

            def _norm(v):
                # Normalise so an int header key matches a str child key
                # (and vice-versa); keep None distinct so absent keys never
                # collide into a false match.
                return None if v is None else str(v).strip()

            def _key(row: dict, cols: list[str | None]) -> tuple:
                return tuple(
                    _norm(row.get(c)) if c is not None else _MISSING
                    for c in cols
                )

            def key_p(row: dict) -> tuple:
                return _key(row, parent_keycols)

            def key_c(row: dict) -> tuple:
                return _key(row, child_keycols)

            parent_index: dict[tuple, dict] = {}
            for prow in parent_rows:
                parent_index[key_p(prow)] = prow

            joined: list[dict] = []
            orphans = 0
            for crow in child_rows:
                prow = parent_index.get(key_c(crow))
                if prow is None:
                    orphans += 1
                    continue
                # Parent fields requested (psel) merged first; child wins on
                # any name collision (the child line is the grain).
                merged: dict = {}
                if psel:
                    merged.update({k: prow.get(k) for k in psel})
                else:
                    merged.update({k: prow.get(k) for k in join_keys})
                if csel:
                    merged.update({k: crow.get(k) for k in csel})
                else:
                    merged.update(crow)
                # Derive YYYY-MM / YYYY-MM-DD keys so group_by can bucket by
                # month or day (e.g. group_by="CustNum,OrderDate_Month,PartNum").
                _add_date_buckets(merged)
                joined.append(merged)

            meta = {
                "service": service,
                "parent_entity": parent_entity,
                "child_entity": child_entity,
                "join_keys": join_keys,
                "join_columns_resolved": {
                    parent_entity: parent_keycols,
                    child_entity: child_keycols,
                },
                "parent_rows": len(parent_rows),
                "parent_rows_with_no_child": len(
                    {key_p(p) for p in parent_rows}
                    - {key_c(c) for c in child_rows}
                ),
                "child_rows": len(child_rows),
                "child_table_in_dataset": child_key_seen,
                "joined_rows": len(joined),
                "orphan_child_rows": orphans,
                "pages_fetched": pages_fetched,
            }

            # Loud failure instead of a silent empty answer. If child rows
            # came back but none (or few) joined, the join key almost
            # certainly didn't resolve on one side — never let that pass as
            # a real result.
            if None in parent_keycols or None in child_keycols:
                missing_side = []
                if None in parent_keycols:
                    missing_side.append(parent_entity)
                if None in child_keycols:
                    missing_side.append(child_entity)
                meta["warning"] = (
                    f"Join key {join_keys} could not be located on "
                    f"{', '.join(missing_side)} (resolved columns: "
                    f"{parent_keycols} / {child_keycols}). Results are NOT "
                    "reliable — the parent/child pair or join key is wrong."
                )
            elif parent_rows and not child_rows:
                meta["warning"] = (
                    f"Got {len(parent_rows)} '{parent_entity}' rows but ZERO "
                    f"'{child_entity}' rows. Either nothing matched "
                    f"child_filter, OR this business object's GetRows "
                    f"populates only the '{parent_entity}' header and not "
                    f"'{child_entity}' (e.g. JobEntrySvc returns JobHead but "
                    "not JobOper/JobMtl). If you expected lines, confirm with "
                    f"epicor_query on '{child_entity}' directly, then fetch "
                    f"the lines per '{parent_entity}' key. This is NOT "
                    "confirmed 'no data'."
                )
            elif child_rows and orphans == len(child_rows):
                meta["warning"] = (
                    f"All {orphans} child rows failed to match a parent — the "
                    "join produced ZERO rows. Do not treat this as 'no data'. "
                    "Likely the parent page and child rows are misaligned or "
                    "the key values differ; narrow parent_filter so the "
                    "matching parents and children land on the same page."
                )
            elif child_rows and orphans > len(child_rows) // 5:
                meta["warning"] = (
                    f"{orphans} of {len(child_rows)} child rows did not match "
                    "any parent (likely paged past their parent). Raise "
                    "page_size or narrow the filter for a complete join."
                )

            if truncated:
                meta["truncated"] = (
                    f"Hit max_pages={max_pages} (page_size={page_size}); "
                    "results may be incomplete. Narrow the filter or raise "
                    "max_pages."
                )
            if scan_error:
                meta["scan_error"] = scan_error
            if early_stop:
                meta["stopped_early"] = (
                    f"Stopped paging after {pages_fetched} page(s) once "
                    f"{len(child_rows)} child row(s) (>= the requested "
                    f"{stop_after_child_rows}) were collected; more parents "
                    "exist beyond this point."
                )

            # --- Optional client-side rollup over joined lines -------------
            if group_by or aggregate:
                # Pre-validate referenced fields. aggregate_records does NOT
                # error on an unknown field — it silently buckets the missing
                # key as None, which collapses the rollup into one bogus group
                # (the "month dimension silently broke" failure). Catch that
                # here and hand back the real field names so Claude fixes it
                # in ONE retry instead of shipping plausible-but-wrong totals.
                available = set(joined[0].keys()) if joined else set()
                requested = group_by_base_fields(group_by)
                try:
                    for ag in parse_aggregates(aggregate):
                        if ag["field"] != "*":
                            requested.append(ag["field"])
                except ValueError as ve:
                    return json.dumps({"error": str(ve), **meta})
                unknown = [c for c in requested if c not in available]
                if unknown and joined:
                    return json.dumps({
                        "error": (
                            f"Unknown field(s) in group_by/aggregate: "
                            f"{', '.join(unknown)}. They are not on the joined "
                            "rows, so the rollup would silently collapse."
                        ),
                        "available_fields": sorted(available),
                        "hint": (
                            "Use only fields from available_fields. To carry a "
                            "header column into the rollup, add it to "
                            "parent_select. For month/day rollups use the "
                            "<DateField>_Month / <DateField>_Date companions "
                            "(e.g. OrderDate_Month — select OrderDate first)."
                        ),
                        **meta,
                    })
                try:
                    agg = aggregate_records(
                        joined, group_by=group_by, aggregate=aggregate,
                        having=having, order_by=order_by,
                    )
                except ValueError as ve:
                    return json.dumps({
                        "error": str(ve),
                        "available_fields": sorted(available),
                        **meta,
                    })
                # Null-collapse guard: a group_by column can be a REAL field
                # yet blank on every joined row (e.g. a related/denormalized
                # field GetRows doesn't populate on the line). Schema
                # validation passes, but the rollup silently collapses into one
                # null bucket — the failure that drives "re-run the heavy join
                # again". Surface it loudly instead of returning a bogus total.
                out_rows = agg.get("records", [])
                if out_rows and len(joined) > 1:
                    collapsed = [
                        lbl for lbl in agg.get("group_by", [])
                        if all(r.get(lbl) in (None, "") for r in out_rows)
                    ]
                    if collapsed:
                        agg["rollup_warning"] = (
                            f"group_by column(s) {collapsed} were null/blank on "
                            f"every one of {len(joined)} joined rows, so they "
                            "did NOT split the data — this rollup is collapsed "
                            "and the totals are not broken out as intended. That "
                            "field is empty on these rows (often a related field "
                            "not carried onto the line). Inspect a sample joined "
                            "row for the populated equivalent, carry a header "
                            "field via parent_select, or drop that dimension."
                        )
                agg.update(meta)
                return format_response(agg, records_key="records", format=format)

            # --- Caller sort over the raw joined rows ----------------------
            # Never silent: an unresolvable sort column is an INV-1 error, and
            # a sort over an INCOMPLETE scan is labelled as such instead of
            # being passed off as a global top-N.
            if order_by.strip():
                available = sorted({k for r in joined for k in r})
                sort_terms, kind = parse_order_by(order_by)
                if kind == "expression" or not sort_terms:
                    return json.dumps({
                        "error": "order_expression_unsupported",
                        "message": (
                            "order_by takes plain columns only ('Col', "
                            "'Col desc', comma-separated). To rank by a "
                            "computed measure use group_by + aggregate and "
                            "order_by the aggregate's alias."),
                        **meta,
                    })
                terms, bad_sort = resolve_sort_terms(sort_terms, available)
                if bad_sort:
                    return json.dumps({
                        "error": "unknown_columns",
                        "message": (
                            f"order_by names column(s) {bad_sort} that are not "
                            f"on the joined {parent_entity}/{child_entity} row. "
                            "Use a name from available_fields, and add it to "
                            "parent_select/child_select if it is projected away."),
                        "available_fields": available,
                        **meta,
                    })
                for col, direction in reversed(terms):
                    desc = direction == "desc"
                    joined.sort(key=lambda r, c=col: _sort_key(r.get(c)),
                                reverse=desc)
                    if desc:
                        # reverse=True floats _sort_key's blank group (1, ...)
                        # to the TOP, so a "top 3 by measure desc" join that is
                        # then trimmed to `limit` returned 3 EMPTY rows. Keep
                        # blanks last, matching _inline_schema.sort_records.
                        joined = ([r for r in joined
                                   if not (r.get(col) is None or r.get(col) == "")]
                                  + [r for r in joined
                                     if r.get(col) is None or r.get(col) == ""])
                meta["order"] = order_terms_to_clause(terms)
                meta["order_source"] = "caller"
                if not complete:
                    meta["order_warning"] = (
                        f"Sorted by {order_terms_to_clause(terms)} over the "
                        f"{len(joined)} joined row(s) actually scanned — the "
                        "scan did NOT reach the end of the set, so this is NOT "
                        "a global top-N. Narrow `where` until the scan "
                        "completes before trusting the ranking.")

            result = {"records": joined, **meta}
            return format_response(result, records_key="records", format=format)

        except Exception:
            logger.exception("epicor_query_with_children failed")
            return json.dumps({
                "error": (
                    f"query_with_children against {service} "
                    f"({parent_entity}->{child_entity}) failed."
                )
            })

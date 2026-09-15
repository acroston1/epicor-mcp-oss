"""Tool: epicor_read — the one read tool (legacy engine).

Absorbs the earlier discover / describe / explore / query / get_record /
query_with_children / dashboard_baq into a single intent tool. The body wires
together the shared engine libraries:

* ``_resolve`` — fuzzy ``target``/``fields`` resolution + the uniform INV-1
  self-correcting error envelope.
* ``_heavy``   — INV-2 routing: heavy services never take the direct-OData path.
* ``_engine``  — ``run_odata`` / ``run_getrows`` executors + SQL→OData /
  column-validation helpers (single-sourced from ``query.py``).
* ``query_with_children`` — reused verbatim (captured, not re-implemented) for
  the single-call parent/child join behind the ``children`` argument.

Contract highlights (legacy read contract):
  * one business question -> one call;
  * bad ``field``/``target``/``entity`` comes back as a correctable envelope
    whose ``valid`` carries the REAL names (INV-1);
  * heavy services auto-route through GetRows (INV-2);
  * pagination is an opaque ``cursor`` (base64 of skip + context), never a
    model-managed ``skip``.
"""

from __future__ import annotations

import base64
import difflib
import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.context import get_arg_notes, get_current_session
from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools import query as _query
from epicor_mcp.tools import query_with_children as _qwc
from epicor_mcp.tools._aggregate import (
    _compile_group_col,
    group_by_base_fields,
    parse_aggregates,
    parse_expr_fields,
    resolve_sort_terms,
    unbounded_rollup_refusal,
)
from epicor_mcp.tools._tenant import PLANTS, match_plant, plant_lines
from epicor_mcp.tools._engine import (
    alternative_targets,
    date_columns_for,
    getrows_services,
    name_resolution_hint,
    run_getrows,
    run_getrows_paged,
    run_odata,
    run_odata_paged,
    sql_to_odata,
    suggest_entities,
    validate_columns,
)
from epicor_mcp.tools._heavy import is_heavy
from epicor_mcp.tools._inline_schema import (
    default_order_clause,
    order_refusal,
    order_terms_to_clause,
    parse_order_by,
    sort_records,
)
from epicor_mcp.tools._attachments import detect_attachments, read_attachments
from epicor_mcp.tools._partviews import detect_part_view, read_bom, read_timephase
from epicor_mcp.tools._whereused import detect_where_used, where_used
from epicor_mcp.tools._screens import (
    ScreenMap,
    detect_screen_query,
    screen_discovery,
    screens_for_term,
)
from epicor_mcp.tools._yield import detect_yield_trend, yield_trend
from epicor_mcp.tools._planner import (
    detect_planner_jobs,
    detect_planner_roster,
    planner_jobs,
    planner_roster,
)
from epicor_mcp.tools._posugg import detect_po_sugg, po_suggestions
from epicor_mcp.tools._analytics import (
    is_count_query,
    match_analytical,
    parse_time_window,
    parse_top_n,
    subjects_without_measure,
)
from epicor_mcp.tools._resolve import (
    _clip,
    coerce_csv,
    column_help,
    epicor_error_envelope,
    error_envelope,
    resolve_fields,
    resolve_target,
    unknown_columns_envelope,
)

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.dataset_handler import DatasetHandler
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_MAX_LIMIT = 1000
# Default page when the caller passes neither `limit` nor `top`. A small page
# next to a satisfied-looking summary reads as "that's all the data" and the
# model reports an incomplete answer as done, which is dangerous for part lists.
_DEFAULT_LIMIT = 100

_DESCRIPTION = (
    "Read Epicor data in ONE call — the go-to for any question about ERP data. "
    "Parameters are exactly: target, fields, where, children, group_by, "
    "aggregate, having, order_by, limit/top, count_only, cursor. `fields` (NOT "
    "`select`) prunes columns and is worth passing — wide tables return 300+ "
    "columns. `order_by=\"DueDate asc\" | \"UnitCost desc\"` sorts the WHOLE "
    "set server-side, so order_by + limit is a true top-N; to rank by a "
    "computed measure use group_by/aggregate and order_by its alias. "
    "BUT if the user points you at a DASHBOARD (says 'dashboard', or names one "
    "like 'Sample Sales Overview'), STOP — do NOT read tables here; that is "
    "epicor_baq action='dashboard', which runs the BAQs behind it. "
    "Give a business term, entity, or 'Service/Entity' as `target` "
    "(fuzzy-resolved); the tool picks OData/GetRows for you and never 500s on "
    "heavy services. For 'top/biggest/most/total X by Y' (e.g. 'top 5 parts by "
    "quantity shipped', 'biggest customers last quarter', 'spend by vendor') just "
    "pass that as `target` — it finds the right table, applies the timeframe, "
    "and ranks for you, no BAQ needed. 'Time phase for part X' and 'BOM/bill of "
    "materials for part X' are each ONE call — pass that as `target` (add "
    "where=\"Plant='..'\" or \"RevisionNum='..'\" to scope). 'What is part X "
    "USED TO MAKE / used in / where used' (the inverse — its parents) is also "
    "ONE call: pass that as `target`. The ATTACHMENT / PDF / scanned document "
    "linked to a record is ONE call as well — target=\"invoice attachments\" "
    "(job / part / PO / order / customer attachments too; a bare \"invoice\" "
    "is the CUSTOMER A/R invoice, so say \"AP invoice attachments\" for a "
    "supplier bill) plus a `where` naming the record, e.g. "
    "where=\"GroupID = 'GROUP001'\" — that one call covers a whole GROUP of "
    "records, so never loop record-by-record. Say \"READ the invoice PDF\" "
    "and the extracted text comes back with the file paths. 'Whose part is X / "
    "which customer is part X for / all parts for customer Y' is answered by "
    "your installation's configured customer attribution field — "
    "NEVER inferred from the part number or description. 'What's on "
    "PO/order/job/invoice N?' is "
    "ONE call: target the header with where=\"PONum = N\" and the lines come "
    "back automatically. `group_by`/`aggregate` rollups auto-scan past the "
    "1000-row page cap and are the way to build a grouped report with NO BAQ: "
    "e.g. open sales orders by customer, by month, by part = target the header "
    "(OrderHed) with group_by=\"CustomerName, month(OrderDate), PartNum\" and "
    "aggregate=\"sum(OrderQty) as qty, sum(DocExtPriceDtl) as amount\". Group a "
    "date by period with month()/quarter()/year()/day() around the date column; "
    "child columns (PartNum, OrderQty) are auto-joined from the header — you do "
    "NOT pass `children` or know which table owns them. aggregate takes "
    "'sum(Col) as name', the 'Col:sum' shorthand, or ARITHMETIC — "
    "aggregate=\"sum(OnHandQty * AvgCost) as InventoryValue\" with "
    "having=\"InventoryValue > 50000\" to threshold the groups. `where` also "
    "takes arithmetic (\"OnHandQty * AvgCost > 50000\"), BETWEEN, and IS NULL; "
    "prefer filtering in `where` over `having` so the scan stays small. "
    "A rollup over a TRANSACTION table (orders, jobs, invoices, POs, "
    "receipts, shipments, PartTran) MUST carry a bounding `where` — a date "
    "window like \"OrderDate >= '2025-07-01'\" is enough; unbounded it is "
    "refused up front, because that scan runs for minutes and truncates "
    "anyway. For 'how many / count of' "
    "set `count_only=true` to get "
    "the true total. `fields`/`where` are fuzzy-resolved; blank fields => a "
    "curated default set. Join a child with `children`; `limit` (alias `top`) "
    "caps rows per page — default 100, max 1000 — and raw listings page with "
    "the opaque `cursor` it returns. Bad field/target/entity comes back as a "
    "correctable envelope with valid names. If the user tells you to use, look "
    "at, or pull from a DASHBOARD, do NOT build a table read — that is "
    "epicor_baq action='dashboard' (with the dashboard name, or no name to "
    "list them). Configured SITES/plants: " + plant_lines() + ". "
    "A site name is a Plant filter. Use administrator-configured names or "
    "look up codes in Epicor; do not guess."
)


# Header entity → (natural key, human label) for the single-record fast path:
# a `where` that is exactly one equality on the pair's key auto-joins the
# default child, so "what's on PO 10001" is ONE call (header + lines).
_SINGLE_RECORD_KEYS: dict[str, tuple[str, str]] = {
    "POHeader": ("PONum", "PO"),
    "OrderHed": ("OrderNum", "Order"),
    "JobHead": ("JobNum", "Job"),
    "InvcHead": ("InvoiceNum", "Invoice"),
    "QuoteHed": ("QuoteNum", "Quote"),
}

# Contact CHILD entity_set -> how to serve a "contacts for <parent>" read.
# Vendor/customer contacts are NOT reachable through their own OData collection
# or a GetRows on the child table — both return zero even for a parent that has
# contacts (``VendCnts?$filter=VendorNum eq N`` returns 0 for a vendor that
# has contacts, and N is not even in the collection). They come back
# ONLY inside the parent's GetByID dataset — the exact path the Epicor UI's
# Contacts sheet uses. So a contact read resolves the parent number (from a
# number, ID, or name in `where`), calls <service>/GetByID, and returns the
# child rows. See _read_contacts.
# `strategy` picks how the child rows are fetched once the parent number is
# pinned. VENDOR contacts ride the parent's GetByID dataset (`VendCnt` child) —
# the child collection returns 0. CUSTOMER contacts are the opposite: the
# CustomerSvc GetByID dataset does not supply a CustCnt child; the dedicated
# Erp.BO.CustCntSvc/CustCnts collection returns contacts keyed by CustNum.
# Customers use `collection`; vendors keep `getbyid`. Parent-name -> number resolution
# still runs against the parent (`service`/`collection`) for both.
_CONTACT_PARENTS: dict[str, dict[str, str]] = {
    "VendCnt": {
        "service": "Erp.BO.VendorSvc", "collection": "Vendors",
        "key": "VendorNum", "id_col": "VendorID", "label": "vendor",
        "strategy": "getbyid",
        "getbyid_param": "vendorNum", "child_table": "VendCnt",
    },
    "CustCnt": {
        "service": "Erp.BO.CustomerSvc", "collection": "Customers",
        "key": "CustNum", "id_col": "CustID", "label": "customer",
        "strategy": "collection",
        "contact_service": "Erp.BO.CustCntSvc", "contact_collection": "CustCnts",
        "child_table": "CustCnt",
    },
}

# Curated columns for a contacts read when the caller didn't name fields.
_CONTACT_DEFAULT_FIELDS = [
    "Name", "FirstName", "LastName", "ContactTitle", "Func",
    "PhoneNum", "CellPhoneNum", "FaxNum", "EmailAddress", "ConNum",
    "PrimaryContact",
]

# One equality and NOTHING else — SQL ("PONum = 10001") or OData
# ("PONum eq 10001") spelling, quoted or bare value.
_SINGLE_EQ_RE = re.compile(
    r"^\s*\[?([A-Za-z_]\w*)\]?\s*(?:=|\beq\b)\s*('[^']*'|[\w.\-]+)\s*$",
    re.IGNORECASE,
)

# year(X) cmp N — Epicor's OData layer has no year() function and GetRows'
# whereClause parser rejects it, so the model's natural
# "year(InvoiceDate) = 2025" must become a date range before translation.
_YEAR_FN_RE = re.compile(
    r"year\s*\(\s*\[?([A-Za-z_]\w*)\]?\s*\)\s*(>=|<=|=|>|<|ge|le|eq|gt|lt)\s*(\d{4})",
    re.IGNORECASE,
)


def _expand_year_fn(where: str) -> str:
    """Pre-pass: rewrite ``year(X) = N`` (and >=/<= variants, SQL or OData
    spelling) as the equivalent explicit date-range clause."""
    def _repl(m: re.Match) -> str:
        col, op, year = m.group(1), m.group(2).lower(), int(m.group(3))
        lo = f"{col} ge {year}-01-01T00:00:00"
        hi = f"{col} le {year}-12-31T23:59:59"
        if op in ("=", "eq"):
            return f"({lo} and {hi})"
        if op in (">=", "ge"):
            return lo
        if op in ("<=", "le"):
            return hi
        if op in (">", "gt"):
            return f"{col} ge {year + 1}-01-01T00:00:00"
        return f"{col} le {year - 1}-12-31T23:59:59"  # < / lt
    return _YEAR_FN_RE.sub(_repl, where or "")


def _strip_filter_literals(expr: str) -> str:
    """Blank out quoted strings and date literals before time-window sniffing.

    ``parse_time_window``'s bare-year regex must never fire on the year inside
    a date literal (``InvoiceDate >= '2024-07-01'``) or a part number
    (``PartNum eq 'X-2024-B'``) — that silently ANDed a bogus calendar-year
    window onto analytic scans and mislabeled the scope.
    """
    if not expr:
        return ""
    # Single-quoted literals ('' is the escaped quote).
    expr = re.sub(r"'(?:''|[^'])*'", " ", expr)
    # Bare ISO date / datetime literals (e.g. from _expand_year_fn output).
    expr = re.sub(r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+)?", " ", expr)
    return expr


def _rollup_sort_keys(group_by: str, aggregate: str) -> list[str]:
    """The only keys a rollup result can be ranked by.

    Mirrors exactly what ``aggregate_records`` puts on an output row: the
    compiled group-key LABELS (``month(OrderDate)`` -> ``OrderDate_Month``)
    plus each aggregate's alias. Validating order_by against the entity's raw
    columns instead would accept a name that no longer exists on the grouped
    rows, which is how a refused rank got labelled as applied.
    """
    keys: list[str] = []
    for c in (group_by or "").split(","):
        if c.strip():
            try:
                keys.append(_compile_group_col(c)[0])
            except Exception:  # noqa: BLE001 — validation is best-effort
                keys.append(c.strip())
    aggs = parse_aggregates(aggregate or "") if aggregate else []
    keys.extend(a["alias"] for a in aggs)
    if not aggs:
        keys.append("count")   # the implicit count(*) a bare group_by gets
    return keys


def _rollup_columns(group_by: str, aggregate: str) -> list[str]:
    """Base column names a group_by/aggregate rollup needs on the joined rows.

    Unwraps ``year(X)``-style bucketing functions and the derived
    ``_Year``/``_Month``/``_Date`` companion keys back to the real column, so
    the join can carry exactly those columns (partitioned to whichever table
    owns them).
    """
    cols = list(group_by_base_fields(group_by or ""))
    try:
        for a in parse_aggregates(aggregate or ""):
            # An expression agg needs EVERY component on the projection. Miss
            # one and the paged scanner projects it away, the rollup reads
            # None on every row and collapses into a single null bucket
            # stamped complete=true — a confidently wrong answer.
            if a.get("expr"):
                cols += parse_expr_fields(a["expr"])
            elif a["field"] != "*":
                cols.append(a["field"])
    except ValueError:
        pass  # malformed aggregate — the join tool reports it with the fix
    out: list[str] = []
    for c in cols:
        base = c
        for suffix in ("_Year", "_Month", "_Date"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base and base not in out:
            out.append(base)
    return out


def _rollup_reaches_child(parent_cols: set[str], group_by: str, aggregate: str) -> bool:
    """True when a rollup references a column the HEADER table doesn't have.

    Such a column lives on the detail table (``OrderHed`` grouped by
    ``OrderDtl.PartNum`` / summing ``OrderQty``), so the header/detail join must
    be auto-added — otherwise the single-table validation rejects it as
    ``unknown_columns`` and the model thrashes. Date buckets (``month(X)``) are
    unwrapped to the base column by ``_rollup_columns`` before the check.
    """
    if not parent_cols:
        return False
    have = {c.lower() for c in parent_cols}
    return any(
        c.lower() not in have for c in _rollup_columns(group_by, aggregate))


def _real_date_columns(index, service: str, entity_set: str, columns) -> list[str]:
    """The entity's columns whose Edm type really is a date, real casing kept.

    ``date_columns_for`` answers in lowercase (and ``None`` when the entity is
    unindexed); the rollup guard offers one of these back inside a ``where``,
    so it must hand out the exact spelling Epicor accepts. Name-driven picking
    is wrong here for the same reason it is wrong in the filter rewriter — 73
    Epicor ``%Date`` fields are ``Edm.String``.
    """
    dcols = date_columns_for(index, service, entity_set) or frozenset()
    return [c for c in (columns or ()) if c.lower() in dcols]


def _recase_columns(expr: str, cols: set[str]) -> str:
    """Rewrite identifiers in an OData filter to the exact casing in *cols*.

    OData property names are case-sensitive and Epicor detail tables don't
    always spell a key the way the model does (``PODetail.PONUM`` vs the
    header's ``PONum``) — a mis-cased ``$filter`` 400s where the
    case-insensitive GetRows whereClause would have worked.
    """
    if not expr or not cols:
        return expr
    lower = {c.lower(): c for c in cols}

    def _sub(m: re.Match) -> str:
        return lower.get(m.group(0).lower(), m.group(0))

    # Only touch identifier tokens OUTSIDE quoted literals.
    parts = re.split(r"('(?:''|[^'])*')", expr)
    return "".join(
        part if i % 2 else re.sub(r"[A-Za-z_]\w*", _sub, part)
        for i, part in enumerate(parts)
    )


# ---------------------------------------------------------------------------
# Opaque pagination cursor  (INV-3: server-managed skip, not model-managed)
# ---------------------------------------------------------------------------

def _encode_cursor(ctx: dict) -> str:
    """base64(json) of {skip + originating args} so a resume is self-contained."""
    try:
        return base64.urlsafe_b64encode(
            json.dumps(ctx, default=str).encode("utf-8")
        ).decode("ascii")
    except Exception:  # pragma: no cover - never worth failing a read over
        return ""


def _decode_cursor(cursor: str) -> dict:
    """Inverse of :func:`_encode_cursor`; ``{}`` on any malformed token."""
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Reuse of the parent/child join tool (captured, NOT re-implemented)
# ---------------------------------------------------------------------------

def _capture_children_fn(index, rbac, client):
    """Register ``query_with_children`` onto a capture shim and return its
    inner coroutine so ``epicor_read`` can drive the SAME join logic without
    exposing it as a separate MCP tool through the same read interface."""
    captured: dict = {}

    class _Shim:
        def tool(self, *args, **kwargs):
            def deco(fn):
                captured["fn"] = fn
                return fn
            return deco

    try:
        _qwc.register(_Shim(), index, rbac, client)
    except Exception:  # pragma: no cover
        logger.exception("failed to capture query_with_children engine fn")
    return captured.get("fn")


def _match_child(index, service: str, term: str, default_child: str) -> str:
    """Map the ``children`` argument to a real child table on *service*.

    A generic word ("lines", "details") falls back to the dataset's preferred
    child; an explicit real table name (case-insensitive) is honoured.
    """
    term = (term or "").strip()
    if not term:
        return default_child
    try:
        real = [
            es for es in (index.get_entity_sets(service) or [])
            if index.get_fields(service, es)
        ]
    except Exception:
        real = []
    for es in real:
        if es.lower() == term.lower():
            return es
    return default_child


def _has_real_table(index, service: str, entity_set: str) -> bool:
    """True when *entity_set* is a real DataSet table (has indexed fields) —
    i.e. a valid GetRows target."""
    try:
        return bool(index.get_fields(service, entity_set))
    except Exception:
        return False


def _field_names(index, service: str, entity_set: str) -> set[str]:
    """Real column names on service/entity_set (empty set when unindexed)."""
    try:
        return {
            f.get("field_name")
            for f in (index.get_fields(service, entity_set) or [])
            if f.get("field_name")
        }
    except Exception:
        return set()


def _partition_fields(
    requested: list[str],
    parent_cols: set[str],
    child_cols: set[str],
    *,
    prefer_child: bool,
) -> tuple[list[str], list[str], list[str]]:
    """Split requested field names by table residency (case-insensitive).

    A field on BOTH tables lands on the preferred side (the table the caller
    actually targeted); a field on neither goes to ``unknown``.
    """
    plow = {c.lower(): c for c in parent_cols}
    clow = {c.lower(): c for c in child_cols}
    psel: list[str] = []
    csel: list[str] = []
    unknown: list[str] = []
    for name in requested:
        p = plow.get(name.lower())
        c = clow.get(name.lower())
        if p is None and c is None:
            unknown.append(name)
        elif c is not None and (prefer_child or p is None):
            csel.append(c)
        else:
            psel.append(p)
    return psel, csel, unknown


def _split_and_conjuncts(expr: str) -> list[str]:
    """Split an OData filter on TOP-LEVEL ``and`` only (parens and quoted
    strings respected). A top-level ``or`` makes the whole expression one
    conjunct — splitting it would change its meaning."""
    expr = (expr or "").strip()
    # Unwrap a fully-enclosing paren pair so "(A) and (B)" still splits.
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        enclosing = True
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i < len(expr) - 1:
                    enclosing = False
                    break
        if not enclosing:
            break
        expr = expr[1:-1].strip()
    if not expr:
        return []
    low = expr.lower()
    splits: list[int] = []
    depth = 0
    in_str = False
    i = 0
    while i < len(expr):
        ch = expr[i]
        if in_str:
            if ch == "'":
                in_str = False  # '' escape reads as close+reopen; harmless
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and low.startswith(" or ", i):
            return [expr]  # top-level OR: unsplittable
        elif depth == 0 and low.startswith(" and ", i):
            splits.append(i)
            i += 5
            continue
        i += 1
    parts: list[str] = []
    start = 0
    for pos in splits:
        parts.append(expr[start:pos].strip())
        start = pos + 5
    parts.append(expr[start:].strip())
    # Recurse into parenthesised groups that themselves split further.
    out: list[str] = []
    for part in parts:
        if part.startswith("(") and part.endswith(")"):
            sub = _split_and_conjuncts(part)
            if len(sub) > 1:
                out.extend(sub)
                continue
        if part:
            out.append(part)
    return out


def _route_conjuncts(
    odata_filter: str,
    parent_cols: set[str],
    child_cols: set[str],
    *,
    prefer_child: bool,
    join_keys: set[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Assign each top-level AND conjunct of *odata_filter* to the table that
    actually has its columns.

    ``join_keys`` (lower-cased) are the parent/child join-key columns. A
    conjunct that references ONLY join keys always routes to the PARENT even
    when ``prefer_child`` is set: the key exists on both tables, and putting
    it on the child leaves the parent unfiltered — the join then pages every
    header in the system instead of fetching the one matching header (and
    silently misses it past the page ceiling). Filtering the parent bounds
    the scan; the child rows only come back under matching parents anyway.

    Returns ``(parent_parts, child_parts, unknown_idents, mixed_parts)`` —
    ``unknown_idents`` are columns on NEITHER table; ``mixed_parts`` are
    conjuncts that span both tables inside one expression (unroutable).
    """
    plow = {c.lower() for c in parent_cols}
    clow = {c.lower() for c in child_cols}
    jlow = {k.lower() for k in (join_keys or set())}
    p_parts: list[str] = []
    c_parts: list[str] = []
    unknown: list[str] = []
    mixed: list[str] = []
    for part in _split_and_conjuncts(odata_filter):
        idents = _query._extract_filter_identifiers(part)
        nowhere = [i for i in idents
                   if i.lower() not in plow and i.lower() not in clow]
        on_p = all(i.lower() in plow for i in idents)
        on_c = all(i.lower() in clow for i in idents)
        if nowhere:
            unknown.extend(nowhere)
        elif (jlow and idents and on_p
              and all(i.lower() in jlow for i in idents)):
            p_parts.append(part)  # join-key condition → parent (bounds the scan)
        elif on_c and (prefer_child or not on_p):
            c_parts.append(part)
        elif on_p:
            p_parts.append(part)
        else:
            mixed.append(part)
    return p_parts, c_parts, unknown, mixed


# ---------------------------------------------------------------------------
def _effective_limit(limit: int, top: int, cursor_limit: int = 0) -> int:
    """Resolve the caller's page size. Explicit `limit` wins; `top` is a
    first-class alias (models reflexively use OData's $top name, and a
    dropped `top=1000` would come back as a default page that reads as the
    complete answer); then the limit carried in the cursor; then the
    default."""
    for candidate in (limit, top, cursor_limit):
        if candidate > 0:
            return candidate
    return _DEFAULT_LIMIT


# ---------------------------------------------------------------------------

# Minimum difflib gap between the best and second-best candidate for a fuzzy
# column fix to count as CONFIDENT. Below this the two names are effectively
# tied and picking either is a guess.
_CORRECTION_MARGIN = 0.10


def _correct_column(bad: str, valid_cols) -> str | None:
    """Real column for an unrecognised name, or None if no confident fix.

    Confident, in order:
      1. a case/spacing variant (``partnum`` -> ``PartNum``);
      2. a BAQ-style alias whose ``Table_`` segment is stripped
         (``OrderDtl_PartNum`` -> ``PartNum``, ``OrderHed_CustNum`` ->
         ``CustNum``);
      3. a sub-string match either way that is the ONLY one
         (``PartDesc`` -> ``PartDescription``);
      4. a difflib ratio >= 0.82 that also beats the runner-up by
         ``_CORRECTION_MARGIN`` (``ResourceGroupID`` -> ``ResourceGrpID``).

    UNIQUENESS and MARGIN are both load-bearing. Without them the rule fired
    on the first alphabetical hit and rewrote generic names to arbitrary wrong
    columns on Erp.BO.PartSvc/Part -- Class -> AttrClassID (real: ClassID),
    Weight -> CNWeight (real: NetWeight), Cost -> CostMethod (a string code,
    not a number) -- each returning 0 rows stamped "complete result".

    Anything looser returns None: the caller drops it (projection) or rejects
    it (filter), and the model gets an INV-1 envelope listing every real
    candidate. Reading the WRONG column silently is the one outcome worse than
    an error, so the bar is deliberately high -- a precise error converges in
    one hop, a silent wrong guess makes the model hunt.
    """
    if not valid_cols:
        return None
    low = {c.lower(): c for c in valid_cols}
    b = bad.lower()
    if b in low:
        return low[b]
    # BAQ result-column aliases (Table_Field) leak into OData where/fields —
    # strip the leading or trailing underscore segment and try the remainder.
    if "_" in bad:
        for part in (bad.split("_", 1)[1], bad.rsplit("_", 1)[1]):
            hit = low.get(part.lower())
            if hit:
                return hit
    bsq = re.sub(r"[^a-z0-9]", "", b)
    if not bsq:
        return None
    scored: list[tuple[float, str]] = []
    subs: list[str] = []
    for c in valid_cols:
        csq = re.sub(r"[^a-z0-9]", "", c.lower())
        if not csq:
            continue
        if bsq == csq:
            return c
        if len(bsq) >= 4 and (bsq in csq or csq in bsq):
            subs.append(c)
        scored.append((difflib.SequenceMatcher(None, bsq, csq).ratio(), c))
    scored.sort(key=lambda t: -t[0])

    # The substring rule is confident ONLY when it is UNIQUE. Returning the
    # first hit in (alphabetical) index order silently rewrote generic names
    # to arbitrary wrong columns on Erp.BO.PartSvc/Part: Class -> AttrClassID
    # (real: ClassID), Weight -> CNWeight (real: NetWeight), Cost ->
    # CostMethod (a string code, not a number), Price -> FSPricePerCode. Each
    # returned 0 rows stamped "complete result" -- the silent wrong guess the
    # fail-soft lesson says is worse than a precise error.
    if len(subs) == 1:
        return subs[0]

    # Several columns contain the name. difflib alone cannot break this tie
    # HONESTLY: its ratio carries a length bias, scoring the SHORTER column
    # higher regardless of meaning (`weight` -> CNWeight 0.857 > NetWeight
    # 0.80), so "highest score wins" just re-picks an arbitrary column one
    # layer down. Require a clear MARGIN over the runner-up instead:
    #   Class  -> ClassID 0.833 vs AttrClassID 0.625  (margin 0.21) -> fix
    #   Weight -> CNWeight 0.857 vs NetWeight 0.80    (margin 0.06) -> None
    # An ambiguous name belongs in an INV-1 unknown_columns envelope, where
    # the model sees every real candidate and converges in one hop.
    if not scored:
        return None
    top_r, top_c = scored[0]
    if top_r < 0.82:
        return None
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if top_r - runner_up < _CORRECTION_MARGIN:
        return None
    return top_c


# Relational operators. Equality against a boolean is legitimate
# (`HasOnHandQty eq true`); ordering against one is nonsense.
_REL_OPS = ("gt", "lt", "ge", "le")


def _correction_type_ok(
    bad: str, fix: str, odata_filter: str, col_types: dict
) -> bool:
    """False when a confident NAME match would be a wrong COLUMN.

    ``_correct_column``'s substring rule fires on ``OnHandQty`` -> the boolean
    ``HasOnHandQty`` (``onhandqty`` IS a substring of ``hasonhandqty``, so it
    returns before difflib is consulted). Rewriting ``OnHandQty gt 0`` into
    ``HasOnHandQty gt 0`` builds a whereClause Epicor answers with its generic
    500. The real
    decimal ``OnHandQty`` lives on PartWhse/PartPlant, not Part.

    Keys on the LITERAL, not the operator. Vetoing only gt/lt/ge/le left the
    identical dead end one keystroke away: ``OnHandQty eq 0`` — the natural
    phrasing of "parts with nothing on hand" — became ``HasOnHandQty eq 0``,
    an Edm.Boolean compared to an integer, which is the same Epicor type
    error the veto exists to kill. So a boolean fix is allowed ONLY against a
    genuine boolean literal (true/false/null); every other comparison is
    vetoed. Non-boolean columns are untouched, so ``Description`` ->
    ``PartDescription`` (the rule this substring match exists for) still runs.
    """
    if (col_types.get(fix) or "") != "Edm.Boolean":
        return True
    # Relational operator against a boolean is nonsense whatever the literal.
    if re.search(rf"\b{re.escape(bad)}\b\s+({'|'.join(_REL_OPS)})\b",
                 odata_filter, re.IGNORECASE):
        return False
    # eq/ne is legitimate, but only against true/false/null.
    for m in re.finditer(rf"\b{re.escape(bad)}\b\s+(?:eq|ne)\s+(\S+)",
                         odata_filter, re.IGNORECASE):
        if m.group(1).strip("()'\" ").lower() not in ("true", "false", "null"):
            return False
    return True


def _uncorrectable_where_columns(
    index, service: str, entity_set: str, odata_filter: str,
    valid_columns: list,
) -> list[str]:
    """Filter columns that are unknown AND not confidently fixable.

    The `fields` envelope fires BEFORE the where is ever validated, so it could
    only ever say "these fields are wrong" — never "and your where is fine".
    That is the 246-of-356 ambiguity: GROUP001 threw away a perfect
    ``GroupID = 'GROUP001'`` filter over two bad amount columns.

    A name the corrector would silently repair a moment later is NOT a caller
    error and must not be listed as one, so the same
    ``_correct_column`` + ``_correction_type_ok`` pair the pre-flight block uses
    runs here. Purely diagnostic — it rewrites nothing.
    """
    if not (odata_filter or "").strip():
        return []
    try:
        refs = [d.get("ref", str(d)) if isinstance(d, dict) else str(d)
                for d in validate_columns(
                    index, service, entity_set, odata_filter, "", "")]
    except Exception:  # noqa: BLE001 — a hint is never worth a failure
        return []
    try:
        col_types = index.get_field_types(service, entity_set)
    except Exception:  # noqa: BLE001 — no types just means no veto
        col_types = {}
    out: list[str] = []
    for ref in refs:
        fix = _correct_column(ref, valid_columns)
        if (fix and fix.lower() != ref.lower()
                and _correction_type_ok(ref, fix, odata_filter, col_types)):
            continue
        out.append(ref)
    return out


async def _retry_odata_plural_after_getrows_500(
    client, index, service: str, entity_set: str, api_key: str,
    raw: str, *, aggregated: bool, filter: str, select: str,
    orderby: str, top: int, skip: int, soft: dict,
) -> str:
    """One OData retry on the plural collection after a generic GetRows 500.

    ``Part`` has NO ``<Entity>SearchSvc`` twin (``Erp.BO.PartSearchSvc`` does
    not exist in the index — the fast-path gate is fine, it correctly found
    nothing), so a GetRows apology on a heavy service was a dead end and the
    model retried six times. The plural OData collection (``PartSvc/Parts``)
    is the remaining route. Deliberately narrow — generic apology only, never
    aggregated, plural must exist in the index, ONE attempt, and the service is
    NOT added to ``getrows_services`` — so this can't reintroduce the 500-storm
    that put the service in HEAVY_SERVICES to begin with.
    """
    if aggregated or not raw:
        return raw
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(payload, dict) or "error" not in payload:
        return raw
    server_msg = (payload.get("detail") or {}).get("message") or ""
    if not _query._is_generic_epicor_apology(server_msg):
        return raw
    plural = f"{entity_set}s"
    try:
        if plural not in (index.get_entity_sets(service) or []):
            return raw
    except Exception:  # noqa: BLE001
        return raw
    try:
        retried = await run_odata(
            client, service, plural, api_key,
            filter=filter, select=select, orderby=orderby,
            top=top, skip=skip, expand="", count_only=False,
            group_by="", aggregate="", distinct="", format="json",
        )
    except EpicorError:
        return raw  # fall through to the INV-1 envelope GetRows built
    soft["route_via"] = f"{service}/{plural} (OData; GetRows 500'd)"
    return retried


def _rewrite_filter_columns(text: str, corrections: dict) -> str:
    """Whole-word replace corrected column tokens OUTSIDE quoted literals.

    Quote-awareness is load-bearing: a bare whole-word substitution also
    rewrites the token inside a string literal, so
    ``contains(PartDescription,'Weight') and Weight gt 5`` with the correction
    Weight->CNWeight became ``contains(PartDescription,'CNWeight') …`` — the
    tool searches for a string the user never asked for, returns 0 rows, and
    reports it as a complete result. ``_outside_quotes`` is query.py's
    existing primitive for exactly this.
    """
    def _rewrite(seg: str) -> str:
        for bad, good in corrections.items():
            seg = re.sub(rf"\b{re.escape(bad)}\b", good, seg)
        return seg

    return _query._outside_quotes(text, _rewrite)


_SEARCH_SVC_CACHE: dict[str, str] = {}


def _search_service_for(index, entity_set: str) -> str:
    """The fast ``<Entity>SearchSvc`` OData twin for *entity_set*, or "".

    A heavy maintenance BO (JobEntrySvc) answers list reads through GetRows,
    which can scan broadly when child filters are ignored. The matching SearchSvc
    exposes the SAME entity with the SAME columns over plain, filterable OData
    directly. Reads belong on the search BO; the maintenance BO is for edits.
    """
    if not entity_set:
        return ""
    if entity_set in _SEARCH_SVC_CACHE:
        return _SEARCH_SVC_CACHE[entity_set]
    twin = f"Erp.BO.{entity_set}SearchSvc"
    try:
        resolved = twin if entity_set in (index.get_entity_sets(twin) or []) else ""
    except Exception:  # noqa: BLE001 — missing service is just "no twin"
        # Do NOT memoize a failure. A transient index error would otherwise
        # disable the SearchSvc fast path for the rest of the process lifetime.
        return ""
    _SEARCH_SVC_CACHE[entity_set] = resolved
    return resolved


def _site_code(val: str) -> str | None:
    """A Plant filter value -> its real code, or None if not a known site.

    Accepts the code itself ('10'), the full site name ('Main Site'), or an
    unambiguous prefix/abbreviation ('BL' -> Main Site). The model should never
    have to memorise that 10==Main Site — it can filter by the place name.
    """
    if not val:
        return None
    if val in PLANTS:                       # already a valid code
        return val
    v = val.strip().lower()
    for code, name in PLANTS.items():       # exact site name
        if name.lower() == v:
            return code
    if len(v) >= 2:                          # unambiguous prefix (BL -> Main Site)
        pref = [c for c, n in PLANTS.items() if n.lower().startswith(v)]
        if len(pref) == 1:
            return pref[0]
    return match_plant(val)                   # substring word match, else None


# Plant/site filter tokens the model writes: real column Plant, plus the
# wrong-but-common SiteID/Site. Longer names first so the alternation wins.
_SITE_FILTER_RE = re.compile(
    r"\b(Plant1|Plant|SiteID|Site)\b(\s*(?:=|eq)\s*)'([^']*)'", re.IGNORECASE)


def _resolve_site_filter(where: str) -> tuple[str, dict]:
    """Rewrite Plant/Site filters so a place NAME/abbreviation becomes the real
    code and SiteID/Site becomes the real column Plant. Returns (where, notes).
    A value that is already a valid code, or that resolves to no site, is left
    alone (a wrong numeric code the model guessed can't be second-guessed)."""
    notes: dict = {}

    def repl(m: "re.Match") -> str:
        col, op, val = m.group(1), m.group(2), m.group(3)
        code = _site_code(val)
        wrong_col = col.lower() not in ("plant", "plant1")
        # Plant AND Plant1 are both REAL columns — Plant1 is the code column
        # on Erp.BO.PlantSvc/Plants, where `Plant` does not exist at all.
        # Rewriting it to `Plant` (silently, with no assumptions note) turned
        # a correct filter into unknown_columns. Only the wrong names
        # (SiteID/Site) get renamed; a correct one keeps its own spelling.
        out_col = "Plant" if wrong_col else col
        if code is None:
            # Unresolvable value. Still fix a wrong column name so it doesn't
            # unknown_columns-error; leave the value for downstream validation.
            if wrong_col:
                notes[col] = "Plant"
                return f"{out_col}{op}'{val}'"
            return m.group(0)
        if code != val or wrong_col:
            notes[f"{col}='{val}'"] = f"{out_col}='{code}'"
        return f"{out_col}{op}'{code}'"

    return _SITE_FILTER_RE.sub(repl, where or ""), notes


_EMPTY_AT_TENANT: dict[str, str] = {}  # No tenant-specific empty-table assertions.


# Result augmentation — attach resolved{} + row_count + next_cursor
# ---------------------------------------------------------------------------

def _augment(
    raw: str,
    resolved: dict,
    *,
    limit: int,
    skip: int,
    cursor_ctx: dict,
    paginated: bool,
    clamp_note: str | None = None,
    single_label: str | None = None,
    trim_to: int | None = None,
    assumptions: dict | None = None,
) -> str:
    """Parse the engine's formatted JSON string and fold in the legacy read
    contract fields. Returns the raw string unchanged if it isn't a JSON
    object (e.g. an offloaded-file pointer or CSV envelope)."""
    try:
        payload = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(payload, dict):
        return json.dumps({"resolved": resolved, "result": payload}, default=str)

    # The join engine stamps its ordering meta at the payload TOP level, but
    # `resolved` is where the plain path publishes it and therefore the only
    # place anything reads. Overwriting `resolved` with the literal dict above
    # meant a join paid for a caller sort and then reported resolved.order =
    # null -- the honesty label existed, in the one spot nobody looks.
    for _k in ("order", "order_source", "order_warning"):
        if payload.get(_k) is not None:
            resolved[_k] = payload[_k]

    payload["resolved"] = resolved
    if assumptions:
        payload["assumptions"] = assumptions
    records = payload.get("records")
    if (trim_to and isinstance(records, list) and len(records) > trim_to
            and "error" not in payload):
        # Joined listings can return a whole page of lines for a small
        # `limit`; keep the response in budget and honest about it.
        payload["limit_trim"] = (
            f"Showing the first {trim_to} of {len(records)} joined row(s) "
            "fetched; raise 'limit' if the user needs more.")
        records = records[:trim_to]
        payload["records"] = records
        payload["record_count"] = len(records)
    if isinstance(records, list):
        row_count = payload.get("record_count", len(records))
    else:
        row_count = payload.get("record_count", 0)
    payload["row_count"] = row_count

    # Only offer a next page when this was a full, non-aggregated page.
    # `len(records)` is the POST-truncation list: when the formatter dropped
    # rows for size, 40 >= 100 is False, so truncation used to DELETE the
    # cursor and flip the summary to "complete result". Measure the page
    # against what was actually fetched. Guarded on `truncated` so ordinary
    # short final pages behave exactly as before.
    fetched = len(records) if isinstance(records, list) else 0
    if payload.get("truncated"):
        fetched = int(payload.get("original_record_count") or fetched)
    has_more = False
    if (
        paginated
        and isinstance(records, list)
        and fetched >= limit
        and "error" not in payload
    ):
        nxt = dict(cursor_ctx)
        nxt["skip"] = skip + limit
        token = _encode_cursor(nxt)
        if token:
            payload["next_cursor"] = token
            has_more = True

    # Convergence affordance (INV-3 / local-model termination): lead with a
    # short summary + an explicit "you have the answer, stop" hint so a weak
    # model doesn't re-run the read with slightly different filters forever.
    if "error" not in payload:
        svc = resolved.get("service", "")
        ent = resolved.get("entity_set", "")
        if single_label:
            # Single-record fast path: header + auto-joined lines. Zero rows
            # means the key matched nothing — never phrase that as a record.
            if row_count == 0:
                payload["summary"] = (
                    f"{single_label}: no matching record. The key does not "
                    "exist (or is outside your access) — tell the user; do "
                    "NOT retry the same key.")
            else:
                payload["summary"] = f"{single_label}: header + {row_count} line(s)."
        else:
            if has_more:
                # A full page is NOT the complete result set. Saying "pass
                # next_cursor ONLY if needed" next to a stop_hint leads models
                # to present one page as the whole answer — state the
                # incompleteness plainly.
                payload["summary"] = (
                    f"INCOMPLETE: first {row_count} row(s) from {svc}/{ent} — "
                    "more rows match this query. To get the rest, call again "
                    "with the next_cursor below (or a larger limit, max 1000). "
                    "Do NOT present this page as the full list."
                )
                if payload.get("truncated"):
                    # The page was ALSO cut for size, so even this page is
                    # short of what was fetched. Name the cause — narrowing
                    # columns fixes it in one hop; paging does not.
                    payload["summary"] += (
                        f" (Only {row_count} of "
                        f"{payload.get('original_record_count')} FETCHED rows "
                        "fit the byte budget — narrow the columns with "
                        "fields=\"col1,col2\".)"
                    )
            elif "limit_trim" in payload:
                # Joined listing trimmed to `limit` — also not the full set.
                payload["summary"] = (
                    f"INCOMPLETE: first {row_count} joined row(s) from "
                    f"{svc}/{ent}; more were fetched (see limit_trim). "
                    "Raise 'limit' for the rest."
                )
            elif payload.get("truncated"):
                # The formatter deleted rows to fit the byte budget. Stamping
                # "complete result" over that is the worst paging-honesty
                # failure in the tool: it discards up to 95% of the rows and
                # tells the model it has everything.
                payload["summary"] = (
                    f"INCOMPLETE: the response from {svc}/{ent} exceeded the "
                    f"byte budget, so only {row_count} of "
                    f"{payload.get('original_record_count', row_count)} "
                    "fetched row(s) are shown. Do NOT present this as the full "
                    "list. Re-run with fields=\"col1,col2\" to narrow the "
                    "columns (the usual cause) or add a `where` filter."
                )
            elif payload.get("complete") is False:
                # A rollup computed over a TRUNCATED scan. The paged engine
                # sets complete=false + note when it hits the page ceiling;
                # stamping "complete result" over that presents a materially
                # understated total as authoritative (the summary is emitted
                # FIRST, so it is what the model reads). Mirrors the analytic
                # path's `payload.get("complete") is False` check.
                payload["summary"] = (
                    f"INCOMPLETE: {row_count} row(s) from {svc}/{ent}, "
                    "computed over a PARTIAL scan — "
                    + str(payload.get("note")
                          or "the scan stopped at its page ceiling")
                    + ". Do NOT present these figures as final totals; "
                    "narrow the filter (add a date range or a Plant) and "
                    "re-run for an exact answer."
                )
            elif row_count == 0 and ent in _EMPTY_AT_TENANT:
                # Real table, resolvable, structurally empty here. Never let
                # this read as a terminal "there is none".
                payload["summary"] = (
                    f"0 rows from {svc}/{ent} — this is EXPECTED and is NOT "
                    "the answer. " + _EMPTY_AT_TENANT[ent]
                )
                payload["retry_with"] = {"target": "BOM for part <PartNum>"}
                payload["_empty_at_tenant"] = ent
            else:
                payload["summary"] = (
                    f"{row_count} row(s) returned from {svc}/{ent}. "
                    "This is the complete result for this query."
                )
        if has_more and not single_label:
            payload["stop_hint"] = (
                "If the user wants the full list, keep calling with "
                "next_cursor until next_cursor disappears — do NOT change "
                "filters/fields between pages. If the first page already "
                "answers, present it but say more rows exist."
            )
        elif payload.pop("_empty_at_tenant", None):
            # The generic "these rows answer the read, do NOT re-run" hint
            # would flatly contradict the advisory above and strand the model
            # on the empty table.
            payload["stop_hint"] = (
                "This empty result does NOT answer the question. Make the ONE "
                "follow-up call named in the summary / retry_with, then "
                "present that answer."
            )
        elif payload.get("truncated"):
            # "do NOT re-run" next to a 95% row loss strands the model on a
            # partial answer it believes is final.
            payload["stop_hint"] = (
                "Rows were dropped to fit the byte budget. Narrow the columns "
                "with fields=\"...\" (or add a `where`) and re-run before "
                "presenting a list."
            )
        else:
            payload["stop_hint"] = (
                "These rows answer the read. Present them to the user now; "
                "do NOT re-run epicor_read with new filters/fields unless "
                "the user asks."
            )
        if clamp_note:
            payload["limit_note"] = clamp_note
        if assumptions:
            # Lead the summary with what we assumed so the model reports it and
            # doesn't silently trust a mis-mapped column / picked entity.
            bits = []
            if "target_assumed" in assumptions:
                bits.append(f"used {assumptions['target_assumed']['used']}")
            if assumptions.get("fields_corrected"):
                bits.append("auto-corrected fields "
                            + str(assumptions["fields_corrected"]))
            if assumptions.get("fields_dropped"):
                bits.append("dropped unknown fields "
                            + str(assumptions["fields_dropped"]))
            if assumptions.get("fields_note"):
                bits.append(assumptions["fields_note"])
            if assumptions.get("fields_capped"):
                cap = assumptions["fields_capped"]
                bits.append(
                    f"showed {cap['shown']} of {cap['of']} columns (this "
                    "entity has no curated default set) — pass fields=\"...\" "
                    "for any others")
            if assumptions.get("arg_aliased"):
                bits.append("read arguments "
                            + str(assumptions["arg_aliased"])
                            + " as their real parameter names")
            if assumptions.get("arg_ignored"):
                bits.append("IGNORED arguments "
                            + str(assumptions["arg_ignored"]))
            if bits:
                payload["summary"] = (
                    "ASSUMPTIONS (verify if the answer looks off): "
                    + "; ".join(bits) + ". " + payload.get("summary", ""))

    # A ranking over a scan that never reached the end of the set is not a
    # top-N. The engine already says so in order_warning; say it in the one
    # field a weak model always reads, or the rows read as the answer.
    if (payload.get("order_warning")
            and "error" not in payload
            and not str(payload.get("summary", "")).startswith("INCOMPLETE")):
        payload["summary"] = (
            "INCOMPLETE RANKING: " + payload["order_warning"] + " "
            + str(payload.get("summary", "")))

    # Put the anchor fields FIRST (before the bulky records) and emit compact
    # JSON — large indented payloads bury the summary and waste the model's
    # context, which is itself a driver of non-termination.
    ordered: dict = {}
    for k in ("summary", "stop_hint", "row_count", "resolved", "assumptions",
              "next_cursor", "limit_note", "limit_trim"):
        if k in payload:
            ordered[k] = payload[k]
    for k, v in payload.items():
        if k not in ordered:
            ordered[k] = v
    return json.dumps(ordered, default=str)


def _with_assumptions(payload: str, notes: dict) -> str:
    """Fold `notes` into a recognizer route's JSON under resolved.assumptions.

    Used where threading the bag through the route body would mean editing a
    dozen return points. The rule it enforces is the same either way: an
    argument the route cannot apply is ANNOUNCED, never swallowed.
    """
    if not notes:
        return payload
    try:
        obj = json.loads(payload)
    except (TypeError, ValueError):
        return payload
    if not isinstance(obj, dict):
        return payload
    res = obj.get("resolved")
    if not isinstance(res, dict):
        res = {}
        obj["resolved"] = res
    merged = dict(res.get("assumptions") or {})
    merged.update(notes)
    res["assumptions"] = merged
    return json.dumps(obj, default=str)


def _args_ignored_note(route: str, **args) -> dict:
    """One announced entry naming every argument a computed route drops."""
    dropped = [k for k, v in args.items() if v not in (None, "", 0, False)]
    if not dropped:
        return {}
    return {"args_ignored": (
        f"{', '.join(sorted(dropped))} do not apply to the {route} route and "
        "were NOT applied. They belong on the follow-up data read "
        "(target=<the entity you pick from this answer>).")}


async def _run_analytic(client, index, rbac, session, recipe, target, where,
                        limit, order_by="", soft=None):
    """Answer a 'top N by measure' query with ONE business-object group-by.

    The measure lives on a single entity (recipe); we fetch a bounded, measure-
    biased page, roll it up client-side, rank it, and label the scope. No BAQ,
    works for every user — the fix for the 16-call aggregation thrash.
    """
    # Line-level measures (sales / on-hand BY PART) live on detail tables that BO
    # GetRows cannot retrieve. This is a TERMINAL answer: do not send the model to
    # epicor_baq (disabled) or back into epicor_read — that ping-pongs forever.
    if not recipe.get("bo_retrievable", True):
        return json.dumps({
            "summary": (f"'{recipe['label']}' isn't available here. It needs "
                        f"{recipe.get('needs', 'line-level detail')}, which requires "
                        "a custom report (BAQ) that is not enabled for you."),
            "stop_hint": ("FINAL — this ranking cannot be produced with the tools "
                          "you have. Do NOT call epicor_read or epicor_baq again for "
                          "this request. Tell the user it needs a custom report that "
                          "isn't enabled; you may offer a header-level ranking (top "
                          "customers by sales, top vendors by spend) or a single-part "
                          "lookup, but only if they ask."),
            "terminal": True,
            "limitation": "line_level_measure_requires_report",
        }, default=str)

    allowed, msg = rbac.check_access(session.user_id, recipe["service"])
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, recipe["service"]).api_key or ""
    top_n = parse_top_n(target, default=min(limit, 25) or 10)
    # Thread the real Edm types here too. Without them the analytic path ran on
    # the anchored-regex fallback alone, so a quoted ISO literal against an
    # Edm.String column could be unquoted (and a genuine date bound left
    # quoted) — the exact type gate the main read path documents as
    # load-bearing, absent on the ranking path.
    where_odata = sql_to_odata(
        where,
        date_columns=date_columns_for(
            index, recipe["service"],
            recipe.get("entity") or recipe.get("parent_entity", "")),
    ) if where.strip() else ""

    # An explicit timeframe ("in 2025", "Q3 2025", "last month") scopes the
    # measure's date field and REPLACES the recency default. On-hand is
    # current-state (date_field None): proceed, but say history doesn't exist.
    # Sniff the window from the TARGET phrase first; only consult `where`
    # AFTER stripping quoted/date literals, so the bare-year regex never
    # fires on the year inside a date literal or part number.
    window = parse_time_window(target)
    if window is None and where.strip():
        window = parse_time_window(_strip_filter_literals(where))
    date_field = recipe.get("date_field")
    # When the caller's own `where` already constrains the date field, it
    # owns the time scope: never AND a synthetic window on top of it, and
    # skip the recency default in favour of a full paged scan.
    where_scopes_dates = bool(
        date_field and where_odata and re.search(
            r"(?<![A-Za-z0-9_])" + re.escape(date_field) + r"(?![A-Za-z0-9_])",
            where_odata, re.IGNORECASE))
    window_clause = ""
    scope = recipe["scope"]
    notes: list[str] = []
    if window and date_field and not where_scopes_dates:
        window_clause = (
            f"{date_field} ge {window['start']}T00:00:00 and "
            f"{date_field} le {window['end']}T23:59:59")
        scope = (f"{recipe['scope_noun']}, {window['label']} "
                 f"({window['start']}..{window['end']})")
    elif window and not date_field:
        notes.append(
            f"On-hand is current-state — there is no on-hand history for "
            f"{window['label']}; showing current quantities.")
    elif where_scopes_dates:
        scope = (f"{recipe['scope_noun']}, scoped by the given "
                 f"{date_field} filter ({where.strip()})")
    # Full-period scan whenever a time scope is in play (explicit window or
    # a caller-supplied date filter); otherwise the bounded recency default.
    full_scan = bool(window_clause) or where_scopes_dates

    # --- Line-level measures (sales/on-hand BY PART) via the parent/child join ---
    # These measures live on a detail table (InvcDtl.ExtPrice, PartWhse.OnHandQty)
    # that BO GetRows can't retrieve alone. Drive the SAME query_with_children
    # engine epicor_read exposes under `children` to pull header+line in one
    # paged call, aggregate the child lines, then rank — no BAQ required.
    if recipe.get("via_join"):
        join_fn = _capture_children_fn(index, rbac, client)
        if join_fn is None:
            return json.dumps(error_envelope(
                "analytic_failed",
                f"Could not compute {recipe['label']}: the parent/child join "
                "engine is unavailable."))
        parent_filter = where_odata
        if window_clause:
            parent_filter = (
                f"({parent_filter}) and {window_clause}"
                if parent_filter else window_clause)
        if full_scan:
            # Explicit period: scan the whole window, not one recency page.
            # 500-row pages, not 1000 — a windowed 1000-row GetRows page on
            # a large header table exceeds the 30s per-request budget (the
            # join also halves further on a timeout, floor 250).
            page_size, max_pages = 500, 40
        else:
            rec = recipe.get("recency")
            if rec:
                from datetime import datetime, timedelta
                cutoff = (datetime.now() - timedelta(days=int(rec["days"]))).strftime(
                    "%Y-%m-%dT00:00:00")
                clause = f"{rec['field']} ge {cutoff}"
                if rec["field"].lower() not in parent_filter.lower():
                    parent_filter = (
                        f"({parent_filter}) and {clause}" if parent_filter else clause)
            page_size = recipe.get("join_page_size", 500)
            max_pages = recipe.get("join_max_pages", 1)
        raw = await join_fn(
            service=recipe["service"],
            parent_entity=recipe["parent_entity"],
            child_entity=recipe["child_entity"],
            parent_filter=parent_filter,
            child_filter="",
            parent_select="",
            child_select="",
            group_by=recipe["group_by"],
            aggregate=recipe["aggregate"],
            page_size=page_size,
            max_pages=max_pages,
            format="json",
        )
        return _rank_result(raw, recipe, top_n, where, scope=scope, notes=notes,
                            page_ceiling=page_size * max_pages,
                            order_by=order_by, soft=soft)

    try:
        if full_scan:
            # Header recipe with a time scope (explicit window or a caller
            # date filter): paged scan over the whole period instead of a
            # single measure-biased page.
            filter_str = where_odata
            if window_clause:
                filter_str = (
                    f"({filter_str}) and {window_clause}"
                    if filter_str else window_clause)
            raw = await run_getrows_paged(
                client, index, recipe["service"], recipe["entity"], api_key,
                filter=filter_str, select="", orderby=recipe["order_hint"],
                page_size=_MAX_LIMIT, max_pages=20,
                group_by=recipe["group_by"], aggregate=recipe["aggregate"],
                distinct="", format="json",
            )
        else:
            raw = await run_getrows(
                client, index, recipe["service"], recipe["entity"], api_key,
                filter=where_odata, select="", orderby=recipe["order_hint"],
                top=_MAX_LIMIT, skip=0, count_only=False,
                group_by=recipe["group_by"], aggregate=recipe["aggregate"],
                distinct="", format="json",
            )
    except EpicorError as exc:
        return json.dumps(error_envelope(
            "analytic_failed", f"Could not compute {recipe['label']}: {exc.message}"))
    return _rank_result(raw, recipe, top_n, where, scope=scope, notes=notes,
                        order_by=order_by, soft=soft)


def _is_ascending_on_measure(order_by: str, alias: str) -> bool:
    """True when the caller's leading sort term is the measure, ascending.

    Only then is the result honestly a "Bottom N" (the slowest movers, the
    smallest customers). Any other ordering is just "First N in that order".
    """
    terms, kind = parse_order_by(order_by)
    if kind or not terms:
        return False
    col, direction = terms[0]
    return (col.split(".")[-1].lower() == str(alias).lower()
            and direction != "desc")


def _rank_result(
    raw: str,
    recipe: dict,
    top_n: int,
    where: str,
    *,
    scope: str | None = None,
    notes: list[str] | None = None,
    page_ceiling: int | None = None,
    order_by: str = "",
    soft: dict | None = None,
) -> str:
    """Sort the grouped rollup by the measure and keep the top N, with a scope note."""
    scope = scope or recipe["scope"]
    notes = list(notes or [])
    soft = dict(soft or {})
    try:
        payload = json.loads(raw)
    except Exception:
        return raw
    recs = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(recs, list):
        if isinstance(payload, dict):
            payload.setdefault("analytic", recipe["label"])
        return json.dumps(payload, default=str)
    # Honest partial-scan flags: the join's parent page ceiling, or an
    # incomplete paged header scan (the paged engine reports complete/note).
    if page_ceiling and payload.get("parent_rows") == page_ceiling:
        notes.append(
            f"The scan hit its page ceiling ({page_ceiling} parent rows); "
            "more rows exist in the period, so these figures may be partial.")
    if payload.get("complete") is False:
        notes.append(payload.get("note")
                     or "Scan stopped before the full period; figures may be partial.")
    # The parent/child join reports partial scans via truncated/scan_error.
    for flag in ("truncated", "scan_error"):
        if payload.get(flag):
            notes.append(str(payload[flag]))
    # Drop the null/blank group-key bucket (e.g. invoice lines with no PartNum,
    # or an unassigned customer/vendor) so it never masquerades as a top-ranked
    # subject. The grouping column is the first group_by field.
    gb = (recipe.get("group_by") or "").split(",")[0].strip()
    if gb:
        recs = [r for r in recs if r.get(gb) not in (None, "")]
    alias = recipe["measure_alias"]

    def _key(r):
        try:
            return float(r.get(alias))
        except (TypeError, ValueError):
            return float("-inf")

    # A caller order_by MUST win over the default measure-DESC ranking. It used
    # to be accepted and discarded, so "order_by='TotalQty asc'" (the 10
    # SLOWEST movers) returned the 10 HIGHEST sellers under a "Top 10" label --
    # the exact inverse of the question, with nothing in the payload to
    # contradict it.
    rank_label = "Top"
    if (order_by or "").strip():
        avail = sorted({k for r in recs if isinstance(r, dict) for k in r})
        sorted_recs, err_kind, valid_cols = sort_records(
            recs, order_by, available=avail)
        if err_kind:
            return json.dumps(order_refusal(err_kind, order_by,
                                            valid_cols or avail))
        recs = sorted_recs
        ranked = recs[:top_n]
        # "Top" would be a lie for an ascending sort; name the real ordering.
        rank_label = "Bottom" if _is_ascending_on_measure(
            order_by, alias) else "First"
        soft["order"] = f"{order_by} (client-side, over the full rollup)"
    else:
        ranked = sorted(recs, key=_key, reverse=True)[:top_n]
    out = {
        "summary": (f"{rank_label} {len(ranked)} {recipe['label']}"
                    + (f", ordered by {order_by.strip()}"
                       if (order_by or "").strip() else "")
                    + (f", filtered: {where.strip()}" if where.strip() else "")
                    + f". Scoped to {scope}."),
        "stop_hint": ("This ranked list answers the question — present it now; "
                      "do NOT re-query or reach for a BAQ."),
        "scope_note": (f"Computed with business objects over a bounded fetch "
                       f"({scope}); an exact all-time ranking would need a BAQ."),
        "resolved": {"service": recipe["service"],
                     "entity_set": recipe.get("entity") or recipe.get("child_entity", ""),
                     "group_by": recipe["group_by"], "aggregate": recipe["aggregate"],
                     # Coercions applied before dispatch (site_resolved, arg
                     # aliasing) were dropped on this route entirely -- the
                     # fail-soft contract requires they ride here.
                     **({"assumptions": soft} if soft else {})},
        "row_count": len(ranked),
        "records": ranked,
    }
    if notes:
        out["notes"] = notes
    for k in ("scanned_rows", "pages_scanned"):
        if k in payload:
            out[k] = payload[k]
    return json.dumps(out, default=str)


def _contact_parent_ref(where: str, spec: dict) -> tuple[int | None, str, bool]:
    """Extract the parent reference from a contacts `where`.

    Returns ``(num, term, ambiguous)``:
      * ``num`` — the parent key number if the caller already gave it
        (``VendorNum eq 1234``), else ``None``;
      * ``term`` — a name/ID string to resolve when ``num`` is ``None``;
      * ``ambiguous`` — True when ``term`` came from the child's own ``Name``
        column (which could be the CONTACT's name), so a lookup miss should be
        reported gently rather than as "parent not found".
    """
    key, id_col = spec["key"], spec["id_col"]
    if not where or not where.strip():
        return None, "", False
    mnum = re.search(
        rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])\s*(?:=|eq)\s*(\d+)",
        where, re.IGNORECASE)
    if mnum:
        return int(mnum.group(1)), "", False
    # Unambiguous parent references: the ID column or the denormalized copies.
    for pat in (
        rf"\b{re.escape(id_col)}\s*(?:=|eq|like)\s*'([^']*)'",
        rf"\b{re.escape(key)}Name\s*(?:=|eq|like)\s*'([^']*)'",
        rf"\b{re.escape(key)}{re.escape(id_col)}\s*(?:=|eq|like)\s*'([^']*)'",
    ):
        mm = re.search(pat, where, re.IGNORECASE)
        if mm:
            return None, mm.group(1).strip().strip("%").strip(), False
    # The child's own Name — treat as a parent hint, but ambiguously.
    mm = re.search(r"\bName\s*(?:=|eq|like)\s*'([^']*)'", where, re.IGNORECASE)
    if mm:
        return None, mm.group(1).strip().strip("%").strip(), True
    # A bare token with no operators ("Example Company").
    bare = where.strip()
    if not re.search(r"[=<>]|\beq\b|\band\b|\bor\b|\blike\b", bare, re.IGNORECASE):
        return None, bare.strip("'").strip("%").strip(), True
    return None, "", False


def _contact_target_residual(target: str) -> str:
    """Pull a parent name out of a contacts `target` phrase.

    "vendor contacts for Example Company" -> "Example Company": strip the
    entity words and connective stopwords, leaving the caller's parent name.
    """
    t = re.sub(
        r"(?i)\b(vendor|vendors|supplier|suppliers|customer|customers|client|"
        r"clients|contact|contacts|list|for|of|at|the|show|me|all|s)\b", " ",
        target or "")
    t = re.sub(r"[^A-Za-z0-9&.\- ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


async def _read_contacts(
    client, index, rbac, session, service, entity_set, target, where, fields,
    limit, *, count_only: bool, order_by: str = "", soft: dict | None = None,
):
    """Serve a vendor/customer contacts read via the parent's GetByID.

    The child collection and a child GetRows both return zero, so this is the
    only path that yields contacts. Resolves the parent (number, ID, or name in
    ``where`` — or a name in ``target`` like "vendor contacts for Example Company"),
    calls ``<service>/GetByID``, and returns the child rows in the standard read
    envelope — or an INV-1 envelope when the parent can't be pinned to exactly
    one record.
    """
    spec = _CONTACT_PARENTS[entity_set]
    psvc, coll = spec["service"], spec["collection"]
    key, id_col, label = spec["key"], spec["id_col"], spec["label"]

    allowed, msg = rbac.check_access(session.user_id, psvc)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, psvc).api_key or ""

    # --- 1) Pin the parent key number ---------------------------------------
    num, term, ambiguous = _contact_parent_ref(where, spec)
    if num is None and not term:
        # No parent in `where` — try the residual of the target phrase.
        residual = _contact_target_residual(target)
        if residual:
            term, ambiguous = residual, True
    if num is None:
        if not term:
            return json.dumps(error_envelope(
                "need_parent",
                f"Name the {label} whose contacts you want — re-call with "
                f"where=\"{id_col} = '<id>'\" or where=\"Name like '%<name>%'\".",
            ))
        esc = term.replace("'", "''")
        try:
            raw = await run_getrows(
                client, index, psvc, coll, api_key,
                filter=f"Name like '%{esc}%' or {id_col} eq '{esc}'",
                select=f"{key},{id_col},Name",
                orderby="", top=25, skip=0, count_only=False,
                group_by="", aggregate="", distinct="", format="json",
            )
            hits = (json.loads(raw) or {}).get("records") or []
        except Exception as exc:
            return json.dumps(error_envelope(
                "parent_lookup_failed",
                f"Could not look up {label} '{term}': {exc}"))
        seen: dict = {}
        for r in hits:
            k = r.get(key)
            if k is not None:
                seen.setdefault(k, r)
        parents = list(seen.values())
        if not parents:
            return json.dumps(error_envelope(
                "parent_not_found",
                f"No {label} matched '{term}'"
                + ("" if not ambiguous else
                   f" (read as a {label} name). If '{term}' is a CONTACT name, "
                   f"name the {label} instead")
                + f". Verify the {label} name or {id_col}.",
            ))
        if len(parents) > 1:
            matches = [{
                key: p[key], id_col: p.get(id_col), "name": p.get("Name"),
                "where": f"{key} eq {p[key]}",
            } for p in parents[:10]]
            return json.dumps(error_envelope(
                "parent_ambiguous",
                f"{len(parents)} {label}s match '{term}'. Re-call epicor_read "
                f"with the `where` for the one you want.",
                valid={"matches": matches},
                retry_with={"target": f"{service}/{entity_set}",
                            "where": matches[0]["where"]},
            ))
        num = parents[0][key]

    # --- 2) Fetch the child contact rows (strategy per parent) --------------
    strategy = spec.get("strategy", "getbyid")
    if strategy == "collection":
        # Customers: contacts live in the dedicated Erp.BO.CustCntSvc/CustCnts
        # collection (the parent GetByID has no CustCnt child), keyed by CustNum.
        csvc = spec["contact_service"]
        c_allowed, c_msg = rbac.check_access(session.user_id, csvc)
        if not c_allowed:
            return json.dumps(error_envelope("access_denied", c_msg))
        c_key = rbac.check_service_access(session.user_id, csvc).api_key or ""
        try:
            raw = await run_getrows(
                client, index, csvc, spec["contact_collection"], c_key,
                filter=f"{key} eq {num}", select="", orderby="",
                top=max(1, limit), skip=0, count_only=False,
                group_by="", aggregate="", distinct="", format="json")
            rows = [r for r in ((json.loads(raw) or {}).get("records") or [])
                    if isinstance(r, dict)]
        except Exception as exc:
            return json.dumps(error_envelope(
                "contacts_read_failed",
                f"Contact lookup failed for {label} {num}: {exc}"))
        source = {"service": csvc, "entity_set": spec["contact_collection"],
                  "via": "collection", key: num}
    else:
        try:
            resp = await client.post(
                f"{psvc}/GetByID", api_key,
                json_body={spec["getbyid_param"]: num})
        except EpicorError as exc:
            return json.dumps(error_envelope(
                "contacts_read_failed",
                f"GetByID failed for {label} {num}: {exc.message}"))
        ds = resp.get("returnObj") if isinstance(resp, dict) else None
        if not isinstance(ds, dict):
            ds = resp if isinstance(resp, dict) else {}
        rows = [r for r in (ds.get(spec["child_table"]) or [])
                if isinstance(r, dict)]
        source = {"service": psvc, "entity_set": spec["child_table"],
                  "via": "GetByID", key: num}

    # `order_by` used to be accepted and discarded here, so the caller could not
    # tell "sorted as requested" from "sort ignored". Sort the RAW rows (any
    # contact column, not just the projected ones); an unsortable clause gets
    # the same INV-1 refusal as every other recognizer route.
    notes = dict(soft or {})
    if (order_by or "").strip():
        if not rows:
            notes["order"] = (
                f"order_by='{order_by}' was not applied — {label} {num} has "
                "no contacts to sort.")
        else:
            avail = sorted({k for r in rows for k in r})
            rows, err_kind, valid_cols = sort_records(
                rows, order_by, available=avail)
            if err_kind:
                return json.dumps(
                    order_refusal(err_kind, order_by, valid_cols or avail))
            notes["order"] = f"{order_by} (client-side, over all contacts)"
    if notes:
        source = {**source, "assumptions": notes}

    if count_only:
        return json.dumps({
            "summary": f"{len(rows)} contact(s) for {label} {num}.",
            "count": len(rows), "exact": True,
            "resolved": source,
            "stop_hint": "This count answers the question — do not re-query.",
        })

    # --- 3) Project fields (requested or curated default), drop empties ------
    want = [f.strip() for f in (fields or "").split(",") if f.strip()] \
        or _CONTACT_DEFAULT_FIELDS
    wl = {w.lower() for w in want}
    projected = []
    for r in rows[: max(1, limit)]:
        row = {k: v for k, v in r.items()
               if k.lower() in wl and v not in (None, "", False)}
        if not row:  # never emit a blank contact — fall back to the essentials
            row = {k: r.get(k) for k in ("Name", "ContactTitle", "PhoneNum",
                                         "EmailAddress") if r.get(k)}
        if row:
            projected.append(row)

    return json.dumps({
        "rows": projected,
        "row_count": len(rows),
        "resolved": source,
        "note": (f"{len(rows)} contact(s) for {label} {num}." if rows
                 else f"{label.capitalize()} {num} has no contacts on file — "
                      f"that is the complete answer."),
    }, default=str)


async def _run_count(client, index, service, entity_set, api_key, odata_filter, where):
    'TRUE count via OData ``$count``; labeled capped-GetRows fallback if it 500s.'
    # A DataSet table name (JobHead) is often not an OData segment while its
    # plural collection (JobHeads) is — try both before falling back to the
    # capped GetRows count, which sends the model chasing an "exact" number
    # it can never get.
    try:
        known_sets = set(index.get_entity_sets(service) or [])
    except Exception:
        known_sets = set()
    candidates = [entity_set] + [
        c for c in (f"{entity_set}s", f"{entity_set}es")
        if c in known_sets and c != entity_set]
    for cand in candidates:
        try:
            raw = await run_odata(
                client, service, cand, api_key,
                filter=odata_filter, select="", orderby="", top=1, skip=0,
                expand="", count_only=True, group_by="", aggregate="",
                distinct="", format="json",
            )
            return _wrap_count(raw, service, entity_set, where, exact=True)
        except EpicorError:
            continue
    try:
        raw = await run_getrows(
            client, index, service, entity_set, api_key,
            filter=odata_filter, select="", orderby="", top=_MAX_LIMIT,
            skip=0, count_only=True, group_by="", aggregate="",
            distinct="", format="json",
        )
        return _wrap_count(raw, service, entity_set, where, exact=False)
    except EpicorError as exc:
        return json.dumps(error_envelope(
            "count_failed", f"Could not count {service}/{entity_set}: {exc.message}"))


def _wrap_count(raw: str, service: str, entity_set: str, where: str, *, exact: bool) -> str:
    try:
        p = json.loads(raw)
    except Exception:
        return raw
    cnt = p.get("count") if isinstance(p, dict) else None
    if cnt is None and isinstance(p, dict):
        recs = p.get("records")
        cnt = len(recs) if isinstance(recs, list) else None
    capped = (not exact) and isinstance(cnt, int) and cnt >= _MAX_LIMIT
    out = {
        "summary": (f"{cnt}{'+ (capped, exact count unavailable)' if capped else ''} "
                    f"{entity_set} record(s)"
                    + (f" where {where.strip()}" if where.strip() else "") + "."),
        # The capped case is where models spiral: they retry with ever-bigger
        # limits that cannot change the answer. Say so explicitly.
        "stop_hint": (
            "This service cannot produce an exact count — no retry, limit, or "
            f"filter tweak will improve it. Report '{cnt}+' to the user now."
            if capped else
            "This count answers the question — present it; do not re-query."),
        "count": cnt,
        "exact": bool(exact and not capped),
        "resolved": {"service": service, "entity_set": entity_set},
    }
    return json.dumps(out, default=str)


async def _run_child_count(
    client, index, service, entity_set, parent, api_key, odata_filter, where,
    join_fn,
):
    """TRUE count for a CHILD/detail table (PODetail, InvcDtl, ...).

    Neither generic count path works on a child: the singular DataSet name is
    not an OData segment (``POSvc/PODetail/$count`` 404s — only plural
    collections like ``PODetails`` exist), and the GetRows fallback zeroes the
    parent's whereClause (``1=0``) so the child comes back structurally empty
    — the old code then reported a confident 0 for a PO that has lines.

    Order here: (1) ``$count`` on the PLURAL OData collection (exact, cheap),
    with the filter re-cased to the child's real column names; (2) the
    parent/child join with a ``count(*)`` rollup — exact unless the parent
    scan was truncated, in which case the count is labeled as capped.
    """
    child_cols = _field_names(index, service, entity_set)

    # (1) Plural OData collection, e.g. PODetail -> PODetails.
    try:
        known_sets = set(index.get_entity_sets(service) or [])
    except Exception:
        known_sets = set()
    plural = next(
        (c for c in (f"{entity_set}s", f"{entity_set}es") if c in known_sets),
        None)
    if plural:
        try:
            raw = await run_odata(
                client, service, plural, api_key,
                filter=_recase_columns(odata_filter, child_cols), select="",
                orderby="", top=1, skip=0, expand="", count_only=True,
                group_by="", aggregate="", distinct="", format="json",
            )
            return _wrap_count(raw, service, entity_set, where, exact=True)
        except EpicorError:
            pass  # fall through to the join count

    # (2) Parent/child join + count(*) rollup.
    if join_fn is None:
        return json.dumps(error_envelope(
            "count_failed",
            f"Could not count {service}/{entity_set}: no countable OData "
            "collection and the parent/child join engine is unavailable."))
    parent_cols = _field_names(index, service, parent)
    pivot = _query._CHILD_TO_PARENT_PIVOT.get(entity_set)
    join_keys = (
        {k.lower() for k in pivot[1]}
        if pivot and pivot[0] == parent else set())
    p_parts, c_parts, unknown, mixed = _route_conjuncts(
        odata_filter, parent_cols, child_cols,
        prefer_child=True, join_keys=join_keys or None)
    if unknown:
        return json.dumps(unknown_columns_envelope(
            target=f"{service}/{entity_set}",
            unknown=sorted(set(unknown)),
            arguments={"where": where},
            valid={
                parent: column_help(
                    service, parent, sorted(parent_cols), unknown),
                entity_set: column_help(
                    service, entity_set, sorted(child_cols), unknown),
            },
            retry_with={"target": f"{service}/{entity_set}",
                        "count_only": True},
            lead=f"exist on neither {parent} nor {entity_set}",
            tail="Retry with valid names (see valid)",
        ))
    # Unroutable mixed conjuncts stay on the child (the grain being counted);
    # Epicor rejects a genuinely bad clause with a real message.
    c_parts.extend(mixed)
    raw = await join_fn(
        service=service,
        parent_entity=parent,
        child_entity=entity_set,
        parent_filter=" and ".join(p_parts),
        child_filter=" and ".join(c_parts),
        parent_select="",
        child_select="",
        group_by="",
        aggregate="count(*) as row_count",
        page_size=500,
        max_pages=20,
        format="json",
    )
    try:
        payload = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(payload, dict) or "error" in payload:
        return json.dumps(error_envelope(
            "count_failed",
            f"Could not count {service}/{entity_set}: "
            f"{payload.get('error') if isinstance(payload, dict) else raw}"))
    recs = payload.get("records") or []
    cnt = recs[0].get("row_count") if recs and isinstance(recs[0], dict) else 0
    partial = bool(payload.get("truncated") or payload.get("scan_error"))
    # A join-engine 'warning' (zero child population, unresolved join key,
    # all-orphans) means the join itself is suspect — never stamp its count
    # exact, especially the confident-looking 0.
    suspect = payload.get("warning") or payload.get("rollup_warning")
    out = {
        "summary": (f"{cnt}{'+ (capped — the scan hit its page ceiling)' if partial else ''} "
                    f"{entity_set} record(s)"
                    + (f" where {where.strip()}" if where.strip() else "")
                    + ("." if not suspect else " — count may be unreliable, see note.")),
        "stop_hint": "This count answers the question — present it; do not re-query.",
        "count": cnt,
        "exact": not (partial or suspect),
        "resolved": {"service": service, "entity_set": entity_set,
                     "counted_via": f"{parent}+{entity_set} join"},
    }
    if partial:
        out["note"] = str(payload.get("truncated") or payload.get("scan_error"))
    elif suspect:
        out["note"] = str(suspect)
    return json.dumps(out, default=str)


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    baq_index: "BAQSchemaIndex | None" = None,
    dataset_handler: "DatasetHandler | None" = None,
) -> None:
    """Bind the ``epicor_read`` tool to *server*."""

    # Capture the parent/child join engine once at registration time.
    _children_join = _capture_children_fn(index, rbac, client)
    # Screen -> BO map for screen-grounded discovery (hot-reloads on rebuild).
    _screen_map = ScreenMap()

    @server.tool(structured_output=False, description=_DESCRIPTION)
    async def epicor_read(
        target: str,
        fields: str | list[str] = "",
        where: str = "",
        children: str | list[str] = "",
        group_by: str | list[str] = "",
        aggregate: str | list[str] = "",
        having: str = "",
        order_by: str = "",
        limit: int = 0,
        top: int = 0,
        count_only: bool = False,
        cursor: str = "",
    ) -> str:
        """Read Epicor data in one call. See tool description."""
        # List / JSON-array forms -> comma-separated strings, before anything
        # downstream does a .split(",").
        fields = coerce_csv(fields)
        children = coerce_csv(children)
        group_by = coerce_csv(group_by)
        aggregate = coerce_csv(aggregate)
        try:
            # --- Resume from an opaque cursor -----------------------------
            skip = 0
            cursor_limit = 0
            if cursor:
                ctx = _decode_cursor(cursor)
                if ctx:
                    skip = int(ctx.get("skip", 0) or 0)
                    target = target or ctx.get("target", "")
                    fields = fields or ctx.get("fields", "")
                    where = where or ctx.get("where", "")
                    children = children or ctx.get("children", "")
                    group_by = group_by or ctx.get("group_by", "")
                    aggregate = aggregate or ctx.get("aggregate", "")
                    # A dropped `having` on page 2 silently WIDENS the result.
                    having = having or ctx.get("having", "")
                    # Same trap as `having`: page 2 re-ordered under a $skip
                    # computed against page 1's ordering silently duplicates
                    # and drops rows.
                    order_by = order_by or ctx.get("order_by", "")
                    cursor_limit = int(ctx.get("limit") or 0)
            limit = _effective_limit(limit, top, cursor_limit)

            # Loud clamp: silent truncation to 1000 reads as "that's all the
            # data" and invites ever-larger limit retries.
            requested_limit = int(limit)
            limit = max(1, min(requested_limit, _MAX_LIMIT))
            clamp_note = None
            if requested_limit > _MAX_LIMIT:
                clamp_note = (
                    f"Requested limit {requested_limit} exceeds the "
                    f"{_MAX_LIMIT}-row page cap; this page holds the first "
                    f"{_MAX_LIMIT} (next_cursor pages onward). For totals/"
                    "rankings pass group_by/aggregate — they auto-scan past "
                    "the cap; for counts use count_only=true.")

            # Models write filters INTO the target, which cannot resolve as
            # a table name. Split the condition into `where` before resolution.
            if target and not where:
                split = re.split(r"\s+where\s+", target, maxsplit=1,
                                 flags=re.IGNORECASE)
                if len(split) == 2 and split[1].strip():
                    target, where = split[0].strip(), split[1].strip()

            # year(X) = N is not valid OData or GetRows SQL — expand it to an
            # explicit date range before any translation sees it.
            where = _expand_year_fn(where)

            # Fail-soft assumptions bag (picked target, corrected fields/where,
            # resolved site). Rides back in resolved.assumptions + the summary.
            soft: dict = {}
            # What the argument guard aliased/ignored on the way in — surfaced
            # here so an alias (select->fields) is never invisible.
            soft.update(get_arg_notes())
            # Site filters: a place NAME ('Main Site'), abbreviation ('BL'), or the
            # wrong column (SiteID/Site) becomes the real Plant code, so the
            # model can filter Plant='Main Site' and never has to know 10==Main Site.
            if where:
                where, _site_notes = _resolve_site_filter(where)
                if _site_notes:
                    soft["site_resolved"] = _site_notes

            session = get_current_session()

            # --- Dashboard asks belong to epicor_baq ----------------------
            # A phrase such as 'open backlog dashboard data' would resolve here
            # to report-param tables that can't be read. Redirect before
            # resolution ever runs.
            m = re.search(r"\bdashboards?\b", target or "", re.IGNORECASE)
            if m:
                dash_name = re.sub(
                    r"\b(?:dashboards?|data|the)\b", " ", target,
                    flags=re.IGNORECASE,
                ).strip(" ,.-")
                return json.dumps(error_envelope(
                    "use_dashboard_action",
                    "The user is pointing you at a DASHBOARD. epicor_read "
                    "reads business-object tables, not dashboards. Call "
                    "epicor_baq with action='dashboard' — set `baq` to the "
                    "dashboard name if one was given, or leave it EMPTY to "
                    "list the available dashboards and ask which they mean. "
                    "It resolves the dashboard's BAQs and runs them in one "
                    "call. Do NOT hunt for the dashboard's data in tables.",
                    retry_with={"tool": "epicor_baq", "action": "dashboard",
                                "baq": dash_name},
                    # A redirect that swallows the caller's other arguments
                    # teaches the model they were accepted.
                    detail=({"ignored": sorted(
                        k for k, v in {
                            "order_by": order_by, "fields": fields,
                            "group_by": group_by, "aggregate": aggregate,
                            "having": having, "limit": limit,
                        }.items() if v not in (None, "", 0, False))}
                        or None),
                ))

            # --- Screen-grounded discovery (menu -> BOs -> fields) --------
            # "tables behind the Job Tracker screen" / "Job Tracker screen" /
            # "which screens can I see": the user (often via their system
            # prompt) names a screen; hand back the business objects, entity
            # sets, and key fields behind it in ONE call (RBAC-filtered) so the
            # model stops hunting a BO. Uses data/menu_security.db (the same
            # map RBAC is built on). Checked before generic resolution.
            screen_q = detect_screen_query(target)
            if screen_q is not None:
                _screen_map.maybe_reload()
                # Screen metadata, not business rows: a caller's order_by /
                # fields / limit were meant for the data read that FOLLOWS.
                return _with_assumptions(
                    screen_discovery(index, rbac, session, _screen_map,
                                     mode=screen_q[0], name=screen_q[1]),
                    {**soft, **_args_ignored_note(
                        "screen-discovery", order_by=order_by, fields=fields,
                        having=having, count_only=count_only,
                        group_by=group_by, aggregate=aggregate)})

            # --- Attachments / linked documents (INV-2, GetRows sibling opt-in) -
            # "the invoice PDF", "attachments for X", "what file is linked to
            # X" had NO route: *Attch tables are INVISIBLE over OData (the
            # plural collection 500s, the singular 404s — the WhereUsed /
            # JobMtls / CustCnt trap) and GetRows pinned every sibling to
            # "1=0", excluding attachments. Ordered FIRST among entity recognizers on
            # purpose: an attachment phrase can legitimately carry a part / job
            # / "used on" fragment ("the drawing attached to the part used on
            # job 123"), which where-used or BOM would steal. The reverse steal
            # is just as real — "attached" is ORDINARY English and co-occurs
            # with BOM vocabulary ("the routing attached to part X") — so the
            # tie-break lives in detect_attachments itself: whichever
            # vocabulary the phrase LEADS with owns it. Position, not order.
            if detect_attachments(target, where):
                return await read_attachments(
                    client, index, rbac, session,
                    target=target, where=where, limit=limit,
                    order_by=order_by, fields=fields,
                    soft={**soft, **_args_ignored_note(
                        "attachments", having=having, group_by=group_by,
                        aggregate=aggregate, count_only=count_only,
                        cursor=cursor)})

            # --- Part WHERE-USED: parents of a part (INV-2, method-backed) ----
            # "what is part X used to make / used in / where used" is the
            # INVERSE of the BOM read: it needs the upward BOM path.
            # One Get*-scoped method answers it:
            # PartSvc/GetPartWhereUsed. Checked BEFORE the BOM recognizer so
            # "which BOM is X used in" routes upward, not downward.
            if detect_where_used(target, where):
                return await where_used(
                    client, rbac, session,
                    target=target, where=where, limit=limit,
                    order_by=order_by, fields=fields,
                    soft={**soft, **_args_ignored_note(
                        "where-used", having=having, group_by=group_by,
                        aggregate=aggregate, count_only=count_only,
                        cursor=cursor)})

            # --- Part views: time phase + BOM (INV-2, no new tool) --------
            # "time phase for part X" is a computed dataset (a method call),
            # and "BOM for part X" is a GetRows on a different service — both
            # are part-centric reads the model shouldn't have to route. Sniff
            # the intent and dispatch to the engine helper before generic
            # resolution can mis-resolve the phrase.
            part_view = detect_part_view(target)
            if part_view == "timephase":
                return await read_timephase(
                    client, rbac, session, where=where, target=target,
                    fields=fields, limit=limit, order_by=order_by,
                    soft={**soft, **_args_ignored_note(
                        "time-phase", having=having, group_by=group_by,
                        aggregate=aggregate, count_only=count_only,
                        cursor=cursor)})
            if part_view == "bom":
                return await read_bom(
                    client, index, rbac, session, where=where, target=target,
                    fields=fields, limit=limit, order_by=order_by,
                    soft={**soft, **_args_ignored_note(
                        "BOM", having=having, group_by=group_by,
                        aggregate=aggregate, count_only=count_only,
                        cursor=cursor)})

            # --- Production yield trend for a part (INV-2, computed metric) ---
            # "trend production yield / scrap for part X over N months" is a
            # group-by plus a ratio across distinct source tables.
            # Compute it here: JobHead.QtyCompleted (good) vs
            # Σ JobOper.ScrapQty (scrap), bucketed by month. Phrased as a read,
            # so it lives in epicor_read, not a dedicated tool.
            if detect_yield_trend(target):
                return await yield_trend(
                    client, index, rbac, session,
                    target=target, where=where, limit=limit,
                    order_by=order_by,
                    soft={**soft, **_args_ignored_note(
                        "yield-trend", fields=fields, having=having,
                        count_only=count_only, cursor=cursor)})

            # --- Jobs by PLANNER (name/code -> JobHead.PersonID, INV-2) -------
            # "jobs for planner Jamie Reed" thrashes (the model guesses
            # Planner/PlannerID/PlanUserID — all wrong) because the planner is
            # JobHead.PersonID, a code like "Plan2" whose name lives in the
            # Person master under a CASUAL spelling ("Jamie R"). Bridge
            # name -> code -> jobs in one call. Roster ("list the planners")
            # answers from the Person master directly.
            if detect_planner_roster(target):
                return await planner_roster(
                    client, index, rbac, session, target=target,
                    order_by=order_by,
                    soft={**soft, **_args_ignored_note(
                        "planner-roster", fields=fields, having=having,
                        group_by=group_by, aggregate=aggregate,
                        count_only=count_only, cursor=cursor)})
            if detect_planner_jobs(target):
                return await planner_jobs(
                    client, index, rbac, session,
                    target=target, where=where, limit=limit,
                    order_by=order_by,
                    soft={**soft, **_args_ignored_note(
                        "planner-jobs", fields=fields, having=having,
                        group_by=group_by, aggregate=aggregate,
                        count_only=count_only, cursor=cursor)})

            # --- PO suggestions: NEW (buy) vs CHANGE (INV-2, look-alike BOs) --
            # "PO change suggestions" has no synonym, so the bare "po" word-
            # matches POHeader and returns purchase orders instead.
            # Route NEW vs CHANGE deterministically, drop the reflex OpenOrder
            # filter (suggestion tables have no such column), and note which BO
            # this is + how to pivot to the other.
            po_kind = detect_po_sugg(target)
            if po_kind is not None:
                return await po_suggestions(
                    client, index, rbac, session,
                    kind=po_kind, target=target, where=where, limit=limit,
                    order_by=order_by, fields_wanted=fields,
                    soft={**soft, **_args_ignored_note(
                        "PO-suggestions", having=having, group_by=group_by,
                        aggregate=aggregate, count_only=count_only,
                        cursor=cursor)})

            # --- Analytical 'top N by measure' (business-object path, no BAQ) ---
            # "top parts by sales", "biggest customers", "vendors by spend" are
            # single-entity group-bys once you know where the measure lives. Do
            # it here so the model never thrashes hunting the entity/column and
            # never needs a BAQ (which most users can't create).
            recipe = None if (group_by or aggregate) else match_analytical(target)
            if recipe:
                return await _run_analytic(
                    client, index, rbac, session, recipe, target, where, limit,
                    order_by=order_by, soft=soft)

            # "top parts by profit margin": subject + rank trigger but no
            # computable measure. The subject word alone still RESOLVES
            # (PartSvc) and would return a plausible-but-useless plain
            # listing — ask "rank by what?" first, with the real options.
            if not (group_by or aggregate or count_only):
                half = subjects_without_measure(target)
                if half:
                    return json.dumps(error_envelope(
                        "measure_needed",
                        "Recognised a ranking request but not the measure. "
                        "If the user asked for one of the measures in "
                        "valid.rankings, re-call with that exact phrase. If "
                        "they asked for anything else (profit margin, cost, "
                        "lead time, ...), that ranking is NOT available "
                        "without a custom report — STOP, do not hunt for the "
                        "data in other tables, and tell the user.",
                        valid={"rankings": [
                            {"subject": h["subject"], "measure": h["measure"],
                             "produces": h["label"]} for h in half]},
                        retry_with={"target": half[0]["example"]},
                    ))

            # "how many X" → resolved normally below, then a TRUE count.
            want_count = bool(count_only) or is_count_query(target)

            # --- Resolve the target (INV-1 on ambiguity) ------------------
            # (Fail-soft `soft` bag was initialised above, before site
            # resolution; a picked candidate + corrected fields/where join the
            # site notes there and ride back in resolved.assumptions.)
            res = resolve_target(index, target)
            service = res.get("service") or ""
            entity_set = res.get("entity_set") or ""
            if not service and len(res.get("candidates") or []) == 1:
                # Exactly ONE candidate is effectively resolved — use it. With
                # TWO OR MORE, do NOT silently pick: a wrong guess returns data
                # that doesn't answer and the model hunts through the other
                # candidates. The ambiguous_target error below hands back the
                # candidate list, which converges in one hop — that precise
                # error IS the fast path, not a failure to avoid.
                best = res["candidates"][0]
                if best.get("service"):
                    service = best["service"]
                    entity_set = best.get("entity_set") or ""
                    soft["target_assumed"] = {
                        "used": best.get("target") or f"{service}/{entity_set}",
                        "for_phrase": target,
                    }
            if not service:
                # A model can pass an analytical phrase as
                # target WITH group_by/aggregate — that skips the recipe
                # pre-route, then the phrase can't resolve. If the phrase is
                # a known recipe, run it (the model's where still applies;
                # the recipe owns the grouping).
                fb = match_analytical(target)
                if fb:
                    return await _run_analytic(
                        client, index, rbac, session, fb, target, where, limit,
                        order_by=order_by, soft=soft)
                # Subject matched but no computable measure ("top parts by
                # profit margin") — ask "rank by what?" with the real options
                # instead of a generic unresolved_target.
                half = subjects_without_measure(target)
                if half:
                    return json.dumps(error_envelope(
                        "measure_needed",
                        "Recognised a ranking request but not the measure. "
                        "If the user asked for one of the measures in "
                        "valid.rankings, re-call with that exact phrase; any "
                        "other measure is NOT available without a custom "
                        "report — STOP and tell the user.",
                        valid={"rankings": [
                            {"subject": h["subject"], "measure": h["measure"],
                             "produces": h["label"]} for h in half]},
                        retry_with={"target": half[0]["example"]},
                    ))
                reason = "ambiguous_target" if res.get("candidates") else "unresolved_target"
                # Screen-aware fallback: if the phrase names an Epicor screen,
                # hand back that screen's business objects so a retry lands —
                # closing the loop the model opens when it asks "what screen
                # are you on?" and then has nowhere to put the answer.
                valid = None
                try:
                    _screen_map.maybe_reload()
                    screen_hits = screens_for_term(_screen_map, target)
                except Exception:
                    screen_hits = []
                if screen_hits:
                    valid = {"screens": screen_hits}
                return json.dumps(error_envelope(
                    reason,
                    "Could not resolve 'target' to one Epicor service/entity. "
                    "Re-call with an exact 'Service/Entity' from candidates"
                    + (", or name the screen this data lives on (see "
                       "valid.screens)." if screen_hits else "."),
                    valid=valid,
                    candidates=res.get("candidates") or None,
                ))
            filter_hint = res.get("filter_hint") or ""

            # --- Fast-path routing: heavy maintenance BO -> SearchSvc twin ----
            # A heavy BO may require a broad GetRows scan; its SearchSvc twin
            # serves the same columns over filterable OData. Swap when a twin exists
            # and the user can read it. NOT for a children-join (that needs the
            # real parent service) — those keep the maintenance BO.
            if not children and is_heavy(service):
                twin = _search_service_for(index, entity_set)
                if twin and twin != service:
                    twin_ok, _ = rbac.check_access(session.user_id, twin)
                    if twin_ok:
                        soft["service_via"] = f"{twin} (fast search endpoint)"
                        service = twin

            # Vendor/customer contacts don't come back from their own collection
            # or a child GetRows — only from the parent's GetByID dataset. Serve
            # them through that path (own RBAC on the parent service) so
            # "vendor contacts for Example Company" is one call, not a VendorNum loop.
            if entity_set in _CONTACT_PARENTS:
                return await _read_contacts(
                    client, index, rbac, session, service, entity_set,
                    target, where, fields, limit, count_only=want_count,
                    order_by=order_by, soft=soft)

            # --- RBAC -----------------------------------------------------
            allowed, msg = rbac.check_access(session.user_id, service)
            if not allowed:
                return json.dumps(error_envelope("access_denied", msg))
            api_key = rbac.check_service_access(session.user_id, service).api_key or ""

            # Parent/child dataset shape — needed by the count path (child
            # counts must go through the parent) and the join routing below.
            pair = _query._resolve_parent_child(entity_set)
            is_child_table = (
                pair is not None
                and entity_set in _query._CHILD_TO_PARENT_PIVOT
                and _has_real_table(index, service, pair[0])
                and _has_real_table(index, service, entity_set))

            # --- Count intent → TRUE $count (never a capped GetRows page) --
            if want_count:
                if order_by.strip():
                    # Never silent: a count is a scalar, so the sort genuinely
                    # cannot apply — say so rather than drop it.
                    soft["order_ignored"] = (
                        "a count returns a scalar; ordering does not apply.")
                cf = (sql_to_odata(
                          where, date_columns=date_columns_for(
                              index, service, entity_set))
                      if where.strip() else "")
                if filter_hint and filter_hint.lower() not in cf.lower():
                    cf = f"({cf}) and {filter_hint}" if cf else filter_hint
                if is_child_table:
                    # A child/detail table is not an OData segment and GetRows
                    # zeroes its parent — both generic count paths return a
                    # confident 0. Count via the plural collection or the join.
                    return await _run_child_count(
                        client, index, service, entity_set, pair[0], api_key,
                        cf, where, _children_join)
                return await _run_count(client, index, service, entity_set, api_key, cf, where)

            # --- Translate SQL-ish where -> OData, AND in the term hint ---
            # date_columns comes from the index's real Edm types so a quoted
            # ISO literal is unquoted ONLY on an actual date column — a quoted
            # date against an Edm.String column is legitimate and must survive.
            _dcols = date_columns_for(index, service, entity_set)
            odata_filter = (
                sql_to_odata(where, date_columns=_dcols) if where.strip() else "")
            if filter_hint and filter_hint.lower() not in odata_filter.lower():
                odata_filter = (
                    f"({odata_filter}) and {filter_hint}"
                    if odata_filter else filter_hint
                )

            # --- Join routing: explicit children, child-table auto-join, --
            # --- or the single-record header fast path ---------------------
            auto_child = False
            single_label = None
            if pair and not children.strip():
                if is_child_table:
                    # Direct read of a detail table: GetRows only returns the
                    # service's PRIMARY table, so a bare child read comes back
                    # empty/wrong — join via parent.
                    # This applies to ROLLUPS too: a group_by/aggregate run
                    # directly on the child pages an empty dataset (parent
                    # whereClause 1=0) and would report 0 rows, complete=true.
                    auto_child = True
                    children = entity_set
                elif ((group_by or aggregate) and entity_set == pair[0]
                        and pair[1]):
                    # Rollup on a HEADER whose group_by/aggregate reaches a
                    # child column (e.g. OrderHed grouped by OrderDtl.PartNum,
                    # summing OrderQty): validated against the header alone the
                    # child columns read as unknown_columns and the model
                    # thrashes. Auto-join the default child so the header/detail
                    # join partitions the columns and rolls up in ONE call — the
                    # model shouldn't have to know PartNum lives on OrderDtl.
                    parent_have = _field_names(index, service, entity_set)
                    if _rollup_reaches_child(parent_have, group_by, aggregate):
                        children = pair[1]
                elif (not group_by and not aggregate
                        and entity_set in _SINGLE_RECORD_KEYS and where.strip()):
                    key, label = _SINGLE_RECORD_KEYS[entity_set]
                    m = _SINGLE_EQ_RE.match(where)
                    if (m and m.group(1).lower() == key.lower()
                            and _has_real_table(index, service, pair[1])):
                        # "what's on PO 10001" → ONE call: header + lines.
                        children = pair[1]
                        single_label = f"{label} {m.group(2).strip(chr(39))}"

            cursor_ctx = {
                "target": target, "fields": fields, "where": where,
                "children": children, "group_by": group_by,
                "aggregate": aggregate, "having": having,
                "order_by": order_by, "limit": limit,
            }

            # --- children: single-call parent/child join ------------------
            if children.strip():
                if pair is None:
                    return json.dumps(error_envelope(
                        "children_unsupported",
                        f"No known parent/child dataset for {service}/"
                        f"{entity_set}; query the child entity directly.",
                    ))
                if _children_join is None:
                    return json.dumps(error_envelope(
                        "children_unavailable",
                        "The parent/child join engine is not available.",
                    ))
                parent, default_child = pair
                child_entity = (
                    entity_set if auto_child
                    else _match_child(index, service, children, default_child))

                # Validate fields/where against BOTH tables, not just the
                # parent — child columns (PartNum, DocExtPrice) are legal here.
                parent_cols = _field_names(index, service, parent)
                child_cols = _field_names(index, service, child_entity)
                aggregated_join = bool(group_by or aggregate)
                if fields.strip():
                    requested = [f.strip() for f in fields.split(",") if f.strip()]
                elif aggregated_join:
                    # A rollup projects its own columns — start empty and add
                    # exactly the group_by/aggregate fields below.
                    requested = []
                else:
                    # Curated defaults for the entity the caller targeted.
                    _jres = resolve_fields(index, service, entity_set, "")
                    requested = _jres["fields"]
                    if _jres.get("capped"):
                        # Announced here for the same reason the plain path
                        # announces it: the model must be able to see it got
                        # a subset of the columns, or it reads the narrowed row as
                        # the whole record and never asks for the rest.
                        soft["fields_capped"] = {
                            "shown": len(requested),
                            "of": _jres["total_columns"]}
                if aggregated_join:
                    # Ensure every rollup column rides on the joined rows
                    # (partitioned to whichever table owns it); otherwise the
                    # join's fail-fast validation rejects the group_by.
                    have = {r.lower() for r in requested}
                    for col in _rollup_columns(group_by, aggregate):
                        if col.lower() not in have:
                            requested.append(col)
                            have.add(col.lower())

                # Join-key columns exist on BOTH tables; conditions on them
                # must land on the PARENT so the scan is bounded (see
                # _route_conjuncts).
                jk_pivot = _query._CHILD_TO_PARENT_PIVOT.get(child_entity)
                join_key_cols = (
                    {k.lower() for k in jk_pivot[1]}
                    if jk_pivot and jk_pivot[0] == parent else set())

                if parent_cols and child_cols:
                    union_cols = list(parent_cols) + list(child_cols)
                    union_low = {c.lower() for c in union_cols}
                    # Auto-correct a high-confidence typo against EITHER table;
                    # a field with no confident match stays unknown and errors
                    # below (precise redirect beats a silent drop that hides the
                    # right column / signals the wrong target).
                    cleaned, cmap = [], {}
                    for f in requested:
                        if f.lower() in union_low:
                            cleaned.append(f)
                            continue
                        fix = _correct_column(f, union_cols)
                        if fix:
                            cleaned.append(fix)
                            cmap[f] = fix
                        else:
                            cleaned.append(f)   # keep -> _partition_fields flags it
                    if cmap:
                        soft.setdefault("fields_corrected", {}).update(cmap)
                    requested = cleaned
                    psel, csel, unknown_f = _partition_fields(
                        requested, parent_cols, child_cols,
                        prefer_child=auto_child)
                    if unknown_f:
                        # Same both-sides-in-ONE-envelope rule the plain path
                        # follows: routing the where happens two lines below,
                        # so probe it here rather than let the model guess that
                        # its filter is why the call failed. A name the
                        # corrector would repair is not a caller error.
                        w_probe: list[str] = []
                        if odata_filter.strip():
                            _pp, _cp, probe_w, _mx = _route_conjuncts(
                                odata_filter, parent_cols, child_cols,
                                prefer_child=auto_child,
                                join_keys=join_key_cols or None)
                            w_probe = [
                                w for w in probe_w
                                if not _correct_column(w, union_cols)]
                        unknown_all = unknown_f + [
                            w for w in w_probe
                            if w.lower() not in {u.lower() for u in unknown_f}]
                        return json.dumps(unknown_columns_envelope(
                            target=f"{service}/{entity_set}",
                            unknown=unknown_all,
                            arguments={"where": where, "fields": fields},
                            valid={
                                parent: column_help(
                                    service, parent,
                                    sorted(parent_cols), unknown_all),
                                child_entity: column_help(
                                    service, child_entity,
                                    sorted(child_cols), unknown_all),
                            },
                            good_fields=psel + csel,
                            retry_with={"target": f"{service}/{entity_set}"},
                            lead=f"exist on neither {parent} nor "
                                 f"{child_entity}",
                            tail="Retry with valid names from either table "
                                 "(see valid)",
                        ))
                    p_parts, c_parts, unknown_w, mixed = _route_conjuncts(
                        odata_filter, parent_cols, child_cols,
                        prefer_child=auto_child,
                        join_keys=join_key_cols or None)
                    if unknown_w:
                        # Auto-correct where columns across both tables, rewrite
                        # the filter, and re-route; only the uncorrectable ones
                        # error (a filter on a truly-absent column).
                        wcorr = {}
                        for w in unknown_w:
                            fix = _correct_column(w, union_cols)
                            if fix and fix.lower() != w.lower():
                                wcorr[w] = fix
                        if wcorr:
                            odata_filter = _rewrite_filter_columns(
                                odata_filter, wcorr)
                            soft.setdefault("where_corrected", {}).update(wcorr)
                            p_parts, c_parts, unknown_w, mixed = _route_conjuncts(
                                odata_filter, parent_cols, child_cols,
                                prefer_child=auto_child,
                                join_keys=join_key_cols or None)
                    if unknown_w:
                        # `fields` already partitioned cleanly above — say so,
                        # and hand the resolved projection back so the retry is
                        # the same call with one filter column corrected.
                        return json.dumps(unknown_columns_envelope(
                            target=f"{service}/{entity_set}",
                            unknown=unknown_w,
                            arguments={"where": where, "fields": fields},
                            valid={
                                parent: column_help(
                                    service, parent, sorted(parent_cols), unknown_w),
                                child_entity: column_help(
                                    service, child_entity, sorted(child_cols), unknown_w),
                            },
                            retry_with={"target": f"{service}/{entity_set}"},
                            lead=f"exist on neither {parent} nor "
                                 f"{child_entity}",
                            tail="Retry with valid names (see valid)",
                        ))
                    if mixed:
                        return json.dumps(error_envelope(
                            "filter_mixed_tables",
                            f"Condition(s) {mixed} mix {parent} and "
                            f"{child_entity} columns inside one expression; "
                            "write each AND-condition against a single table.",
                            retry_with={"target": f"{service}/{entity_set}"},
                        ))
                    parent_filter = " and ".join(p_parts)
                    child_filter = " and ".join(c_parts)
                    parent_select = ",".join(psel)
                    child_select = ",".join(csel)

                    # --- Caller sort: resolve BEFORE the scan --------------
                    # order_by was the ONE join argument checked only AFTER
                    # the join materialised (the post-sort block in
                    # query_with_children), so a mistyped sort column cost a
                    # full paginated scan just to be told the
                    # name was wrong. The BAQ-alias form that `fields` and
                    # `where` already auto-correct here was never corrected
                    # for order_by either, so the model burned that scan on
                    # the one argument the corrector could not see.
                    if order_by.strip():
                        o_terms, o_kind = parse_order_by(order_by)
                        if o_kind or not o_terms:
                            return json.dumps(error_envelope(
                                "order_expression_unsupported",
                                "order_by takes plain columns only ('Col', "
                                "'Col desc', comma-separated). To rank by a "
                                "computed measure use group_by + aggregate "
                                "and order_by the aggregate's alias.",
                                retry_with={
                                    "target": f"{service}/{entity_set}",
                                    "group_by": "<dimension>",
                                    "aggregate": "sum(<measure>) as Total",
                                    "order_by": "Total desc"},
                            ))
                        parent_low = {c.lower() for c in parent_cols}
                        proj = {c.lower(): c for c in psel + csel}
                        union_map = {c.lower(): c for c in union_cols}
                        try:
                            o_types = {
                                **index.get_field_types(service, parent),
                                **index.get_field_types(service, child_entity)}
                        except Exception:  # noqa: BLE001 — types are advisory
                            o_types = {}
                        sorted_terms, bad_order = [], []
                        for col, direction in o_terms:
                            real = proj.get(col.lower()) or union_map.get(
                                col.lower())
                            if real is None:
                                fix = _correct_column(col, union_cols)
                                # A boolean sort key is nonsense, and the
                                # substring rule reaches one (OnHandQty ->
                                # HasOnHandQty). Unlike a bad filter, a
                                # boolean ranking still returns plausible
                                # rows, so nothing downstream catches it --
                                # refuse rather than guess.
                                if fix and (o_types.get(fix)
                                            or "") != "Edm.Boolean":
                                    real = fix
                                    soft.setdefault(
                                        "order_corrected", {})[col] = fix
                            if real is None:
                                bad_order.append(col)
                                continue
                            if real.lower() not in proj:
                                # Valid but projected away. Carrying it costs
                                # one column and saves the caller a second
                                # full scan to discover it must be selected.
                                if real.lower() in parent_low:
                                    psel.append(real)
                                else:
                                    csel.append(real)
                                proj[real.lower()] = real
                                soft.setdefault(
                                    "order_column_added", []).append(real)
                            sorted_terms.append((real, direction))
                        if bad_order:
                            return json.dumps(unknown_columns_envelope(
                                target=f"{service}/{entity_set}",
                                unknown=bad_order,
                                arguments={"where": where, "fields": fields,
                                           "order_by": order_by},
                                valid={
                                    parent: column_help(
                                        service, parent, sorted(parent_cols),
                                        bad_order),
                                    child_entity: column_help(
                                        service, child_entity,
                                        sorted(child_cols), bad_order),
                                },
                                retry_with={
                                    "target": f"{service}/{entity_set}"},
                                lead=f"exist on neither {parent} nor "
                                     f"{child_entity}",
                                tail="Retry with a name from valid",
                            ))
                        # Keep the caller's ORIGINAL clause in the audit trail
                        # (soft), but scan with the resolved one.
                        order_by = order_terms_to_clause(sorted_terms)
                        parent_select = ",".join(psel)
                        child_select = ",".join(csel)

                        # An unbounded global sort cannot finish. With no
                        # parent-side conjunct at all, ranking the whole set
                        # means paging every header page of a heavy BO: the
                        # scan hits max_pages, and the answer is an 82s
                        # ranking the engine itself then labels "NOT a global
                        # top-N". Paying maximum cost for a result we already
                        # know is incomplete is the worst of both worlds --
                        # refuse in ~300ms and name the paths that do work.
                        if (not aggregated_join and not single_label
                                and not p_parts and is_heavy(service)):
                            return json.dumps(error_envelope(
                                "order_scan_unbounded",
                                f"Ranking {child_entity} by '{order_by}' with "
                                f"no {parent}-side condition would have to "
                                f"scan every {parent} page, which cannot "
                                "complete -- the result would be a partial "
                                "ranking presented as a top-N. Do ONE of: "
                                f"add a {parent} condition to `where` so the "
                                "scan is bounded; drop `order_by` for a fast "
                                "unordered page; or use group_by + aggregate "
                                "to rank a measure.",
                                valid={parent: column_help(
                                    service, parent, sorted(parent_cols), [])},
                                retry_with={
                                    "target": f"{service}/{entity_set}",
                                    # str.strip takes a CHARACTER SET, not a
                                    # suffix -- ".strip(' and ')" ate any
                                    # leading run of {' ','a','n','d'}, so a
                                    # caller where like "Approved eq true"
                                    # came back corrupted as "pproved eq true".
                                    "where": (f"{where.strip()} and <{parent} "
                                              "condition>") if where.strip()
                                    else f"<{parent} condition>",
                                    "order_by": ""},
                            ))
                else:
                    # One side has no indexed schema — keep the legacy
                    # parent-only wiring rather than guess residency.
                    parent_filter, child_filter = odata_filter, ""
                    parent_select, child_select = ",".join(requested), ""

                # An unbounded rollup is the slowest shape this path can run.
                # Sibling of the
                # order_scan_unbounded refusal above, and for the same reason:
                # the 20-page ceiling truncates the totals anyway. A CHILD-only
                # `where` does not bound a PARENT scan, so the test is
                # parent_filter — exactly what `not p_parts` means up there.
                if aggregated_join:
                    refusal = unbounded_rollup_refusal(
                        target=f"{service}/{entity_set}",
                        scanned=parent,
                        group_by=group_by,
                        aggregate=aggregate,
                        where=where,
                        bounded=bool(parent_filter.strip()),
                        date_columns=_real_date_columns(
                            index, service, parent, sorted(parent_cols)),
                        scan_rows=500 * 20,
                        baq_tables=f"Erp.{parent},Erp.{child_entity}",
                    )
                    if refusal:
                        return json.dumps(refusal)

                # Paging posture: 500-row pages fit the 30s per-request budget
                # (1000-row pages on big header tables time out; the join also
                # halves further on a timeout). Rollups scan up to 20 pages
                # and label truncation; raw listings stop as soon as `limit`
                # child rows are in hand instead of paging every header.
                raw = await _children_join(
                    service=service,
                    parent_entity=parent,
                    child_entity=child_entity,
                    parent_filter=parent_filter,
                    child_filter=child_filter,
                    parent_select=parent_select,
                    child_select=child_select,
                    group_by=group_by,
                    aggregate=aggregate,
                    having=having,
                    order_by=order_by,
                    page_size=500,
                    max_pages=20 if aggregated_join else 10,
                    # A caller sort must rank the WHOLE set, so it disables the
                    # early stop: sorting the arbitrary prefix the early stop
                    # happens to collect is not a top-N, it just looks like one.
                    stop_after_child_rows=(
                        0 if (aggregated_join or single_label
                              or order_by.strip()) else limit),
                    format="json",
                )
                return _augment(
                    raw,
                    {"service": service, "entity_set": parent,
                     "child_entity": child_entity, "fields": requested},
                    limit=limit, skip=skip, cursor_ctx=cursor_ctx,
                    paginated=False,   # the join tool pages internally
                    clamp_note=clamp_note, single_label=single_label,
                    trim_to=(
                        None if (aggregated_join or single_label) else limit),
                    assumptions=soft or None,
                )

            # --- Resolve fields (blank => curated default) ----------------
            fres = resolve_fields(index, service, entity_set, fields)
            if fres.get("capped"):
                # Announced, never silent: the model must be able to see that
                # it got a column subset and how to ask for any other.
                soft["fields_capped"] = {
                    "shown": len(fres["fields"]), "of": fres["total_columns"]}
            if fres["unknown"]:
                # Silently auto-correct only a HIGH-confidence typo (case/alias/
                # sub-string, e.g. Description->PartDescription). A field with
                # NO confident match is NOT dropped: dropping the column that IS
                # the question (ScrapQty off the wrong table) hid the "valid
                # columns are..." redirect and the model hunted. Error precisely
                # instead — that redirect converges in one hop.
                mapped, still_unknown = {}, []
                for bad in fres["unknown"]:
                    cand = _correct_column(bad, fres["valid_columns"])
                    if cand:
                        fres["fields"].append(cand)
                        mapped[bad] = cand
                    else:
                        still_unknown.append(bad)
                if mapped:
                    soft["fields_corrected"] = mapped
                if still_unknown:
                    nrh = name_resolution_hint(
                        entity_set, f"{where} {','.join(still_unknown)}")
                    # Validate the OTHER side too, so ONE envelope reports both.
                    # Erroring on `fields` while saying nothing about `where` is
                    # what made the model abandon a perfect filter.
                    where_bad = _uncorrectable_where_columns(
                        index, service, entity_set, odata_filter,
                        fres["valid_columns"])
                    unknown_all = still_unknown + [
                        w for w in where_bad
                        if w.lower() not in {u.lower() for u in still_unknown}]
                    env = unknown_columns_envelope(
                        target=f"{service}/{entity_set}",
                        unknown=unknown_all,
                        # ONLY the two arguments actually validated here:
                        # calling an unchecked group_by "clean" is the same
                        # class of lie as not naming the side at all.
                        arguments={"where": where, "fields": fields},
                        valid=column_help(
                            service, entity_set, fres["valid_columns"],
                            unknown_all, index=index),
                        good_fields=[
                            f for f in fres["fields"]
                            if f.lower() not in {u.lower()
                                                 for u in unknown_all}],
                        retry_with={"target": f"{service}/{entity_set}"},
                        tail="Retry with valid column names (see "
                             "valid.did_you_mean / valid.columns) — or a "
                             "different target if this entity doesn't hold "
                             "them",
                    )
                    if nrh:
                        env["resolve_hint"] = nrh
                    return json.dumps(env)
            resolved_fields = fres["fields"]

            aggregated = bool(group_by or aggregate)
            if aggregated:
                # Mirror the join path above: the paged scanners project
                # `select` client-side on EVERY page, so a group_by/aggregate
                # column missing from the projection reads as None on every
                # row and the whole rollup collapses into a single null
                # bucket stamped complete=true — a confidently wrong answer.
                # Re-case the rollup specs to the real schema first
                # (aggregate_records looks keys up case-SENSITIVELY with
                # row.get(), and resolve_fields already re-cases explicit
                # fields), then make sure every rollup column rides on the
                # scanned rows.
                if fres["valid_columns"]:
                    col_set = set(fres["valid_columns"])
                    group_by = _recase_columns(group_by, col_set)
                    aggregate = _recase_columns(aggregate, col_set)
                rollup_cols = _rollup_columns(group_by, aggregate)
                if fres["default_used"] and rollup_cols:
                    # A rollup projects its own columns — the curated
                    # listing defaults are dead weight on a 20-page scan
                    # (same posture as the join path's aggregated_join).
                    resolved_fields = rollup_cols
                else:
                    have = {f.lower() for f in resolved_fields}
                    resolved_fields = resolved_fields + [
                        c for c in rollup_cols if c.lower() not in have]

            select_str = ",".join(resolved_fields)

            # --- Pre-flight column validation on where/select (INV-1) -----
            try:
                unknown_cols = validate_columns(
                    index, service, entity_set, odata_filter, select_str, "",
                )
            except Exception:
                logger.exception("pre-flight column validation crashed; skipping")
                unknown_cols = []
            if unknown_cols:
                # validate_columns returns diagnostic dicts; column_help and
                # the message want the bare column names.
                unknown_refs = [
                    d.get("ref", str(d)) if isinstance(d, dict) else str(d)
                    for d in unknown_cols]
                # Fail-soft: auto-correct where/select columns to a confident
                # real column (Description->PartDescription, the BAQ-alias
                # OrderDtl_PartNum->PartNum) and re-validate. Only genuinely
                # uncorrectable columns error — a filter on a nonexistent
                # column with NO match really would change the answer.
                try:
                    col_types = index.get_field_types(service, entity_set)
                except Exception:  # noqa: BLE001 — no types just means no veto
                    col_types = {}
                corr = {}
                for ref in unknown_refs:
                    fix = _correct_column(ref, fres["valid_columns"])
                    if fix and fix.lower() != ref.lower():
                        if not _correction_type_ok(
                                ref, fix, odata_filter, col_types):
                            # Type-incompatible "fix" — leave `ref` unknown so
                            # it reaches the envelope below, which now names
                            # the entity that really owns the column.
                            continue
                        corr[ref] = fix
                if corr:
                    odata_filter = _rewrite_filter_columns(odata_filter, corr)
                    select_str = _rewrite_filter_columns(select_str, corr)
                    soft.setdefault("where_corrected", {}).update(corr)
                    try:
                        unknown_cols = validate_columns(
                            index, service, entity_set,
                            odata_filter, select_str, "")
                        unknown_refs = [
                            d.get("ref", str(d)) if isinstance(d, dict) else str(d)
                            for d in unknown_cols]
                    except Exception:
                        unknown_refs = []
                if unknown_refs:
                    nrh = name_resolution_hint(entity_set, odata_filter)
                    vhelp = column_help(
                        service, entity_set, fres["valid_columns"],
                        unknown_refs, index=index)
                    # When the column exists on exactly one other entity, offer
                    # that target in retry_with. A HINT inside an ERROR — it
                    # must NOT auto-retarget the read (blind target promotion
                    # multiplies round trips whenever the guess is wrong).
                    retry: dict = {"target": f"{service}/{entity_set}"}
                    owners = {t for ts in (vhelp.get("column_lives_on") or {}).values()
                              for t in ts}
                    if len(owners) == 1:
                        retry = {"target": owners.pop(),
                                 "fields": fields, "where": where}
                    # "in 'where'/'fields'/'group_by'/'aggregate'" named all
                    # four and therefore none of them. A valid filter can
                    # accompany invalid fields. `order_by` is checked later, so it is
                    # deliberately absent here: it has not earned a clean bill.
                    env = unknown_columns_envelope(
                        target=f"{service}/{entity_set}",
                        unknown=unknown_refs,
                        arguments={"where": where, "fields": fields,
                                   "group_by": group_by,
                                   "aggregate": aggregate},
                        valid=vhelp,
                        good_fields=[
                            f for f in resolved_fields
                            if f.lower() not in {u.lower()
                                                 for u in unknown_refs}],
                        retry_with=retry,
                        tail="Retry with valid names (see valid.did_you_mean "
                             "/ valid.columns), or with the entity that DOES "
                             "carry them (valid.column_lives_on)",
                    )
                    if nrh:
                        env["resolve_hint"] = nrh
                    return json.dumps(env)

            resolved_meta = {
                "service": service,
                "entity_set": entity_set,
                "fields": resolved_fields,
            }

            # --- Sort order: caller `order_by`, else newest-first ---------
            # A raw listing with no $orderby comes back oldest-first (PK order),
            # surfacing years-old rows as if current. Sort plain listings by the
            # entity's best date column DESC. Aggregations own their own order;
            # skip them.
            #
            # A caller `order_by` OVERRIDES that default on a plain read, and
            # does so SERVER-side: Epicor orders the whole set and $top/$skip
            # then slice it, so order_by + limit is a true top-N, not a
            # page-local sort. Ignoring it would answer newest-first — "POs due
            # soonest" would come back in exactly the inverse order, looking right.
            # A rollup's order_by ranks GROUPS and is applied in _aggregate.
            default_order = "" if aggregated else default_order_clause(
                service, entity_set, fres["valid_columns"])
            order_source = "default"
            if order_by.strip():
                terms, kind = parse_order_by(order_by)
                if kind == "expression" or not terms:
                    return json.dumps(error_envelope(
                        "order_expression_unsupported",
                        "order_by takes plain columns only ('Col', 'Col desc', "
                        "comma-separated). Epicor's $orderby 500s on "
                        "arithmetic and aggregate functions. To rank by a "
                        "COMPUTED measure, roll it up instead: pass group_by + "
                        "aggregate and order_by the aggregate's alias.",
                        retry_with={
                            "target": f"{service}/{entity_set}",
                            "group_by": "<the grouping column>",
                            "aggregate": "<sum(...) as Measure>",
                            "order_by": "Measure desc",
                        },
                    ))
                if not aggregated and fres["valid_columns"]:
                    # Confident typo fixed silently, ambiguity returned as a
                    # precise error — the KEPT half of the fail-soft contract.
                    fixed: list[tuple[str, str]] = []
                    bad: list[str] = []
                    lower = {c.lower(): c for c in fres["valid_columns"]}
                    # Same Edm.Boolean veto the join path applies: the
                    # substring rule reaches OnHandQty -> HasOnHandQty, and a
                    # boolean sort key still returns plausible rows, so
                    # nothing downstream catches it. This is the FAR more
                    # common path -- it had no veto at all.
                    try:
                        o_types = index.get_field_types(service, entity_set)
                    except Exception:  # noqa: BLE001 — no types, no veto
                        o_types = {}
                    for col, direction in terms:
                        real = lower.get(col.lower())
                        if real is None:
                            fix = _correct_column(col, fres["valid_columns"])
                            if fix and (o_types.get(fix) or "") != "Edm.Boolean":
                                real = fix
                                soft.setdefault("order_corrected", {})[col] = fix
                        if real is None:
                            bad.append(col)
                        else:
                            fixed.append((real, direction))
                    if bad:
                        vhelp = column_help(
                            service, entity_set, fres["valid_columns"], bad,
                            index=index)
                        # `where`/`fields` both cleared validation above, so
                        # this envelope can say so — and carry them into the
                        # retry, which is the whole call minus one sort key.
                        return json.dumps(unknown_columns_envelope(
                            target=f"{service}/{entity_set}",
                            unknown=bad,
                            arguments={"where": where, "fields": fields,
                                       "order_by": order_by},
                            valid=vhelp,
                            retry_with={"target": f"{service}/{entity_set}"},
                            tail="Use a name from valid.columns",
                        ))
                    terms = fixed
                if aggregated:
                    # Ranked client-side over the completed scan; the paged
                    # scan itself stays unordered on purpose (pushing a sort
                    # into a 20-page scan risks the transient-dataset 500 for
                    # zero benefit).
                    #
                    # Validate against the ROLLUP's own key space (group-key
                    # labels + aggregate aliases) BEFORE claiming the caller's
                    # ordering. Stamping order_source="caller" unconditionally
                    # labelled a rank _aggregate had silently refused as the
                    # caller's — e.g. order_by="OrderDate desc" on a rollup
                    # keyed by CustomerName came back ranked by the first
                    # aggregate DESC while the response said OrderDate.
                    valid_sort = _rollup_sort_keys(group_by, aggregate)
                    rterms, bad_sort = resolve_sort_terms(terms, valid_sort)
                    if bad_sort:
                        return json.dumps(unknown_columns_envelope(
                            target=f"{service}/{entity_set}",
                            unknown=bad_sort,
                            # order_by ONLY: a rollup's group_by/aggregate
                            # legitimately mention the same raw column the sort
                            # key names, so attributing across them would blame
                            # a spec that is perfectly valid.
                            arguments={"order_by": order_by},
                            valid={"columns": valid_sort},
                            retry_with={
                                "target": f"{service}/{entity_set}",
                                "group_by": group_by,
                                "aggregate": aggregate,
                                "order_by": f"{valid_sort[-1]} desc",
                            },
                            lead="are not part of this rollup",
                            tail="A rollup is ranked by a group_by key or an "
                                 "aggregate alias — the raw columns of the "
                                 "entity are gone by then",
                        ))
                    terms = rterms
                    order_by = order_terms_to_clause(rterms)
                    resolved_meta["order"] = order_by
                    resolved_meta["order_source"] = "caller"
                    order_source = "caller"
                else:
                    default_order = order_terms_to_clause(terms)
                    order_source = "caller"
                    # A sort column projected away is invisible in the rows —
                    # the model cannot see WHY they are in that order and
                    # re-queries. Carry it into the projection.
                    have = {f.lower() for f in resolved_fields}
                    add = [c for c, _ in terms if c.lower() not in have]
                    if add and resolved_fields:
                        resolved_fields = resolved_fields + add
                        resolved_meta["fields"] = resolved_fields
                        select_str = ",".join(resolved_fields)
                        soft["order_columns_added"] = add
            if default_order:
                resolved_meta["order"] = default_order
            resolved_meta["order_source"] = order_source

            # --- Unbounded-rollup guard (INV-1) ---------------------------
            # Runs LAST of the pre-flight checks so everything it echoes into
            # retry_with has already been resolved and validated: refusing on
            # cost while a column name is also wrong would cost the extra hop
            # this whole surface exists to remove.
            if aggregated:
                refusal = unbounded_rollup_refusal(
                    target=f"{service}/{entity_set}",
                    scanned=entity_set,
                    group_by=group_by,
                    aggregate=aggregate,
                    where=where,
                    bounded=bool(odata_filter.strip()),
                    date_columns=_real_date_columns(
                        index, service, entity_set, fres["valid_columns"]),
                    scan_rows=_MAX_LIMIT * 20,
                )
                if refusal:
                    return json.dumps(refusal)

            # --- Routing decision (INV-2): OData vs GetRows ---------------
            route_getrows = is_heavy(service) or service in getrows_services
            # Aggregations run through GetRows when the entity is a real
            # DataSet table; OData-only collections keep in-process rollup.
            if (group_by or aggregate) and _has_real_table(index, service, entity_set):
                route_getrows = True

            raw = None

            # What we ACTUALLY attempted, so a failure can never claim a
            # path it never tried — a heavy service routes straight to
            # GetRows, yet the old payload said "Neither OData nor GetRows
            # worked" and offered no alternative.
            attempted: list[str] = []
            try:
                if not route_getrows:
                    attempted.append("odata")
                    try:
                        if aggregated:
                            # Rollups auto-scan past the 1000-row page cap; raw
                            # listings stay on the single-page cursor path.
                            raw = await run_odata_paged(
                                client, service, entity_set, api_key,
                                filter=odata_filter, select=select_str, orderby="",
                                page_size=_MAX_LIMIT, max_pages=20, expand="",
                                group_by=group_by, aggregate=aggregate,
                                distinct="", having=having,
                                order_by=order_by, format="json",
                            )
                        else:
                            raw = await run_odata(
                                client, service, entity_set, api_key,
                                filter=odata_filter, select=select_str,
                                orderby=default_order,
                                top=limit, skip=skip, expand="", count_only=False,
                                group_by=group_by, aggregate=aggregate, distinct="",
                                having=having, format="json",
                            )
                    except EpicorError as exc:
                        m = (exc.message or "").lower()
                        if "resource not found for the segment" in m:
                            known = index.get_entity_sets(service) or []
                            if entity_set in known:
                                # Service has no OData collections -> GetRows.
                                getrows_services.add(service)
                                route_getrows = True
                            else:
                                return json.dumps(error_envelope(
                                    "unknown_entity",
                                    f"Entity set '{entity_set}' not found on "
                                    f"{service}. Retry with a valid entity.",
                                    valid={"entities": suggest_entities(
                                        index, service, entity_set)},
                                    retry_with={"target": service},
                                ))
                        elif _query._is_generic_epicor_apology(exc.message or ""):
                            # INV-2 core: a heavy-behaving service NOT in the static
                            # set emitted Epicor's generic "unexpected internal
                            # problem" 500. Learn it and fall back to GetRows so the
                            # model never sees the apology and never refine-loops.
                            logger.info(
                                "OData 500 (generic apology) on %s; caching as "
                                "GetRows-only and retrying.", service)
                            getrows_services.add(service)
                            route_getrows = True
                        else:
                            raise

                if route_getrows:
                    attempted.append("getrows")
                    if aggregated:
                        raw = await run_getrows_paged(
                            client, index, service, entity_set, api_key,
                            filter=odata_filter, select=select_str, orderby="",
                            page_size=_MAX_LIMIT, max_pages=20,
                            group_by=group_by, aggregate=aggregate,
                            distinct="", having=having,
                            order_by=order_by, format="json",
                        )
                    else:
                        raw = await run_getrows(
                            client, index, service, entity_set, api_key,
                            filter=odata_filter, select=select_str,
                            orderby=default_order,
                            top=limit, skip=skip, count_only=False,
                            group_by=group_by, aggregate=aggregate, distinct="",
                            having=having, format="json",
                            attempted=tuple(attempted),
                        )
                        raw = await _retry_odata_plural_after_getrows_500(
                            client, index, service, entity_set, api_key,
                            raw, aggregated=aggregated,
                            filter=odata_filter, select=select_str,
                            orderby=default_order, top=limit, skip=skip,
                            soft=soft,
                        )

            except EpicorError as exc:
                # INV-1 on the execution path. Deliberately OUTSIDE the
                # inner handler above, so the INV-2 apology->GetRows
                # fallback still runs FIRST and only a post-fallback
                # failure is classified here. Epicor's own message carried
                # the fix in every observed case; the blanket except below
                # used to discard it for a fixed generic string.
                return json.dumps(epicor_error_envelope(
                    exc,
                    service=service,
                    entity_set=entity_set,
                    valid_columns=fres["valid_columns"],
                    odata_filter=odata_filter,
                    fields=fields,
                    where=where,
                    attempted=tuple(attempted) or ("odata",),
                    index=index,
                    # The upstream_error branch's message directs the model to
                    # "try an alternative target (valid.alternatives)"; without
                    # these it promised content and shipped an empty list.
                    alternatives=alternative_targets(
                        index, service, entity_set, odata_filter),
                ))

            if raw is None:
                return json.dumps(error_envelope(
                    "read_failed",
                    f"Read against {service}/{entity_set} produced no result.",
                ))

            return _augment(
                raw, resolved_meta,
                limit=limit, skip=skip, cursor_ctx=cursor_ctx,
                paginated=not aggregated,
                clamp_note=clamp_note,
                assumptions=soft or None,
            )

        except Exception as exc:
            # Last resort. Still never a bare reason code (INV-1): name the
            # exception type and its text. `service`/`entity_set`/`fres` bind
            # inside the try and are unbound for any pre-resolution failure, so
            # they are deliberately NOT referenced here.
            logger.exception("epicor_read failed")
            return json.dumps(error_envelope(
                "read_failed",
                "epicor_read failed — check target, fields, and where syntax.",
                detail={"type": type(exc).__name__,
                        "message": _clip(str(exc), 400)},
            ))

"""Inline schema block for the ``epicor_query`` tool description.

Generates a compact, human-readable schema summary for a curated set of
high-traffic Epicor BO services so Claude can issue most queries without
a preceding ``epicor_describe_service`` round trip.

The block is built **at server startup** from the live ``ServiceIndex``,
so it stays in sync with the Swagger JSONs whenever the index is
rebuilt.  RBAC is unaffected — schema visibility is already universal in
this server; only the data call is gated.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from epicor_mcp.tools._tenant import commercial_brand_lines, plant_lines

if TYPE_CHECKING:
    from epicor_mcp.index.service_index import ServiceIndex


# ---------------------------------------------------------------------------
# Curated allowlist
# ---------------------------------------------------------------------------
#
# Each tuple is (service_id, [entity_sets_to_inline]).  Entity-set names
# must be the form that has fields populated in the index — usually the
# singular entity-type name (e.g. ``Customer``, not ``Customers``).
#
# Adding a service here costs ~150–250 tokens.  Keep the list focused on
# high-traffic tables that show up in real questions across departments.

TOP_SERVICES: list[tuple[str, list[str]]] = [
    # Sales
    ("Erp.BO.CustomerSvc",   ["Customer"]),
    ("Erp.BO.SalesOrderSvc", ["OrderHed", "OrderDtl"]),
    ("Erp.BO.QuoteSvc",      ["QuoteHed", "QuoteDtl"]),
    # Purchasing
    ("Erp.BO.VendorSvc",     ["Vendor"]),
    ("Erp.BO.POSvc",         ["POHeader", "PODetail"]),
    ("Erp.BO.ReqSvc",        ["ReqHead", "ReqDetail"]),
    # Inventory / Parts
    ("Erp.BO.PartSvc",       ["Part"]),
    # Production — JobOper carries production yield (QtyCompleted good vs
    # ScrapQty/ActScrapQty scrap); JobMtl/JobAsmbl carry material/assembly.
    ("Erp.BO.JobEntrySvc",   ["JobHead", "JobOper", "JobMtl", "JobAsmbl"]),
    ("Erp.BO.ResourceSvc",   ["Resource"]),
    # Shipping / Receiving
    ("Erp.BO.CustShipSvc",   ["ShipHead", "ShipDtl"]),
    ("Erp.BO.ReceiptSvc",    ["RcvHead", "RcvDtl"]),
    # AP / AR
    ("Erp.BO.APInvoiceSvc",  ["APInvHed", "APInvDtl"]),
    ("Erp.BO.ARInvoiceSvc",  ["InvcHead", "InvcDtl"]),
    # HR / People
    ("Erp.BO.EmpBasicSvc",   ["EmpBasic"]),
    ("Ice.BO.UserFileSvc",   ["UserFile"]),
    # Project
    ("Erp.BO.ProjectSvc",    ["Project"]),
]


# ---------------------------------------------------------------------------
# Per-(service, entity_set) explicit field lists.  Use sparingly — only
# where the heuristic picks badly.  Field names not present on the
# entity are silently skipped.

FIELD_OVERRIDES: dict[tuple[str, str], list[str]] = {
    ("Erp.BO.CustomerSvc", "Customer"): [
        "CustNum", "CustID", "Name", "State", "City", "Country",
        "CreditHold", "TermsCode", "SalesRepCode", "EMailAddress",
        "Phone", "InActive",
    ],
    ("Erp.BO.VendorSvc", "Vendor"): [
        "VendorNum", "VendorID", "Name", "State", "City", "Country",
        "TermsCode", "Inactive", "EMailAddress", "Phone",
        "DefaultFOB", "CurrencyCode",
    ],
    ("Erp.BO.POSvc", "POHeader"): [
        "PONum", "VendorNum", "OpenOrder", "OrderDate", "DueDate",
        "BuyerID", "EntryPerson", "TotalOrder", "ApprovalStatus",
        "POType", "Plant", "TermsCode",
    ],
    ("Erp.BO.POSvc", "PODetail"): [
        "PONUM", "POLine", "PartNum", "LineDesc", "OrderQty",
        "DocUnitCost", "ReceivedQty", "OpenLine", "DueDate",
        "VenPartNum", "JobNum", "IUM",
    ],
    ("Erp.BO.QuoteSvc", "QuoteHed"): [
        "QuoteNum", "CustNum", "DateQuoted", "ExpirationDate",
        "ClosedDate", "DocQuoteAmt", "DocTotalGrossValue",
        "DaysOpen", "ChangeDate", "EntryPerson", "Plant",
        "AutoPrintReady",
    ],
    ("Erp.BO.QuoteSvc", "QuoteDtl"): [
        "QuoteNum", "QuoteLine", "PartNum", "LineDesc", "OrderQty",
        "OrderUnitPrice", "OrderUM", "NeedByDate", "ExpirationDate",
        "Ordered", "OrderWorthy", "LineStatus",
    ],
    ("Erp.BO.SalesOrderSvc", "OrderHed"): [
        "OrderNum", "CustNum", "PONum", "OpenOrder", "OrderDate",
        "RequestDate", "NeedByDate", "OrderHeld", "ReadyToInvoice",
        "TotalCharges", "EntryPerson", "Plant",
    ],
    ("Erp.BO.SalesOrderSvc", "OrderDtl"): [
        "OrderNum", "OrderLine", "PartNum", "LineDesc", "OrderQty",
        "ShippedQty", "OpenLine", "RequestDate", "NeedByDate",
        "DocUnitPrice", "DocExtPriceDtl", "VoidLine",
    ],
    ("Erp.BO.PartSvc", "Part"): [
        "PartNum", "PartDescription", "ClassID", "ProdCode",
        "TypeCode", "InActive", "NonStock", "IUM", "PUM",
        "UnitPrice", "RevisionNum", "PartsClass",
    ],
    ("Erp.BO.JobEntrySvc", "JobHead"): [
        # PersonID = the PLANNER assignment (a code like "Plan16"); PersonIDName
        # is the resolved planner name — the ONLY natural way to filter jobs by
        # planner. NOT PlanUserID (that's the DCD login that last ran planning,
        # usually blank). See _planner.py.
        "JobNum", "PartNum", "ProdQty", "JobReleased", "JobClosed",
        "JobComplete", "JobEngineered", "JobFirm", "ReqDueDate",
        "DueDate", "StartDate", "Plant", "PersonID", "PersonIDName",
    ],
    ("Erp.BO.JobEntrySvc", "JobOper"): [
        # Production yield lives here: QtyCompleted = good units, ScrapQty /
        # ActScrapQty = scrapped units (per the data dictionary).
        # JobHead/JobMtl/JobAsmbl carry NO reported-scrap column.
        "JobNum", "AssemblySeq", "OprSeq", "OpCode", "Description",
        "QtyCompleted", "ScrapQty", "ActScrapQty", "ProductionQty",
        "RunQty", "OpComplete", "DueDate",
    ],
    ("Erp.BO.JobEntrySvc", "JobMtl"): [
        "JobNum", "AssemblySeq", "MtlSeq", "PartNum", "Description",
        "RequiredQty", "IssuedQty", "QtyPer", "EstScrap", "EstScrapType",
    ],
    ("Erp.BO.JobEntrySvc", "JobAsmbl"): [
        "JobNum", "AssemblySeq", "PartNum", "Description", "RequiredQty",
        "IssuedQty", "QtyPer", "OverRunQty", "EstScrap", "EstScrapType",
        "JobComplete",
    ],
    ("Erp.BO.CustShipSvc", "ShipHead"): [
        "PackNum", "ShipDate", "ShipStatus", "ReadyToInvoice",
        "Invoiced", "CustNum", "ShipViaCode", "TrackingNumber",
        "Plant", "EntryPerson", "PMUID", "WarehouseCode",
    ],
    ("Erp.BO.CustShipSvc", "ShipDtl"): [
        "PackNum", "PackLine", "OrderNum", "OrderLine", "PartNum",
        "LineDesc", "OurInventoryShipQty", "OrderShipUOM",
        "WarehouseCode", "BinNum", "DropShipment", "Voided",
    ],
    ("Erp.BO.APInvoiceSvc", "APInvHed"): [
        "InvoiceNum", "VendorNum", "InvoiceDate", "OpenPayable",
        "DocInvoiceAmt", "TermsCode", "DueDate", "PONum", "Posted",
        "InvoiceRef", "GroupID", "EntryPerson",
    ],
    ("Erp.BO.ARInvoiceSvc", "InvcHead"): [
        "InvoiceNum", "CustNum", "InvoiceDate", "OpenInvoice",
        "DocInvoiceAmt", "TermsCode", "DueDate", "OrderNum",
        "Posted", "InvoiceRef", "GroupID", "EntryPerson",
    ],
    ("Erp.BO.ARInvoiceSvc", "InvcDtl"): [
        "InvoiceNum", "InvoiceLine", "PartNum", "LineDesc",
        "SellingShipQty", "DocExtPrice", "OrderNum", "OrderLine",
        "PackNum", "ShipToCustNum", "ProdCode", "ClassID",
    ],
    ("Erp.BO.EmpBasicSvc", "EmpBasic"): [
        "EmpID", "Name", "FirstName", "LastName", "DeptCode",
        "Inactive", "EMailAddress", "Phone", "EmpType", "HireDate",
        "TermDate", "JCDept",
    ],
    ("Ice.BO.UserFileSvc", "UserFile"): [
        "DcdUserID", "Name", "EMailAddress", "DisplayName",
        "DefaultPlant", "Disabled", "AdminUser", "WindowsAuth",
        "JobTitle", "ExternalEmailAddress", "GlbUserID", "AllowDataDiscovery",
    ],
    ("Erp.BO.ProjectSvc", "Project"): [
        "ProjectID", "Description", "ProjectStatus", "Closed",
        "OnHold", "CustNum", "ContractNum", "StartDate", "EndDate",
        "ApprovalStatus", "ProjectMgrID", "DueDate",
    ],
    ("Erp.BO.ResourceSvc", "Resource"): [
        "ResourceID", "Description", "ResourceGrpID", "InActive",
        "ResourceType", "ResourceCalendarID", "Plant",
        "FinishedCapacity", "BurdenRate", "AvailableForJob",
        "FiniteHorizon", "ConstrainedMaterial",
    ],
}

# Same curated sets keyed by ENTITY NAME alone.
#
# SearchSvc routing changes the service before fields are resolved. Fall back
# by entity name so a service alias retains the curated projection instead of
# returning every column. `resolve_fields` intersects that projection with
# the target's actual columns, avoiding an invalid
# $select.
CURATED_BY_ENTITY: dict[str, list[str]] = {
    entity: cols for (_svc, entity), cols in FIELD_OVERRIDES.items()
}


# ---------------------------------------------------------------------------
# Default sort order — newest-first (INV: recency default)
#
# With no $orderby, Epicor returns rows in primary-key order, so a plain
# listing surfaces the OLDEST records first. Default every
# plain listing to the most transaction-meaningful date column DESCENDING so
# the freshest rows lead. Curated per entity where the "recency" column isn't
# the obvious one; otherwise `default_order_clause` heuristically picks the best
# date column actually present on the entity.

DEFAULT_ORDER: dict[tuple[str, str], str] = {
    ("Erp.BO.POSvc", "POHeader"): "OrderDate desc",
    ("Erp.BO.POSvc", "PODetail"): "DueDate desc",
    ("Erp.BO.SalesOrderSvc", "OrderHed"): "OrderDate desc",
    ("Erp.BO.SalesOrderSvc", "OrderDtl"): "RequestDate desc",
    ("Erp.BO.QuoteSvc", "QuoteHed"): "EntryDate desc",
    ("Erp.BO.JobEntrySvc", "JobHead"): "CreateDate desc",
    ("Erp.BO.ARInvoiceSvc", "InvcHead"): "InvoiceDate desc",
    ("Erp.BO.APInvoiceSvc", "APInvHed"): "InvoiceDate desc",
    ("Erp.BO.CustShipSvc", "ShipHead"): "ShipDate desc",
    ("Erp.BO.ReceiptSvc", "RcvHead"): "ReceiptDate desc",
    ("Erp.BO.ReqSvc", "ReqHead"): "RequestDate desc",
    ("Erp.BO.POSuggSvc", "SugPoDtl"): "DueDate desc",
    ("Erp.BO.POSuggChgSvc", "SugPOChg"): "DueDate desc",
}

# Heuristic fallback: lowercased date-column names in descending order of
# "recency meaning" (a transaction/entry date beats a due/promise date, which
# beats an audit change date). First one PRESENT on the entity wins. Kept
# conservative — if none of these exist we leave the read unordered rather than
# sort by some arbitrary config date.
_ORDER_DATE_PRIORITY: tuple[str, ...] = (
    "orderdate", "invoicedate", "trandate", "transdate", "applydate",
    "billingdate", "receiptdate", "arriveddate", "shipdate", "shippeddate",
    "packslipdate", "entrydate", "createdate", "creationdate", "createddate",
    "createdon", "requestdate", "postdate", "posteddate", "gldate",
    "datequoted", "duedate", "promisedate", "needbydate", "requiredate",
    "startdate", "changedate",
)


def default_order_clause(
    service: str, entity_set: str, valid_columns: list[str] | None,
) -> str:
    """Return a ``"Col desc"`` default sort for a plain listing, or ``""``.

    Curated ``DEFAULT_ORDER`` first (validated against the real columns when we
    have them); else the best date column present by ``_ORDER_DATE_PRIORITY``.
    Empty string when no sensible date column exists — callers leave the read
    unordered rather than guess.
    """
    lower = {c.lower(): c for c in (valid_columns or [])}
    curated = DEFAULT_ORDER.get((service, entity_set))
    if curated:
        col = curated.split()[0]
        if not lower or col.lower() in lower:
            return curated
    if not lower:
        return ""
    for name in _ORDER_DATE_PRIORITY:
        real = lower.get(name)
        if real:
            return f"{real} desc"
    return ""


def best_date_column(columns) -> str:
    """The most transaction-meaningful date column in *columns*, or ``""``.

    Same ranking `default_order_clause` sorts by, exposed on its own so the
    unbounded-rollup guard can offer a date window to bound a scan with. Feed
    it REAL Edm date columns (``date_columns_for`` ∩ the entity's columns) —
    73 Epicor ``%Date`` fields are ``Edm.String`` and a window against one of
    those is a string comparison, not a date filter.
    """
    lower: dict[str, str] = {}
    for c in columns or ():
        lower.setdefault(str(c).lower(), str(c))
    for name in _ORDER_DATE_PRIORITY:
        if name in lower:
            return lower[name]
    return ""


_ORDER_EXPR_CHARS = re.compile(r"[()*/+%]")


def parse_order_by(order_by: str) -> tuple[list[tuple[str, str]], str]:
    """``([(col, "asc"|"desc"), ...], error_kind)`` from a caller sort clause.

    A bare column means ASCENDING — both OData's default and what a caller
    means: a bare ``DueDate`` on JobHead/POHeader in an expediting context wants
    soonest-due first, and newest-first would be the exact inverse of that
    worklist.

    Arithmetic and aggregate-function sort keys are REPORTED, never stripped:
    ``$orderby`` 500s on them, and silently dropping the sort is the failure
    being fixed. Callers convert them to a rollup instead.
    """
    terms: list[tuple[str, str]] = []
    for raw in (order_by or "").split(","):
        term = raw.strip()
        if not term:
            continue
        if _ORDER_EXPR_CHARS.search(term):
            return [], "expression"
        parts = term.split()
        if len(parts) == 1:
            terms.append((parts[0], "asc"))
        elif len(parts) == 2 and parts[1].lower() in ("asc", "desc"):
            terms.append((parts[0], parts[1].lower()))
        else:
            return [], "expression"
    return terms, ""


def sort_records(
    rows: list[dict], order_by: str, *, available: list[str] | None = None,
) -> tuple[list[dict], str, list[str]]:
    """``(rows, err_kind, valid_columns)`` — caller ordering for a recognizer.

    Exists so the eleven recognizer routes cannot drift apart: they all
    materialise their own result set, so each one used to have to invent its
    own sort AND its own refusal message, and a caller would see two different
    answers for ``order_by='qty*cost'`` depending on which route it hit.
    ``parse_order_by`` is delegated to FIRST, so the expression/aggregate
    refusal is byte-identical to epicor_read's main path.

    ``err_kind`` is ``""`` | ``"expression"`` | ``"unknown_column"``.

    None/blank sort LAST in both directions — a mixed None/str Python sort key
    raises TypeError, which would turn a working recognizer into a 500 (the
    ``_posugg._due_key`` pattern).
    """
    if not (order_by or "").strip():
        return rows, "", []
    terms, kind = parse_order_by(order_by)
    cols = list(available) if available is not None else (
        list(rows[0].keys()) if rows and isinstance(rows[0], dict) else [])
    if kind or not terms:
        return rows, "expression", cols
    lower = {str(c).lower(): c for c in cols}
    resolved: list[tuple[str, bool]] = []
    for col, direction in terms:
        real = lower.get(col.split(".")[-1].lower())
        if real is None:
            return rows, "unknown_column", sorted(cols)
        resolved.append((real, direction == "desc"))
    out = list(rows)
    for real, desc in reversed(resolved):
        def _key(row, _r=real):
            v = row.get(_r) if isinstance(row, dict) else None
            blank = v is None or v == ""
            if blank:
                return (1, 0.0, "")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return (0, float(v), "")
            return (0, 0.0, str(v))
        out.sort(key=_key, reverse=desc)
        if desc:
            # reverse=True floats the blanks to the top; keep them last.
            out = ([r for r in out
                    if not (r.get(real) is None or r.get(real) == "")]
                   + [r for r in out
                      if r.get(real) is None or r.get(real) == ""])
    return out, "", cols


def order_refusal(kind: str, order_by: str, valid_cols: list[str], *,
                  reason: str = "") -> dict:
    """The INV-1 envelope for a sort a recognizer route cannot honour."""
    from epicor_mcp.tools._resolve import error_envelope
    if kind == "expression":
        return error_envelope(
            "order_expression_unsupported",
            f"order_by='{order_by}' is an expression or aggregate. order_by "
            "takes plain column names only ('Col', 'Col desc'). To rank by a "
            "computed measure, use group_by + aggregate instead.",
            valid={"columns": valid_cols} if valid_cols else None,
        )
    if kind == "order_not_applicable":
        return error_envelope(
            "order_not_applicable",
            reason,
            valid={"columns": valid_cols} if valid_cols else None,
        )
    return error_envelope(
        "unknown_order_column",
        f"order_by='{order_by}' names a column this result does not have. "
        "Sort by one of valid.columns.",
        valid={"columns": valid_cols},
        retry_with={"order_by": ""},
    )


def order_terms_to_clause(terms: list[tuple[str, str]]) -> str:
    """Canonical ``"Col asc, Col2 desc"`` — OData takes it verbatim and
    ``run_getrows`` already converts it to GetRows ``By <col>``."""
    return ", ".join(f"{c} {d}" for c, d in terms)


# ---------------------------------------------------------------------------
# Heuristic field selector (used when no override is defined)

# Suffix patterns that mark a "hot" field worth including.
_HOT_SUFFIXES = re.compile(
    r"(Num|ID|Code|Name|Date|Time|Status|Hold|Held|Open|Closed|Posted|Type|"
    r"Class|Total|Amount|Qty|Price|Cost|Description|Desc|Plant|Site|"
    r"Active|InActive|Inactive)$"
)

# Field names to exclude regardless of pattern match.
_AUDIT_FIELDS: frozenset[str] = frozenset({
    "SysRevID", "SysRowID", "BitFlag", "RowMod", "RowIdent",
    "GlobalLock", "GlobalRowMod", "Company",
    "CreatedBy", "CreatedOn", "CreatedDate", "CreatedTime",
    "ChangedBy", "ChangedOn", "ChangeDate", "ChangeTime",
    "EntryDate", "EntryTime",
})

_AUDIT_PREFIXES: tuple[str, ...] = (
    "Glb", "TaxConnect", "ETC", "MX", "PE", "TH", "AG",
    "AttributeSet", "ABT", "DspWithhold", "Enable",
)

_MAX_FIELDS_PER_ENTITY = 12


def _select_fields_heuristic(
    fields: list[dict],
    table_prefix: str,
) -> list[dict]:
    """Pick up to _MAX_FIELDS_PER_ENTITY fields using suffix patterns."""
    picks: list[dict] = []
    seen: set[str] = set()

    # Pass 1: primary-key candidates that lead with the table prefix.
    for f in fields:
        n = f["field_name"]
        if n in _AUDIT_FIELDS or n.startswith(_AUDIT_PREFIXES):
            continue
        if (n.startswith(table_prefix) and
                (n.endswith("Num") or n.endswith("ID")) and
                n not in seen):
            picks.append(f)
            seen.add(n)
            if len(picks) >= 3:
                break

    # Pass 2: hot suffix matches, in field order.
    for f in fields:
        if len(picks) >= _MAX_FIELDS_PER_ENTITY:
            break
        n = f["field_name"]
        if n in seen or n in _AUDIT_FIELDS or n.startswith(_AUDIT_PREFIXES):
            continue
        if _HOT_SUFFIXES.search(n):
            picks.append(f)
            seen.add(n)

    return picks


# ---------------------------------------------------------------------------
# Type rendering

_TYPE_SHORT: dict[str, str] = {
    "Edm.String":         "str",
    "Edm.Boolean":        "bool",
    "Edm.Int16":          "int",
    "Edm.Int32":          "int",
    "Edm.Int64":          "int",
    "Edm.Byte":           "int",
    "Edm.Double":         "num",
    "Edm.Single":         "num",
    "Edm.Decimal":        "num",
    "Edm.DateTimeOffset": "date",
    "Edm.DateTime":       "date",
    "Edm.Guid":           "guid",
    "Edm.Binary":         "bin",
}


def _short_type(odata_type: str) -> str:
    return _TYPE_SHORT.get(odata_type or "", "?")


# ---------------------------------------------------------------------------
# Public builder

_PREAMBLE = """Query Epicor data using filters. Read-only.

Returns matching records from the specified service. Works with all
Epicor services — automatically uses GetRows when OData entity set
access is not available.

PARAMETERS
  service       Full service name, e.g. "Erp.BO.VendorSvc".
  entity_set    Collection name to query.  Use the exact name shown in
                the inline schema below — it's the form Epicor's OData
                layer accepts (often pluralized: "Vendors", "Customers",
                "Parts"; some are singular: "POHeader", "JobHead",
                "OrderHed").
  filter        OData ($filter) or SQL syntax. Both are accepted; the
                tool translates as needed.
                Examples: "OpenOrder eq true", "JobNum like 'T%'"
  select        Comma-separated field names to return. OData only.
                Cheapest way to keep responses small.
  orderby       OData $orderby, e.g. "OrderDate desc".
  top           Max records, 1-1000. Default 10.
  skip          Row offset for pagination — OData $skip. Default 0.
                Use skip=top, skip=2*top, etc. to walk through a large
                table; no need to hand-roll a "WHERE col gt 'last_seen'"
                cursor.
  expand        OData $expand for related tables (rare).
  count_only    Return only the count.
  format        "json" (default), "csv", or "tsv". CSV/TSV fits ~2-3x
                more rows in the same byte budget.
  group_by      Comma-separated columns to roll up (in-process, over the
                rows fetched within 'top'). Wrap a date column in a time
                bucket: year(OrderDate), quarter(...), month(...) (→YYYY-MM),
                day(...). A year-over-year total is group_by="year(OrderDate)"
                — select that date column too, and raise 'top' to cover the
                full span (rollup is bounded by 'top').
  aggregate     "func(field) as alias, …" — sum/avg/min/max/count. E.g.
                "sum(CCTotal) as YearlyTotal, count(*)".

EXAMPLES
  epicor_query(service="Erp.BO.VendorSvc", entity_set="Vendors",
      filter="VendorNum eq 1234", select="VendorNum,VendorID,Name")
  epicor_query(service="Erp.BO.POSvc", entity_set="POHeader",
      filter="OpenOrder eq true", top=25)
  epicor_query(service="Erp.BO.JobEntrySvc", entity_set="JobHead",
      filter="JobNum like 'T%' and JobClosed eq false", top=25)

GOTCHAS — read before filtering by a customer/vendor
  • Filter transactional tables (OrderHed, InvcHead, APInvHed, ShipHead…)
    by the NUMERIC key — CustNum / VendorNum — NEVER by the name/ID copied
    onto the row (CustomerName, CustomerCustID, CustID, BTCustID,
    VendorName, VendorID). Those denormalized fields aren't filterable;
    Epicor answers with a content-free 500 and you'll burn calls guessing.
  • To go from a name to that key, resolve on the master BO FIRST:
      epicor_query(service="Erp.BO.CustomerSvc", entity_set="Customers",
          filter="Name like '%EXAMPLE%'", select="CustNum,CustID,Name")
    then filter the transactional table by "CustNum eq <n>". Same for
    vendors via Erp.BO.VendorSvc → VendorNum.
  • Wildcard search uses LIKE/contains on the MASTER name field only, e.g.
    Customers: "Name like '%EXAMPLE%'"; Vendors: "Name like '%Example Supplier%'".
  • "Whose part is X / which customer is part X for / all parts for customer Y"
    is a PART-attribution question, not a transactional one — answer it from
    the Part table:
      __COMMERCIAL_BRAND__
  • A site/plant name must resolve to an actual Epicor Plant code.
    Administrator-configured site names:
      __PLANT_LINES__
    Verify site codes through Erp.BO.PlantSvc/Plants before filtering.

INLINE SCHEMA — frequently-queried tables and key fields
(Field types: str, int, num, bool, date.  Call epicor_describe_service
for any service or field not listed below.)
"""

_FOOTER = (
    "\nFor services or fields not in the inline schema above, call "
    "epicor_describe_service first to see the full field list."
)


def _table_prefix(entity_set: str) -> str:
    """Best-effort guess of a table's primary-key prefix.

    e.g. ``POHeader`` → ``PO``, ``JobHead`` → ``Job``, ``Customer`` → ``Cust``.
    Used only by the heuristic — overrides bypass this entirely.
    """
    n = entity_set
    # Strip common suffixes
    for suf in ("Header", "Detail", "Head", "Dtl", "Oper", "Hed"):
        if n.endswith(suf) and len(n) > len(suf):
            return n[: -len(suf)]
    # Fall back to first 4-5 chars
    return n[:4]


def _resolve_url_name(
    index: "ServiceIndex",
    service_id: str,
    entity_type: str,
) -> str:
    """Map an entity-TYPE name (where fields live) to the OData URL form
    Epicor actually accepts at runtime.

    Epicor's REST/BO API and OData layer use different names for the
    same table: the dataset uses the singular entity-type name
    (``Vendor``, ``Customer``, ``Part``) while the OData collection
    endpoint uses a pluralized name (``Vendors``, ``Customers``,
    ``Parts``).  Some services use the singular as the URL too
    (``POHeader``, ``JobHead``, ``OrderHed``) — those don't have a
    plural variant in the index.

    Strategy: if a pluralized form exists in the service's entity-set
    list, use it.  Otherwise fall back to the entity-type name.
    """
    all_es = set(index.get_entity_sets(service_id))
    for plural in (entity_type + "s", entity_type + "es"):
        if plural in all_es:
            return plural
    return entity_type


def _format_entity_block(
    service_id: str,
    entity_set: str,
    fields: list[dict],
) -> str:
    """Render one ``entity_set (service)\n  field:type ...`` block."""
    line_fields = "  ".join(
        f"{f['field_name']}:{_short_type(f['field_type'])}" for f in fields
    )
    return f"  {entity_set} ({service_id})\n    {line_fields}"


def build_query_description(index: "ServiceIndex") -> str:
    """Build the dynamic description string for ``epicor_query``.

    Reads field metadata from *index* for each entry in TOP_SERVICES,
    applies FIELD_OVERRIDES first then the suffix heuristic, and
    formats a compact schema block.
    """
    blocks: list[str] = []

    for service_id, entity_sets in TOP_SERVICES:
        for es in entity_sets:
            all_fields = index.get_fields(service_id, es)
            if not all_fields:
                # Service or entity-set missing from index — skip silently.
                # The startup smoke check (see scripts/build_index.py)
                # will flag this in CI.
                continue

            override = FIELD_OVERRIDES.get((service_id, es))
            if override:
                by_name = {f["field_name"]: f for f in all_fields}
                picks = [by_name[n] for n in override if n in by_name]
            else:
                picks = []

            # Top up with heuristic picks if override yielded fewer than
            # the cap.  This handles the case where some override field
            # names don't exist on the entity (Epicor renames between
            # versions, customizations, etc.).
            if len(picks) < _MAX_FIELDS_PER_ENTITY:
                already = {f["field_name"] for f in picks}
                topup = _select_fields_heuristic(
                    all_fields, _table_prefix(es)
                )
                for f in topup:
                    if len(picks) >= _MAX_FIELDS_PER_ENTITY:
                        break
                    if f["field_name"] not in already:
                        picks.append(f)
                        already.add(f["field_name"])

            if picks:
                # Display the OData URL name (typically plural) so
                # Claude passes a name Epicor's OData layer accepts.
                url_name = _resolve_url_name(index, service_id, es)
                blocks.append(_format_entity_block(service_id, url_name, picks))

    schema = "\n\n".join(blocks)
    preamble = _PREAMBLE.replace("__PLANT_LINES__", plant_lines())
    preamble = preamble.replace("__COMMERCIAL_BRAND__", commercial_brand_lines())
    return f"{preamble}\n{schema}\n{_FOOTER}"


def validate_top_services(index: "ServiceIndex") -> list[str]:
    """Return a list of (service_id, entity_set) entries that resolved to
    zero fields.  Used by the build script as a smoke check.
    """
    missing: list[str] = []
    for service_id, entity_sets in TOP_SERVICES:
        for es in entity_sets:
            if not index.get_fields(service_id, es):
                missing.append(f"{service_id}/{es}")
    return missing

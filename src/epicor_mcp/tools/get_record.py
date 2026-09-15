"""Tool: epicor_get_record

Retrieve a specific Epicor record by its primary key using the service's
``GetByID`` method.  Supports shorthand record types (e.g. ``"job"``,
``"po"``) so callers don't need to know service names or key parameters.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

# Maps shorthand record types to (service, key_param_name).
# For composite keys, key_params is a list of param names — the caller
# must provide values in the same order.
_RECORD_TYPES: dict[str, tuple[str, list[str]]] = {
    # key: (service, [key_param_names])
    "po": ("Erp.BO.POSvc", ["poNum"]),
    "purchase order": ("Erp.BO.POSvc", ["poNum"]),
    "vendor": ("Erp.BO.VendorSvc", ["vendorNum"]),
    "supplier": ("Erp.BO.VendorSvc", ["vendorNum"]),
    "ap invoice": ("Erp.BO.APInvoiceSvc", ["vendorNum", "invoiceNum"]),
    "ap": ("Erp.BO.APInvoiceSvc", ["vendorNum", "invoiceNum"]),
    "ar invoice": ("Erp.BO.ARInvoiceSvc", ["invoiceNum"]),
    "ar": ("Erp.BO.ARInvoiceSvc", ["invoiceNum"]),
    "invoice": ("Erp.BO.ARInvoiceSvc", ["invoiceNum"]),
    "customer": ("Erp.BO.CustomerSvc", ["custNum"]),
    "job": ("Erp.BO.JobEntrySvc", ["jobNum"]),
    "part": ("Erp.BO.PartSvc", ["partNum"]),
    "sales order": ("Erp.BO.SalesOrderSvc", ["orderNum"]),
    "order": ("Erp.BO.SalesOrderSvc", ["orderNum"]),
    "employee": ("Erp.BO.EmpBasicSvc", ["empID"]),
    "quote": ("Erp.BO.QuoteSvc", ["quoteNum"]),
    "rma": ("Erp.BO.RMAProcSvc", ["rmANum"]),
}

# Reverse map: service_id → set of valid GetByID parameter names.
# Used to reject extra params Claude might pass thinking they'd narrow
# the lookup (e.g., quoteLine, partNum). GetByID returns the dataset
# keyed only on the header PK; extra params are silently dropped by
# Epicor and the response looks the same on every call.
_SERVICE_KEYS: dict[str, frozenset[str]] = {
    svc: frozenset(keys)
    for svc, keys in {s: k for s, k in _RECORD_TYPES.values()}.items()
}

# Hint for which child entity set to query when GetByID isn't enough.
# Used in the extra-params error message so Claude knows where to pivot.
_CHILD_ENTITY_HINTS: dict[str, str] = {
    "Erp.BO.QuoteSvc": "QuoteDtl",
    "Erp.BO.SalesOrderSvc": "OrderDtl",
    "Erp.BO.POSvc": "PODetail",
    "Erp.BO.JobEntrySvc": "JobOper",
    "Erp.BO.ARInvoiceSvc": "InvcDtl",
    "Erp.BO.APInvoiceSvc": "APInvDtl",
    "Erp.BO.CustShipSvc": "ShipDtl",
    "Erp.BO.ReceiptSvc": "RcvDtl",
}

# GetByID uses camelCase param names; the corresponding OData entity-set
# column is PascalCase, and the mapping isn't pure capitalisation
# (``poNum`` → ``PONum``, ``rmANum`` → ``RMANum``, ``empID`` → ``EmpID``).
# Used when building the next_steps filter string so Claude can copy-
# paste it directly into ``epicor_query``.
_PARAM_TO_COLUMN: dict[str, str] = {
    "quoteNum": "QuoteNum",
    "orderNum": "OrderNum",
    "poNum": "PONum",
    "jobNum": "JobNum",
    "invoiceNum": "InvoiceNum",
    "vendorNum": "VendorNum",
    "custNum": "CustNum",
    "partNum": "PartNum",
    "empID": "EmpID",
    "rmANum": "RMANum",
}

# In-memory dedupe window: when the same user fetches the same record
# header more than ``_REPEAT_THRESHOLD`` times within
# ``_REPEAT_WINDOW_SECONDS``, return a guidance message instead of
# burning another round-trip. Cleared on context switch (different id)
# or a successful pivot to query/include_children.
_REPEAT_THRESHOLD = 2
_REPEAT_WINDOW_SECONDS = 30.0
_recent_fetches: dict[tuple[str, str, str], deque[float]] = {}


def _record_fetch(user: str, service: str, key: str) -> int:
    """Record a header fetch and return the recent-fetch count within
    the dedupe window."""
    k = (user, service, key)
    q = _recent_fetches.get(k)
    if q is None:
        q = deque()
        _recent_fetches[k] = q
    cutoff = time.monotonic() - _REPEAT_WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    q.append(time.monotonic())
    return len(q)


# Fields worth showing in the child-row preview for each known child
# entity. Picked to be small + identifying so Claude can answer most
# "what's on this record?" questions from the preview alone, without
# pulling the full child rows.
_CHILD_PREVIEW_FIELDS: dict[str, list[str]] = {
    "QuoteDtl":  ["QuoteLine", "PartNum", "LineDesc"],
    "OrderDtl":  ["OrderLine", "PartNum", "LineDesc"],
    "PODetail":  ["POLine", "PartNum", "LineDesc"],
    "JobOper":   ["OprSeq", "OpCode", "OpDesc"],
    "JobMtl":    ["MtlSeq", "PartNum", "Description"],
    "InvcDtl":   ["InvoiceLine", "PartNum", "LineDesc"],
    "APInvDtl":  ["InvoiceLine", "PartNum", "LineDesc"],
    "ShipDtl":   ["PackLine", "PartNum", "LineDesc"],
    "RcvDtl":    ["PackLine", "PartNum", "PONum"],
    "OrderRel":  ["OrderLine", "OrderRelNum", "ReqDate"],
}

_PREVIEW_ROWS = 3


def _summarize_child_table(rows: list, table_name: str) -> dict:
    """Compact ``{count, preview}`` for a child table — first N rows,
    projected to a small set of identifying fields."""
    count = len(rows)
    if count == 0:
        return {"count": 0}
    wanted = _CHILD_PREVIEW_FIELDS.get(table_name)
    preview: list[dict] = []
    for row in rows[:_PREVIEW_ROWS]:
        if not isinstance(row, dict):
            continue
        if wanted:
            picked = {k: row.get(k) for k in wanted if k in row}
            if picked:
                preview.append(picked)
                continue
        # Unknown child entity — keep just key-like fields heuristically.
        picked = {
            k: v for k, v in row.items()
            if isinstance(v, (str, int, float, bool)) and not k.startswith("_")
        }
        # Cap to the first 4 fields so the preview stays small.
        preview.append(dict(list(picked.items())[:4]))
    return {"count": count, "preview": preview}


def _build_next_steps(
    service: str,
    parsed_params: dict,
    child_counts: dict[str, int],
) -> list[str]:
    """Copy-pasteable epicor_query templates for the child entity, plus
    an include_children=True option. Picks the largest-non-empty child
    table as the most-likely next pivot."""
    if not child_counts:
        return []

    pk_clause = " and ".join(
        (
            f"{_PARAM_TO_COLUMN.get(k, k[:1].upper() + k[1:])} eq "
            + (f"'{v}'" if isinstance(v, str) else str(v))
        )
        for k, v in parsed_params.items()
    )

    # Pick the child the caller most likely wants — the canonical hint
    # if it exists and is non-empty, else the largest non-empty table.
    canonical = _CHILD_ENTITY_HINTS.get(service)
    candidates = [t for t, n in child_counts.items() if n > 0]
    if canonical in candidates:
        target = canonical
    elif candidates:
        target = max(candidates, key=lambda t: child_counts[t])
    else:
        target = None

    steps: list[str] = []
    if target:
        steps.append(
            f"For all {target} rows: epicor_query(service=\"{service}\", "
            f"entity_set=\"{target}\", filter=\"{pk_clause}\")"
        )
        steps.append(
            f"For one row: add ' and <Field> eq <value>' to that filter "
            "(e.g. PartNum, OprSeq, etc.)."
        )
    steps.append(
        "For the full dataset (header + every child) in one call, "
        "re-invoke this tool with include_children=True."
    )
    return steps


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_get_record`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_get_record(
        service: str = "",
        params: str = "{}",
        include_children: bool = False,
        record_type: str = "",
        id: str = "",
    ) -> str:
        """Get a specific Epicor record by its primary key using GetByID.

        **Preferred usage** — use ``record_type`` and ``id`` for common
        record types (no need to know the service name or key parameter):

            epicor_get_record(record_type="job", id="100001")
            epicor_get_record(record_type="po", id="500001")
            epicor_get_record(record_type="part", id="ABC-100")
            epicor_get_record(record_type="customer", id="500")
            epicor_get_record(record_type="vendor", id="1001")
            epicor_get_record(record_type="ar invoice", id="10002")
            epicor_get_record(record_type="order", id="10003")
            epicor_get_record(record_type="quote", id="5000")

        For AP invoices (composite key), provide both values separated
        by a comma:
            epicor_get_record(record_type="ap invoice", id="1234,INV-001")

        **Advanced usage** — specify the service and params directly:
            epicor_get_record(service="Erp.BO.POSvc", params='{"poNum": 500001}')

        **GetByID returns the HEADER only by default.** Extra keys in
        ``params`` are ignored by Epicor (it keys only on the header PK
        — ``quoteNum`` for quotes, ``orderNum`` for orders, etc.). Do
        NOT keep retrying ``get_record`` with different ``quoteLine`` /
        ``partNum`` combinations hoping for a specific child row — the
        response will be identical every time.

        If you need line-level / child-row data, you have two options:

        1. ``epicor_query`` against the child entity set with a
           ``$filter`` — the precise, low-bandwidth answer::

               epicor_query(service="Erp.BO.QuoteSvc", entity_set="QuoteDtl",
                            filter="QuoteNum eq 10001 and PartNum eq 'PART-001'")
               epicor_query(service="Erp.BO.JobEntrySvc", entity_set="JobOper",
                            filter="JobNum eq 'T001' and OpCode eq 'ASSM'")

        2. ``include_children=True`` — pulls the whole dataset (header
           + every child table) in one shot. Bigger payload, but it's
           one call instead of N, and the result is offloaded so it
           doesn't blow context. Use this when you want a full snapshot.

        Either is correct; don't loop on ``get_record`` itself. The
        server now rejects calls with extra params and short-circuits
        repeated identical fetches.

        Parameters
        ----------
        service : str, optional
            Full service name. Not needed if ``record_type`` is used.
        params : str, optional
            JSON string of key-value pairs for GetByID.
            Not needed if ``record_type`` and ``id`` are used.
        include_children : bool, optional
            If True, return the full dataset with all child tables.
            If False (default), return only the primary header table.
        record_type : str, optional
            Shorthand for the record type: ``"job"``, ``"po"``,
            ``"vendor"``, ``"customer"``, ``"part"``, ``"ar invoice"``,
            ``"ap invoice"``, ``"order"``, ``"quote"``, ``"employee"``,
            ``"rma"``.
        id : str, optional
            The primary key value. For composite keys (like AP invoices),
            provide values separated by a comma.
        """
        try:
            session = get_current_session()

            # --- Resolve record_type shorthand -----------------------------
            if record_type and id:
                rt_key = record_type.strip().lower()
                if rt_key not in _RECORD_TYPES:
                    available = sorted(set(
                        k for k in _RECORD_TYPES
                        if not any(k == v for v in _RECORD_TYPES if v != k and _RECORD_TYPES[v] == _RECORD_TYPES[k])
                    ) | set(_RECORD_TYPES.keys()))
                    return json.dumps({
                        "error": (
                            f"Unknown record_type '{record_type}'. "
                            f"Available: {', '.join(sorted(_RECORD_TYPES.keys()))}"
                        )
                    })

                svc, key_names = _RECORD_TYPES[rt_key]
                service = svc

                # Split id by comma for composite keys
                id_values = [v.strip() for v in id.split(",")]
                if len(id_values) != len(key_names):
                    return json.dumps({
                        "error": (
                            f"Record type '{record_type}' requires "
                            f"{len(key_names)} key value(s) ({', '.join(key_names)}), "
                            f"but got {len(id_values)}. "
                            f"Provide as: id=\"{',' .join('<' + k + '>' for k in key_names)}\""
                        )
                    })

                # Build the params dict — try numeric conversion, but
                # preserve leading zeros (invoice numbers / packing slips
                # are often string-typed in Epicor, e.g. "00001004").
                parsed_params: dict = {}
                for key_name, val in zip(key_names, id_values):
                    if (
                        len(val) > 1
                        and val.startswith("0")
                        and not val.startswith("0.")
                    ):
                        parsed_params[key_name] = val
                        continue
                    try:
                        parsed_params[key_name] = int(val)
                    except ValueError:
                        parsed_params[key_name] = val

            elif service and params:
                # --- Parse explicit params ---------------------------------
                try:
                    parsed_params = json.loads(params)
                except json.JSONDecodeError as exc:
                    return json.dumps({
                        "error": (
                            f"Invalid JSON in 'params': {exc}. "
                            "Provide a valid JSON object, e.g. "
                            "'{\"poNum\": 12345}'"
                        )
                    })

                if not isinstance(parsed_params, dict):
                    return json.dumps({
                        "error": (
                            "'params' must be a JSON object (dict), not "
                            f"{type(parsed_params).__name__}."
                        )
                    })
            else:
                return json.dumps({
                    "error": (
                        "Provide either (record_type + id) or (service + params). "
                        "Examples: epicor_get_record(record_type=\"job\", id=\"100001\") "
                        "or epicor_get_record(service=\"Erp.BO.POSvc\", params='{\"poNum\": 12345}')"
                    )
                })

            # --- RBAC check ------------------------------------------------
            allowed, msg = rbac.check_access(session.user_id, service)
            if not allowed:
                return json.dumps({"error": msg})

            svc_result = rbac.check_service_access(session.user_id, service)
            api_key = svc_result.api_key or ""

            # --- Guard: reject extra params Epicor would silently ignore ---
            # GetByID keys on the header PK. Extra keys (quoteLine, partNum,
            # etc.) get dropped server-side and the response is identical
            # to a call without them — which Claude then misreads as "wrong
            # combination" and retries with variations forever. Block here
            # with a precise pivot hint.
            valid_keys = _SERVICE_KEYS.get(service)
            if valid_keys:
                extra = [k for k in parsed_params if k not in valid_keys]
                if extra:
                    child_hint = _CHILD_ENTITY_HINTS.get(service)
                    pivot = (
                        f"epicor_query(service=\"{service}\", "
                        f"entity_set=\"{child_hint}\", "
                        f"filter=\"...\")"
                        if child_hint
                        else "epicor_query against the child entity set"
                    )
                    return json.dumps({
                        "error": "extra_params_ignored",
                        "service": service,
                        "ignored_keys": extra,
                        "valid_keys": sorted(valid_keys),
                        "hint": (
                            f"GetByID for {service} keys only on "
                            f"{sorted(valid_keys)}. The extra param(s) "
                            f"{extra} would be silently ignored by "
                            "Epicor — the response would look the same. "
                            "To fetch specific child rows use "
                            f"{pivot}, or pass include_children=True to "
                            "get the full dataset in one call."
                        ),
                    })

            # --- Guard: short-circuit repeated identical header fetches ----
            # Same user, same service, same key dict, > _REPEAT_THRESHOLD
            # calls in _REPEAT_WINDOW_SECONDS → return guidance instead.
            fetch_key = json.dumps(parsed_params, sort_keys=True, default=str)
            recent = _record_fetch(session.user_id, service, fetch_key)
            if recent > _REPEAT_THRESHOLD and not include_children:
                child_hint = _CHILD_ENTITY_HINTS.get(service)
                return json.dumps({
                    "error": "repeated_header_fetch",
                    "service": service,
                    "params": parsed_params,
                    "fetch_count_in_window": recent,
                    "window_seconds": int(_REPEAT_WINDOW_SECONDS),
                    "hint": (
                        f"You've already fetched this record's header "
                        f"{recent} times in the last "
                        f"{int(_REPEAT_WINDOW_SECONDS)}s. The header "
                        "won't change between calls. If you need line-"
                        "level fields, switch to "
                        + (
                            f"epicor_query(service=\"{service}\", "
                            f"entity_set=\"{child_hint}\", filter=...)"
                            if child_hint else "epicor_query"
                        )
                        + " or re-call this tool once with "
                        "include_children=True."
                    ),
                })

            # --- Call GetByID ----------------------------------------------
            url = f"{service}/GetByID"
            response = await client.post(url, api_key, json_body=parsed_params)

            dataset = (
                response.get("returnObj")
                or response.get("parameters", {}).get("ds")
                or response
            )

            # Reduce response size based on include_children flag.
            if isinstance(dataset, dict):
                from epicor_mcp.epicor_client.dataset_handler import DatasetHandler
                if include_children:
                    dataset = DatasetHandler.trim_dataset(dataset)
                else:
                    # Header path: keep the primary table + a compact
                    # summary of every child table that came back in the
                    # SAME GetByID response. We've already paid the
                    # bandwidth — surfacing counts + 3-row previews
                    # turns "what's on this record?" into a 1-call
                    # question instead of forcing a follow-up
                    # include_children=True / filter_offloaded chain.
                    primary: dict = {}
                    child_summary: dict[str, dict] = {}
                    child_counts: dict[str, int] = {}
                    header_seen = False
                    for key, value in dataset.items():
                        if not isinstance(value, list):
                            primary[key] = value
                            continue
                        if not header_seen and value:
                            primary[key] = value
                            header_seen = True
                            continue
                        # Treat every other list as a child table.
                        child_summary[key] = _summarize_child_table(value, key)
                        child_counts[key] = len(value)

                    # Only emit the summary block when there's something
                    # to say — otherwise it's noise.
                    if any(s.get("count", 0) for s in child_summary.values()):
                        primary["_child_summary"] = {
                            k: v for k, v in child_summary.items()
                            if v.get("count", 0) > 0
                        }
                        next_steps = _build_next_steps(
                            service, parsed_params, child_counts,
                        )
                        if next_steps:
                            primary["next_steps"] = next_steps

                    dataset = primary

            return format_response(dataset, records_key=None)

        except Exception:
            logger.exception("epicor_get_record failed")
            return json.dumps(
                {
                    "error": (
                        f"GetByID on {service} failed. "
                        "Verify the service name and primary key parameters."
                    )
                }
            )

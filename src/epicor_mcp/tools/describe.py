"""Tool: epicor_describe_service

Retrieve detailed metadata for a specific Epicor service, including its
entity sets, fields, and all available methods.  Use this after
``epicor_discover_services`` to understand a service's data model before
querying or calling methods.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

# Entities that come in header/detail pairs. When field_search on one
# returns no matches, we silently scan the sibling and surface the hits
# — header-level columns like ``InvoiceVariance``, ``MiscChrgVariance``,
# ``TotalTax`` live on the *Head/Hed/Header* entity, not the *Dtl*.
_SIBLING_ENTITIES: dict[str, str] = {
    # AP / AR invoices
    "APInvHed":  "APInvDtl",   "APInvDtl":  "APInvHed",
    "InvcHead":  "InvcDtl",    "InvcDtl":   "InvcHead",
    # Sales / Quotes / POs
    "OrderHed":  "OrderDtl",   "OrderDtl":  "OrderHed",
    "QuoteHed":  "QuoteDtl",   "QuoteDtl":  "QuoteHed",
    "POHeader":  "PODetail",   "PODetail":  "POHeader",
    # Jobs / Receipts / Shipments
    "JobHead":   "JobOper",    "JobOper":   "JobHead",
    "JobMtl":    "JobHead",
    "RcvHead":   "RcvDtl",     "RcvDtl":    "RcvHead",
    "ShipHead":  "ShipDtl",    "ShipDtl":   "ShipHead",
}


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_describe_service`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_describe_service(
        service: str,
        include_fields: bool = True,
        entity_set: str = "",
        field_search: str = "",
    ) -> str:
        """Get detailed information about a specific Epicor service.

        Returns entity sets, field definitions, and all available methods.
        **Use after** ``epicor_discover_services`` to understand a service's
        data model before querying or writing data.

        Use ``field_search`` to find relevant fields by keyword instead
        of returning all fields. This is much faster for large services.

        Examples
        --------
        - ``epicor_describe_service(service="Erp.BO.VendorSvc")``
        - ``epicor_describe_service(service="Erp.BO.POSvc", include_fields=False)``
        - ``epicor_describe_service(service="Erp.BO.ARInvoiceSvc", entity_set="InvcHead")``
        - ``epicor_describe_service(service="Erp.BO.ARInvoiceSvc", entity_set="InvcHead", field_search="posted date invoice")``

        Parameters
        ----------
        service : str
            Full service name (e.g. ``"Erp.BO.VendorSvc"``).
        include_fields : bool, optional
            Whether to include field definitions for each entity set
            (default ``True``).  Set to ``False`` for a lighter overview.
        entity_set : str, optional
            Filter to specific entity set(s) (e.g. ``"InvcHead"`` or
            ``"InvcHead,InvcDtl"``).  Only fields for the named entity
            set(s) are returned.  If empty (default), returns all entity
            sets.
        field_search : str, optional
            Search keyword(s) to filter fields by name or description.
            Only fields whose name or description contains any of the
            search words are returned.  Useful for finding the right
            field on large entity sets (e.g. ``"posted date"`` to find
            date fields related to posting).
        """
        try:
            session = get_current_session()

            # --- RBAC check ------------------------------------------------
            allowed, msg = rbac.check_access(session.user_id, service)
            if not allowed:
                return json.dumps({"error": msg})

            # --- Service metadata ------------------------------------------
            svc_meta = index.get_service(service)
            if svc_meta is None:
                return json.dumps(
                    {
                        "error": (
                            f"Service '{service}' not found in the index. "
                            "Use epicor_discover_services to search for available services."
                        )
                    }
                )

            # --- Entity sets (with optional fields) ------------------------
            # get_entity_sets returns a list of strings (entity set names).
            raw_entity_sets = index.get_entity_sets(service)

            # Filter to requested entity set(s) if specified
            if entity_set:
                requested = {s.strip() for s in entity_set.split(",")}
                raw_entity_sets = [es for es in raw_entity_sets if es in requested]
                if not raw_entity_sets:
                    available = sorted(index.get_entity_sets(service))
                    return json.dumps({
                        "error": (
                            f"Entity set(s) '{entity_set}' not found on "
                            f"service '{service}'. "
                            f"Available: {available}"
                        )
                    })

            # Parse search keywords for field filtering
            search_words = [w.lower() for w in field_search.split()] if field_search else []

            def _match_fields(es: str) -> tuple[list, int]:
                """Return (matched_fields, total_fields) for an entity set
                given the active ``search_words`` (case-insensitive
                substring match on name OR description)."""
                fields = index.get_fields(service, es)
                if not search_words:
                    return fields, len(fields)
                matched = []
                for f in fields:
                    name_lower = f.get("field_name", "").lower()
                    desc_lower = (f.get("description") or "").lower()
                    text = name_lower + " " + desc_lower
                    if any(w in text for w in search_words):
                        matched.append(f)
                return matched, len(fields)

            entity_sets: list[dict] = []
            sibling_hints: list[dict] = []
            for es_name in raw_entity_sets:
                entry: dict = {"name": es_name}
                if include_fields:
                    if search_words:
                        matched, total = _match_fields(es_name)
                        entry["fields"] = matched
                        entry["fields_matched"] = len(matched)
                        entry["fields_total"] = total

                        # Empty match → check the sibling (Hed/Dtl pair).
                        # Only when the sibling is also on this service.
                        if not matched:
                            sibling = _SIBLING_ENTITIES.get(es_name)
                            if (
                                sibling
                                and sibling in (index.get_entity_sets(service) or [])
                            ):
                                sib_matched, sib_total = _match_fields(sibling)
                                if sib_matched:
                                    sibling_hints.append({
                                        "searched_in": es_name,
                                        "found_in_sibling": sibling,
                                        "fields_matched": len(sib_matched),
                                        "fields_total": sib_total,
                                        "fields": sib_matched,
                                        "hint": (
                                            f"No fields matching {search_words} "
                                            f"on {es_name}, but {len(sib_matched)} "
                                            f"matched on the sibling entity "
                                            f"{sibling}. Header-level columns "
                                            "(variances, totals, taxes) usually "
                                            "live on the Head/Header entity, "
                                            "not the Dtl/Detail one."
                                        ),
                                    })
                    else:
                        entry["fields"] = index.get_fields(service, es_name)
                entity_sets.append(entry)

            # --- Methods ---------------------------------------------------
            methods = index.get_methods(service)

            result = {
                "service_id": svc_meta.get("service_id", service),
                "description": svc_meta.get("description", ""),
                "entity_sets": entity_sets,
                "methods": [
                    {
                        "name": m.get("method_name", ""),
                        "description": m.get("description", ""),
                        "is_read_only": m.get("is_read_only", False),
                        "has_dataset_param": m.get("has_dataset_param", False),
                    }
                    for m in methods
                ],
            }
            if sibling_hints:
                result["sibling_entity_hints"] = sibling_hints

            return format_response(result, records_key=None)

        except Exception:
            logger.exception("epicor_describe_service failed")
            return json.dumps(
                {
                    "error": (
                        f"Failed to describe service '{service}'. "
                        "Verify the service name is correct."
                    )
                }
            )

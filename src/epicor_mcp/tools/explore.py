"""Tool: epicor_explore

Combined discover + describe in one call.

Callers typically call ``epicor_discover_services``
followed by 1-3 ``epicor_describe_service`` calls (no-fields then
with-fields then per-entity-set). This tool collapses that orchestration:
search returns matched services WITH their entity sets and the most
relevant fields per set inline.
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


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_explore`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_explore(
        query: str,
        field_search: str = "",
        services_limit: int = 5,
        entity_sets_per_service: int = 6,
        fields_per_entity_set: int = 12,
    ) -> str:
        """Discover services AND their entity sets AND key fields in one call.

        Use this as the FIRST call when you don't yet know which service /
        entity set holds the data. It returns matched services along with
        their entity sets and the most relevant fields per set — so you
        can usually go straight to ``epicor_query`` without intermediate
        ``epicor_describe_service`` calls.

        Args:
            query: Keywords for the service search (e.g. ``"purchase order"``,
                ``"AR invoice"``, ``"job material"``).
            field_search: Optional space-separated keywords used to rank
                fields within each entity set. When set, the top matching
                fields are surfaced first; when empty, the first N
                alphabetical fields are shown.
            services_limit: Max services to return (1-20, default 5).
            entity_sets_per_service: Max entity sets shown per service
                (default 6 — primary table set is usually first).
            fields_per_entity_set: Max fields shown per entity set
                (default 12). A ``fields_total`` counter is always
                included so you know how much was trimmed.
        """
        try:
            session = get_current_session()

            services_limit = max(1, min(services_limit, 20))
            entity_sets_per_service = max(1, min(entity_sets_per_service, 20))
            fields_per_entity_set = max(1, min(fields_per_entity_set, 50))

            results = index.search_services(
                query,
                department=session.department,
                limit=services_limit,
            )

            field_words = [w.lower() for w in field_search.split()] if field_search else []

            services_out: list[dict] = []
            for svc in results:
                svc_id = svc.get("service_id", "")

                allowed, _ = rbac.check_access(session.user_id, svc_id)
                if not allowed:
                    continue

                method_rows = index.get_methods(svc_id)
                method_names = [m.get("method_name", "") for m in method_rows]
                priority_set = {"GetByID", "GetList", "GetRows", "Update", "Delete"}
                key_methods = (
                    [m for m in method_names if m in priority_set]
                    + [m for m in method_names if m not in priority_set]
                )[:5]

                entity_sets = index.get_entity_sets(svc_id) or []
                es_out: list[dict] = []
                for es_name in entity_sets[:entity_sets_per_service]:
                    fields = index.get_fields(svc_id, es_name)
                    if field_words and fields:
                        def _score(f: dict) -> int:
                            text = (
                                (f.get("field_name", "") + " "
                                 + (f.get("description") or "")).lower()
                            )
                            return sum(1 for w in field_words if w in text)
                        scored = sorted(fields, key=_score, reverse=True)
                        if _score(scored[0]) == 0:
                            scored = fields
                        shown = scored[:fields_per_entity_set]
                    else:
                        shown = fields[:fields_per_entity_set]
                    es_out.append({
                        "name": es_name,
                        "field_count": len(fields),
                        "fields": [
                            {
                                "name": f.get("field_name", ""),
                                "type": f.get("field_type", ""),
                                "description": f.get("description", ""),
                            }
                            for f in shown
                        ],
                    })

                services_out.append({
                    "service_id": svc_id,
                    "description": svc.get("description", ""),
                    "category": svc.get("category", ""),
                    "key_methods": key_methods,
                    "entity_sets_shown": len(es_out),
                    "entity_sets_total": len(entity_sets),
                    "entity_sets": es_out,
                })

            return format_response(
                {
                    "query": query,
                    "field_search": field_search or None,
                    "service_count": len(services_out),
                    "services": services_out,
                    "next_steps": (
                        "Call epicor_query(service=..., entity_set=...) "
                        "directly. If the right entity set isn't visible, "
                        "raise entity_sets_per_service or call "
                        "epicor_describe_service for the full picture."
                    ),
                },
                records_key=None,
            )

        except Exception:
            logger.exception("epicor_explore failed")
            return json.dumps(
                {"error": "Explore failed. Try different query keywords."}
            )

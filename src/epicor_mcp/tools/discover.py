"""Tool: epicor_discover_services

Search for Epicor services by keyword, category, or domain.  This is the
recommended starting point when the caller does not yet know which service
handles their query.
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
    """Bind the ``epicor_discover_services`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_discover_services(
        query: str,
        category: str = "",
        limit: int = 15,
    ) -> str:
        """Search for Epicor services by keyword, category, or domain.

        Returns matching services with descriptions and key methods.
        **Use this first** to find which service handles your task.

        Examples
        --------
        - ``epicor_discover_services(query="purchase order")``
        - ``epicor_discover_services(query="AP invoice", category="Accounts Payable")``
        - ``epicor_discover_services(query="vendor", limit=5)``

        Parameters
        ----------
        query : str
            Search keywords (e.g. ``"purchase order"``, ``"AP invoice"``,
            ``"vendor"``).
        category : str, optional
            Category filter such as ``"Accounts Payable"``,
            ``"Manufacturing"``, ``"Sales"``, etc.
        limit : int, optional
            Maximum number of services to return (default ``15``).
        """
        try:
            # Clamp limit to a sane range.
            limit = max(1, min(limit, 50))

            # Retrieve the current session from the request context.
            session = get_current_session()

            # Search the pre-built service index.
            results = index.search_services(
                query,
                department=session.department,
                limit=limit,
            )

            # Optional category post-filter.
            if category:
                cat_lower = category.lower()
                results = [
                    r for r in results if cat_lower in r.get("category", "").lower()
                ]

            # Build the response list.
            output: list[dict] = []
            for svc in results:
                svc_id = svc.get("service_id", "")

                # Fetch methods from the index for this service.
                method_rows = index.get_methods(svc_id)
                method_names = [m.get("method_name", "") for m in method_rows]

                # Separate priority methods from custom ones.
                priority_set = {"GetByID", "GetList", "GetRows", "Update", "Delete"}
                priority = [m for m in method_names if m in priority_set]
                custom = [m for m in method_names if m not in priority_set]

                # Show up to 5 key methods: priority first, then custom.
                key_methods = (priority + custom)[:5]

                output.append(
                    {
                        "service_id": svc_id,
                        "short_name": svc.get("short_name", ""),
                        "description": svc.get("description", ""),
                        "category": svc.get("category", ""),
                        "method_count": svc.get("method_count", 0),
                        "key_methods": key_methods,
                    }
                )

            return format_response(output, records_key=None)

        except Exception:
            logger.exception("epicor_discover_services failed")
            return json.dumps(
                {"error": "Failed to search services. Please try a different query."}
            )

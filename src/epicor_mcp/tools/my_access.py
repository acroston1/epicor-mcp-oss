"""Tool: epicor_my_access

Returns the current user's access profile — departments, access level,
BAQ write capability, and available tools.  Claude should call this at
the start of a conversation to plan its approach.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.config import get_settings
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
    """Bind the ``epicor_my_access`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_my_access(
        include_services: bool = False,
        include_entity_sets: bool = False,
    ) -> str:
        """Return the current user's access profile for this MCP server.

        Call this tool FIRST at the start of every conversation to
        understand what the user can and cannot do.  Use the result to
        plan your approach — do not attempt tools or actions that the
        user's access level does not permit.

        Args:
            include_services: When ``True``, include the list of accessible
                service IDs grouped by department (with one-line
                descriptions). Lets you skip a follow-up
                ``epicor_discover_services`` call when you already know
                what you're looking for.
            include_entity_sets: When ``True``, also include each service's
                entity-set names. Implies ``include_services=True``.
                Useful at session start so ``epicor_query`` can be called
                directly without a describe roundtrip.

        Returns the user's departments, access level, BAQ-write
        capability, per-department service counts, and the list of
        accessible tools.
        """
        try:
            session = get_current_session()
            user = rbac._user_map.get_user(session.user_id)

            if user is None:
                return json.dumps({"error": "User profile not found."})

            all_departments = [user.department] + (user.extra_departments or [])
            # Report the tools ACTUALLY registered on this server — the
            # enforcer's static list still holds older short names (discover,
            # query, run_baq, ...) that don't exist on the legacy surface, and a
            # wrong list here makes the model deny capabilities it has.
            try:
                available_tools = sorted(
                    t.name for t in server._tool_manager.list_tools()
                )
            except Exception:  # pragma: no cover — private-API fallback
                available_tools = rbac.get_available_tools(session.user_id)

            guidance: list[str] = []

            if user.access_level.value == "read_only":
                guidance.append(
                    "You have read-only access to ERP RECORDS: query freely "
                    "(epicor_read; epicor_baq run/dashboard) but you cannot "
                    "create, update, or delete ERP records (epicor_act "
                    "writes are blocked). NOTE: BAQ authoring is a separate "
                    "permission — see the next item; read-only records "
                    "access does NOT mean you can't create BAQs."
                )
            else:
                guidance.append(
                    "You have read-write access. You can query data and also "
                    "create, update, and delete records via epicor_act."
                )

            if user.can_write_baqs and get_settings().enable_baq_create:
                guidance.append(
                    "You CAN create BAQs (independent of the record-access "
                    "level above — never tell the user you lack BAQ-write "
                    "access). Use epicor_baq with action='create': pass "
                    "`tables` (business terms or Schema.Table names), "
                    "optional `fields`, and ALL filter criteria in `where` "
                    "(business terms; you never write SQL — the server "
                    "composes, saves, and runs it). action='find'/'schema' "
                    "explore the data dictionary first if needed."
                )
            elif user.can_write_baqs:
                guidance.append(
                    "You have BAQ authoring permission, but authoring is "
                    "switched OFF on this server deployment — epicor_baq "
                    "supports only run/dashboard/schema here."
                )
            else:
                guidance.append(
                    "You do NOT have BAQ creation access. Do not attempt "
                    "epicor_baq action='create'. Answer data questions with "
                    "epicor_read."
                )

            # Menu-derived authorization chain. Present when
            # the enforcer runs in shadow/enforce with a menu authorizer wired.
            # The authorizer's explain() carries: epicor_user_id, security_mgr,
            # stale, allowed_service/menu counts, and per-service granted_by
            # [{menu_id, sec_code, matched_principals}] (plus groups/age when it
            # exposes them). Under menu-authz, departments are informational.
            authorizer = getattr(rbac, "_authorizer", None)
            mode = getattr(rbac, "_mode", "off")
            menu_chain: dict | None = None
            if authorizer is not None and mode in ("shadow", "enforce"):
                try:
                    if authorizer.get_snapshot(session.user_id) is None:
                        await authorizer.ensure_snapshot(session.user_id)
                    menu_chain = authorizer.explain(session.user_id)
                except Exception:
                    logger.exception("my_access: menu-authz explain failed")
                    menu_chain = None

            services_by_department: dict = {}
            for dept in all_departments:
                svc_ids = sorted(index.get_department_services(dept) or [])
                entry: dict = {"service_count": len(svc_ids)}

                if include_services or include_entity_sets:
                    services_list: list[dict] = []
                    for sid in svc_ids:
                        svc_meta = index.get_service(sid) or {}
                        item: dict = {
                            "service_id": sid,
                            "description": svc_meta.get("description", ""),
                        }
                        if include_entity_sets:
                            es = index.get_entity_sets(sid) or []
                            item["entity_sets"] = es
                            item["entity_set_count"] = len(es)
                        services_list.append(item)
                    entry["services"] = services_list

                services_by_department[dept] = entry

            result = {
                "user": session.user_id,
                "display_name": user.display_name,
                "departments": all_departments,
                "departments_note": (
                    "Informational only — your access is derived from your "
                    "Epicor menu security (the apps behind the menu items you "
                    "can launch), not these department labels."
                    if menu_chain is not None
                    else "Department-based access (legacy)."
                ),
                "access_level": user.access_level.value,
                "can_write_baqs": user.can_write_baqs,
                "environment": user.environment,
                "available_tools": available_tools,
                "services_by_department": services_by_department,
                "guidance": guidance,
            }

            if menu_chain is not None:
                result["authorization_source"] = (
                    "menu"
                    if mode == "enforce"
                    else "shadow (menu computed, department currently enforced)"
                )
                result["menu_authorization"] = menu_chain
            else:
                result["authorization_source"] = "department"

            return format_response(result, records_key=None)

        except Exception:
            logger.exception("epicor_my_access failed")
            return json.dumps({"error": "Failed to retrieve access profile."})

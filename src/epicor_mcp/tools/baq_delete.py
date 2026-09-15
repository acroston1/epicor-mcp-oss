"""Tool: epicor_baq_delete

Delete a BAQ (Business Activity Query) from Epicor by its ID.
Requires BAQ write access or full write access.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session

if TYPE_CHECKING:
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    baq_index: "BAQSchemaIndex",
) -> None:
    """Bind the ``epicor_baq_delete`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_baq_delete(baq_id: str) -> str:
        """Delete a BAQ (Business Activity Query) from Epicor.

        Requires BAQ write access or full write access. Only deletes
        BAQs prefixed with ``AUTO-`` to prevent accidental deletion
        of manually created BAQs.

        Args:
            baq_id: The BAQ ID to delete (e.g., ``"AUTO-ar-invoices-yesterday"``).

        Examples
        --------
        - ``epicor_baq_delete(baq_id="AUTO-ar-invoices-yesterday")``
        """
        try:
            session = get_current_session()

            # --- RBAC: must have BAQ write or full write access ---
            user_profile = rbac._user_map.get_user(session.user_id)
            has_baq_permission = (
                session.access_level == "read_write"
                or (user_profile and user_profile.can_write_baqs)
            )
            if not has_baq_permission:
                return json.dumps({
                    "error": (
                        "BAQ deletion requires BAQ Designer permissions or "
                        "full write access. Contact your administrator."
                    )
                })

            # --- Validate baq_id ---
            baq_id = baq_id.strip()
            if not baq_id:
                return json.dumps({"error": "baq_id cannot be empty."})

            if not baq_id.startswith("AUTO-"):
                return json.dumps({
                    "error": (
                        f"Can only delete BAQs with the AUTO- prefix. "
                        f"'{baq_id}' does not start with AUTO-. "
                        "Manually created BAQs must be deleted through the Epicor UI."
                    )
                })

            # --- Get API key ---
            if session.access_level == "read_write":
                api_key = rbac._user_map.get_write_key()
            else:
                api_key = rbac._user_map.get_baq_key()
            if not api_key:
                return json.dumps({"error": "No BAQ write API key configured."})

            # --- Delete the BAQ ---
            url = "Ice.BO.BAQDesignerSvc/DeleteByID"
            response = await client.post(url, api_key, json_body={"queryID": baq_id})

            logger.info(
                "epicor_baq_delete: Deleted BAQ '%s' (user=%s)",
                baq_id, session.user_id,
            )

            return json.dumps({
                "success": True,
                "deleted": baq_id,
                "message": f"BAQ '{baq_id}' has been deleted.",
            })

        except RuntimeError as exc:
            return json.dumps({"error": f"Authentication required: {exc}"})
        except Exception as exc:
            logger.exception("epicor_baq_delete failed")
            return json.dumps({"error": f"Failed to delete BAQ: {exc}"})

"""Tool: epicor_workflow

Execute multi-step Epicor dataset workflows for creating, updating, and
deleting records.  Wraps :class:`DatasetHandler` with RBAC checks and
JSON serialisation for the MCP tool layer.

This is the primary write-operation tool.  It automates Epicor's multi-step
dataset (``ds``) pattern so callers don't need to manage intermediate state:

- **Create**: ``GetNew{Entity}`` -> set fields -> ``Update``
- **Update**: ``GetByID`` -> set ``RowMod="U"`` -> modify fields -> ``Update``
- **Delete**: ``GetByID`` -> set ``RowMod="D"`` -> ``Update``

``Change*`` methods are called automatically where the service index shows
they exist, so server-side validation and side effects fire correctly.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.dataset_handler import DatasetHandler
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"create", "update", "delete"}


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    dataset_handler: "DatasetHandler",
) -> None:
    """Bind the ``epicor_workflow`` tool to *server*.

    Parameters
    ----------
    server:
        The MCP ``Server`` instance.
    index:
        Pre-built service index for metadata lookups.
    rbac:
        RBAC enforcer for access checks.
    client:
        HTTP client for Epicor API calls (not used directly; the
        ``DatasetHandler`` uses it internally).
    dataset_handler:
        Pre-configured ``DatasetHandler`` instance.
    """

    @server.tool(structured_output=False)
    async def epicor_workflow(
        service: str,
        action: str,
        entity: str,
        changes: str = "{}",
        record_id: str = "{}",
    ) -> str:
        """Execute a multi-step Epicor workflow for creating, updating, or deleting records.

        This tool handles the complex dataset (ds) pattern automatically:
        - Create: GetNew -> set fields -> Update
        - Update: GetByID -> modify fields -> Update
        - Delete: GetByID -> mark for deletion -> Update

        Change* methods are called automatically when the service exposes
        them for a given field, ensuring server-side validation and
        calculated-field logic runs correctly.

        Args:
            service: Full service name (e.g., "Erp.BO.POSvc").
            action: One of "create", "update", "delete".
            entity: The entity/table to operate on (e.g., "POHeader", "APInvHed").
            changes: JSON string of field changes
                (e.g., '{"VendorNum": 1234, "InvoiceNum": "INV-001"}').
                Required for create and update actions; ignored for delete.
            record_id: JSON string of primary key for update/delete
                (e.g., '{"poNum": 12345}').
                Required for update and delete actions; ignored for create.

        Examples:
            Create a PO header:
                epicor_workflow(service="Erp.BO.POSvc", action="create",
                    entity="POHeader",
                    changes='{"VendorNum": 1234, "BuyerID": "JSmith"}')

            Update a vendor name:
                epicor_workflow(service="Erp.BO.VendorSvc", action="update",
                    entity="Vendor",
                    record_id='{"vendorNum": 5678}',
                    changes='{"Name": "New Vendor Name"}')

            Delete an AP invoice:
                epicor_workflow(service="Erp.BO.APInvoiceSvc", action="delete",
                    entity="APInvHed",
                    record_id='{"vendorNum": 1234, "invoiceNum": "INV-001"}')
        """
        try:
            session = get_current_session()

            # ------------------------------------------------------------------
            # RBAC check: user must have read_write access
            # ------------------------------------------------------------------
            if session.access_level != "read_write":
                return json.dumps(
                    {
                        "error": (
                            "Write access required. Your current access level "
                            f"is '{session.access_level}'. Contact your "
                            "administrator to request read_write access."
                        )
                    }
                )

            # ------------------------------------------------------------------
            # RBAC check: department must have service access
            # ------------------------------------------------------------------
            allowed, msg = rbac.check_access(session.user_id, service)
            if not allowed:
                return json.dumps({"error": msg})

            write_allowed, write_msg = rbac.check_write_access(
                session.user_id, service
            )
            if not write_allowed:
                return json.dumps({"error": write_msg})

            # Workflow is always a write operation — use write key
            from epicor_mcp.rbac.user_map import UserMap
            write_key = rbac._user_map.get_write_key()
            api_key = write_key or rbac.check_service_access(session.user_id, service).api_key or ""

            # ------------------------------------------------------------------
            # Validate action
            # ------------------------------------------------------------------
            action_lower = action.strip().lower()
            if action_lower not in _VALID_ACTIONS:
                return json.dumps(
                    {
                        "error": (
                            f"Invalid action '{action}'. "
                            f"Must be one of: {', '.join(sorted(_VALID_ACTIONS))}"
                        )
                    }
                )

            # ------------------------------------------------------------------
            # Parse JSON string parameters
            # ------------------------------------------------------------------
            try:
                parsed_changes = json.loads(changes)
            except json.JSONDecodeError as exc:
                return json.dumps(
                    {
                        "error": (
                            f"Invalid JSON in 'changes': {exc}. "
                            "Provide a valid JSON object, e.g. "
                            "'{\"VendorNum\": 1234}'"
                        )
                    }
                )

            if not isinstance(parsed_changes, dict):
                return json.dumps(
                    {
                        "error": (
                            "'changes' must be a JSON object (dict), not "
                            f"{type(parsed_changes).__name__}."
                        )
                    }
                )

            try:
                parsed_record_id = json.loads(record_id)
            except json.JSONDecodeError as exc:
                return json.dumps(
                    {
                        "error": (
                            f"Invalid JSON in 'record_id': {exc}. "
                            "Provide a valid JSON object, e.g. "
                            "'{\"poNum\": 12345}'"
                        )
                    }
                )

            if not isinstance(parsed_record_id, dict):
                return json.dumps(
                    {
                        "error": (
                            "'record_id' must be a JSON object (dict), not "
                            f"{type(parsed_record_id).__name__}."
                        )
                    }
                )

            # ------------------------------------------------------------------
            # Parameter validation per action
            # ------------------------------------------------------------------
            if action_lower == "create" and not parsed_changes:
                return json.dumps(
                    {
                        "error": (
                            "The 'changes' parameter is required for create "
                            "actions. Provide field values as a JSON object."
                        )
                    }
                )

            if action_lower in ("update", "delete") and not parsed_record_id:
                return json.dumps(
                    {
                        "error": (
                            f"The 'record_id' parameter is required for "
                            f"{action_lower} actions. Provide primary key "
                            "values as a JSON object (e.g., "
                            "'{\"poNum\": 12345}')."
                        )
                    }
                )

            if action_lower == "update" and not parsed_changes:
                return json.dumps(
                    {
                        "error": (
                            "The 'changes' parameter is required for update "
                            "actions. Provide the field values to modify."
                        )
                    }
                )

            # ------------------------------------------------------------------
            # Resolve base_url from the user map / credential manager.
            # The dataset handler needs the base URL to construct full URLs
            # for the Epicor API, but since the EpicorClient uses relative
            # URLs passed directly, we pass an empty string -- the client
            # will prepend the base URL if configured, or use the URL as-is.
            # ------------------------------------------------------------------
            base_url = ""  # Client handles URL resolution internally

            # ------------------------------------------------------------------
            # Dispatch to the DatasetHandler
            # ------------------------------------------------------------------
            if action_lower == "create":
                logger.info(
                    "epicor_workflow: CREATE %s/%s (user=%s, dept=%s)",
                    service,
                    entity,
                    session.user_id,
                    session.department,
                )
                result = await dataset_handler.create_record(
                    base_url=base_url,
                    service=service,
                    entity=entity,
                    api_key=api_key,
                    changes=parsed_changes,
                )

            elif action_lower == "update":
                logger.info(
                    "epicor_workflow: UPDATE %s/%s id=%s (user=%s, dept=%s)",
                    service,
                    entity,
                    parsed_record_id,
                    session.user_id,
                    session.department,
                )
                result = await dataset_handler.update_record(
                    base_url=base_url,
                    service=service,
                    api_key=api_key,
                    record_id=parsed_record_id,
                    entity=entity,
                    changes=parsed_changes,
                )

            elif action_lower == "delete":
                logger.info(
                    "epicor_workflow: DELETE %s/%s id=%s (user=%s, dept=%s)",
                    service,
                    entity,
                    parsed_record_id,
                    session.user_id,
                    session.department,
                )
                result = await dataset_handler.delete_record(
                    base_url=base_url,
                    service=service,
                    api_key=api_key,
                    record_id=parsed_record_id,
                    entity=entity,
                )

            # ------------------------------------------------------------------
            # Return the result as JSON
            # ------------------------------------------------------------------
            # Trim empty child tables from the result to reduce response size
            if isinstance(result, dict):
                result = dataset_handler.trim_dataset(result, target_entity=entity)

            return format_response(
                {
                    "success": True,
                    "action": action_lower,
                    "service": service,
                    "entity": entity,
                    "result": result,
                },
                records_key=None,
                strip_only=True,
            )

        except Exception as exc:
            logger.exception("epicor_workflow failed")

            # Build a structured error response with as much context as
            # possible so the caller (or an LLM) can diagnose the issue.
            error_msg = str(exc)
            error_response: dict = {
                "error": error_msg,
                "action": action,
                "service": service,
                "entity": entity,
            }

            # Try to extract the failing step from the error message
            # (DatasetHandler embeds it, e.g., "... failed at step 'Update': ...")
            if "failed at step" in error_msg:
                try:
                    step_part = error_msg.split("failed at step '")[1]
                    step_name = step_part.split("'")[0]
                    error_response["step"] = step_name
                except (IndexError, ValueError):
                    pass

            return json.dumps(error_response, indent=2)

"""Tool: epicor_run_method

Execute any method on an Epicor service.  Read methods (``Get*``, excluding
``GetNew*``) are available to all users who have access to the service.
Write methods require explicit write permission in RBAC.
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

# Method name prefixes that are considered read-only.  ``GetNew*`` is
# intentionally excluded because it mutates the dataset (adds a blank row).
_READ_PREFIXES = ("Get",)
_WRITE_LIKE_GET_PREFIXES = ("GetNew",)


def _is_read_only(method: str) -> bool:
    """Return ``True`` if *method* is a read-only accessor."""
    if any(method.startswith(p) for p in _WRITE_LIKE_GET_PREFIXES):
        return False
    return any(method.startswith(p) for p in _READ_PREFIXES)


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_run_method`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_run_method(
        service: str,
        method: str,
        params: str = "{}",
    ) -> str:
        """Execute a specific method on an Epicor service.

        For read methods (``Get*``, excluding ``GetNew*``), any user with
        service access can call them.  For write/mutation methods, the user
        must also have write permission.

        Examples
        --------
        - ``epicor_run_method(service="Erp.BO.VendorSvc",
              method="GetList",
              params='{"whereClause": "VendorNum > 100", "pageSize": 10, "absolutePage": 1}')``
        - ``epicor_run_method(service="Erp.BO.POSvc",
              method="GetRows",
              params='{"whereClausePOHeader": "OpenOrder eq true", "pageSize": 25, "absolutePage": 1}')``
        - ``epicor_run_method(service="Erp.BO.VendorSvc",
              method="ChangeVendorID",
              params='{"ds": {...}, "proposedVendorID": "NEW-ID"}')``

        Parameters
        ----------
        service : str
            Full service name (e.g. ``"Erp.BO.VendorSvc"``).
        method : str
            Method name (e.g. ``"GetList"``, ``"GetRows"``,
            ``"ChangeVendorID"``).
        params : str, optional
            JSON string of method parameters (default ``"{}"``).
            For ``GetList`` this typically includes ``whereClause``,
            ``pageSize``, and ``absolutePage``.
        """
        try:
            session = get_current_session()

            # --- RBAC: service-level access --------------------------------
            allowed, msg = rbac.check_access(session.user_id, service)
            if not allowed:
                return json.dumps({"error": msg})

            # Get the API key from the RBAC enforcer.
            svc_result = rbac.check_service_access(session.user_id, service)
            api_key = svc_result.api_key or ""

            # --- RBAC: write check for mutation methods --------------------
            read_only = _is_read_only(method)
            if not read_only:
                write_allowed, write_msg = rbac.check_write_access(
                    session.user_id, service
                )
                if not write_allowed:
                    return json.dumps({"error": write_msg})
                # Use write API key for mutation methods
                method_result = rbac.check_method_access(session.user_id, service, method)
                if method_result.api_key:
                    api_key = method_result.api_key

            # --- Validate method exists on service -------------------------
            known_methods = index.get_methods(service)
            if known_methods:
                valid_names = {m.get("method_name", "") for m in known_methods}
                if method not in valid_names:
                    return json.dumps(
                        {
                            "error": (
                                f"Method '{method}' not found on service "
                                f"'{service}'. Use epicor_describe_service "
                                "to list available methods."
                            )
                        }
                    )

            # --- Parse params ----------------------------------------------
            try:
                parsed_params = json.loads(params)
            except json.JSONDecodeError as exc:
                return json.dumps(
                    {
                        "error": (
                            f"Invalid JSON in 'params': {exc}. "
                            "Provide a valid JSON string."
                        )
                    }
                )

            # --- Execute method --------------------------------------------
            url = f"{service}/{method}"
            response = await client.post(url, api_key, json_body=parsed_params)

            # Epicor may return the useful payload under ``returnObj`` or
            # ``parameters`` (which itself may contain ``ds`` and/or scalar
            # return values).
            result = (
                response.get("returnObj")
                or response.get("parameters")
                or response
            )

            return format_response(result, records_key=None)

        except Exception:
            logger.exception("epicor_run_method failed")
            return json.dumps(
                {
                    "error": (
                        f"Method {service}/{method} failed. "
                        "Verify the service name, method, and parameters."
                    )
                }
            )

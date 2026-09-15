"""Tool: epicor_time_phase — the part time-phased material inquiry.

A DEDICATED tool (not folded into ``epicor_read``) on purpose. Weak models
treat "time phase" as an
*operation* to run, not a table to read: they never call ``epicor_read`` for it
— instead they burn calls in ``epicor_help`` hunting for "the PartSchedSvc /
MRP business object / method to run time phasing", and give up. Giving the
intent its own clearly-named tool (next to ``epicor_mrp_status`` /
``epicor_mrp_output``) is what makes it discoverable — the model looking for a
time-phase/MRP affordance finds it by name and calls it in one shot.

The engine is shared with ``epicor_read``'s time-phase route via
``_partviews.timephase_for_part`` (``Erp.BO.TimePhasSvc/GoProcessTimePhase``,
per-plant, auto-iterated over all configured plants when none is given).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.tools._partviews import timephase_for_part

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_DESCRIPTION = (
    "Time-phased material requirements for a PART — its supply & demand "
    "schedule: on-hand, incoming jobs & POs (supply) vs sales orders & job "
    "material demand, with the running projected balance and MRP suggestions. "
    "This is the 'Time Phased Inquiry' / time phasing / MRP part-schedule view. "
    "ONE call — just give the part number; it runs the inquiry for you (all "
    "sites, or pass `plant` for one). Do NOT hunt for a PartSched/TimePhas/"
    "PartDtl business object or a BAQ — this tool is that capability."
)


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_time_phase`` tool to *server*."""

    @server.tool(structured_output=False, description=_DESCRIPTION)
    async def epicor_time_phase(
        part: str,
        plant: str = "",
        limit: int = 25,
    ) -> str:
        """Time-phased supply/demand schedule for a part. See tool description.

        Parameters
        ----------
        part : str
            The part number (exact). Required.
        plant : str, optional
            Plant/site code (e.g. "10") to scope one site. Blank = every
            configured plant, merged.
        limit : int, optional
            Max schedule rows to return (default 25).
        """
        try:
            part = (part or "").strip()
            if not part:
                return json.dumps({
                    "error": "need_part",
                    "message": "Give the part number whose time phase you want, "
                               "e.g. epicor_time_phase(part=\"PART-001\").",
                })
            session = get_current_session()
            return await timephase_for_part(
                client, rbac, session,
                part=part, plant=(plant or "").strip(),
                fields="", limit=max(1, min(int(limit), 500)))
        except Exception:
            logger.exception("epicor_time_phase failed")
            return json.dumps({
                "error": "time_phase_failed",
                "message": "epicor_time_phase failed — check the part number.",
            })

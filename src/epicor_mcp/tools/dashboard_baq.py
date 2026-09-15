"""Tool: epicor_dashboard_baq

Look up an Epicor dashboard by name, resolve the BAQ(s) that power it,
and optionally execute them to return the underlying data — all in one call.

Uses ``Ice.BO.DashBoardSvc`` for dashboard metadata and ``BaqSvc`` for
BAQ execution.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.response import format_response

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_DASHBOARD_SERVICE = "Ice.BO.DashBoardSvc"
_MAX_BAQS = 5
_MAX_DASHBOARD_LIST = 60


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_dashboard_baq`` tool to *server*."""

    @server.tool(structured_output=False)
    async def epicor_dashboard_baq(
        dashboard: str,
        filter: str = "",
        top: int = 25,
        execute: bool = True,
    ) -> str:
        """Look up an Epicor dashboard and retrieve the BAQ data that powers it.

        Use this when a user asks about a dashboard, wants to see dashboard
        data, or asks "what BAQ runs behind [dashboard]?"

        This tool finds the dashboard by name, resolves its underlying BAQ
        query IDs, and executes them to return the actual data.

        Examples
        --------
        - ``epicor_dashboard_baq(dashboard="OpenBacklog")``
        - ``epicor_dashboard_baq(dashboard="ARAnalysis", top=50)``
        - ``epicor_dashboard_baq(dashboard="OpenBacklog", execute=False)``
          — just return the BAQ IDs without running them

        Parameters
        ----------
        dashboard : str
            Dashboard name to search for (exact match tried first,
            then partial/fuzzy match).
        filter : str, optional
            OData ``$filter`` expression applied to the BAQ results.
        top : int, optional
            Maximum records to return per BAQ, 1--1000 (default ``25``).
            For CSV output or larger result sets, call
            ``epicor_run_baq(baq_id=<id>, format="csv")`` directly on
            a specific BAQ after ``execute=False`` returns its ID.
        execute : bool, optional
            If ``True`` (default), execute each BAQ and return data.
            If ``False``, return only the dashboard metadata and BAQ IDs.
        """
        try:
            session = get_current_session()

            # --- RBAC: dashboard lookup needs read access to Ice.BO -------
            svc_result = rbac.check_service_access(
                session.user_id, _DASHBOARD_SERVICE
            )
            if not svc_result.allowed:
                return json.dumps({"error": svc_result.message})

            read_key = svc_result.api_key or ""

            # --- Step 0: Discovery ("which dashboards can I use?") ----------
            # A blank or purely-generic name ("a dashboard", "which
            # dashboards", "list") means the user wants to SEE what exists,
            # not resolve one. List the real dashboards so the model offers
            # actual names instead of guessing one. These are all DEFINED
            # dashboards; a per-user menu filter is a separate project
            # (dashboards are unmapped in menu_security.db), so this is
            # honestly labelled.
            if _is_list_request(dashboard):
                items = await _list_dashboards(client, read_key)
                out: dict = {
                    "mode": "dashboard_list",
                    "count": len(items),
                    "dashboards": items[:_MAX_DASHBOARD_LIST],
                    "hint": (
                        "Ask the user which of these they mean, then call "
                        "action='dashboard' with its exact `id` (or name) to "
                        "run the BAQs behind it. Do NOT invent a dashboard "
                        "name that is not in this list."
                    ),
                }
                if len(items) > _MAX_DASHBOARD_LIST:
                    out["note"] = (
                        f"{len(items)} dashboards exist; showing the first "
                        f"{_MAX_DASHBOARD_LIST}. Not every one is necessarily "
                        "on this user's menu."
                    )
                return json.dumps(out)

            # --- Step 1: Find the dashboard --------------------------------
            # Exact -> fuzzy -> tokenized/squashed (definitions are usually
            # squashed like 'OpenBacklog' while users say 'Open Backlog dashboard').
            dashboard_def, near_misses = await _find_dashboard(
                client, read_key, dashboard
            )
            if dashboard_def is None:
                err: dict = {
                    "error": f"No dashboard found matching '{dashboard}'.",
                }
                if near_misses:
                    err["candidates"] = [
                        {
                            "id": c.get("DefinitionID", ""),
                            "description": c.get("Description", ""),
                        }
                        for c in near_misses
                    ]
                    err["hint"] = (
                        "If one of `candidates` is the dashboard the user "
                        "meant, re-call ONCE with its exact `id`. If none "
                        "match, the dashboard does not exist — tell the "
                        "user; do NOT search for BAQs or query tables to "
                        "reconstruct it."
                    )
                else:
                    err["terminal"] = True
                    err["hint"] = (
                        "No dashboard has a similar name. Tell the user it "
                        "was not found; do NOT search for BAQs or query "
                        "tables to reconstruct it."
                    )
                return json.dumps(err)

            defn_id = dashboard_def["DefinitionID"]
            description = dashboard_def.get("Description", defn_id)

            # --- Step 2: Resolve BAQ IDs -----------------------------------
            baq_ids = await _get_dashboard_baqs(client, read_key, defn_id)

            result: dict = {
                "dashboard": defn_id,
                "description": description,
                "baq_ids": baq_ids,
            }

            if not baq_ids:
                result["note"] = "This dashboard has no BAQ queries attached."
                return format_response(result, records_key=None)

            if not execute:
                return format_response(result, records_key=None)

            # --- Step 3: Execute each BAQ ----------------------------------
            baq_result = rbac.check_baq_access(session.user_id)
            if not baq_result.allowed:
                result["note"] = (
                    "BAQ IDs resolved but cannot execute: "
                    + baq_result.message
                )
                return format_response(result, records_key=None)

            baq_key = baq_result.api_key or ""
            top = max(1, min(top, 1000))

            baq_results: dict = {}
            for baq_id in baq_ids[:_MAX_BAQS]:
                baq_results[baq_id] = await _execute_baq(
                    client, baq_key, baq_id, filter, top
                )

            result["baq_results"] = baq_results

            if len(baq_ids) > _MAX_BAQS:
                result["note"] = (
                    f"Dashboard has {len(baq_ids)} BAQs; only the "
                    f"first {_MAX_BAQS} were executed."
                )

            return format_response(result, records_key=None)

        except Exception:
            logger.exception("epicor_dashboard_baq failed")
            return json.dumps({
                "error": (
                    f"Failed to look up dashboard '{dashboard}'. "
                    "Verify the name and try again."
                )
            })


# Words users append that never appear in DefinitionIDs.
_NOISE_WORDS = {"dashboard", "dashboards", "the", "a", "an", "for",
                "epicor", "accounts"}

# Words that signal a DISCOVERY ("show me the dashboards") rather than a
# specific dashboard name. If a phrase is nothing but these plus noise words,
# there is no name to resolve — list what's available instead.
_LIST_TRIGGERS = {"list", "which", "available", "all", "my", "see", "show",
                  "me", "what", "whats", "are", "there", "any", "some",
                  "view", "go", "to", "look", "at", "pull", "up", "use",
                  "using", "check", "can", "i"}


def _is_list_request(name: str) -> bool:
    """True when *name* names no specific dashboard (blank or all-generic)."""
    if not name or not name.strip():
        return True
    meaningful = [
        t.lower() for t in re.split(r"[^A-Za-z0-9]+", name)
        if t and t.lower() not in _NOISE_WORDS
        and t.lower() not in _LIST_TRIGGERS
    ]
    return not meaningful


async def _list_dashboards(
    client: "EpicorClient", api_key: str,
) -> list[dict]:
    """All dashboard definitions as ``[{id, description}]``, deduped + sorted."""
    rows = await _get_list(client, api_key, "", page_size=500)
    seen: set[str] = set()
    items: list[dict] = []
    for row in rows:
        did = (row.get("DefinitionID") or "").strip()
        if not did or did in seen:
            continue
        seen.add(did)
        items.append({
            "id": did,
            "description": (row.get("Description") or "").strip(),
        })
    items.sort(key=lambda d: (d["description"] or d["id"]).lower())
    return items

# Business-speak -> the abbreviation Epicor definitions actually use.
_TOKEN_SYNONYMS = {
    "receivables": "ar", "receivable": "ar",
    "payables": "ap", "payable": "ap",
}


def _squash(text: str) -> str:
    """Lowercase and drop every non-alphanumeric character."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _name_tokens(name: str) -> list[str]:
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", name)
            if t and t.lower() not in _NOISE_WORDS]


def _alternatives(word: str) -> list[str]:
    """A token plus its synonym and crude stem variants ('aged' ~ 'aging')."""
    alts = [word]
    syn = _TOKEN_SYNONYMS.get(word)
    if syn:
        alts.append(syn)
    if word.endswith("ing") and len(word) > 4:
        alts += [word[:-3] + "ed", word[:-3]]
    elif word.endswith("ed") and len(word) > 3:
        alts += [word[:-2] + "ing", word[:-1]]
    elif word.endswith("s") and len(word) > 3:
        alts.append(word[:-1])
    return alts


def _group_score(alts: list[str], sid: str, sdesc: str, hay: str) -> float:
    """Best match for one query word (any of its variants) in a candidate."""
    best = 0.0
    for a in alts:
        if len(a) <= 2:
            # Short abbreviations ('ar', 'ap') appear inside too many words
            # ('part', 'margin') — only credit them anchored at the start.
            if sid.startswith(a) or sdesc.startswith(a):
                return 1.0
            continue
        if a in hay:
            return 1.0
        if len(a) > 4 and a[:4] in hay:
            best = max(best, 0.5)
    return best


async def _find_dashboard(
    client: "EpicorClient",
    api_key: str,
    name: str,
) -> tuple[dict | None, list[dict]]:
    """Resolve a dashboard name to its definition row.

    Returns ``(match, near_misses)``: a confident match with no
    near-misses, or ``(None, candidates)`` when only partial matches
    exist so the caller can offer them for a one-shot retry.
    """
    # Exact match on DefinitionID
    rows = await _get_list(client, api_key, f"DefinitionID = '{name}'")
    if rows:
        return rows[0], []

    # Exact match on Description (the user-facing name)
    rows = await _get_list(client, api_key, f"Description = '{name}'")
    if rows:
        return rows[0], []

    # Fuzzy match on both DefinitionID and Description
    rows = await _get_list(
        client, api_key,
        f"DefinitionID like '%{name}%' or Description like '%{name}%'",
    )
    if rows:
        return rows[0], []

    # Definitions are usually squashed ('OpenBacklog') while users speak in
    # words ('Open Backlog dashboard') — no server-side LIKE on the whole
    # phrase can ever match. Pull the full definition list once (small)
    # and match client-side on squashed/tokenized forms.
    toks = _name_tokens(name)
    squashed_query = _squash("".join(toks))
    if not squashed_query:
        return None, []
    groups = [_alternatives(t) for t in toks]
    all_rows = await _get_list(client, api_key, "", page_size=500)

    scored: list[tuple[float, dict]] = []
    for row in all_rows:
        sid = _squash(row.get("DefinitionID") or "")
        sdesc = _squash(row.get("Description") or "")
        # Noise-stripped squashed equality is a confident hit
        # ('Open Backlog dashboard' -> 'openbacklog' == 'openbacklog').
        if squashed_query in (sid, sdesc):
            return row, []
        hay = f"{sid} {sdesc}"
        hits = sum(_group_score(g, sid, sdesc, hay) for g in groups)
        if hits:
            scored.append((hits / max(len(groups), 1), row))

    scored.sort(key=lambda pair: -pair[0])
    if scored:
        full = [r for s, r in scored if s >= 1.0]
        # Every query word matched in exactly one definition -> unambiguous.
        if len(full) == 1:
            return full[0], []
        return None, [r for _s, r in scored[:8]]
    return None, []


async def _get_list(
    client: "EpicorClient",
    api_key: str,
    where: str,
    page_size: int = 10,
) -> list[dict]:
    """Call DashBoardSvc/GetList and return the definition rows."""
    resp = await client.call_method(
        client._base_url,
        _DASHBOARD_SERVICE,
        "GetList",
        api_key,
        params={
            "whereClause": where,
            "pageSize": page_size,
            "absolutePage": 1,
        },
    )
    return resp.get("returnObj", {}).get("DashBdDefList", [])


async def _get_dashboard_baqs(
    client: "EpicorClient",
    api_key: str,
    definition_id: str,
) -> list[str]:
    """Return the BAQ QueryIDs attached to a dashboard."""
    resp = await client.call_method(
        client._base_url,
        _DASHBOARD_SERVICE,
        "GetRows",
        api_key,
        params={
            "whereClauseDashBdDef": f"DefinitionID = '{definition_id}'",
            "whereClauseDashBdBAQ": "",
            "whereClauseDashBdChunk": "",
            "whereClauseDashBdLike": "",
            "pageSize": 50,
            "absolutePage": 1,
        },
    )
    baq_rows = resp.get("returnObj", {}).get("DashBdBAQ", [])
    return [row["QueryID"] for row in baq_rows if row.get("QueryID")]


async def _execute_baq(
    client: "EpicorClient",
    api_key: str,
    baq_id: str,
    odata_filter: str,
    top: int,
) -> dict:
    """Execute a single BAQ and return a summary dict."""
    params: dict[str, str | int] = {"$top": top}
    if odata_filter:
        params["$filter"] = odata_filter

    try:
        resp = await client.get(
            f"BaqSvc/{baq_id}/Data", api_key, params=params
        )
        records = resp.get("value", [])
        result: dict = {
            "record_count": len(records),
            "records": records,
        }
        if len(records) == top:
            result["note"] = (
                f"Result limited to {top} rows. "
                "Increase 'top' or add a filter to narrow results."
            )
        return result
    except Exception as exc:
        logger.warning("BAQ %s execution failed: %s", baq_id, exc)
        return {"error": f"BAQ '{baq_id}' execution failed: {exc}"}

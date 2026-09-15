"""PO suggestion recognizer for ``epicor_read`` — NEW vs CHANGE (INV-2, no new tool).

Two Epicor business objects sit right next to each other and weak models cannot
keep them apart:

* **NEW buy suggestions** — ``Erp.BO.POSuggSvc/SugPoDtl`` — parts MRP says to
  order that have no PO yet ("generate PO suggestions").
* **CHANGE suggestions** — ``Erp.BO.POSuggChgSvc/SugPOChg`` — reschedule / cancel
  / qty-change actions on lines of POs that ALREADY exist ("Purchase Order
  Changes").

The failure this fixes: a model called
``epicor_read(target="PO change suggestions", where="OpenOrder = true")`` and got
**200 purchase orders**. Reason: no synonym covered "PO change suggestions", the
bare word "po" word-matched the plain-PO entry, so it resolved to
``Erp.BO.POSvc/POHeader`` — which *does* have an ``OpenOrder`` column, so the
filter succeeded and returned open POs instead of change suggestions. Worse,
neither suggestion entity even HAS an ``OpenOrder`` column, so correct routing
alone would 400 on that habitual filter.

This recognizer:

* routes NEW vs CHANGE deterministically from the phrasing (before
  ``resolve_target`` can mis-resolve it),
* drops an inapplicable ``Open*`` predicate — a suggestion is by definition an
  open, un-actioned recommendation; there is no open/closed flag — and says so,
* orders by ``DueDate desc`` so the freshest actions lead (INV: recency default),
* and always returns a ``note`` naming THIS BO and how to pivot to the other, so
  a wrong pick self-corrects in one turn instead of thrashing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools._engine import run_getrows, run_odata, sql_to_odata
from epicor_mcp.tools._inline_schema import order_refusal, sort_records
from epicor_mcp.tools._resolve import error_envelope

if TYPE_CHECKING:  # pragma: no cover
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

# --- The two look-alike BOs -------------------------------------------------
# Each BO exposes its rows under TWO names: an OData collection segment and a
# GetRows DataSet table (the ``whereClause{Table}`` key). They differ for the
# CHANGE BO — the OData segment is the plural ``POSuggChgs`` and ``SugPOChg`` is
# ONLY the DataSet table (querying ``SugPOChg`` as an OData segment 404s).
_NEW_SVC = "Erp.BO.POSuggSvc"
_NEW_ODATA, _NEW_TABLE = "SugPoDtl", "SugPoDtl"
_CHG_SVC = "Erp.BO.POSuggChgSvc"
_CHG_ODATA, _CHG_TABLE = "POSuggChgs", "SugPOChg"

_NEW_FIELDS = [
    "PONUM", "POLine", "PartNum", "POLinePartNum", "VendorID", "VendorNum",
    "MfgNum", "Plant", "BuyerID", "DueDate", "OrderByDate", "XRelQty", "RelQty",
    "SugType", "SugReason", "OrderNum", "OrderRelNum",
]
_CHG_FIELDS = [
    "PONum", "POLine", "PORelNum", "POLinePartNum", "VendorName", "Plant",
    "BuyerID", "DueDate", "PromiseDate", "ReqPromiseDate", "RequireDate",
    "XRelQty", "SupplierQty", "CancelReason", "OrderNum", "OrderRelNum",
]
_ORDER = "DueDate desc"

# --- Recognition ------------------------------------------------------------
_SUGG_RE = re.compile(r"\bsugg?(?:estion)?s?\b", re.I)   # "sugg", "suggestion(s)"
_PO_RE = re.compile(r"(?<![a-z0-9])(po|purchase\s*orders?)(?![a-z0-9])", re.I)
_CHANGE_RE = re.compile(
    r"\b(change|chg|reschedul\w*|re-?schedul\w*|expedit\w*|de-?expedit\w*|"
    r"cancel\w*|pull-?in|push-?out)\b", re.I)


def _despace(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def detect_po_sugg(target: str) -> str | None:
    """Return ``"change"``, ``"new"``, or ``None`` for a PO-suggestion ask.

    Bare table names (``sugpochg`` / ``sugpodtl``) short-circuit. Otherwise a
    suggestion word is required (so plain "PO changes"/"open POs" is left to the
    normal resolver); a change verb then selects CHANGE over NEW.
    """
    if not target:
        return None
    flat = _despace(target)
    if "sugpochg" in flat or "sugpochgs" in flat:
        return "change"
    if "sugpodtl" in flat:
        return "new"

    has_sugg = bool(_SUGG_RE.search(target))
    if not has_sugg:
        return None
    has_po = bool(_PO_RE.search(target))
    is_buy = "buysugg" in flat or "buysuggestion" in flat
    if not (has_po or is_buy):
        return None
    if _CHANGE_RE.search(target):
        return "change"
    return "new"


# --- Open-predicate scrub ---------------------------------------------------
# "OpenOrder = true" is a reflex the model carries over from POHeader; the
# suggestion tables have no such column. Strip only the open* predicate, keep
# everything else, and report what was dropped.
_OPEN_PRED_RE = re.compile(
    r"(?<![a-z0-9])open\w*\s*(?:=|==|eq)\s*(?:true|false|1|0|'?(?:true|false)'?)"
    r"(?![a-z0-9])", re.I)
_DANGLING_RE = re.compile(r"\b(and|or)\b\s*(\b(and|or)\b\s*)+", re.I)


def _strip_open(where: str) -> tuple[str, str | None]:
    """Remove ``open* = true/false`` predicates from a SQL-ish where.

    Returns ``(cleaned_where, dropped_text_or_None)``.
    """
    if not where or not where.strip():
        return where, None
    hits = _OPEN_PRED_RE.findall(where)
    if not _OPEN_PRED_RE.search(where):
        return where, None
    cleaned = _OPEN_PRED_RE.sub(" ", where)
    # Tidy the connectives the removal orphaned.
    cleaned = _DANGLING_RE.sub(r" \1 ", cleaned)
    cleaned = re.sub(r"^\s*(and|or)\b\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*\b(and|or)\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    dropped = re.search(_OPEN_PRED_RE, where)
    return cleaned, (dropped.group(0).strip() if dropped else "open filter")


async def po_suggestions(
    client,
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    session,
    *,
    kind: str,
    target: str,
    where: str = "",
    limit: int = 20,
    order_by: str = "",
    fields_wanted: str = "",
    soft: dict | None = None,
) -> str:
    """Read NEW or CHANGE PO suggestions in one call, with a pivot note."""
    if kind == "change":
        svc, odata_set, gr_set, fields = _CHG_SVC, _CHG_ODATA, _CHG_TABLE, _CHG_FIELDS
        this_label = "PO CHANGE suggestions"
        pivot = (
            "For NEW buy suggestions (parts to order that have no PO yet) call "
            "epicor_read with target='PO suggestions' "
            "(Erp.BO.POSuggSvc/SugPoDtl).")
    else:
        svc, odata_set, gr_set, fields = _NEW_SVC, _NEW_ODATA, _NEW_TABLE, _NEW_FIELDS
        this_label = "NEW PO (buy) suggestions"
        pivot = (
            "For CHANGE suggestions (reschedule/cancel/qty changes on EXISTING "
            "PO lines) call epicor_read with target='PO change suggestions' "
            "(Erp.BO.POSuggChgSvc/SugPOChg).")

    allowed, msg = rbac.check_access(session.user_id, svc)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, svc).api_key or ""

    cleaned_where, dropped = _strip_open(where)
    odata_filter = sql_to_odata(cleaned_where) if cleaned_where.strip() else ""
    select = ",".join(fields)
    # These are transient/computed suggestion datasets: server-side ordering is
    # NOT supported — BOTH $orderby and GetRows "By <col>" 500 with Epicor's
    # generic apology. So fetch UNORDERED and sort by
    # DueDate client-side. Fetch a pool (not just `limit`) so the soonest-due
    # rows are the globally-soonest, not an arbitrary page, then trim.
    pool = max(1, min(int(limit) if int(limit) > 300 else 300, 1000))

    # POSugg* are not in HEAVY_SERVICES; try OData, fall back to GetRows on any
    # engine error (mirrors the main read path's INV-2 posture).
    raw = None
    try:
        raw = await run_odata(
            client, svc, odata_set, api_key,
            filter=odata_filter, select=select, orderby="",
            top=pool, skip=0, expand="", count_only=False,
            group_by="", aggregate="", distinct="", format="json")
    except EpicorError:
        logger.info("PO suggestion OData failed on %s; falling back to GetRows", svc)
        raw = None
    if raw is None or (_looks_failed(raw)):
        raw = await run_getrows(
            client, index, svc, gr_set, api_key,
            filter=odata_filter, select=select, orderby="",
            top=pool, skip=0, count_only=False,
            group_by="", aggregate="", distinct="", format="json")

    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict) and "error" in parsed:
        return raw
    rows = (parsed.get("records") if isinstance(parsed, dict) else None) or []

    # Soonest-due first (ascending) — the natural purchasing worklist order.
    # Missing/blank due dates sort last.
    def _due_key(r):
        d = r.get("DueDate")
        return (d is None or d == "", str(d) if d else "")

    notes = dict(soft or {})
    order_label = "DueDate asc (client-side; BO rejects server-side order)"
    if (order_by or "").strip():
        # Caller ordering swaps the sort KEY only — it stays CLIENT-SIDE.
        # Do NOT "optimize" this to $orderby/GetRows `By <col>`: both 500 on
        # POSugg*/POSuggChg.
        rows, err_kind, valid_cols = sort_records(
            rows, order_by, available=list(fields))
        if err_kind:
            return json.dumps(order_refusal(err_kind, order_by, valid_cols))
        order_label = (f"{order_by} (client-side; BO rejects server-side "
                       "order)")
        notes["order"] = order_label
    else:
        rows.sort(key=_due_key)
    truncated = len(rows) >= pool
    rows = rows[:max(1, int(limit))]
    if (fields_wanted or "").strip():
        want = [c for c in fields
                if c.lower() in {t.strip().lower()
                                 for t in fields_wanted.split(",") if t.strip()}]
        if want:
            rows = [{c: r.get(c) for c in want} for r in rows]
            notes["fields"] = f"showed {len(want)} of {len(fields)} columns"
        else:
            notes["fields"] = (f"none of '{fields_wanted}' are suggestion "
                               f"columns ({', '.join(fields)}); all shown")

    note = f"These are {this_label} ({svc}/{gr_set}). {pivot}"
    if dropped:
        note += (
            f" (Dropped the '{dropped}' filter — suggestion records have no "
            "open/closed flag; every suggestion is an open recommendation.)")
    if truncated:
        note += (
            f" (Sorted within the first {pool} suggestions fetched — more exist; "
            "narrow with a 'where' if you need a specific vendor/part/plant.)")

    return json.dumps({
        "summary": f"{len(rows)} {this_label.lower()} (sorted by {order_label}).",
        "stop_hint": "These rows answer the read — present them; do NOT re-run "
                     "with a different PO business object.",
        "row_count": len(rows),
        "resolved": {
            "service": svc, "entity_set": gr_set,
            "order": order_label,
            "filter": odata_filter or None,
            **({"assumptions": notes} if notes else {}),
        },
        "records": rows,
        "note": note,
    }, default=str)


def _looks_failed(raw: str) -> bool:
    """True if the engine returned a failure payload instead of records.

    ``run_odata`` swallows an OData 500 and retries GetRows internally, then
    returns an ``error``/apology envelope as a STRING rather than raising — so a
    plain ``except EpicorError`` misses it. Detect it so we retry GetRows via
    the correct DataSet-table name ourselves.
    """
    try:
        d = json.loads(raw)
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    if "error" in d and "records" not in d:
        return True
    txt = str(d.get("error") or d.get("message") or "")
    return "unexpected internal problem" in txt or "Neither OData nor GetRows" in txt

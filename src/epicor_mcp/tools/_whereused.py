"""Legacy part where-used inquiry engine; not registered on the OSS surface."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools._inline_schema import order_refusal, sort_records
from epicor_mcp.tools._partviews import _from_where, resolve_part
from epicor_mcp.tools._resolve import error_envelope

if TYPE_CHECKING:  # pragma: no cover
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

PART_SERVICE = "Erp.BO.PartSvc"

# One fetch page — the method pages via pageSize/absolutePage; a part used in
# more than this many distinct places is vanishingly rare in practice.
_PAGE_SIZE = 500

# Curated projection (PartNum here is the PARENT — the part being made).
_WHEREUSED_FIELDS = [
    "PartNum", "PartNumPartDescription", "RevisionNum", "AltMethod",
    "MtlSeq", "QtyPer", "TypeDesc",
]

# Phrases that ask for PARENTS of a part. "used in/on" is guarded against
# job/order-scoped asks ("materials used in job 123" is the downward BOM).
_WHEREUSED_RE = re.compile(
    r"where[\s\-_]?used"                                   # where used / WhereUsed
    r"|\bused\s+(?:to\s+(?:make|build|manufacture|produce)\b|in\b|on\b)"
    r"|\bgo(?:es)?\s+into\b"
    r"|\bwhat\s+uses\b|\bwhich\s+(?:parts?|assembl\w+)\s+use\b"
    r"|\bparents?\s+(?:of|for)\b",
    re.IGNORECASE)
# Downward-BOM look-alikes of the weak "used in/on" trigger: "materials used in
# job 123" asks for a job's CHILDREN. The explicit forms ("where used", "used to
# make", "goes into", "what uses") are never guarded.
_SCOPE_GUARD_RE = re.compile(
    r"\b(?:materials?|components?|parts?)\s+used\s+(?:in|on)\b"
    r"|\bused\s+(?:in|on)\s+(?:jobs?|orders?)\b",
    re.IGNORECASE)
_STRONG_RE = re.compile(
    r"where[\s\-_]?used|\bused\s+to\s+(?:make|build|manufacture|produce)\b"
    r"|\bgo(?:es)?\s+into\b|\bwhat\s+uses\b",
    re.IGNORECASE)

# Trigger words stripped before part-number extraction (resolve_part's own
# stop-list handles the rest).
_TRIGGER_STRIP_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._/\-])(?:where|used|uses|use|to|make|makes|build|builds|manufacture|"
    r"produce|goes|go|into|what|which|parents?|parent|on)(?![A-Za-z0-9._/\-])")


def detect_where_used(target: str, where: str = "") -> bool:
    """True when the phrase asks what a part is USED TO MAKE (its parents).

    Also fires when the *filter* says so: ``MtlPartNum = 'X'`` against a
    PartMtl-ish target is a where-used ask regardless of phrasing.
    """
    if not target:
        return False
    if (_from_where(where, "MtlPartNum") or _from_where(where, "Material")) \
            and re.search(r"partmtl|where[\s\-_]?used|\bbom\b", target,
                          re.IGNORECASE):
        return True
    m = _WHEREUSED_RE.search(target)
    if not m:
        return False
    if _STRONG_RE.search(target):
        return True
    # Only the weak "used in/on" / "parents of" match remains — reject when the
    # phrase is scoped to a job/assembly/order (that's the downward BOM read).
    return not _SCOPE_GUARD_RE.search(target)


def _resolve_wu_part(where: str, target: str) -> str | None:
    """The part whose parents are wanted: MtlPartNum wins, then PartNum/target."""
    return (
        _from_where(where, "MtlPartNum")
        or _from_where(where, "Material")      # the model's guess-spelling
        or resolve_part(where, _TRIGGER_STRIP_RE.sub(" ", target or ""))
    )


def _dedupe(rows: list[dict]) -> list[dict]:
    """Collapse the per-plant/alternate repeats the method returns."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in rows:
        key = (r.get("PartNum"), r.get("RevisionNum"),
               r.get("AltMethod"), r.get("MtlSeq"))
        if key in seen:
            continue
        seen.add(key)
        out.append({f: r.get(f) for f in _WHEREUSED_FIELDS if f in r})
    return out


async def where_used(
    client: "EpicorClient",
    rbac: "RBACEnforcer",
    session,
    *,
    target: str,
    where: str = "",
    limit: int = 25,
    order_by: str = "",
    fields: str = "",
    soft: dict | None = None,
) -> str:
    """Answer "what is part X used to make" in ONE ``GetPartWhereUsed`` call."""
    part = _resolve_wu_part(where, target)
    if not part:
        return json.dumps(error_envelope(
            "need_part",
            "Name the part whose where-used (parents) you want — re-call with "
            "where=\"PartNum = '<part>'\". With only a description, first find "
            "the part number via target=\"part\" and a Part lookup.",
        ))

    allowed, msg = rbac.check_access(session.user_id, PART_SERVICE)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, PART_SERVICE).api_key or ""

    try:
        resp = await client.post(
            f"{PART_SERVICE}/GetPartWhereUsed", api_key,
            json_body={"whereUsedPartNum": part,
                       "pageSize": _PAGE_SIZE, "absolutePage": 0})
    except EpicorError as exc:
        return json.dumps(error_envelope(
            "whereused_failed",
            f"Could not run the where-used inquiry for part '{part}': "
            f"{exc.message or exc}"))

    obj = resp.get("returnObj") if isinstance(resp, dict) else None
    obj = obj if isinstance(obj, dict) else {}
    raw_rows = [r for r in (obj.get("PartWhereUsed") or []) if isinstance(r, dict)]
    rows = _dedupe(raw_rows)
    parents = sorted({r.get("PartNum") for r in rows if r.get("PartNum")})

    if not rows:
        return json.dumps({
            "summary": (f"Part {part} has no where-used on record — no other "
                        "part's engineering BOM lists it as a material. That is "
                        "the complete answer."),
            "stop_hint": ("FINAL — do NOT hunt for usage in JobMtl/PartMtl/other "
                          "tables (JobMtl cannot be filtered by material part). "
                          "Tell the user nothing on file uses this part; it may "
                          "be a top-level or sold-direct part."),
            "terminal": True,
            "row_count": 0,
            "resolved": {"service": PART_SERVICE, "via": "GetPartWhereUsed",
                         "PartNum": part},
            "records": [],
        }, default=str)

    # Sort the FULL set BEFORE the trim, so "top 5 by PartNum desc" is a real
    # top-N rather than the first page re-sorted.
    rows, err_kind, valid_cols = sort_records(
        rows, order_by, available=list(_WHEREUSED_FIELDS))
    if err_kind:
        return json.dumps(order_refusal(err_kind, order_by, valid_cols))

    notes = dict(soft or {})
    if (order_by or "").strip():
        notes["order"] = f"{order_by} (client-side, over the full result)"
    if (fields or "").strip():
        want = [c for c in _WHEREUSED_FIELDS
                if c.lower() in {t.strip().lower()
                                 for t in fields.split(",") if t.strip()}]
        if want:
            rows = [{c: r.get(c) for c in want} for r in rows]
            notes["fields"] = f"showed {len(want)} of {len(_WHEREUSED_FIELDS)} columns"
        else:
            notes["fields"] = (
                f"none of '{fields}' are where-used columns "
                f"({', '.join(_WHEREUSED_FIELDS)}); all columns shown")

    shown = rows[: max(1, limit)]
    if len(rows) > len(shown):
        notes["limit_trim"] = (
            f"showed {len(shown)} of {len(rows)} where-used links (limit); "
            "raise `limit` for the rest.")
    if len(raw_rows) >= _PAGE_SIZE:
        # The fetch is a fixed single page — never imply completeness.
        notes["incomplete"] = (
            f"GetPartWhereUsed returned a full page ({_PAGE_SIZE} rows); more "
            "where-used links may exist beyond it.")

    return json.dumps({
        "summary": (f"Part {part} is used to make {len(parents)} part(s): "
                    + ", ".join(parents[:10])
                    + ("…" if len(parents) > 10 else "") + "."),
        "stop_hint": ("This IS the where-used answer — present it now; do NOT "
                      "re-query other tables for the same part."),
        "row_count": len(shown),
        "resolved": {"service": PART_SERVICE, "via": "GetPartWhereUsed",
                     "PartNum": part,
                     **({"assumptions": notes} if notes else {})},
        "note": ("Rows are engineering where-used links: PartNum is the PARENT "
                 "(what gets made), the queried part is its material. For the "
                 "parent's full recipe ask \"BOM for part <parent>\"."),
        "records": shown,
    }, default=str)

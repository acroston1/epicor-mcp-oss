"""Part-view reads folded into ``epicor_read`` (INV-2, no new tool surface).

Two part-centric questions that aren't plain table queries:

* **Time phase** — the time-phased supply/demand picture for a part. A *computed*
  dataset, not a table: ``Erp.BO.TimePhasSvc/GoProcessTimePhase`` builds it by
  ``partNum`` + ``plant`` and returns the ``TimePhas`` rows inside ``returnObj``.
  Served via the method-POST pattern already used by ``_read_contacts``.

* **BOM** — this legacy helper selects a part's most recent job and returns
  the JobMtl / JobOper structure. It is retained as an engine library, not a
  registered OSS tool; installations needing engineering BOMs should query
  their own PartMtl metadata through the read-only query surface.

Both keep the intent surface fixed: ``epicor_read`` recognizes the phrase,
resolves the part, routes internally, and returns the uniform read envelope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools._tenant import PLANTS, match_plant
from epicor_mcp.tools._engine import run_getrows
from epicor_mcp.tools._inline_schema import order_refusal, sort_records
from epicor_mcp.tools._resolve import error_envelope

if TYPE_CHECKING:
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

TIMEPHASE_SERVICE = "Erp.BO.TimePhasSvc"
JOB_SERVICE = "Erp.BO.JobEntrySvc"

# Curated default projections (the model rarely names fields for these).
_TIMEPHASE_FIELDS = [
    "PartNum", "RevisionNum", "Plant", "PartDescription", "DueDate",
    "RequirementFlag", "ReceiptQty", "RequiredQty", "BalanceQty",
    "SourceName", "JobNum", "OrderNum", "PONum", "ExceptionReason",
    "SugOrderDate", "LeadTime", "IUM",
]
# Job-method (BOM) projections — the material list and the routing. GetByID
# returns the full JobMtl/JobOper rows; we project to these for a clean envelope.
_JOBMTL_FIELDS = [
    "JobNum", "AssemblySeq", "MtlSeq", "PartNum", "Description", "QtyPer",
    "RequiredQty", "IUM", "RelatedOperation",
]
_JOBOPR_FIELDS = [
    "JobNum", "AssemblySeq", "OprSeq", "OpCode", "OpDesc", "ProdStandard",
    "RunQty", "QtyCompleted", "OpComplete", "PrimaryResourceGrpID",
]
_JOBHEAD_SELECT = ("JobNum,PartNum,RevisionNum,JobReleased,JobClosed,"
                   "JobComplete,ProdQty,StartDate,DueDate,Plant")

# Phrase → part-view kind. Checked as whole words so a stray "bomb" or a part
# number that merely contains "bom" never trips it. The second alternative in
# each catches the model's fallback of targeting the underlying table/service
# directly (e.g. "Erp.BO.TimePhasSvc/TimePhas", "PartDtl") — those otherwise
# resolve to a table with no OData/GetRows path and 500, sending the model into
# the exact thrash the intent route exists to prevent.
_TIMEPHASE_RE = re.compile(
    r"\btime[\s\-]?phase(?:d|s)?\b|TimePhas|PartDtl", re.IGNORECASE)
_BOM_RE = re.compile(
    r"\b(?:bom|boms|bill[\s\-]?of[\s\-]?materials?|"
    r"components?\s+of|components?\s+for|what(?:'s| is)?\s+in\s+the\s+bom|"
    r"method\s+of\s+manufacture|routing)\b",
    re.IGNORECASE)

# Words to strip out of a target phrase when hunting for the part reference.
_STOPWORDS_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._/\-])(time|phased?|timephase|bom|boms|bill|of|materials?|components?|"
    r"component|supply|demand|for|the|part|parts|show|me|all|list|whats?|is|"
    r"in|goes?|into|against|run|lookup|look|up|a|an|number|no|num|rev|"
    r"revision|plant|site|job|method|s)(?![A-Za-z0-9._/\-])")

_PART_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]*")


def detect_part_view(target: str) -> str | None:
    """Return ``"timephase"`` / ``"bom"`` if *target* asks for one, else None.

    Time phase wins when both match ("time-phased BOM" is nonsensical but the
    time-phase intent is the more specific request).
    """
    if not target:
        return None
    if _TIMEPHASE_RE.search(target):
        return "timephase"
    if _BOM_RE.search(target):
        return "bom"
    return None


def _from_where(where: str, key: str) -> str | None:
    """Pull ``<key> eq '...'`` (or ``=`` / ``like``, quoted or bare) from *where*."""
    if not where:
        return None
    m = re.search(
        rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])\s*(?:=|eq|like)\s*"
        r"('[^']*'|[\w./\-]+)",
        where, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip("'").strip("%").strip() or None


def _part_from_target(target: str) -> str | None:
    """Best-effort part number left in the target phrase after stopword strip.

    "time phase for 6205-2RS" → "6205-2RS". Prefers a token that looks like a
    part number (contains a digit or a dash) when several remain.
    """
    residual = _STOPWORDS_RE.sub(" ", target or "")
    tokens = _PART_TOKEN_RE.findall(residual)
    if not tokens:
        return None
    partish = [t for t in tokens if re.search(r"[0-9\-]", t)]
    pick = (partish or tokens)
    # Longest wins — part numbers are usually the longest surviving token.
    return max(pick, key=len)


def resolve_part(where: str, target: str) -> str | None:
    """Resolve the part number from ``where`` (PartNum/MtlPartNum) or *target*."""
    return (
        _from_where(where, "PartNum")
        or _from_where(where, "MtlPartNum")
        or _part_from_target(target))


def _resolve_plant(where: str, target: str) -> str:
    """Plant code from ``where`` (Plant eq 'X') or a site name in *target*; else ''."""
    return _from_where(where, "Plant") or match_plant(target) or ""


def _project(rows: list[dict], fields: list[str]) -> list[dict]:
    """Keep only *fields* (case-insensitively), preserving field order."""
    wl = {f.lower(): f for f in fields}
    out: list[dict] = []
    for r in rows:
        row = {wl[k.lower()]: v for k, v in r.items() if k.lower() in wl}
        out.append({f: row[f] for f in fields if f in row})
    return out


def _esc(v: str) -> str:
    return v.replace("'", "''")


# --------------------------------------------------------------------------- #
# Time phase (method-POST path)
# --------------------------------------------------------------------------- #

async def read_timephase(
    client: "EpicorClient",
    rbac: "RBACEnforcer",
    session,
    *,
    where: str,
    target: str,
    fields: str,
    limit: int,
    order_by: str = "",
    soft: dict | None = None,
) -> str:
    """``epicor_read`` entry point: resolve part+plant from where/target, then
    delegate to :func:`timephase_for_part` (shared with the dedicated
    ``epicor_time_phase`` tool)."""
    part = resolve_part(where, target)
    if not part:
        return json.dumps(error_envelope(
            "need_part",
            "Name the part whose time phase you want — re-call with "
            "where=\"PartNum = '<part>'\" (add \"and Plant = '<code>'\" to "
            "scope one site).",
        ))
    return await timephase_for_part(
        client, rbac, session,
        part=part, plant=_resolve_plant(where, target),
        fields=fields, limit=limit, order_by=order_by, soft=soft)


async def timephase_for_part(
    client: "EpicorClient",
    rbac: "RBACEnforcer",
    session,
    *,
    part: str,
    plant: str = "",
    fields: str = "",
    limit: int = 25,
    order_by: str = "",
    soft: dict | None = None,
) -> str:
    """Compute a part's time phase via ``GoProcessTimePhase`` (method-POST).

    ``GoProcessTimePhase`` is **per-plant** and returns ZERO rows for an empty
    plant (models rarely pass a plant, so every call would come back
    "no supply/demand" and the model would thrash). So when *plant* is blank, run the
    process for every configured plant concurrently and merge the non-empty
    results — the ``TimePhas`` rows carry their own ``Plant``.
    """
    allowed, msg = rbac.check_access(session.user_id, TIMEPHASE_SERVICE)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, TIMEPHASE_SERVICE).api_key or ""

    async def _call(pl: str):
        """(rows, access_denied, other_error) for one plant."""
        try:
            resp = await client.post(
                f"{TIMEPHASE_SERVICE}/GoProcessTimePhase", api_key,
                json_body={"partNum": part, "attributeSetID": 0, "plant": pl,
                           "whatIfTog": False, "tFSug": False,
                           "plnCtInfo": False, "contractID": ""})
        except EpicorError as exc:
            m = (exc.message or "")
            denied = ("access denied" in m.lower()
                      or getattr(exc, "status_code", None) == 401)
            return [], denied, (None if denied else m)
        ds = resp.get("returnObj") if isinstance(resp, dict) else None
        return ([r for r in ((ds or {}).get("TimePhas") or []) if isinstance(r, dict)],
                False, None)

    plants = [plant] if plant else list(PLANTS)
    if not plants:
        return json.dumps(error_envelope("plant_required", "Specify a Plant code or configure EPICOR_MCP_PLANTS before requesting time phase."))
    results = await asyncio.gather(*[_call(p) for p in plants])
    rows: list[dict] = []
    plants_with: list[str] = []
    denied_any = False
    other_err = None
    for pl, (pr, denied, err) in zip(plants, results):
        denied_any = denied_any or denied
        other_err = other_err or err
        if pr:
            plants_with.append(pl)
            rows.extend(pr)

    if not rows and denied_any:
        # GoProcessTimePhase must be in the MCP Access Scope; a denial means
        # it is missing from the scope this deployment uses.
        return json.dumps(error_envelope(
            "access_denied",
            f"Epicor's Access Scope blocked the time-phase inquiry for part "
            f"'{part}'. GoProcessTimePhase must be in the MCP key's Access Scope "
            "allowed methods (it is not a Get* method, so a read-only scope "
            "excludes it by default). This is an Access Scope config, not a "
            "query problem — do NOT retry."))
    if not rows and other_err:
        return json.dumps(error_envelope(
            "timephase_failed",
            f"Could not build the time phase for part '{part}': {other_err}"))

    # The six-plant gather EXTENDS a flat list per plant, so the merged set had
    # no coherent order at all — it was plant-grouped-then-arbitrary. Sort the
    # MERGED rows (caller clause, else date ascending) BEFORE the limit trim,
    # or a "soonest first" ask silently returns plant 10's tail.
    notes = dict(soft or {})
    avail = sorted({k for r in rows for k in r}) or list(_TIMEPHASE_FIELDS)
    if (order_by or "").strip():
        rows, err_kind, valid_cols = sort_records(rows, order_by, available=avail)
        if err_kind:
            return json.dumps(order_refusal(err_kind, order_by, valid_cols))
        notes["order"] = f"{order_by} (caller, over the merged plants)"
    else:
        date_col = next((c for c in _TIMEPHASE_FIELDS
                         if "date" in c.lower() and c in avail), "")
        if date_col:
            rows, _k, _v = sort_records(rows, f"{date_col} asc", available=avail)
            notes["order"] = f"{date_col} asc (default, over the merged plants)"

    want = [f.strip() for f in (fields or "").split(",") if f.strip()] or _TIMEPHASE_FIELDS
    shown = rows[: max(1, limit)]
    projected = _project(shown, want)
    if len(rows) > len(shown):
        # row_count used to report the FULL merged count beside a trimmed
        # `records` — 200 claimed, 25 shipped, unannounced.
        notes["limit_trim"] = (
            f"showed {len(shown)} of {len(rows)} time-phase rows (limit); "
            "raise `limit` for the rest.")

    if plant:
        scope = f"part {part}, plant {plant}"
    elif plants_with:
        scope = (f"part {part}, plant(s) "
                 + ", ".join(f"{p} ({PLANTS.get(p, p)})" for p in plants_with))
    else:
        scope = f"part {part}, all plants"
    return json.dumps({
        "summary": (f"Time phase for {scope}: {len(rows)} supply/demand row(s)."
                    if rows else
                    f"Time phase for {scope}: no supply or demand on record — "
                    "that is the complete answer."),
        "stop_hint": ("This is the part's time-phased picture — present it now; "
                      "do NOT re-run epicor_read for the same part."),
        "row_count": len(projected),
        "total_rows": len(rows),
        "resolved": {"service": TIMEPHASE_SERVICE, "entity_set": "TimePhas",
                     "via": "GoProcessTimePhase", "PartNum": part,
                     "Plant": plant or (",".join(plants_with) or "(all)"),
                     **({"assumptions": notes} if notes else {})},
        "records": projected,
    }, default=str)


# --------------------------------------------------------------------------- #
# BOM = job method (JobMtl + JobOper on JobEntrySvc)
# --------------------------------------------------------------------------- #

_MAX_JOB_PROBES = 6


def _rank_jobs(jobs: list[dict]) -> list[dict]:
    """Order jobs by how likely they carry a real, loaded method.

    Completed jobs that actually ran (released + closed) come first — their
    method is the one used to build the part. Then released-but-open
    jobs, then the rest (unreleased MRP/planning jobs, whose ``JobMtl`` is
    typically empty and whose future ``StartDate`` would otherwise win a naive
    'newest' sort). Within each tier, newest ``StartDate`` first (JobNum tie).
    """
    def _newest(pool):
        return sorted(pool, key=lambda j: (str(j.get("StartDate") or ""),
                                           str(j.get("JobNum") or "")),
                      reverse=True)

    ran = [j for j in jobs if j.get("JobReleased") and j.get("JobClosed")]
    rel_open = [j for j in jobs if j.get("JobReleased") and not j.get("JobClosed")]
    rest = [j for j in jobs if not j.get("JobReleased")]
    return _newest(ran) + _newest(rel_open) + _newest(rest)


def _order_hits(order_by: str, cols: list[str]) -> bool:
    """True when EVERY plain column named in *order_by* exists in *cols*."""
    from epicor_mcp.tools._inline_schema import parse_order_by
    terms, kind = parse_order_by(order_by or "")
    if kind or not terms:
        return False
    low = {c.lower() for c in cols}
    return all(c.split(".")[-1].lower() in low for c, _d in terms)


async def read_bom(
    client: "EpicorClient",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    session,
    *,
    where: str,
    target: str,
    fields: str,
    limit: int,
    order_by: str = "",
    soft: dict | None = None,
) -> str:
    """Serve a part's BOM as its job method (materials + operations).

    Some installations keep methods on jobs rather than engineering BOMs, so this reads
    the part's most recent job's ``JobMtl`` (materials) and ``JobOper``
    (operations). An explicit ``JobNum`` in ``where`` pins one job.
    """
    job = _from_where(where, "JobNum")
    part = resolve_part(where, target)
    if not job and not part:
        return json.dumps(error_envelope(
            "need_part",
            "Name the part (or job) whose BOM you want — re-call with "
            "where=\"PartNum = '<part>'\", or where=\"JobNum = '<job>'\" for a "
            "specific job.",
        ))

    allowed, msg = rbac.check_access(session.user_id, JOB_SERVICE)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, JOB_SERVICE).api_key or ""

    # --- Pull a job's method via GetByID -------------------------------------
    # The child OData collections (JobMtls/JobOpers) are unreliable — they come
    # back EMPTY for jobs whose method GetByID returns in full (verified: job
    # N261215 → 0 via collections, 9 JobMtl + 5 JobOper via GetByID). Same trap
    # as vendor/customer contacts, so use the parent GetByID dataset here too.
    async def _job_method(jobnum: str) -> tuple[list[dict], list[dict]]:
        try:
            resp = await client.post(
                f"{JOB_SERVICE}/GetByID", api_key, json_body={"jobNum": jobnum})
        except EpicorError:
            return [], []
        obj = resp.get("returnObj") if isinstance(resp, dict) else None
        obj = obj if isinstance(obj, dict) else {}
        mats = [r for r in (obj.get("JobMtl") or []) if isinstance(r, dict)]
        ops = [r for r in (obj.get("JobOper") or []) if isinstance(r, dict)]
        return _project(mats, _JOBMTL_FIELDS), _project(ops, _JOBOPR_FIELDS)

    job_meta: dict = {}
    materials: list[dict] = []
    operations: list[dict] = []
    # --- Resolve the job from the part when not given explicitly -------------
    if not job:
        try:
            raw = await run_getrows(
                client, index, JOB_SERVICE, "JobHead", api_key,
                filter=f"PartNum eq '{_esc(part)}'", select=_JOBHEAD_SELECT,
                orderby="StartDate desc", top=50, skip=0, count_only=False,
                group_by="", aggregate="", distinct="", format="json",
            )
            payload = json.loads(raw)
        except (EpicorError, ValueError) as exc:
            return json.dumps(error_envelope(
                "bom_failed",
                f"Could not look up jobs for part '{part}': "
                f"{getattr(exc, 'message', exc)}"))
        jobs = payload.get("records") if isinstance(payload, dict) else None
        jobs = [j for j in (jobs or []) if isinstance(j, dict)]
        if not jobs:
            return json.dumps({
                "summary": f"No job method found for part '{part}'. Engineering BOMs may still exist.",
                "stop_hint": "Check the part's engineering method if the user needs a part-level BOM.",
                "terminal": False,
                "row_count": 0,
                "resolved": {"service": JOB_SERVICE, "PartNum": part},
                "records": [],
            }, default=str)
        # Newest jobs are often MRP/planning or future releases with an EMPTY
        # method — probe the ranked candidates until one actually has a method.
        candidates = _rank_jobs(jobs)[:_MAX_JOB_PROBES]
        chosen = candidates[0]
        for cand in candidates:
            mats, ops = await _job_method(cand.get("JobNum"))
            if mats or ops:
                chosen, materials, operations = cand, mats, ops
                break
        job = chosen.get("JobNum")
        job_meta = {k: chosen.get(k) for k in (
            "JobNum", "PartNum", "RevisionNum", "JobReleased", "JobClosed",
            "ProdQty", "StartDate") if chosen.get(k) not in (None, "")}
    else:
        materials, operations = await _job_method(job)

    materials.sort(key=lambda r: (r.get("AssemblySeq") or 0, r.get("MtlSeq") or 0))
    operations.sort(key=lambda r: (r.get("AssemblySeq") or 0, r.get("OprSeq") or 0))
    notes = dict(soft or {})
    # TWO result sets with independent sorts, so one `order_by` is inherently
    # ambiguous: resolve against materials first, then operations, and SAY
    # which one was sorted. Present on neither -> INV-1 with BOTH lists.
    if (order_by or "").strip():
        which = ""
        reason = ""
        if _order_hits(order_by, _JOBMTL_FIELDS):
            materials, k, v = sort_records(
                materials, order_by, available=list(_JOBMTL_FIELDS))
            which = "materials"
        elif _order_hits(order_by, _JOBOPR_FIELDS):
            operations, k, v = sort_records(
                operations, order_by, available=list(_JOBOPR_FIELDS))
            which = "operations"
        else:
            # An expression must give the SAME refusal here as on the main
            # read path — one bad clause, one message (INV-1 uniformity).
            _m, k, v = sort_records(
                [], order_by,
                available=sorted(set(_JOBMTL_FIELDS) | set(_JOBOPR_FIELDS)))
            reason = ""
            if not k:
                # Every term IS a real column, just split across the two
                # independent result sets. Forcing "unknown_column" here made
                # the message contradict its own `valid` block and named no
                # supported path.
                k, reason = "order_not_applicable", (
                    f"order_by='{order_by}' mixes materials columns with "
                    "operations columns. A job method is TWO independent "
                    "result sets, so one order_by can only sort one of them. "
                    "Re-run with an order_by drawn entirely from "
                    "materials_columns OR entirely from operations_columns.")
        if k:
            env = order_refusal(k, order_by, v, reason=reason)
            env["valid"] = {"materials_columns": list(_JOBMTL_FIELDS),
                            "operations_columns": list(_JOBOPR_FIELDS)}
            return json.dumps(env)
        notes["order"] = f"{order_by} ({which})"
    if fields.strip():
        want = [f.strip() for f in fields.split(",") if f.strip()]
        materials = _project(materials, want)
        # `fields` has only ever projected MATERIALS. Half-applying an argument
        # unannounced is the same defect class as dropping it.
        notes["fields"] = (
            f"`fields` was applied to materials only; operations keep their "
            f"full projection ({', '.join(_JOBOPR_FIELDS)}).")
    mtl_total = len(materials)
    if limit and mtl_total > max(1, limit):
        # `limit` was accepted and never referenced — the BOM always came back
        # whole. A partial BOM reads as the FULL recipe, so trimming it must be
        # loud or not done at all. Capture the TRUE total before the slice:
        # reading len() after it reported "showed 100 of the materials" with
        # no denominator, and summary/row_count then repeated the trimmed
        # count as if it were the recipe size.
        materials = materials[: max(1, limit)]
        notes["limit_trim"] = (
            f"showed {len(materials)} of the job's {mtl_total} materials "
            f"(limit={limit}) — this is a PARTIAL bill of materials, not the "
            "whole recipe. Raise `limit` or omit it for the complete method.")

    part_label = part or job_meta.get("PartNum") or "(job)"
    out = {
        "summary": (f"BOM for part {part_label} from job {job}: "
                    + (f"{len(materials)} of {mtl_total} material(s) (PARTIAL — "
                       f"trimmed by limit={limit})"
                       if len(materials) != mtl_total else
                       f"{mtl_total} material(s)")
                    + f", {len(operations)} operation(s)."
                    if (materials or operations) else
                    f"Job {job} has no materials or operations on record."),
        "stop_hint": ("This is the part's BOM (job method) — present the "
                      "materials and operations now; do NOT re-run epicor_read "
                      "for the same part/job."),
        "row_count": len(materials),
        # The job method's TRUE material count, so a trimmed BOM can never be
        # mistaken for the whole recipe (mirrors timephase_for_part).
        "total_rows": mtl_total,
        "resolved": {"service": JOB_SERVICE, "via": "job method (JobMtl/JobOper)",
                     "PartNum": part_label, "JobNum": job,
                     **({"assumptions": notes} if notes else {})},
        "note": ("This result is a job method, not a part-level engineering "
                 "BOM. It is the method of the part's most recent job "
                 f"with a loaded method ({job}); other jobs for this part may "
                 "differ. Pass where=\"JobNum='..'\" for a specific job."),
        "job": job_meta or {"JobNum": job},
        "materials": materials,
        "operations": operations,
    }
    return json.dumps(out, default=str)

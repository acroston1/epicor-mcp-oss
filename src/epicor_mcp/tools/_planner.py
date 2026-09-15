"""Planner-aware job reads — resolve a planner name/code to ``JobHead.PersonID``.

The job **planner** is ``JobHead.PersonID`` (a code like ``"Plan16"``),
with the resolved name in ``JobHead.PersonIDName``. It is **NOT** ``PlanUserID``
(the DCD login that last ran planning — usually blank), and it is not the
planner's full name. The planner codes and their (informal) names live in the
**Person master** (``Erp.BO.PersonSvc/People`` — the "Person" screen):
``Plan2="Jamie R"``, ``Plan6="Alex R"``, ``Plan16="Sam Taylor"``, … Names are
stored casually (first name + last initial), so a plain
``PersonIDName like '%Reed%'`` finds nothing even though ``Plan2`` *is*
Jamie Reed.

There is no ``Planner`` / ``PlannerID`` / ``SchedulerID`` column and
``PlanUserID`` is usually empty, so a question such as "what jobs is planner
Jamie Reed on" needs the bridge "Jamie Reed" → the informal "Jamie R" → the
``Plan2`` code. This recognizer does the whole bridge in ONE ``epicor_read`` call
(collapse, don't chain): name → planner code (fuzzy, via the Person master) →
``JobHead`` filtered by ``PersonID``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.tools._engine import run_getrows, run_odata
from epicor_mcp.tools._inline_schema import (
    order_refusal, order_terms_to_clause, parse_order_by, sort_records,
)
from epicor_mcp.tools._resolve import error_envelope

if TYPE_CHECKING:  # pragma: no cover
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_PERSON_SVC = "Erp.BO.PersonSvc"
_PERSON_SET = "People"
_JOB_SVC = "Erp.BO.JobEntrySvc"
_JOB_SET = "JobHead"

# Planner-assignment fields on a job. PersonID is the code; PersonIDName the name.
_JOB_FIELDS = (
    "JobNum,PartNum,PartDescription,ProdQty,PersonID,PersonIDName,"
    "JobReleased,JobClosed,JobComplete,StartDate,DueDate,ReqDueDate,Plant"
)

# "planner" (as a role word) near a job word — the trigger for a planner→jobs read.
_PLANNER_JOBS_RE = re.compile(
    r"\bplanner\b(?=.*\bjobs?\b)|\bjobs?\b(?=.*\bplanner\b)"
    r"|\bjobs?\s+planned\s+by\b|\bplanned\s+by\b",
    re.IGNORECASE | re.DOTALL,
)
# Roster: "list/show planners", "who are the planners", "who is planner Plan16".
_ROSTER_RE = re.compile(
    r"\b(?:list|show|all|which|who\s+are|who\s+is)\b[^.]*\bplanners?\b"
    r"|\bplanner\s+list\b|\bplanners?\s+roster\b",
    re.IGNORECASE,
)
# A planner CODE: "Plan16", "plan #17", "planner 17". The (?<!ner) guard keeps the
# word "planner" itself from reading as code 0.
_PLAN_CODE_RE = re.compile(r"\bplan\s*#?\s*0*(\d{1,3})\b", re.IGNORECASE)

# Words to strip when pulling a planner name out of the phrase.
_STOP = re.compile(
    r"(?i)\b(?:open|closed|complete|completed|active|current|released|firm|all|"
    r"the|jobs?|job|for|assigned|to|by|planner|planners|planned|who|is|are|on|"
    r"of|show|me|list|what|which|does|do|have|has|with|and|s)\b")


def detect_planner_jobs(target: str) -> bool:
    """True when the phrase asks for JOBS by planner (not the roster)."""
    if not target:
        return False
    if _ROSTER_RE.search(target) and "job" not in target.lower():
        return False
    return bool(_PLANNER_JOBS_RE.search(target))


def detect_planner_roster(target: str) -> bool:
    """True for "list the planners" / "who is planner Plan16" (no job word)."""
    if not target or "job" in target.lower():
        return False
    return bool(_ROSTER_RE.search(target))


def _open_filter(target: str) -> str:
    """Translate an open/closed hint into a JobHead filter fragment ('' = any)."""
    t = (target or "").lower()
    if re.search(r"\b(closed|completed|finished)\b", t):
        return "JobClosed eq true"
    if re.search(r"\b(open|active|current|released|in\s+process|wip)\b", t):
        return "JobClosed eq false"
    return ""


def _extract_planner_term(target: str) -> tuple[str, str]:
    """Return ``(code, name)`` pulled from the phrase.

    ``code`` is a bare planner code ("Plan2") if one was named; ``name`` is the
    residual free-text name ("jamie reed") after stripping stop-words.
    Exactly one is normally non-empty.
    """
    code = ""
    m = _PLAN_CODE_RE.search(target or "")
    if m:
        code = f"Plan{int(m.group(1))}"
    residual = _PLAN_CODE_RE.sub(" ", target or "")
    residual = _STOP.sub(" ", residual)
    residual = re.sub(r"[^A-Za-z0-9.\-' ]", " ", residual)
    name = re.sub(r"\s+", " ", residual).strip(" '-")
    return code, name


def _match_by_name(term: str, people: list[dict]) -> list[dict]:
    """Fuzzy-match a free-text planner name against the Person master.

    Handles the casual naming ("Jamie Reed" → "Jamie R"): exact name,
    then first-name + last-initial, then first-name only, then any-token contains.
    """
    t = " ".join(term.lower().split())
    if not t:
        return []
    toks = t.split()

    def name_of(p: dict) -> str:
        return " ".join((p.get("Name") or "").lower().split())

    exact = [p for p in people if name_of(p) == t]
    if exact:
        return exact
    if len(toks) >= 2:
        first, last_initial = toks[0], toks[-1][0]
        fi = [
            p for p in people
            if (nm := name_of(p).split())
            and nm[0] == first and len(nm) >= 2 and nm[1][:1] == last_initial
        ]
        if fi:
            return fi
    first_only = [p for p in people if name_of(p).split()[:1] == [toks[0]]]
    if first_only:
        return first_only
    return [p for p in people if any(tok in name_of(p) for tok in toks if len(tok) > 2)]


async def _load_planners(client, api_key: str) -> list[dict]:
    """Every planner code from the Person master (``PersonID`` like ``Plan…``).

    Uses OData (PersonSvc is not a HEAVY service; its GetRows returns nothing
    here), sorted so the roster reads Plan2, Plan6, … Plan10, not Plan10,
    Plan2.
    """
    raw = await run_odata(
        client, _PERSON_SVC, _PERSON_SET, api_key,
        filter="startswith(PersonID,'Plan')",
        select="PersonID,Name,EMailAddress,InActive",
        orderby="PersonID", top=200, skip=0, expand="", count_only=False,
        format="json")
    people = (json.loads(raw) or {}).get("records") or []
    # Natural sort by the numeric suffix (Plan2 < Plan6 < … < Plan10).
    def keyf(p: dict) -> int:
        m = re.search(r"(\d+)$", p.get("PersonID") or "")
        return int(m.group(1)) if m else 9999
    return sorted(people, key=keyf)


def _roster_view(people: list[dict], *, active_only: bool = True) -> list[dict]:
    return [
        {"planner_code": p.get("PersonID"), "name": p.get("Name"),
         "email": p.get("EMailAddress") or None}
        for p in people
        if not (active_only and p.get("InActive"))
    ]


async def planner_roster(client, index, rbac, session, *, target: str,
                         order_by: str = "", soft: dict | None = None):
    """Answer "list the planners" / "who is planner Plan16" in one call."""
    allowed, msg = rbac.check_access(session.user_id, _PERSON_SVC)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, _PERSON_SVC).api_key or ""
    people = await _load_planners(client, api_key)

    code, _ = _extract_planner_term(target)
    if code:
        one = [p for p in people if (p.get("PersonID") or "").lower() == code.lower()]
        if not one:
            return json.dumps(error_envelope(
                "planner_not_found",
                f"No planner code {code} in the Person master.",
                valid={"planners": _roster_view(people)}))
        p = one[0]
        return json.dumps({
            "summary": f"Planner {p['PersonID']} is {p.get('Name')}.",
            "planner": {"planner_code": p.get("PersonID"), "name": p.get("Name"),
                        "email": p.get("EMailAddress") or None,
                        "inactive": bool(p.get("InActive"))},
            "note": "Filter jobs for this planner with "
                    f"where=\"PersonID = '{p['PersonID']}'\" on JobHead.",
            "stop_hint": "This answers the question — do not re-query.",
        })

    active = _roster_view(people, active_only=True)
    notes = dict(soft or {})
    if (order_by or "").strip():
        active, err_kind, valid_cols = sort_records(
            active, order_by, available=["planner_code", "name", "email"])
        if err_kind:
            return json.dumps(order_refusal(err_kind, order_by, valid_cols))
        notes["order"] = f"{order_by} (client-side over the full roster)"
    return json.dumps({
        "summary": f"{len(active)} active planner(s) in the Person master.",
        **({"assumptions": notes} if notes else {}),
        "planners": active,
        "note": "Planner assignment on a job is JobHead.PersonID (the code); "
                "PersonIDName is the name. To see a planner's jobs, ask "
                "\"jobs for planner <name>\".",
        "stop_hint": "This roster answers the question — do not re-query.",
    }, default=str)


async def planner_jobs(client, index, rbac, session, *, target: str,
                       where: str = "", limit: int = 25,
                       order_by: str = "", soft: dict | None = None):
    """Resolve a planner (name or code) → their jobs, in ONE call.

    Bridges the casual Person-master naming to the ``Plan##`` code, then filters
    ``JobHead`` by ``PersonID``. Returns an INV-1 envelope (with the planner
    roster) when the name matches zero or several planners.
    """
    # Access for the Person-master lookup and the JobHead read.
    allowed, msg = rbac.check_access(session.user_id, _PERSON_SVC)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    person_key = rbac.check_service_access(session.user_id, _PERSON_SVC).api_key or ""

    code, name = _extract_planner_term(target)
    # A code may also arrive pre-resolved in `where` (PersonID = 'Plan2').
    if not code:
        wm = re.search(r"personid\s*(?:=|eq|like)\s*'?\s*(plan\d{1,3})", where or "",
                       re.IGNORECASE)
        if wm:
            code = wm.group(1).capitalize()

    people = await _load_planners(client, person_key)

    if code:
        matches = [p for p in people
                   if (p.get("PersonID") or "").lower() == code.lower()]
        if not matches:
            return json.dumps(error_envelope(
                "planner_not_found",
                f"No planner code {code} exists.",
                valid={"planners": _roster_view(people)}))
    elif name:
        matches = _match_by_name(name, people)
    else:
        return json.dumps(error_envelope(
            "need_planner",
            "Name the planner (name or Plan-code) whose jobs you want.",
            valid={"planners": _roster_view(people)}))

    if not matches:
        return json.dumps(error_envelope(
            "planner_not_found",
            f"No planner matched '{name}'. Planner names are stored casually "
            "(first name + last initial, e.g. 'Jamie R'); pick the code below.",
            valid={"planners": _roster_view(people)}))
    if len(matches) > 1:
        picks = [{"planner_code": p.get("PersonID"), "name": p.get("Name"),
                  "where": f"PersonID = '{p.get('PersonID')}'"} for p in matches[:12]]
        return json.dumps(error_envelope(
            "planner_ambiguous",
            f"{len(matches)} planners match '{name or code}'. Re-call epicor_read "
            "with the `where` for the one you want.",
            valid={"matches": picks},
            retry_with={"target": "JobHead", "where": picks[0]["where"]}))

    planner = matches[0]
    pcode, pname = planner.get("PersonID"), planner.get("Name")

    job_allowed, job_msg = rbac.check_access(session.user_id, _JOB_SVC)
    if not job_allowed:
        return json.dumps(error_envelope("access_denied", job_msg))
    job_key = rbac.check_service_access(session.user_id, _JOB_SVC).api_key or ""

    # Caller ordering is PUSHED SERVER-SIDE, never post-sorted: the fetch is
    # already trimmed to `top=limit`, so sorting the returned page would rank
    # only the DueDate-earliest `limit` rows and present it as a global top-N —
    # a new silent wrong answer. Validate against _JOB_FIELDS BEFORE the HTTP
    # call so a bad key never becomes a GetRows 500.
    notes = dict(soft or {})
    orderby = "DueDate"
    job_cols = _JOB_FIELDS.split(",")
    if (order_by or "").strip():
        terms, kind = parse_order_by(order_by)
        if kind or not terms:
            return json.dumps(order_refusal("expression", order_by, job_cols))
        low = {c.lower(): c for c in job_cols}
        if any(c.split(".")[-1].lower() not in low for c, _d in terms):
            return json.dumps(order_refusal(
                "unknown_column", order_by, sorted(job_cols)))
        orderby = order_terms_to_clause(
            [(low[c.split(".")[-1].lower()], d) for c, d in terms])
        notes["order"] = f"{orderby} (server-side)"

    filt = f"PersonID eq '{pcode}'"
    open_frag = _open_filter(target)
    if open_frag:
        filt = f"{filt} and {open_frag}"

    raw = await run_getrows(
        client, index, _JOB_SVC, _JOB_SET, job_key,
        filter=filt, select=_JOB_FIELDS, orderby=orderby,
        top=max(1, limit), skip=0, count_only=False,
        group_by="", aggregate="", distinct="", format="json")
    parsed = json.loads(raw) or {}
    rows = parsed.get("records") or []

    scope = ("open " if open_frag == "JobClosed eq false"
             else "closed " if open_frag == "JobClosed eq true" else "")
    return json.dumps({
        "summary": f"{len(rows)} {scope}job(s) for planner {pcode} ({pname}).",
        "resolved": {"planner_code": pcode, "planner_name": pname,
                     "service": _JOB_SVC, "entity_set": _JOB_SET,
                     "filter": filt, "order": orderby,
                     **({"assumptions": notes} if notes else {})},
        "row_count": len(rows),
        "records": rows,
        "note": f"Planner {pname} = code {pcode}; jobs filtered on "
                f"JobHead.PersonID (NOT PlanUserID).",
        "stop_hint": "These rows answer the read — present them; do not re-query "
                     "with new planner filters unless the user asks.",
    }, default=str)

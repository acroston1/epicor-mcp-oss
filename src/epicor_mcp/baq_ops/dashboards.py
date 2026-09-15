"""Resolve a dashboard name to saved BAQ ids without executing those BAQs.

Ids are handed to ``epicor_query(saved_baq=...)``, which reads each definition
and enforces the denylist before execution. Keeping one row-producing path
preserves the response format, paging policy and runtime-budget accounting.

Resolution uses exact ids/descriptions first, then an unambiguous LIKE match,
then scored candidates. A multi-row LIKE result must not select the first row:
ambiguous input remains ambiguous. Single quotes are escaped and LIKE wildcard
characters neutralized before composing a server-side filter.

Both definition and attached-BAQ lookups report truncation. A full page cannot
establish a complete list, and an incomplete corpus cannot establish a terminal
not-found result. Results are bounded by ``max_list`` and a byte limit.

``base_url`` and ``api_key`` are explicit dependencies; no deployment defaults
are inferred here. Registration uses the actual query-tool availability when
building next-call guidance, so it never recommends an unavailable tool.
See ``tests/test_dashboards_tool.py`` for deterministic lookup coverage.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from epicor_mcp.sql.envelope import error_envelope

logger = logging.getLogger(__name__)

__all__ = ["register_dashboards_tool", "dashboards_description"]

_DASHBOARD_SERVICE = "Ice.BO.DashBoardSvc"

#: The corpus fetch for the client-side scoring pass and for list mode.
_CORPUS_PAGE_SIZE = 500
#: The attached-BAQ page. A full page is never reported as the whole answer.
_BAQ_PAGE_SIZE = 50
#: How many dashboards a list response may name.
_MAX_DASHBOARD_LIST = 60
#: How many near-misses a miss may offer.
_MAX_CANDIDATES = 8

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

# Business-speak -> the abbreviation Epicor definitions actually use.
_TOKEN_SYNONYMS = {
    "receivables": "ar", "receivable": "ar",
    "payables": "ap", "payable": "ap",
}


# --------------------------------------------------------------------------- #
# pure helpers — the ported ladder
# --------------------------------------------------------------------------- #


def _squash(text: str) -> str:
    """Lowercase and drop every non-alphanumeric character."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _name_tokens(name: str) -> list[str]:
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", str(name or ""))
            if t and t.lower() not in _NOISE_WORDS]


def _is_list_request(name: str) -> bool:
    """True when *name* names no specific dashboard (blank or all-generic)."""
    if not name or not str(name).strip():
        return True
    meaningful = [
        t.lower() for t in re.split(r"[^A-Za-z0-9]+", str(name))
        if t and t.lower() not in _NOISE_WORDS
        and t.lower() not in _LIST_TRIGGERS
    ]
    return not meaningful


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
    """Best match for one query word (any of its variants) in a candidate.

    The ``len(a) <= 2`` branch is load-bearing, not a micro-optimisation:
    unanchored, ``ar`` matches ``Part``, ``Margin`` and ``Warehouse``, so every
    "AR ..." question would score against a third of the corpus and the
    exactly-one-full-match rule would never fire.
    """
    best = 0.0
    for a in alts:
        if len(a) <= 2:
            if sid.startswith(a) or sdesc.startswith(a):
                return 1.0
            continue
        if a in hay:
            return 1.0
        if len(a) > 4 and a[:4] in hay:
            best = max(best, 0.5)
    return best


def _quote(value: str) -> str:
    """A single-quoted SQL literal with the quotes doubled.

    "Buyer's Dashboard" is a legitimate dashboard name. Unescaped it breaks the
    ``whereClause`` — and the same seam lets a caller push an arbitrary clause
    fragment into a BO call, which is a where-clause injection into Epicor.
    """
    return "'" + str(value or "").replace("'", "''") + "'"


def _like_body(value: str) -> str:
    """Escape a phrase for use INSIDE a ``like '%…%'`` pattern.

    ``%`` and ``_`` are wildcards. Left raw they WIDEN the match set that the
    lone-hit rule then reads from — i.e. the caller's own punctuation could
    decide whether rung 3 auto-picks. T-SQL bracket escaping (``[%]``) needs no
    ``ESCAPE`` clause; if Epicor's clause parser does not honour it the pattern
    simply fails to match and the ladder falls through to the client-side pass,
    which is the safe direction.
    """
    out = str(value or "").replace("'", "''")
    out = out.replace("[", "[[]")
    out = out.replace("%", "[%]").replace("_", "[_]")
    return out


# --------------------------------------------------------------------------- #
# BO calls
# --------------------------------------------------------------------------- #


async def _get_list(
    client: Any, api_key: str, base_url: str, where: str,
    page_size: int = 10,
) -> tuple[list[dict], bool]:
    """``(rows, complete)`` from ``DashBoardSvc/GetList``.

    *complete* is False when the page came back FULL — the honest reading of a
    server page, and the thing a naive reader never checks.
    """
    resp = await client.call_method(
        base_url,
        _DASHBOARD_SERVICE,
        "GetList",
        api_key,
        params={
            "whereClause": where,
            "pageSize": page_size,
            "absolutePage": 1,
        },
    )
    rows = (resp or {}).get("returnObj", {}).get("DashBdDefList", []) or []
    rows = [r for r in rows if isinstance(r, dict)]
    return rows, len(rows) < page_size


async def _get_dashboard_baqs(
    client: Any, api_key: str, base_url: str, definition_id: str,
) -> tuple[list[str], bool]:
    """``(baq_ids, complete)`` from ``DashBoardSvc/GetRows``.

    EVERY DataSet table needs a ``whereClause`` param or Epicor 400s with
    *"Parameter whereClauseX is not found in the input object"*, and the sibling
    values must be ``""`` — not ``1=0``, not absent.
    """
    resp = await client.call_method(
        base_url,
        _DASHBOARD_SERVICE,
        "GetRows",
        api_key,
        params={
            "whereClauseDashBdDef": f"DefinitionID = {_quote(definition_id)}",
            "whereClauseDashBdBAQ": "",
            "whereClauseDashBdChunk": "",
            "whereClauseDashBdLike": "",
            "pageSize": _BAQ_PAGE_SIZE,
            "absolutePage": 1,
        },
    )
    rows = (resp or {}).get("returnObj", {}).get("DashBdBAQ", []) or []
    rows = [r for r in rows if isinstance(r, dict)]
    ids: list[str] = []
    for row in rows:
        qid = str(row.get("QueryID") or "").strip()
        if qid and qid not in ids:
            ids.append(qid)
    return ids, len(rows) < _BAQ_PAGE_SIZE


# --------------------------------------------------------------------------- #
# the resolution ladder
# --------------------------------------------------------------------------- #


@dataclass
class _Resolution:
    match: dict | None = None
    near_misses: list[dict] = field(default_factory=list)
    #: False when the 500-row corpus page came back FULL. A miss built on a
    #: partial corpus is an UNVERIFIED negative and must never be terminal.
    corpus_complete: bool = True
    #: True when the corpus was never fetched (an earlier rung matched).
    corpus_read: bool = False
    #: True when `near_misses` are EXACT matches the server could not tell
    #: apart, not approximations. "No dashboard is named 'Open Backlog', here are
    #: some close ones" is a false statement when two of them are named exactly
    #: that — the caller has to choose, not be told it does not exist.
    ambiguous: bool = False


async def _find_dashboard(
    client: Any, api_key: str, base_url: str, name: str,
) -> _Resolution:
    """Resolve a dashboard name to its definition row.

    Rungs: exact id → exact description → a LIKE probe that is taken ONLY when
    it matches exactly one row → the client-side squashed/tokenised pass over
    the full corpus (definitions are squashed like ``OpenBacklog`` while people say
    "Open Backlog dashboard", so no server-side LIKE on the whole phrase can match).
    """
    rows, _ = await _get_list(
        client, api_key, base_url, f"DefinitionID = {_quote(name)}")
    if rows:
        return _Resolution(match=rows[0])

    # Rung 2. DefinitionID above is the primary key, so its `rows[0]` is unique
    # by construction. Description is NOT unique — two dashboards can carry the
    # same one — so taking `rows[0]` here is the same silent guess this module
    # removed from rung 3, just one rung earlier. Exactly one match is taken;
    # several fall through to the scoring pass and are kept as near-misses.
    rows, _ = await _get_list(
        client, api_key, base_url, f"Description = {_quote(name)}")
    exact_desc_rows = list(rows)
    if len(exact_desc_rows) == 1:
        return _Resolution(match=exact_desc_rows[0])

    body = _like_body(name)
    rows, _ = await _get_list(
        client, api_key, base_url,
        f"DefinitionID like '%{body}%' or Description like '%{body}%'",
    )
    like_rows = list(rows)
    if len(like_rows) == 1:
        return _Resolution(match=like_rows[0])
    # Two or more: taking rows[0] of an UNRANKED server-side match would be a
    # silent guess, and it contradicts the len(full) == 1 rule below. Fall
    # through to the scoring pass and keep these only as fallback near-misses.

    # An EXACT description hit outranks a LIKE hit as a candidate, so the two
    # ambiguous rungs feed one fallback list in that order, deduped by id.
    _seen_ids = {str(r.get("DefinitionID") or "") for r in exact_desc_rows}
    fallback_rows = exact_desc_rows + [
        r for r in like_rows if str(r.get("DefinitionID") or "") not in _seen_ids
    ]

    toks = _name_tokens(name)
    squashed_query = _squash("".join(toks))
    if not squashed_query:
        return _Resolution(near_misses=fallback_rows[:_MAX_CANDIDATES])

    groups = [_alternatives(t) for t in toks]
    all_rows, corpus_complete = await _get_list(
        client, api_key, base_url, "", page_size=_CORPUS_PAGE_SIZE)

    scored: list[tuple[float, dict]] = []
    squashed_hits: list[dict] = []
    for row in all_rows:
        sid = _squash(row.get("DefinitionID") or "")
        sdesc = _squash(row.get("Description") or "")
        # Noise-stripped squashed equality is a confident hit
        # ('Open Backlog dashboard' -> 'openbacklog' == 'openbacklog') — but only when it is
        # UNIQUE. Returning on the first hit made this the last silent guess in
        # the ladder: two definitions whose descriptions squash to the same
        # string ("Open Backlog" on both an old and a new one) resolved to whichever
        # Epicor listed first, with full confidence and no candidates offered.
        # Collect and decide after the sweep, the same rule as `len(full) == 1`.
        if squashed_query in (sid, sdesc):
            squashed_hits.append(row)
            continue
        hay = f"{sid} {sdesc}"
        hits = sum(_group_score(g, sid, sdesc, hay) for g in groups)
        if hits:
            scored.append((hits / max(len(groups), 1), row))

    if len(squashed_hits) == 1:
        return _Resolution(match=squashed_hits[0], corpus_complete=corpus_complete,
                           corpus_read=True)
    if squashed_hits:
        return _Resolution(near_misses=squashed_hits[:_MAX_CANDIDATES],
                           corpus_complete=corpus_complete, corpus_read=True,
                           ambiguous=True)

    scored.sort(key=lambda pair: -pair[0])
    if scored:
        full = [r for s, r in scored if s >= 1.0]
        # Every query word matched in exactly one definition -> unambiguous.
        if len(full) == 1:
            return _Resolution(match=full[0], corpus_complete=corpus_complete,
                               corpus_read=True)
        return _Resolution(near_misses=[r for _s, r in scored[:_MAX_CANDIDATES]],
                           corpus_complete=corpus_complete, corpus_read=True)
    return _Resolution(near_misses=fallback_rows[:_MAX_CANDIDATES],
                       corpus_complete=corpus_complete, corpus_read=True)


def _candidates(rows: list[dict]) -> list[dict]:
    return [
        {"id": str(r.get("DefinitionID") or "").strip(),
         "description": str(r.get("Description") or "").strip()}
        for r in rows
        if str(r.get("DefinitionID") or "").strip()
    ]


# --------------------------------------------------------------------------- #
# responses
# --------------------------------------------------------------------------- #


_RUN_HINT = ("Run each one with epicor_query(saved_baq='<id>') — that path "
             "reads the BAQ's definition and enforces the deny-list over it "
             "before executing.")

_NO_QUERY_TOOL = ("This server does not register a tool that runs a saved BAQ, "
                  "so these ids cannot be run from here — give them to the "
                  "user to run in Epicor.")


def _fit(payload: dict, max_bytes: int) -> dict:
    """Trim a LIST payload until it serialises inside *max_bytes*.

    The cap is the primary control; this is the backstop for a corpus of
    unusually long descriptions. Trimming is ANNOUNCED — a silently shortened
    list is a list the model will treat as complete.
    """
    try:
        if len(json.dumps(payload)) <= max_bytes:
            return payload
    except (TypeError, ValueError):  # pragma: no cover - payload is plain data
        return payload
    items = payload.get("dashboards")
    if not isinstance(items, list):
        return payload
    while len(items) > 5:
        items = items[: len(items) // 2]
        payload["dashboards"] = items
        payload["count_complete"] = False
        payload["note"] = (
            f"Trimmed to {len(items)} entries to fit the response budget; "
            "more dashboards exist than are shown."
        )
        try:
            if len(json.dumps(payload)) <= max_bytes:
                break
        except (TypeError, ValueError):  # pragma: no cover
            break
    return payload


def _list_payload(rows: list[dict], corpus_complete: bool, max_list: int,
                  max_bytes: int) -> dict:
    seen: set[str] = set()
    items: list[dict] = []
    for row in rows:
        did = str(row.get("DefinitionID") or "").strip()
        if not did or did in seen:
            continue
        seen.add(did)
        items.append({"id": did,
                      "description": str(row.get("Description") or "").strip()})
    items.sort(key=lambda d: (d["description"] or d["id"]).lower())

    out: dict = {
        "mode": "dashboard_list",
        "count": len(items),
        "count_complete": bool(corpus_complete),
        "dashboards": items[:max_list],
        "hint": ("Ask the user which of these they mean, then call "
                 "epicor_dashboards again with its exact `id`. Do NOT invent a "
                 "dashboard name that is not in this list."),
        # Load-bearing label: dashboards are `unmapped` in menu_security.db, so
        # this list is NOT personalised and must never read as if it were.
        "scope": ("These are all dashboards DEFINED in Epicor, not a personal "
                  "menu — this server cannot filter them per user."),
        "terminal": False,
    }
    notes: list[str] = []
    if len(items) > max_list:
        notes.append(f"{len(items)} dashboards exist; showing the first {max_list}.")
    if not corpus_complete:
        notes.append(
            f"The definition list came back FULL at {_CORPUS_PAGE_SIZE} rows, so "
            "more dashboards exist than were read — this count is a floor, not a total."
        )
    if notes:
        out["note"] = " ".join(notes)
    return _fit(out, max_bytes)


def _resolved_payload(row: dict, baq_ids: list[str], baq_ids_complete: bool,
                      *, query_tool_available: bool) -> dict:
    defn_id = str(row.get("DefinitionID") or "").strip()
    description = str(row.get("Description") or "").strip() or defn_id

    out: dict = {
        "mode": "dashboard",
        "dashboard": defn_id,
        "description": description,
        "baq_ids": list(baq_ids),
        "baq_ids_complete": bool(baq_ids_complete),
    }

    if not baq_ids:
        # Not an error: a dashboard genuinely can have no BAQ attached, and that
        # IS the answer. Only a truncated page makes it non-terminal.
        out["note"] = "This dashboard has no BAQ queries attached."
        out["terminal"] = bool(baq_ids_complete)
        if not baq_ids_complete:
            out["note"] += (
                f" The attached-BAQ page came back FULL at {_BAQ_PAGE_SIZE} rows, "
                "so this reading is not trustworthy."
            )
        return out

    if query_tool_available:
        out["run_with"] = [{"tool": "epicor_query", "saved_baq": bid}
                           for bid in baq_ids]
        out["next_step"] = (
            f"Run epicor_query(saved_baq='{baq_ids[0]}') to get its rows."
        )
        out["note"] = ("These are the BAQs the dashboard is built from. This "
                       "tool does not run them. " + _RUN_HINT)
    else:
        out["note"] = ("These are the BAQs the dashboard is built from. "
                       + _NO_QUERY_TOOL)

    if not baq_ids_complete:
        out["terminal"] = False
        out["note"] += (
            f" INCOMPLETE: the attached-BAQ page came back FULL at "
            f"{_BAQ_PAGE_SIZE} rows, so the dashboard may have more BAQs than "
            "are listed here."
        )
    else:
        # The ids are in hand but the ROWS are not, so the turn is not over.
        out["terminal"] = not query_tool_available
    return out


def _miss_envelope(name: str, resolution: _Resolution) -> dict:
    candidates = _candidates(resolution.near_misses)
    if candidates and resolution.ambiguous:
        return error_envelope(
            "dashboard_ambiguous",
            f"{len(candidates)} dashboards match {name!r} exactly — Epicor allows "
            "two definitions to carry the same name. They are in "
            "`valid.dashboards`; ask the user WHICH one they mean and re-call "
            "with its exact `id`. Do not pick one for them.",
            valid={"dashboards": candidates},
            retry_with={"tool": "epicor_dashboards",
                        "dashboard": candidates[0]["id"]},
            detail={"stage": "dashboard_resolve",
                    "corpus_complete": resolution.corpus_complete},
            terminal=False,
        )
    if candidates:
        return error_envelope(
            "dashboard_not_found",
            f"No dashboard is named {name!r}. The closest defined dashboards are "
            "in `valid.dashboards`. If one of them is what the user meant, "
            "re-call ONCE with its exact `id`. If none match, the dashboard does "
            "not exist — tell the user; do NOT search for BAQs or query tables to "
            "reconstruct it.",
            valid={"dashboards": candidates},
            retry_with={"tool": "epicor_dashboards",
                        "dashboard": candidates[0]["id"]},
            detail={"stage": "dashboard_resolve",
                    "corpus_complete": resolution.corpus_complete},
            terminal=False,
        )
    # `terminal=True` is a claim that the dashboard DOES NOT EXIST, and only a
    # completed read of the whole corpus earns it. `corpus_complete` defaults to
    # True on `_Resolution`, so on any path that returned before the corpus was
    # fetched it carries the DEFAULT, not a measurement — `corpus_read` is what
    # separates the two, and without this it was written four times and never
    # read, implying a guard that did not exist.
    if resolution.corpus_read and resolution.corpus_complete:
        return error_envelope(
            "dashboard_not_found",
            f"No dashboard is named {name!r} and none has a similar name. Tell "
            "the user it was not found; do NOT search for BAQs or query tables "
            "to reconstruct it.",
            detail={"stage": "dashboard_resolve", "corpus_complete": True},
            terminal=True,
        )
    if not resolution.corpus_read:
        return error_envelope(
            "dashboard_not_found",
            f"No dashboard named {name!r} was found, but the full definition "
            "list was never read on this path, so this is NOT proof it does not "
            "exist. Call epicor_dashboards with no name to list them, or ask the "
            "user for the exact id as it appears in Epicor.",
            retry_with={"tool": "epicor_dashboards"},
            detail={"stage": "dashboard_resolve", "corpus_read": False},
            terminal=False,
        )
    return error_envelope(
        "dashboard_not_found",
        f"No dashboard named {name!r} was found in the definitions that were "
        f"read — but the definition list came back FULL at {_CORPUS_PAGE_SIZE} "
        "rows, so the search was over a PARTIAL corpus and this is not proof "
        "the dashboard does not exist. Ask the user for the exact id as it "
        "appears in Epicor.",
        retry_with={"tool": "epicor_dashboards", "dashboard": "<exact id>"},
        detail={"stage": "dashboard_resolve", "corpus_complete": False},
        terminal=False,
    )


def _service_envelope(exc: Exception) -> dict:
    """Carry Epicor's OWN status and message. Never a fixed generic string.

    A blanket ``except Exception`` that returns *"Failed to look up dashboard X.
    Verify the name and try again."* for a 401 sends the model back to
    re-check a name that was never the problem.
    """
    status = getattr(exc, "status_code", None)
    message = str(getattr(exc, "message", "") or str(exc))[:500]
    return error_envelope(
        "dashboard_service_unavailable",
        "Epicor refused the dashboard lookup, so no dashboard could be read. "
        "This is an access/service problem, not a wrong name — re-sending the "
        "same name will fail the same way. Epicor's own message is in `detail`.",
        detail={"stage": "dashboard_service", "service": _DASHBOARD_SERVICE,
                "status": status, "message": message},
        terminal=True,
    )


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


_DESCRIPTION_HEAD = """\
Resolve an Epicor DASHBOARD to the BAQ(s) behind it.

Call this the moment the user names a dashboard ("the Open Backlog dashboard",
"what runs behind OpenBacklog") — never answer a dashboard question by reading
tables. Pass the words the user used: exact ids, descriptions and spoken forms
("Open Backlog dashboard" -> OpenBacklog) all resolve. Ask "which dashboards" to list
what is defined."""

_DESCRIPTION_RUN = """

It returns BAQ ids and does NOT run them: run each with
epicor_query(saved_baq="<id>")."""

_DESCRIPTION_NO_RUN = """

It returns BAQ ids only. Nothing on this server runs a saved BAQ, so give the
ids to the user to run in Epicor."""


def dashboards_description(*, query_tool_available: bool) -> str:
    """The tool description. Names ``epicor_query`` only when it registered.

    ``register_query_tool`` can return a FALSE ``RegistrationDecision``
    (registration gate), and naming a tool the model cannot call is a guaranteed dead
    turn — the same conditional ``tool_description(discovery_available=…)``
    exists for.
    """
    return _DESCRIPTION_HEAD + (
        _DESCRIPTION_RUN if query_tool_available else _DESCRIPTION_NO_RUN
    )


def register_dashboards_tool(
    mcp: Any,
    *,
    client: Any,
    api_key: str,
    base_url: str,
    query_tool_available: bool,
    max_list: int = _MAX_DASHBOARD_LIST,
    max_bytes: int = 700_000,
) -> None:
    """Bind ``epicor_dashboards`` to *mcp*.

    ``dashboard`` is REQUIRED (no default) on purpose:
    ``test_every_listed_tool_passes_the_gate`` calls every listed tool with
    ``{}``, and a defaulted parameter would send that call into list mode and
    make a live HTTP request from the deterministic suite. List mode stays
    reachable through the words a caller actually uses ("which dashboards").
    """

    @mcp.tool(
        name="epicor_dashboards",
        description=dashboards_description(
            query_tool_available=query_tool_available),
    )
    async def epicor_dashboards(dashboard: str) -> dict:  # noqa: D401
        name = str(dashboard or "").strip()
        try:
            if _is_list_request(name):
                rows, complete = await _get_list(
                    client, api_key, base_url, "", page_size=_CORPUS_PAGE_SIZE)
                return _list_payload(rows, complete, max_list, max_bytes)

            resolution = await _find_dashboard(client, api_key, base_url, name)
            if resolution.match is None:
                return _miss_envelope(name, resolution)

            defn_id = str(resolution.match.get("DefinitionID") or "").strip()
            baq_ids, baq_complete = await _get_dashboard_baqs(
                client, api_key, base_url, defn_id)
            return _resolved_payload(
                resolution.match, baq_ids, baq_complete,
                query_tool_available=query_tool_available,
            )
        except Exception as exc:  # noqa: BLE001
            # Split deliberately: an Epicor refusal carries a status_code and is
            # reported AS Epicor's refusal, message intact. Anything else is a
            # bug in this module and must not masquerade as an access problem.
            if getattr(exc, "status_code", None) is not None:
                logger.warning("epicor_dashboards: Epicor refused (%s): %s",
                               getattr(exc, "status_code", None), exc)
                return _service_envelope(exc)
            logger.exception("epicor_dashboards failed for %r", name)
            return error_envelope(
                "dashboard_lookup_failed",
                "The dashboard lookup failed inside this server, not in Epicor. "
                "Nothing was read. Tell the user the dashboard could not be "
                "looked up; re-sending the same name will not help.",
                detail={"stage": "dashboard_service",
                        "exception": type(exc).__name__},
                terminal=True,
            )

    logger.info("epicor_dashboards registered (query_tool_available=%s)",
                query_tool_available)

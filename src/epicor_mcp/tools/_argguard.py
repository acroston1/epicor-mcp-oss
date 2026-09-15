"""Argument-validation guard: pydantic internals never reach the model (INV-1).

FastMCP validates a tool's arguments BEFORE invoking the function
(``func_metadata.call_fn_with_arg_validation`` runs ``arg_model.model_validate``
first), and ``Tool.run`` re-raises the resulting ``ValidationError`` as
``ToolError(f"Error executing tool {name}: {e}")``. That string carries the
``https://errors.pydantic.dev/...`` docs URL and the internal
``epicor_baqArguments`` model name straight to the model — framework internals
in place of a self-correcting envelope. A tool body's own try/except cannot
catch it, because the body never runs.

The failure IS structured (``exc.__cause__.errors()`` gives loc/type/input); it
is merely stringified on the way out. This wrapper re-structures it.

Installed on ``ToolManager.call_tool`` for the same reason ``install_audit_hook``
is (see audit.py): the low-level server captures ``FastMCP.call_tool`` at
registration time, so patching the FastMCP attribute has no effect. It is
installed UNCONDITIONALLY — folding it into the audit hook would silently
disable it wherever ``audit_logger`` is None (test harnesses, stdio dev mode).
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from epicor_mcp.audit import _build_breaker_response
from epicor_mcp.context import clear_arg_notes, set_arg_notes
from epicor_mcp.tools._resolve import coerce_csv, error_envelope

logger = logging.getLogger(__name__)

# JSON-schema type -> the phrasing the model should read back.
_TYPE_HINT = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}


def _declared_types(mcp: Any, name: str) -> dict[str, str] | None:
    """``{arg: human type}`` from the tool's advertised JSON schema, or None.

    The None/{} split is load-bearing, not cosmetic. ``{}`` used to be a single
    sentinel for two OPPOSITE outcomes — "this tool could not be resolved" and
    "this tool resolves and legitimately takes no arguments" — and
    ``_screen_arguments`` stood down for both. A registered zero-arg tool's
    schema really is ``{'properties': {}, ...}`` (verified against the installed
    FastMCP), so ``epicor_mrp_output(history=3)`` sailed through with the
    argument silently discarded — the 552-instance fix voided for exactly the
    tool where the confusion is likeliest (``history``/``date`` are real
    parameters of its sibling ``epicor_mrp_status``, so the model gets a
    confidently wrong whole-week answer).

    None now means UNRESOLVABLE; ``{}`` means resolved-and-argument-less and
    falls through into normal screening.
    """
    try:
        tool = mcp._tool_manager.get_tool(name)
        props = tool.parameters["properties"] if tool is not None else None
    except Exception:  # noqa: BLE001 — treated as unresolvable, announced below
        props = None
    if not isinstance(props, dict):
        logger.error("argument guard could not resolve tool schema for %s", name)
        return None
    out: dict[str, str] = {}
    for arg, spec in props.items():
        t = spec.get("type")
        if t is None and isinstance(spec.get("anyOf"), list):
            t = next((o.get("type") for o in spec["anyOf"] if o.get("type")), None)
        out[arg] = _TYPE_HINT.get(t, t or "any")
    return out


def _validation_envelope(
    cause: ValidationError, arguments: dict, declared: dict[str, str],
) -> dict:
    """INV-1 envelope for an argument-type mismatch.

    Never includes ``str(exc)`` — that string IS the leak.
    """
    bad: list[str] = []
    retry = dict(arguments or {})
    uncorrected: list[str] = []
    for err in cause.errors():
        loc = err.get("loc") or ()
        if not loc:
            continue
        arg = str(loc[0])
        got = type(err.get("input")).__name__
        want = declared.get(arg, "the declared type")
        bad.append(f"'{arg}' must be {want}, not {got}")
        if arg not in retry:
            continue
        fixed = _coerce_for(retry[arg], declared.get(arg, ""))
        if fixed is None or fixed == retry[arg]:
            # NOTHING was corrected. Echoing the failing value back produces a
            # retry_with byte-identical to the call that just failed, under a
            # message saying "re-call with the values in retry_with" — a
            # guaranteed identical-retry loop. Drop the key instead, so the
            # model must supply a new value rather than replay the old one.
            retry.pop(arg, None)
            uncorrected.append(arg)
        else:
            retry[arg] = fixed
    msg = "Argument type mismatch: " + "; ".join(bad) + ". "
    if retry:
        msg += ("Re-call with the values in retry_with (lists must be passed "
                "as one comma-separated string). ")
    if uncorrected:
        msg += (f"No automatic correction was possible for "
                f"{', '.join(sorted(uncorrected))} — it is OMITTED from "
                "retry_with on purpose; supply a NEW value of the declared "
                "type (see valid.arguments), do not resend the old one.")
    return error_envelope(
        "invalid_argument_type",
        msg.strip(),
        valid={"arguments": declared},
        retry_with=retry,
    )


def _coerce_for(value, declared_type: str):
    """Best-effort correction of *value* toward *declared_type*, else None.

    ``coerce_csv`` alone returns a plain string untouched, so the single most
    common weak-model mistake — a JSON-object STRING for a ``dict|None``
    param, e.g. ``params='{"partNum": "X"}'`` (a form ``epicor_baq`` itself
    tolerates) — came back unchanged.
    """
    if declared_type in ("object", "integer", "number", "boolean"):
        if isinstance(value, str):
            try:
                parsed = json.loads(value.strip())
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, (dict, int, float, bool)) else None
        return None
    return coerce_csv(value)


# ---------------------------------------------------------------------------
# Unknown-argument screening — the silent-drop fix
# ---------------------------------------------------------------------------
# FastMCP builds its arg model with pydantic's default ``extra='ignore'`` and
# ``model_dump_one_level`` iterates DECLARED fields only, so any argument the
# model passes that is not a parameter is parsed and THROWN AWAY: no error, no
# note, no trace. Dropping arguments can change the answer:
#   * ``select`` prunes columns; dropping it can exceed the response budget;
#   * dropping ``query='SELECT ...'`` on a create request can compose a
#     different BAQ from the surviving arguments and report success.
# The rule: an unknown argument either MAPS to a real parameter (aliased, with
# the coercion recorded) or it does NOT (INV-1 envelope naming the SUPPORTED
# path). Silence is never an outcome.
#
# Aliasing is allowed ONLY when all three hold, which is what keeps this from
# becoming the fail-soft silent drop in new clothes:
#   (1) uniqueness   — exactly one plausible target parameter,
#   (2) shape        — the value is usable as-is,
#   (3) semantic identity — the names mean the SAME thing, not related things.

_ARG_ALIASES: dict[str, dict[str, str]] = {
    # `select` IS the OData projection — a deterministic rename, not a guess.
    "epicor_read": {"select": "fields"},
    "epicor_baq": {
        "top": "limit",
        "select": "fields",
        "table": "tables",
        # `baq` is the sole identity-bearing parameter and is correct under
        # every action: run/delete=id, find=search text, create=new name,
        # dashboard=dashboard name.
        "baq_id": "baq", "baq_name": "baq", "query_name": "baq",
        "name": "baq", "id": "baq", "dashboard_id": "baq",
    },
    "epicor_time_phase": {"part_num": "part", "top": "limit"},
    "epicor_help": {"max_results": "limit"},
    # `reference` is not an invented API — it is lifted verbatim from this
    # tool's OWN docstring ("Resolve ANY person/user/employee reference"), and
    # `search_name` already resolves login ids / emp ids / names. Rejecting the
    # word the tool itself taught the model is a self-inflicted round trip.
    "epicor_find_person": {"query": "search_name", "name": "search_name",
                           "reference": "search_name"},
    # ---- the public surface -----------------------------------------------
    # Models call ``epicor_query(query="SELECT TOP 50 ...")``; without this
    # alias that draws a hard `unknown_arguments` with an EMPTY retry_with,
    # because nothing else sent maps either. That is not the model being sloppy — the surface teaches it:
    # the tool is NAMED epicor_query, and its two siblings both take their search
    # text in a parameter literally called `query`. Three tools, one obvious word,
    # and exactly one of them means something else by it.
    #
    # All three alias rules hold: uniqueness (`sql` is the only string parameter;
    # `page_size`/`page` are ints), shape (a str is a str), and semantic identity
    # — "query" and "sql" name the same SELECT statement. The one case where
    # identity FAILS is a natural-language value, and that is not silently passed
    # through: `_query_rejects` below catches it on the POST-alias dict and routes
    # to epicor_tables by name.
    #
    # The save/saved-BAQ parameters are aliased ONE WAY
    # ONLY: toward the READ side. **Nothing aliases to `save_as`**, and that is
    # this table's own three-part rule doing its job rather than a stylistic
    # choice. `baq_name` is the obvious candidate — it is the earlier *create*
    # tool's parameter, so a model carrying those habits reaches for it — and it is
    # ambiguous between "the BAQ I want you to run" and "the name to save
    # under". Aliased to `save_as` on a call that also carries `sql`,
    # ``baq_name="AUTO-open-pos"`` would OVERWRITE that existing definition in
    # place, silently, on the first turn. No other alias in this file can
    # destroy data; this one could, so it is a hard reject that names both
    # parameters and makes the model say which it meant.
    "epicor_query": {
        "query": "sql", "statement": "sql", "sql_query": "sql", "q": "sql",
        "select": "sql", "sql_statement": "sql", "sqlquery": "sql",
        "page_num": "page", "page_number": "page",
        "page_size_limit": "page_size", "rows_per_page": "page_size",
        # Read side: every one of these names an EXISTING BAQ to run.
        "baq": "saved_baq", "baq_id": "saved_baq", "query_id": "saved_baq",
        "saved_query": "saved_baq",
        "parameters": "params", "baq_params": "params",
        "query_params": "params",
        # `baq_description` is unambiguous (a BAQ has exactly one description
        # and only the save path writes one). Plain `description` is NOT
        # aliased: a model uses that word for its own QUESTION as often as for
        # the artifact, which fails the semantic-identity test.
        "baq_description": "save_description",
    },
    "epicor_dashboards": {
        "name": "dashboard", "dashboard_name": "dashboard",
        "dashboard_id": "dashboard", "id": "dashboard",
        "query": "dashboard", "q": "dashboard",
        "definition_id": "dashboard", "search": "dashboard",
    },
    # `search`/`q`/`question`/`text` are the same word for `query`. `table` is
    # aliased too: epicor_tables(table="POHeader") is a caller asking whether a
    # table exists, which IS this tool's question.
    #
    # The identity spellings (`email`/`user`/`user_email`) are deliberately
    # ABSENT: they aliased to the removed `for_email`, a parameter that
    # OUTRANKED the session in gate mode — i.e. any caller could pick whose
    # authorization applied to them. The parameter is gone from the surface
    # and a stray spelling now draws the standard `unknown_arguments`
    # envelope (with the `_REJECT_HINTS` entry below), never silent honoring.
    "epicor_tables": {
        "search": "query", "q": "query", "question": "query", "text": "query",
        "subject": "query", "table": "query", "table_name": "query",
        "search_query": "query", "top": "limit", "max_results": "limit",
    },
    # `tables` -> `table` is not a coercion: the parameter ALREADY accepts a
    # list, so the plural spelling is the same argument with an s.
    "epicor_fields": {
        "tables": "table", "table_name": "table", "table_names": "table",
        "search": "query", "q": "query", "question": "query", "text": "query",
        "column": "query", "columns": "query", "field": "query",
        "fields": "query", "search_query": "query",
        "top": "limit", "max_results": "limit",
    },
}

# Arguments that belong to a DIFFERENT tool. `valid.arguments: {}` on a zero-arg
# tool names no supported path at all, so the model dead-ends; naming the tool
# that owns the argument converges it in one hop (same reasoning as
# ``_baq_rejects``).
_WRONG_TOOL_ARGS: dict[str, dict[str, str]] = {
    "epicor_mrp_output": {
        "history": "epicor_mrp_status",
        "date": "epicor_mrp_status",
        "days": "epicor_mrp_status",
    },
    # A dashboard is resolved by its OWN tool; epicor_dashboards never runs SQL.
    "epicor_dashboards": {"sql": "epicor_query"},
}

#: Every spelling that could mean EITHER "save under this name" or "run the BAQ
#: with this name". Aliasing it would let one guess overwrite a live BAQ, so it
#: is refused with a hint that names both parameters.
_AMBIGUOUS_BAQ_NAME = (
    "ambiguous, and the two meanings do opposite things: `save_as` names a NEW "
    "BAQ to WRITE (it overwrites any BAQ of that name), `saved_baq` names an "
    "EXISTING one to RUN. Say which you meant."
)

#: The removed identity parameter. It used to OUTRANK the session
#: in gate mode, so a caller could choose whose table authorization applied to
#: them — identity spoofing. A supplied spelling must draw this hint, never be
#: honored and never be silently dropped.
_IDENTITY_FROM_SESSION = (
    "identity comes from the connection session (the bearer token), never from "
    "an argument — authorization is applied automatically for the connected "
    "user. To inspect someone ELSE's access, an administrator can call "
    "GET /admin/authz/{email}."
)

# Why the invented argument was reached for, and what already covers it. A bare
# "no such parameter" is what makes the model invent the NEXT one.
_REJECT_HINTS: dict[str, dict[str, str]] = {
    "epicor_find_person": {
        "resolve_to": "every match already returns all identities it has — "
                      "name, email, user_id (login) and emp_id (employee "
                      "number). Pick the one matching the column you are about "
                      "to filter; there is nothing to resolve TO.",
        "role_hint": "epicor_find_person has no role filter. To find jobs by "
                     "planner, call epicor_read with the planner's name — it "
                     "resolves name->PersonID itself.",
    },
    "epicor_query": {
        "save": "a boolean is not how saving works. Pass "
                "save_as='<short-name>' — the name IS the instruction, and "
                "leaving it out is how you DON'T save. Only set it when the "
                "user asked you to save the query.",
        "baq_name": _AMBIGUOUS_BAQ_NAME,
        "save_name": _AMBIGUOUS_BAQ_NAME,
        "save_baq_as": _AMBIGUOUS_BAQ_NAME,
        "name": _AMBIGUOUS_BAQ_NAME,
        "dashboard": "dashboards are a different tool — call "
                     "epicor_dashboards(dashboard='<name>'), which returns the "
                     "BAQ ids behind it; run each with "
                     "epicor_query(saved_baq='<id>').",
        "cursor": "this tool pages with `page` (1-based) and `page_size`. "
                  "There is no opaque cursor.",
    },
    "epicor_tables": {
        "for_email": _IDENTITY_FROM_SESSION,
        "email": _IDENTITY_FROM_SESSION,
        "user_email": _IDENTITY_FROM_SESSION,
        "user": _IDENTITY_FROM_SESSION,
    },
    "epicor_fields": {
        "for_email": _IDENTITY_FROM_SESSION,
        "email": _IDENTITY_FROM_SESSION,
        "user_email": _IDENTITY_FROM_SESSION,
        "user": _IDENTITY_FROM_SESSION,
    },
}

# Arguments with no target parameter that provably cannot change the result.
# Dropped, but RECORDED — erroring would cost a round trip for a cosmetic key.
_ARG_IGNORABLE: dict[str, dict[str, str]] = {
    "epicor_baq": {
        # Refusing is right — a timeout knob lets the model ABORT work, it does
        # not make the work cheaper. But the note has to name the levers that
        # do, or the underlying worry about slow reads
        # just gets re-expressed as another invented argument.
        "timeout_ms": "There is no per-call timeout. Bound the cost of a slow "
                      "query instead: lower `limit`, or narrow `where` so "
                      "Epicor filters server-side.",
    },
}


def _reject(error: str, message: str, **kw) -> dict:
    return error_envelope(error, message, **kw)


def _merge_fields(*parts: str) -> str:
    """Comma-merge field-ish strings, de-duped, order preserved."""
    out: list[str] = []
    for part in parts:
        for term in str(part or "").split(","):
            term = term.strip()
            if term and term not in out:
                out.append(term)
    return ", ".join(out)


# The epicor_baq arguments `_baq_rejects` owns. Held back from the alias loop
# so the reject can be evaluated against the POST-alias dict: it used to run on
# the raw arguments, so a create carrying baq_name= lost the name out of its own
# recovery template (retry_with.baq == "").
_BAQ_REJECT_KEYS = frozenset({"group_by", "aggregate", "query"})

# Every declared value the caller supplied has to survive into retry_with, or
# the "re-call with this" template silently discards the user's own words.
_BAQ_RETRY_KEYS = ("action", "baq", "tables", "fields", "where", "description",
                   "order_by", "limit")


def _baq_retry(args: dict, **overrides) -> dict:
    out = {k: args.get(k, "") for k in _BAQ_RETRY_KEYS
           if args.get(k) not in (None, "")}
    out.setdefault("action", str(args.get("action") or "create"))
    out.update(overrides)
    return out


def _baq_rejects(args: dict) -> dict | None:
    """INV-1 envelopes for epicor_baq arguments that map to NOTHING."""
    action = str(args.get("action") or "").strip().lower()
    group_by = args.get("group_by")
    aggregate = args.get("aggregate")
    if group_by or aggregate:
        # Deliberately rejected TOGETHER in one envelope. `fields` does accept
        # `sum(x) as y`, so aliasing `aggregate` alone is tempting — but
        # aliasing one half while rejecting the other yields a half-applied
        # query, i.e. the silent wrong artifact this whole change exists to
        # kill. Hand back the correctly MERGED string instead, so it still
        # converges in one hop AND teaches the real grammar.
        buckets = [t for t in str(group_by or "").split(",")
                   if re.search(r"\b(month|quarter|year|day)\s*\(", t, re.I)]
        if buckets:
            return _reject(
                "group_by_not_a_baq_param",
                "The BAQ composer groups by RAW columns only — it cannot "
                "bucket dates by month/quarter/year. Use epicor_read's rollup "
                "instead: it buckets dates and auto-joins header/detail with "
                "no BAQ at all.",
                retry_with={"tool": "epicor_read",
                            "group_by": str(group_by or ""),
                            "aggregate": str(aggregate or "")},
            )
        merged = _merge_fields(group_by or "", aggregate or "",
                               args.get("fields") or "")
        return _reject(
            "group_by_not_a_baq_param" if group_by
            else "aggregate_not_a_baq_param",
            "epicor_baq has no `group_by`/`aggregate` parameter. Grouping and "
            "aggregation both live in `fields`: an aggregate is a function "
            "call there (count(*), sum(OrderQty) as TotalQty) and every plain "
            "field alongside it AUTO-becomes the GROUP BY. The merged `fields` "
            "you want is in retry_with — re-call with it.",
            retry_with=_baq_retry(args, fields=merged),
        )
    if args.get("query"):
        # NOT gated on action='create'. A `query='SELECT ...'` on run/find is
        # the same misconception and deserves the same targeted redirect —
        # falling through to the generic unknown_arguments bucket teaches the
        # model nothing about how a BAQ is actually composed here.
        # NOT raw SQL by design (legacy BAQ contract: schema-SERVED, not
        # SQL-demanded). Epicor's ParseFromSQL ACCEPTS unknown columns and the
        # BAQ then dies at RUN time with an empty 400 — model-authored SQL
        # produces the worst artifact class: parses, saves, fails later.
        return _reject(
            "raw_sql_unsupported",
            "epicor_baq composes the BAQ SQL for you; there is no `query` "
            "parameter and raw SQL is not accepted. Express the query "
            "structurally: `tables` (Schema.Table or business terms), `fields` "
            "(aggregate function calls allowed — plain fields alongside one "
            "auto-become the GROUP BY; a parenthesised expression is a "
            "computed column), and `where` for ALL criteria. The composer "
            "emits INNER JOINs only and has no HAVING — filter an aggregate "
            "with epicor_read's `having` instead.",
            retry_with=_baq_retry(args, action=action or "create"),
        )
    return None


_FIND_PERSON_REJECTS = {"resolve_to", "role_hint"}

#: A statement `epicor_query` can actually run starts with one of these. The set
#: is deliberately tiny: this gate decides only "is this SQL AT ALL", never
#: whether the SQL is any good — the transpiler, the lint and the deny-list each
#: own a piece of that and every one of them gives a better message than a
#: keyword sniff could.
_SQL_OPENERS = ("select", "with", "(select", "(with")


def _query_rejects(args: dict) -> dict | None:
    """The `sql` value is not SQL — route to `epicor_tables` by name.

    Aliasing ``query`` -> ``sql`` is right for a SELECT and WRONG for plain
    English: those are different questions, and answering the second by handing
    it to the SQL pipe produces a parser error about a keyword the caller never
    wrote. Routed here instead, the model is told the actual first step. Also
    catches the caller who spelled ``sql`` correctly and still sent English,
    which no alias table can see.
    """
    sql = str(args.get("sql") or "").strip()
    if not sql:
        return None
    head = sql.lstrip("([ \t\r\n").lower()
    if head.startswith(_SQL_OPENERS) or head.startswith("--") or head.startswith("/*"):
        return None
    words = sql.split()
    return _reject(
        "not_sql",
        "epicor_query runs SQL, and this is not a SQL statement — it must start "
        "with `select` (or a `with` CTE). If you know the tables and columns, "
        "re-call with the SELECT written out. If you do NOT, that is what the "
        "other two tools are for: epicor_tables finds the table from a "
        "plain-English subject, then epicor_fields lists that table's real "
        "columns, then come back here.",
        valid={
            # ALL SEVEN. Held in step with the registered signature on purpose:
            # a stale list here teaches the model that a parameter which does
            # exist does not, which is the same silent-drop defect this guard
            # was written to remove, one layer up.
            "arguments": {
                "sql": "string", "page_size": "integer", "page": "integer",
                "saved_baq": "string", "params": "object",
                "save_as": "string", "save_description": "string",
            },
            "shape": "select top 100 [T].[Col] as [Col] from Erp.Table as [T] where ...",
        },
        retry_with={"tool": "epicor_tables", "query": " ".join(words[:8])},
    )


def _screen_arguments(
    name: str, arguments: dict, declared: dict[str, str] | None,
) -> tuple[dict, dict, dict | None]:
    """``(rewritten_args, notes, terminal_envelope_or_None)``.

    Runs BEFORE the tool is invoked — short-circuiting here is the load-bearing
    property, because it is what stops an under-specified create from reaching
    live Epicor at all.
    """
    if declared is None:
        # Should never happen. Passing through in SILENCE is what made this
        # branch invisible for the entire life of the guard — announce the
        # degraded state instead, so a mcp-library schema change is loud.
        return arguments, {
            "arg_guard_unavailable":
                f"{name}: argument screening could not run; arguments were "
                "passed through unvalidated.",
        }, None
    unknown = {k: v for k, v in arguments.items() if k not in declared}
    # Runs BEFORE the no-unknowns early return on purpose: a caller who spelled
    # `sql` correctly and still sent plain English has no unknown argument at
    # all, and is the case an alias table structurally cannot see.
    if name == "epicor_query":
        env = _query_rejects(arguments)
        if env is not None:
            return arguments, {}, env
    if not unknown:
        return arguments, {}, None

    aliases = dict(_ARG_ALIASES.get(name, {}))
    ignorable = _ARG_IGNORABLE.get(name, {})
    out = {k: v for k, v in arguments.items() if k in declared}
    notes: dict = {}
    aliased: dict[str, str] = {}
    ignored: dict[str, str] = {}
    # Two sources for one target must never be silently picked between.
    claimed: dict[str, str] = {}
    hard: list[str] = []

    # Deferred until AFTER aliasing so `_baq_rejects` sees the resolved names.
    deferred = _BAQ_REJECT_KEYS if name == "epicor_baq" else frozenset()

    for key, value in unknown.items():
        if key in deferred:
            continue
        if name == "epicor_find_person" and key in _FIND_PERSON_REJECTS:
            hard.append(key)
            continue
        if key in ignorable:
            ignored[key] = ignorable[key]
            continue
        target = aliases.get(key)
        if target is None or target not in declared:
            # Unknown-unknown: a confident TYPO is corrected (same 0.82
            # threshold as read's `_correct_column`); anything else is a
            # precise error, which converges in one hop.
            close = difflib.get_close_matches(key, list(declared), n=1, cutoff=0.82)
            if close:
                target = close[0]
                notes.setdefault("arg_typo_fixed", {})[key] = target
            else:
                hard.append(key)
                continue
        prior = claimed.get(target)
        if prior is not None and str(arguments.get(prior)) != str(value):
            return arguments, {}, _reject(
                "ambiguous_argument",
                f"'{prior}'={arguments.get(prior)!r} and '{key}'={value!r} "
                f"both mean '{target}' and they differ. Re-call with a single "
                f"'{target}'."
                + (" Under action='create', `baq` is the BAQ ID (spaces become "
                   "dashes, truncated to 25 chars); a human-readable title "
                   "belongs in `description`."
                   if name == "epicor_baq" and target == "baq" else ""),
                valid={"arguments": declared},
            )
        existing = out.get(target)
        if existing not in (None, "", 0, [], {}) and str(existing) != str(value):
            # The model already supplied the real parameter. Erroring here
            # would cost a round trip for zero information — the DECLARED
            # value wins (same precedence as `limit` over `top`).
            ignored[key] = (f"'{target}' was also supplied and takes "
                            f"precedence.")
            continue
        out[target] = value
        aliased[key] = target
        claimed[target] = key
        if (name == "epicor_baq" and key == "dashboard_id"
                and str(out.get("action") or "").strip().lower() in ("", "run")):
            # `dashboard_id` names a DASHBOARD, and _do_run's auto-route only
            # fires on a spaced/"dashboard"-worded value — so a tidy id like
            # DEMO-DASHBOARD under action='run' would be looked up as a saved BAQ and
            # 404. The resolver rule is absolute: "dashboard" means
            # action='dashboard', never run (INV-2, the tool picks the path).
            out["action"] = "dashboard"
            notes["arg_coerced"] = {
                "action": "dashboard_id was supplied, so action was coerced to "
                          "'dashboard' — 'dashboard' never means action='run'.",
            }

    if name == "epicor_baq":
        env = _baq_rejects({**out, **{k: v for k, v in unknown.items()
                                      if k in deferred}})
        if env is not None:
            return arguments, {}, env
    if name == "epicor_query":
        # Second call, on the POST-alias dict: this is the one that catches
        # `query="show me open POs"`, where the English only becomes visible as
        # `sql` after the alias resolves.
        env = _query_rejects(out)
        if env is not None:
            return arguments, {}, env

    if hard:
        # retry_with is built from the POST-alias dict, not the raw arguments.
        # Built from raw, every alias resolved earlier in this same loop was
        # thrown away under a message promising "the values you meant are
        # echoed in retry_with" — {"query":"service-account","resolve_to":"employee"}
        # handed back {} and left the model with nothing to re-call.
        hints = _REJECT_HINTS.get(name, {})
        why = {k: hints[k] for k in hard if k in hints}
        wrong_tool = {k: t for k, t in _WRONG_TOOL_ARGS.get(name, {}).items()
                      if k in hard}
        if declared:
            msg = (f"{name} has no parameter named {', '.join(sorted(hard))}. "
                   "These were NOT applied. Re-call using only the names in "
                   "valid.arguments — the arguments that DID map are in "
                   "retry_with.")
        else:
            msg = (f"{name} takes no arguments; call it with none. "
                   f"{', '.join(sorted(hard))} were NOT applied.")
        retry = {k: v for k, v in out.items() if k in declared}
        if wrong_tool:
            other = sorted(set(wrong_tool.values()))
            msg += (f" {', '.join(sorted(wrong_tool))} belong to "
                    f"{', '.join(other)} — call that tool instead.")
            retry["tool"] = other[0]
        for k in hard:
            if k in why:
                msg += f" ({k}: {why[k]})"
        return arguments, {}, _reject(
            "unknown_arguments", msg,
            valid={"arguments": declared},
            retry_with=retry,
            detail={"why": why} if why else None,
        )
    if aliased:
        notes["arg_aliased"] = aliased
    if ignored:
        notes["arg_ignored"] = ignored
    return out, notes, None


def _inject_notes(result: Any, notes: dict) -> Any:
    """Fold ``notes`` into the tool's JSON response as ``arg_notes``.

    Done HERE, centrally, rather than per tool. Relying on each tool body to
    call ``get_arg_notes()`` left four of the five tools with alias tables
    (epicor_baq, epicor_time_phase, epicor_help, epicor_find_person) plus
    every difflib typo correction on epicor_act surfacing NOTHING — the guard
    rewrote the arguments and the response said nothing about it. That turns a
    silent DROP into a silent REWRITE, which is the same defect wearing a
    different hat, and on epicor_baq/epicor_act the rewrite can change what is
    persisted to Epicor (``table``->``tables`` under action='create', or a
    fuzzy ``environmnet``->``environment`` flipping the live/pilot switch).

    Best-effort by construction: a non-JSON or non-object payload is returned
    untouched rather than mangled.
    """
    if not notes:
        return result

    def _fold(text: str) -> str:
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return text
        if not isinstance(payload, dict):
            return text
        existing = payload.get("arg_notes")
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(notes)
        payload["arg_notes"] = merged
        try:
            return json.dumps(payload)
        except (TypeError, ValueError):
            return text

    if isinstance(result, str):
        return _fold(result)
    if isinstance(result, list) and result:
        first = result[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            folded = _fold(text)
            if folded != text:
                try:
                    return [first.model_copy(update={"text": folded})] + result[1:]
                except Exception:  # noqa: BLE001 — never break the response
                    return result
    return result


def install_validation_guard(mcp: Any) -> None:
    """Wrap ``ToolManager.call_tool`` so a ValidationError becomes an envelope.

    Install BEFORE ``install_audit_hook`` so the audit wrapper sits outside
    this one: the returned ``{"error": ...}`` is then classified by
    ``_classify_response`` as an error, logged, and counted by the circuit
    breaker. Reversed, these failures would vanish from the audit log.
    """
    tool_manager = mcp._tool_manager
    original_call_tool = tool_manager.call_tool

    async def guarded_call_tool(name: str, arguments: dict, **kwargs):
        args = arguments or {}
        notes: dict = {}
        try:
            declared = _declared_types(mcp, name)
            args, notes, env = _screen_arguments(name, args, declared)
            if env is not None:
                return _build_breaker_response(
                    json.dumps(env), bool(kwargs.get("convert_result")))
        except Exception:  # noqa: BLE001
            # The screening pass now runs on EVERY call, not just the error
            # path. A bug in it must never be able to take the server down —
            # fall through to the unmodified arguments (today's behavior).
            logger.exception("argument screening failed for %s; passing through", name)
            # ...but say so. A silent fallback degrades straight back to the
            # drop behaviour this guard exists to kill, invisibly.
            args = arguments or {}
            notes = {"arg_guard_unavailable":
                     f"{name}: argument screening failed; arguments were "
                     "passed through unvalidated."}

        token = set_arg_notes(notes) if notes else None
        try:
            result = await original_call_tool(name, args, **kwargs)
            # Applied to EVERY tool, epicor_read included: read folds these
            # into resolved.assumptions on its main path, but its recognizer
            # routes (time phase, BOM, where-used, ...) return before that
            # happens, so a guard-aliased `select` was dropped a second time
            # with no note. Belt and braces beats a silent gap.
            return _inject_notes(result, notes)
        except ToolError as exc:
            cause = exc.__cause__
            if not isinstance(cause, ValidationError):
                # A genuine crash inside the tool body. Swallowing it into a
                # fake 'invalid_argument_type' would be a silent wrong guess —
                # the one outcome worse than a precise error.
                raise
            # `or {}` is load-bearing: _declared_types now returns None for an
            # unresolvable tool, and _validation_envelope indexes it as a dict.
            # An AttributeError here would be swallowed by the caller and
            # silently restore passthrough — masking the very bug being fixed.
            env = _validation_envelope(
                cause, args, _declared_types(mcp, name) or {})
            return _build_breaker_response(
                json.dumps(env), bool(kwargs.get("convert_result")))
        finally:
            # A leaked note would attach a FABRICATED assumption to an
            # unrelated later call — worse than no note at all.
            if token is not None:
                clear_arg_notes(token)

    tool_manager.call_tool = guarded_call_tool
    logger.info("Argument-validation guard installed on ToolManager.call_tool")

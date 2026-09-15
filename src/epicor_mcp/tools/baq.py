"""Tool: epicor_baq — the one BAQ tool (legacy engine).

Absorbs the earlier baq_search / baq_create / baq_delete / baq_schema / table_lookup tools.

BAQ SQL is **SCHEMA-SERVED, not SQL-demanded**: for ``action="create"`` the
model states ``tables`` + ``fields`` + ``where`` in *business* terms and this
tool resolves the real ``Schema.Table`` / column names via ``baq_index``,
composes valid Epicor BAQ SQL (aliasing every field, adding Company-first
joins), then creates and runs it. The model never hand-writes the strict
dialect that produced the earlier tools' 500s. ``run`` / ``find`` / ``schema`` / ``delete``
reuse the legacy engine primitives; ``dashboard`` reuses the legacy
``epicor_dashboard_baq`` engine (captured at register time, the same shim
pattern ``read.py`` uses for ``query_with_children``). BAQ ids are
AUTO-prefixed and truncated to 25 chars (never an error). Every
resolution/validation failure returns the uniform INV-1 ``error_envelope``
with the *valid* table/column names inline. Saved BAQs cannot be *searched*:
``find`` searches data-dictionary tables (authoring), not saved BAQs, and is
gated off together with ``create``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from epicor_mcp.config import get_settings
from epicor_mcp.context import get_current_session
from epicor_mcp.epicor_client.error_handler import EpicorError, ErrorHandler
from epicor_mcp.response import format_response
from epicor_mcp.tools import dashboard_baq as _dash
from epicor_mcp.tools._baq_helpers import create_baq, run_baq as run_baq_impl
from epicor_mcp.tools._inline_schema import parse_order_by
from epicor_mcp.tools._resolve import coerce_csv, error_envelope
# Single-sourced from the read path's translator so `epicor_baq` and
# `epicor_read` agree on what an arithmetic filter means.
from epicor_mcp.tools.query import (
    _arith_to_odata as _q_arith_to_odata,
    _outside_quotes as _q_outside_quotes,
)

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.dataset_handler import DatasetHandler
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_DESCRIPTION = (
    "Run a saved Epicor BAQ or dashboard the USER named. `action` is 'run' | "
    "'dashboard' | 'find' | 'schema'. 'run': execute a saved BAQ whose EXACT id "
    "the user gave (`baq`=id; page with the opaque `cursor`). If the BAQ takes "
    "execution parameters, pass them as `params` (JSON object keyed by "
    "parameter name, dates YYYY-MM-DD) — `where` canNOT supply them; "
    "action='schema' with `baq`=id lists a saved BAQ's parameters and result "
    "columns. 'dashboard': the "
    "user told you to use/look at a dashboard — set `baq` to the dashboard "
    "name and it resolves the BAQ(s) behind it and runs them in one call; "
    "call it with NO `baq` to LIST the available dashboards when the user "
    "didn't name one. Any time the user says 'dashboard', use "
    "action='dashboard' — never action='run' with a dashboard name. "
    "'find'/'schema' with `tables` search data-dictionary "
    "TABLES/columns for BAQ "
    "AUTHORING only (useless unless create is enabled — they do NOT search "
    "saved BAQs). NEVER call this tool to answer a general data question — "
    "that is epicor_read ('top N by X', counts, lookups). Saved BAQs canNOT be "
    "discovered by searching or guessing names: if the user did not give you a "
    "BAQ id or dashboard name, do not invent one — use epicor_read. "
    "There is no `query` parameter — you never write SQL here."
)

# Shown instead of _DESCRIPTION when EPICOR_MCP_ENABLE_BAQ_CREATE is on. The
# description is the model's ONLY window into what this tool can do — if
# create/delete aren't advertised here, the model will (correctly, from its
# view) tell the user it cannot write BAQs even though the gate is open.
_DESCRIPTION_AUTHORING = (
    "Run a saved Epicor BAQ or dashboard, or CREATE one (BAQ authoring is "
    "enabled for you). `action` is 'run' | 'dashboard' | 'find' | 'schema' | "
    "'create' | 'delete'. 'create': you NEVER write SQL — pass `tables` "
    "(business terms or Schema.Table names), optional `fields`, and put ALL "
    "filter criteria in `where` (SQL-ish business terms, e.g. "
    "\"LaborDtl.ApprovedBy = 'service-account' and LaborDtl.ClockOutDate >= "
    "'2026-06-01'\"); the server resolves real tables/columns, composes and "
    "saves the SQL (id auto-prefixed 'AUTO-'), and runs it immediately. Do "
    "NOT create an unfiltered BAQ planning to filter at run time — bake the "
    "criteria into `where` at create. To COUNT/SUM/AVG/MIN/MAX, put the "
    "aggregate right in `fields` as a function call — e.g. fields=\"BuyerID, "
    "count(PONum)\" gives POs-per-buyer; a trailing 'as Name' is honored "
    "(fields=\"BuyerID, sum(OrderQty) as TotalQty\"); every plain field "
    "alongside an aggregate auto-becomes the GROUP BY (fields=\"count(*)\" "
    "alone is a grand total). A computed column is a parenthesised expression: "
    "fields=\"PartNum, (OnHandQty * AvgCost) as InventoryValue\" (it is NOT "
    "added to the GROUP BY). Use this for 'how many / total / average per X' "
    "asks. To group by a time PERIOD (by month/quarter/year) prefer epicor_read "
    "with group_by=\"month(OrderDate), ...\" — it buckets dates and auto-joins "
    "header/detail with no BAQ; the BAQ composer groups by raw columns only. "
    "Quality "
    "questions usually span several tables — DMRs typically need DMRHead + "
    "DMRActn (+ LaborDtl for the inspector's labor) together in `tables`. "
    "'run': execute a saved BAQ by EXACT id "
    "(`baq`=id; page with the opaque `cursor`; optional `where` narrows the "
    "run and accepts the same SQL-ish syntax, auto-translated to OData over "
    "the BAQ's Alias_Field result columns; if the BAQ takes execution "
    "parameters pass them as `params` — a JSON object keyed by parameter "
    "name, dates YYYY-MM-DD — `where` canNOT supply them, and "
    "action='schema' with `baq`=id lists them). 'dashboard': the user told "
    "you to use/look at a dashboard — `baq`=dashboard name resolves the "
    "BAQ(s) behind it and runs them; NO `baq` LISTS the available dashboards. "
    "Any time the user says 'dashboard', use action='dashboard', never "
    "action='run' with a dashboard name. "
    "'find'/'schema': search data-dictionary tables/columns to prepare a "
    "create — they do NOT search saved BAQs, and saved BAQ ids canNOT be "
    "discovered by searching or guessing. 'delete': remove an AUTO- BAQ "
    "(`baq`=id). For a simple data question ('top N by X', counts, lookups) "
    "still prefer epicor_read; reach for 'create' when the user asks for a "
    "BAQ or the answer genuinely needs a multi-table join. There is no "
    "`query` parameter — you NEVER write SQL — and no `group_by`/`aggregate`: "
    "both live in `fields`. `description` (free text) and `order_by` "
    "(\"Col desc\", or an output alias from `fields`) ARE real parameters; "
    "order_by also applies to action='run'."
)

# ---------------------------------------------------------------------------
# Join-key + column heuristics for schema-served SQL composition
# ---------------------------------------------------------------------------

# Real Epicor primary/foreign key column names. A column shared (case-
# insensitively) by two tables and present here is treated as a join key.
# ``Company`` is always a join key (tenant partition) and is added separately.
_STRONG_KEYS: set[str] = {
    "company", "ponum", "poline", "porelnum", "ordernum", "orderline",
    "orderrelnum", "jobnum", "assemblyseq", "oprseq", "mtlseq", "partnum",
    "vendornum", "vendorpp", "purpoint", "custnum", "shiptonum", "shiptocustnum",
    "invoicenum", "invoiceline", "vendorinvoicenum", "packnum", "packline",
    "rmanum", "rmaline", "quotenum", "quoteline", "contractnum", "contractid",
    "groupid", "apinvhedseq", "headnum", "trannum", "receiptnum", "conname",
    "connum", "plant", "warehousecode", "binnum", "lotnum",
}

# Document-identity chains, most-specific first. If a chain's base key is
# shared by two tables, the join uses Company + the chain's shared members
# alone — see _shared_join_keys.
_KEY_CHAINS: list[tuple[str, ...]] = [
    ("jobnum", "assemblyseq", "oprseq", "mtlseq"),
    ("ponum", "poline", "porelnum"),
    ("ordernum", "orderline", "orderrelnum"),
    ("quotenum", "quoteline"),
    ("invoicenum", "invoiceline"),
    ("packnum", "packline"),
    ("rmanum", "rmaline"),
    ("apinvhedseq",),
    ("headnum",),
    ("groupid",),
    ("trannum",),
    ("receiptnum",),
    ("contractnum",),
    ("vendornum", "purpoint", "vendorpp"),
    ("custnum", "shiptonum", "shiptocustnum"),
    ("partnum", "plant", "warehousecode", "binnum", "lotnum"),
]

# System / housekeeping columns that must never be used as join keys even
# though they may end in "id"/"num" and appear on every table.
_SYSTEM_COLS: set[str] = {
    "sysrevid", "sysrowid", "bitflag", "rowmod", "glbcompany", "extcompany",
    "edilog", "changedby", "createdby", "lastchangedby", "foreignsysrowid",
}

# Fields worth surfacing when the caller does not name any (blank ``fields``).
_INTEREST_RE = re.compile(
    r"(num|number|date|name|desc|qty|quantity|amt|amount|total|price|cost|"
    r"status|code|part|line|open|due)",
    re.IGNORECASE,
)

# where-clause operator translation: OData-ish / word ops -> BAQ SQL ops.
_OP_MAP = {
    "eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<=",
    "=": "=", "<>": "<>", "!=": "<>", ">": ">", "<": "<", ">=": ">=",
    "<=": "<=", "like": "like",
}

_COND_RE = re.compile(
    r"^\s*(?P<field>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)\s+"
    r"(?P<op>=|<>|!=|>=|<=|>|<|eq|ne|gt|ge|lt|le|like)\s+"
    r"(?P<value>.+?)\s*$",
    re.IGNORECASE,
)

# Aggregate field term: count(*) / count() / count(PONum) / sum(OrderQty) /
# avg(POHeader.DocTotalOrder) / count(distinct VendorNum). The inner arg is
# optional (bare ``count()`` == ``count(*)``). Function synonyms are folded to
# the canonical SQL name in _AGG_FN_SYNONYMS. This is how the model expresses
# "how many POs per buyer" (fields="BuyerID, count(PONum)") — non-aggregate
# fields alongside an aggregate become the GROUP BY.
_AGG_RE = re.compile(
    r"^\s*(?P<fn>count|cnt|sum|total|avg|average|mean|min|minimum|max|maximum)"
    r"\s*\(\s*(?P<distinct>distinct\s+)?"
    r"(?P<arg>\*|[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)?\s*\)\s*$",
    re.IGNORECASE,
)
_AGG_FN_SYNONYMS = {
    "count": "count", "cnt": "count",
    "sum": "sum", "total": "sum",
    "avg": "avg", "average": "avg", "mean": "avg",
    "min": "min", "minimum": "min",
    "max": "max", "maximum": "max",
}

# Split a where string on top-level ``and`` / ``or`` (no paren nesting).
_CONNECTOR_RE = re.compile(r"\s+(and|or)\s+", re.IGNORECASE)

_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")


# ---------------------------------------------------------------------------
# Table / column resolution
# ---------------------------------------------------------------------------

def _table_columns(baq_index: "BAQSchemaIndex", full_name: str) -> dict[str, str]:
    """Return ``{lowercase_name: real_name}`` for every column on *full_name*."""
    out: dict[str, str] = {}
    try:
        for f in baq_index.get_fields(full_name):
            name = f.get("field_name") or ""
            if name:
                out.setdefault(name.lower(), name)
    except Exception:
        logger.debug("get_fields failed for %s", full_name, exc_info=True)
    return out


def _resolve_tables(
    baq_index: "BAQSchemaIndex", tables_raw: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve business table terms to real ``Schema.Table`` definitions.

    Returns ``(resolved, unresolved, ambiguous)``. Each resolved entry is
    ``{full_name, table_name, alias, cols}``; each unresolved entry is
    ``{term, suggestions:[full_name...]}``; each ambiguous entry is
    ``{term, candidates:[full_name...]}`` -- all for the INV-1 envelope.

    Confidence gate: a term resolves confidently only when it is an explicit
    ``Schema.Table`` dictionary hit, its top search hit's ``table_name`` /
    ``full_name`` *equals* the term (case-insensitively), or it produced a
    single search hit. When the top hit is a weak FTS/prefix match with other
    comparable candidates, the term is flagged ``ambiguous`` (candidates
    surfaced) instead of silently collapsing to ``hits[0]``.
    """
    terms = [t.strip() for t in tables_raw.split(",") if t.strip()]
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    used_aliases: set[str] = set()

    for term in terms:
        info = None
        # Explicit Schema.Table -> exact dictionary hit.
        if "." in term and re.match(r"^[A-Za-z_][\w]*\.[A-Za-z_][\w]*$", term):
            info = baq_index.get_table(term)
        if info is None:
            hits = baq_index.search_tables(term, limit=5)
            if not hits:
                unresolved.append({"term": term, "suggestions": []})
                continue
            top = hits[0]
            tl = term.lower()
            exact = (
                (top.get("table_name") or "").lower() == tl
                or (top.get("full_name") or "").lower() == tl
            )
            if exact or len(hits) == 1:
                info = top
            else:
                # Weak/ambiguous: multiple comparable hits, none exact.
                ambiguous.append({
                    "term": term,
                    "candidates": [h.get("full_name") for h in hits],
                })
                continue

        full_name = info.get("full_name") or ""
        table_name = info.get("table_name") or full_name.split(".")[-1]
        alias = table_name
        n = 2
        while alias.lower() in used_aliases:
            alias = f"{table_name}{n}"
            n += 1
        used_aliases.add(alias.lower())
        resolved.append({
            "full_name": full_name,
            "table_name": table_name,
            "alias": alias,
            "cols": _table_columns(baq_index, full_name),
        })
    return resolved, unresolved, ambiguous


def _shared_join_keys(
    t1: dict[str, Any], t2: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return join key column pairs ``(t1_real, t2_real)`` shared by two tables.

    A shared column qualifies when it is ``Company`` or a recognised strong
    key and is not a system/housekeeping column. Each table's own casing is
    preserved (Epicor mixes ``PONum`` / ``PONUM``).
    """
    c1, c2 = t1["cols"], t2["cols"]
    shared = set(c1) & set(c2)
    pairs: list[tuple[str, str]] = []
    if "company" in shared:
        pairs.append((c1["company"], c2["company"]))
    shared_keys = sorted(
        low for low in shared
        if low != "company" and low not in _SYSTEM_COLS and low in _STRONG_KEYS
    )
    # When a document-identity chain's BASE key is shared, join on Company +
    # that chain's shared members ONLY. Folding in every coincidentally-
    # shared strong key over-constrains the join and silently empties it:
    # JobOper<->JobHead share PartNum and ContractID, but JobOper.PartNum is
    # the op-level part, not JobHead's end part, so that join can lose valid rows.
    for chain in _KEY_CHAINS:
        if chain[0] in shared_keys:
            pairs.extend(
                (c1[k], c2[k]) for k in chain if k in shared_keys
            )
            return pairs
    for low in shared_keys:
        pairs.append((c1[low], c2[low]))
    return pairs


def _resolve_one_field(
    resolved: list[dict[str, Any]], name: str,
) -> tuple[str, str] | None:
    """Resolve one bare/qualified field term to ``(alias, real_column)``.

    Exact case-insensitive match only (qualified ``Table.Field`` first, then a
    bare field across tables). Returns ``None`` when nothing matches — the
    caller reports it as unknown rather than fuzzy-substituting.
    """
    if "." in name:
        tname, fname = name.split(".", 1)
        tl, fl = tname.lower(), fname.lower()
        hit = next(
            (t for t in resolved
             if t["alias"].lower() == tl or t["table_name"].lower() == tl),
            None,
        )
        if hit and fl in hit["cols"]:
            return (hit["alias"], hit["cols"][fl])
        return None
    low = name.lower()
    hit = next((t for t in resolved if low in t["cols"]), None)
    if hit is not None:
        return (hit["alias"], hit["cols"][low])
    return None


# A trailing "as <alias>" on an aggregate term (sum(OrderQty) as TotalQty). The
# model writes this by reflex — and epicor_read's own error message tells it to
# — so the BAQ composer must accept it instead of treating the whole string as
# an unknown column (the regression that made every aliased aggregate fail).
_AGG_ALIAS_RE = re.compile(
    r"^(?P<core>.+?)\s+as\s+(?P<alias>[A-Za-z_][\w]*)\s*$", re.IGNORECASE)


def _split_agg_alias(term: str) -> tuple[str, str | None]:
    """Peel a trailing ``as <alias>`` off *term*.

    Returns ``(core, alias)``; ``alias`` is ``None`` when there is no ``as``
    clause. Only meaningful for aggregate terms — a plain field is resolved
    from the ORIGINAL term by the caller, so a stray ``as`` there is harmless.
    """
    m = _AGG_ALIAS_RE.match(term.strip())
    if m:
        return m.group("core").strip(), m.group("alias")
    return term.strip(), None


def _parse_agg(term: str) -> tuple[str, bool, str] | None:
    """Parse an aggregate field term into ``(fn, distinct, arg)``.

    ``fn`` is the canonical SQL function (count/sum/avg/min/max); ``arg`` is
    ``"*"`` for a row count or a column reference. Returns ``None`` when the
    term is not an aggregate call. ``count()`` / ``count(*)`` normalise to
    ``("count", False, "*")``.
    """
    m = _AGG_RE.match(term)
    if not m:
        return None
    fn = _AGG_FN_SYNONYMS.get(m.group("fn").lower())
    if fn is None:
        return None
    arg = (m.group("arg") or "").strip()
    distinct = bool(m.group("distinct"))
    if arg in ("", "*"):
        # Only count is meaningful without a column; sum(*)/avg(*) are nonsense.
        return (fn, False, "*") if fn == "count" else None
    return (fn, distinct, arg)


# Restricted arithmetic: identifiers, numbers, * / + - and parens. Anything
# else is not an expression and falls through to the unknown-column envelope.
_EXPR_OK_RE = re.compile(r"^[A-Za-z_0-9.\s*/+\-()]+$")
# An arithmetic condition: `OnHandQty * AvgCost > 50000`.
_EXPR_COND_RE = re.compile(
    r"^\s*(?P<lhs>[A-Za-z_0-9.\s*/+\-()]*[*/+\-][A-Za-z_0-9.\s*/+\-()]*?)\s*"
    r"(?P<op>>=|<=|<>|!=|=|>|<)\s*(?P<value>.+?)\s*$")
_EXPR_IDENT_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?")


def _parse_expr_field(
    core: str, resolved: list[dict[str, Any]],
) -> tuple[str | None, list[str]]:
    """Qualify every identifier in an arithmetic expression.

    ``(OnHandQty * AvgCost)`` -> ``([pw].[OnHandQty] * [p].[AvgCost])``.
    Returns ``(sql, unknown_names)``; ``sql`` is None when *core* is not an
    expression at all (no operator) or when a component doesn't resolve —
    in which case ``unknown_names`` carries the misses so the INV-1 envelope
    can still hand back the real column names.
    """
    core = (core or "").strip()
    if not core or not _EXPR_OK_RE.match(core):
        return None, []
    if not any(op in core for op in "*/+-"):
        return None, []  # a plain field the caller already tried

    # `_compose_baq_sql` wraps the result in its own parens, so a caller who
    # already wrote "(a * b)" got "((a * b))" in the stored Formula. Harmless
    # (depth 41 still parsed) but noisy in the Designer — strip exactly one
    # REDUNDANT balanced outer pair, and only when removal leaves the whole
    # expression balanced, so "(A + B) * C" is never mangled.
    if core.startswith("(") and core.endswith(")"):
        depth = 0
        for i, ch in enumerate(core):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and i < len(core) - 1:
                break
        else:
            core = core[1:-1].strip()

    unknown: list[str] = []
    out_parts: list[str] = []
    pos = 0
    for m in _EXPR_IDENT_RE.finditer(core):
        out_parts.append(core[pos:m.start()])
        hit = _resolve_one_field(resolved, m.group(0))
        if hit is None:
            unknown.append(m.group(0))
        else:
            alias, real = hit
            out_parts.append(f"[{alias}].[{real}]")
        pos = m.end()
    out_parts.append(core[pos:])
    if unknown:
        return None, unknown
    return "".join(out_parts), []


def _resolve_fields(
    resolved: list[dict[str, Any]], fields_raw: str,
) -> tuple[
    list[tuple[str, str]], list[str], list[str], dict[str, list[str]],
    list[dict[str, Any]],
]:
    """Back-compat 5-tuple view of :func:`_resolve_fields_ex`.

    Computed columns are a later addition and live in a SIXTH slot; this
    wrapper keeps the original arity for callers that don't compose SQL.
    """
    return _resolve_fields_ex(resolved, fields_raw)[:5]


def _resolve_fields_ex(
    resolved: list[dict[str, Any]], fields_raw: str,
) -> tuple[
    list[tuple[str, str]], list[str], list[str], dict[str, list[str]],
    list[dict[str, Any]], list[dict[str, Any]],
]:
    """Resolve business field terms to selections + aggregates.

    Returns ``(selected, unknown, valid_columns, did_you_mean, aggregates)``.
    ``selected`` is the plain ``(alias, real_column)`` list; when ``aggregates``
    is non-empty those same plain fields become the GROUP BY. Each aggregate is
    ``{fn, alias, real, distinct, out}`` (``real``/``alias`` are ``None`` for
    ``count(*)``). ``valid_columns`` is the ``Alias.Column`` union across the
    resolved tables (INV-1 ``valid.columns``); ``did_you_mean`` maps each
    *unknown* requested term to its closest real column names.

    STRICT resolution: a requested field is accepted only on an exact
    (case-insensitive) bare or qualified ``Table.Field`` match. A term with no
    exact match is NOT silently fuzzy-substituted (that produced semantically
    wrong BAQs, e.g. ``PoNumber`` -> ``LegalNumber``); it is reported as
    ``unknown`` with close-match hints in ``did_you_mean`` instead.
    """
    import difflib

    valid_columns = [
        f"{t['alias']}.{real}"
        for t in resolved for real in t["cols"].values()
    ]
    # Bare real column names across every resolved table, for did_you_mean.
    all_real: list[str] = [real for t in resolved for real in t["cols"].values()]
    real_by_lower: dict[str, str] = {c.lower(): c for c in all_real}

    def _did_you_mean(term: str) -> list[str]:
        # Match on the field part only when the term is qualified.
        needle = term.split(".", 1)[1] if "." in term else term
        low = needle.lower()
        close = difflib.get_close_matches(
            low, list(real_by_lower.keys()), n=5, cutoff=0.5,
        )
        hints = [real_by_lower[c] for c in close]
        if not hints:
            hints = [c for c in all_real if low in c.lower() or c.lower() in low][:5]
        return hints

    if not fields_raw or not fields_raw.strip():
        selected: list[tuple[str, str]] = []
        for t in resolved:
            picked = 0
            # Always include the table's Company + strong keys first.
            for low, real in t["cols"].items():
                if low == "company" or low in _STRONG_KEYS:
                    selected.append((t["alias"], real))
            for low, real in t["cols"].items():
                if low in _SYSTEM_COLS or low == "company" or low in _STRONG_KEYS:
                    continue
                if _INTEREST_RE.search(low) and picked < 8:
                    selected.append((t["alias"], real))
                    picked += 1
        # Dedupe preserving order.
        seen: set[tuple[str, str]] = set()
        selected = [s for s in selected if not (s in seen or seen.add(s))]
        return selected, [], valid_columns, {}, [], []

    requested = [f.strip() for f in fields_raw.split(",") if f.strip()]
    selected = []
    aggregates: list[dict[str, Any]] = []
    computed: list[dict[str, Any]] = []
    unknown: list[str] = []
    did_you_mean: dict[str, list[str]] = {}
    used_out: set[str] = set()

    def _uniq_out(base: str) -> str:
        out = base
        n = 2
        while out.lower() in used_out:
            out = f"{base}{n}"
            n += 1
        used_out.add(out.lower())
        return out

    for term in requested:
        core, user_alias = _split_agg_alias(term)
        agg = _parse_agg(core)
        if agg is not None:
            fn, distinct, arg = agg
            if arg == "*":
                aggregates.append({
                    "fn": "count", "alias": None, "real": None,
                    "distinct": False,
                    "out": _uniq_out(user_alias or "Count_All"),
                })
                continue
            hit = _resolve_one_field(resolved, arg)
            if hit is None:
                unknown.append(term)
                did_you_mean[term] = _did_you_mean(arg)
                continue
            alias, real = hit
            label = ("CountDistinct" if (fn == "count" and distinct)
                     else fn.capitalize())
            aggregates.append({
                "fn": fn, "alias": alias, "real": real, "distinct": distinct,
                "out": _uniq_out(user_alias or f"{label}_{real}"),
            })
            continue

        # Not an aggregate: resolve the alias-stripped CORE. Resolving `term`
        # (alias still attached) could never match a column name, so
        # `fields="PartNum as P"` always landed in `unknown` — and so did every
        # computed term, since _parse_agg rejects `(A * B)`.
        hit = _resolve_one_field(resolved, core)
        if hit is not None:
            selected.append(hit)
            continue

        # Arithmetic expression: (OnHandQty * AvgCost) as InventoryValue.
        # Its own list — `selected` doubles as the GROUP BY in
        # _compose_baq_sql, so folding a computed column in there would give
        # every computed BAQ a bogus grouping key and wrong row counts.
        expr_sql, expr_unknown = _parse_expr_field(core, resolved)
        if expr_sql is not None:
            computed.append({
                "sql": expr_sql,
                "out": _uniq_out(user_alias or "Computed"),
            })
            continue
        if expr_unknown:
            for name in expr_unknown:
                unknown.append(name)
                did_you_mean[name] = _did_you_mean(name)
            continue

        # No exact match -> report as unknown with correction hints. Do NOT
        # fuzzy-substitute: a close-but-wrong real column silently produces a
        # semantically incorrect BAQ.
        unknown.append(term)
        did_you_mean[term] = _did_you_mean(term)

    seen2: set[tuple[str, str]] = set()
    selected = [s for s in selected if not (s in seen2 or seen2.add(s))]
    return selected, unknown, valid_columns, did_you_mean, aggregates, computed


def _translate_where(
    where: str, resolved: list[dict[str, Any]],
) -> tuple[str, list[str], list[str]]:
    """Translate a business/OData-ish ``where`` into a BAQ SQL WHERE body.

    Field names are resolved to ``[Alias].[Column]`` (correct casing) and
    operators mapped to SQL. Returns ``(sql_where, unknown_fields, unparsed)``.
    Unknown fields feed the INV-1 envelope; ``unparsed`` lists fragments that
    were passed through VERBATIM (and therefore unqualified) so the caller can
    name them as the likely cause of an ``epicor_rejected_sql``, rather than
    letting a silent pass-through 500 with no diagnosis.
    """
    where = (where or "").strip()
    if not where:
        return "", [], []

    def resolve_ref(name: str) -> str | None:
        if "." in name:
            tname, fname = name.split(".", 1)
            tl, fl = tname.lower(), fname.lower()
            hit = next(
                (t for t in resolved
                 if t["alias"].lower() == tl or t["table_name"].lower() == tl),
                None,
            )
            if hit and fl in hit["cols"]:
                return f"[{hit['alias']}].[{hit['cols'][fl]}]"
            return None
        low = name.lower()
        hit = next((t for t in resolved if low in t["cols"]), None)
        if hit is not None:
            return f"[{hit['alias']}].[{hit['cols'][low]}]"
        return None

    parts = _CONNECTOR_RE.split(where)
    out: list[str] = []
    unknown: list[str] = []
    unparsed: list[str] = []
    # parts alternates: cond, connector, cond, connector, ...
    for i, chunk in enumerate(parts):
        if i % 2 == 1:
            out.append(chunk.lower())  # and / or
            continue
        m = _COND_RE.match(chunk)
        if not m:
            # An arithmetic condition (`OnHandQty * AvgCost > 50000`) doesn't
            # match _COND_RE, and passing it through verbatim emits UNQUALIFIED
            # column names into the composed SQL -> epicor_rejected_sql with no
            # diagnosis. Try to qualify the left-hand side first.
            expr_m = _EXPR_COND_RE.match(chunk.strip())
            if expr_m:
                expr_sql, expr_unknown = _parse_expr_field(
                    expr_m.group("lhs"), resolved)
                if expr_sql is not None:
                    sql_op = _OP_MAP.get(
                        expr_m.group("op").lower(), expr_m.group("op"))
                    out.append(
                        f"({expr_sql}) {sql_op} {expr_m.group('value').strip()}")
                    continue
                unknown.extend(expr_unknown)
                if expr_unknown:
                    out.append(chunk.strip())
                    continue
            # Still unparseable — pass through verbatim rather than block, but
            # RECORD it so the failure can be explained.
            unparsed.append(chunk.strip())
            out.append(chunk.strip())
            continue
        ref = resolve_ref(m.group("field"))
        if ref is None:
            unknown.append(m.group("field"))
            out.append(chunk.strip())
            continue
        sql_op = _OP_MAP.get(m.group("op").lower(), m.group("op"))
        out.append(f"{ref} {sql_op} {m.group('value').strip()}")
    return " ".join(out), unknown, unparsed


# BAQ run filters go to OData ($filter on BaqSvc/<id>/Data) where columns are
# the composed result aliases (Alias_Field, e.g. LaborDtl_ApprovedBy) and the
# operators are OData words. Models reliably send SQL-ish filters instead
# ("LaborDtl.ApprovedBy = 'service-account' AND OpComplete = true"), so translate best-
# effort rather than bounce them through a syntax-error loop.
_ODATA_OP_MAP = {
    "=": "eq", "==": "eq", "eq": "eq", "<>": "ne", "!=": "ne", "ne": "ne",
    ">": "gt", "gt": "gt", ">=": "ge", "ge": "ge",
    "<": "lt", "lt": "lt", "<=": "le", "le": "le",
}

_ODATA_COND_RE = re.compile(
    r"^\s*(?P<field>\[?[A-Za-z_]\w*\]?(?:\.\[?[A-Za-z_]\w*\]?)?)\s*"
    r"(?P<op>>=|<=|<>|!=|==|=|>|<|eq|ne|gt|ge|lt|le|like)\s+"
    r"(?P<value>.+?)\s*$",
    re.IGNORECASE,
)

# ISO date/datetime literal, quoted or bare. BAQ result date columns are
# Edm.DateTimeOffset, and BaqSvc accepts ONLY the full unquoted
# YYYY-MM-DDT00:00:00Z form (bare '2026-06-01' ->
# 400 empty "BAQ execution failed", quoted -> 400 type mismatch, Z-form -> 200).
_ISO_DATE_RE = re.compile(r"^'?(\d{4}-\d{2}-\d{2})([T ][\d:.]+Z?)?'?$")


# Arithmetic outside quotes, e.g. `OnHandQty * AvgCost`. `_ODATA_COND_RE`
# cannot parse an expression on the left of the comparator, so such a chunk
# used to pass through VERBATIM and Epicor rejected the $filter as a syntax
# error — while epicor_read's own translator handles it. A model that follows
# the read envelope's advice to pivot to epicor_baq for an inventory-value
# filter hit the same wall it had just escaped.
_BAQ_ARITH_RE = re.compile(r"[\w)]\s*[*/%+]\s*[\w(]|[\w)]\s+-\s+[\w(]")


def _baq_arith_condition(chunk: str) -> str:
    """Translate a comparator chunk whose LHS is an arithmetic expression.

    Deliberately narrow: fires only when arithmetic is actually present
    outside quotes, so already-valid OData (``contains(PartNum,'A-B')``,
    parenthesised groups) and part numbers stay untouched.
    """
    stripped = chunk.strip()
    if not _q_outside_quotes(
            stripped, lambda seg: "\x01" if _BAQ_ARITH_RE.search(seg) else ""):
        return stripped

    def _ops(seg: str) -> str:
        seg = seg.replace("<>", " ne ").replace("!=", " ne ")
        seg = seg.replace(">=", " ge ").replace("<=", " le ")
        seg = re.sub(r"\s*>\s*", " gt ", seg)
        seg = re.sub(r"\s*<\s*", " lt ", seg)
        seg = re.sub(r"\s*=\s*", " eq ", seg)
        return seg

    out = _q_outside_quotes(_q_arith_to_odata(stripped), _ops)
    return re.sub(r"\s+", " ", out).strip()


def _baq_filter_to_odata(where: str) -> str:
    """Best-effort translation of a SQL-ish ``where`` into an OData $filter.

    Handles: ``A.B``/``[A].[B]`` -> ``A_B`` column refs, SQL comparison
    operators -> OData words, quoted ISO dates -> unquoted literals,
    ``like`` with %-wildcards -> contains/startswith/endswith, and
    AND/OR -> lowercase. Fragments that don't parse (parens, functions,
    already-valid OData) pass through verbatim, so valid input is a no-op.
    """
    where = (where or "").strip()
    if not where:
        return ""

    parts = _CONNECTOR_RE.split(where)
    out: list[str] = []
    for i, chunk in enumerate(parts):
        if i % 2 == 1:
            out.append(chunk.lower())  # and / or
            continue
        m = _ODATA_COND_RE.match(chunk)
        if not m:
            out.append(_baq_arith_condition(chunk))
            continue
        field = m.group("field").replace("[", "").replace("]", "")
        field = field.replace(".", "_")
        op = m.group("op").lower()
        value = m.group("value").strip()

        date_m = _ISO_DATE_RE.match(value)
        if date_m:
            day = date_m.group(1)
            time_part = (date_m.group(2) or "").replace(" ", "").lstrip("T")
            if not time_part:
                time_part = "00:00:00"
            if not time_part.endswith("Z"):
                time_part += "Z"
            value = f"{day}T{time_part}"
        elif value.lower() in ("true", "false"):
            value = value.lower()

        if op == "like":
            text = value.strip("'\"")
            if text.startswith("%") and text.endswith("%"):
                out.append(f"contains({field},'{text.strip('%')}')")
            elif text.endswith("%"):
                out.append(f"startswith({field},'{text.rstrip('%')}')")
            elif text.startswith("%"):
                out.append(f"endswith({field},'{text.lstrip('%')}')")
            else:
                out.append(f"{field} eq '{text}'")
            continue

        out.append(f"{field} {_ODATA_OP_MAP.get(op, op)} {value}")
    return " ".join(out)


def _compose_baq_sql(
    resolved: list[dict[str, Any]],
    selected: list[tuple[str, str]],
    where_sql: str,
    aggregates: list[dict[str, Any]] | None = None,
    computed: list[dict[str, Any]] | None = None,
    order_terms: list[tuple[str, str]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Compose Epicor BAQ SQL from resolved tables/fields. Returns (sql, joins).

    ``joins`` is a machine-readable description of every join added, surfaced
    in the response so the caller can see how the tables were linked. When
    ``aggregates`` is non-empty, the plain ``selected`` fields become the
    GROUP BY and each aggregate is emitted as ``fn([Alias].[Col]) as [Out]``
    (Epicor ParseFromSQL accepts count/sum/avg/min/max + GROUP
    BY). A grand-total (aggregates with no plain fields) omits GROUP BY.
    """
    aggregates = aggregates or []
    computed = computed or []
    anchor = resolved[0]
    select_parts = [f"[{a}].[{f}] as [{a}_{f}]" for (a, f) in selected]
    # Computed columns sit between the plain fields and the aggregates, and
    # deliberately never reach the GROUP BY built at the bottom of this
    # function (which groups by RAW `selected` columns only).
    for comp in computed:
        select_parts.append(f"({comp['sql']}) as [{comp['out']}]")
    for agg in aggregates:
        if agg.get("real") is None:
            expr = "count(*)"
        else:
            inner = f"[{agg['alias']}].[{agg['real']}]"
            if agg.get("distinct"):
                inner = "distinct " + inner
            expr = f"{agg['fn']}({inner})"
        select_parts.append(f"{expr} as [{agg['out']}]")
    sql_lines = ["select " + ",\n       ".join(select_parts)]
    sql_lines.append(f"from {anchor['full_name']} as [{anchor['alias']}]")

    join_desc: list[dict[str, Any]] = []
    included = [anchor]
    for tbl in resolved[1:]:
        best_partner = None
        best_keys: list[tuple[str, str]] = []
        for inc in included:
            keys = _shared_join_keys(inc, tbl)
            if len(keys) > len(best_keys):
                best_keys, best_partner = keys, inc
        if best_partner is None or not best_keys:
            # No shared key beyond nothing — anchor on Company if both have it.
            best_partner = anchor
            if "company" in anchor["cols"] and "company" in tbl["cols"]:
                best_keys = [(anchor["cols"]["company"], tbl["cols"]["company"])]
        conds = " and ".join(
            f"[{best_partner['alias']}].[{lk}] = [{tbl['alias']}].[{rk}]"
            for (lk, rk) in best_keys
        ) or "1 = 1"
        sql_lines.append(
            f"inner join {tbl['full_name']} as [{tbl['alias']}] on {conds}"
        )
        join_desc.append({
            "table": tbl["full_name"],
            "joined_to": best_partner["full_name"],
            "on": [f"{lk}={rk}" for (lk, rk) in best_keys],
        })
        included.append(tbl)

    if where_sql:
        sql_lines.append("where " + where_sql)
    if aggregates and selected:
        group_parts = [f"[{a}].[{f}]" for (a, f) in selected]
        sql_lines.append("group by " + ", ".join(group_parts))
    if order_terms:
        # Emits whatever terms it is handed — the SQL-safety filter lives in
        # `_do_create` (`_split_sql_order_terms`), because a bare output alias
        # PARSES here and then 400s on every run of the saved BAQ (of the
        # persisted variants, only the qualified
        # [Table].[Col] form of a REAL column runs). Parse-accepts is NOT
        # run-succeeds.
        sql_lines.append(
            "order by " + ", ".join(f"{expr} {d}" for expr, d in order_terms))
    return "\n".join(sql_lines), join_desc


def _split_sql_order_terms(
    order_terms: list[tuple[str, str]] | None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(safe_for_sql, must_sort_client_side)``.

    A saved BAQ's ``order by`` may reference ONLY ``[Table].[Column]`` of a
    real column. Anything else (a bare aggregate/computed output alias) parses
    fine and then kills the BAQ at run time, so it is peeled off here and
    handled the way ``_posugg`` handles a BO that refuses server-side ordering:
    fetch unordered, sort client-side, say so.
    """
    safe: list[tuple[str, str]] = []
    client: list[tuple[str, str]] = []
    for expr, direction in order_terms or []:
        (safe if "].[" in expr else client).append((expr, direction))
    return safe, client


def _sort_records_by(records: list[dict], terms: list[tuple[str, str]]) -> list[dict]:
    """Stable client-side sort of BAQ result rows by bracketed output aliases.

    None/blank sort LAST in both directions — a mixed None/str Python sort key
    raises TypeError, which would turn a working create into a 500.
    """
    if not isinstance(records, list) or not records or not terms:
        return records
    keys = {str(k).lower(): k for k in (records[0] or {})}
    out = list(records)
    for expr, direction in reversed(terms):
        name = expr.strip("[]").split(".")[-1].strip("[]")
        real = keys.get(name.lower())
        if real is None:
            # Also try the BAQ's Alias_Field shape (Part_ClassID).
            real = next((v for k, v in keys.items()
                         if k.endswith("_" + name.lower())), None)
        if real is None:
            continue
        desc = direction.lower().startswith("desc")
        def _key(row, _r=real):
            v = row.get(_r)
            if v is None or v == "":
                return (1, 0.0, "")
            # Numerics MUST compare numerically. A str() key made every
            # aggregate ranking lexicographic -- 95 > 1000 -- so
            # "sum(POAmt) as Total desc" returned the SMALLEST buyers under
            # an order_note asserting the rows were sorted.
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return (0, float(v), "")
            return (0, 0.0, str(v))
        out.sort(key=_key, reverse=desc)
        if desc:
            # reverse=True would float the blanks to the top; keep them last.
            out = [r for r in out if not (r.get(real) is None or r.get(real) == "")] \
                + [r for r in out if r.get(real) is None or r.get(real) == ""]
    return out


def _make_baq_name(baq: str, resolved: list[dict[str, Any]]) -> str:
    """Derive a sanitized BAQ name (<=25 chars, no AUTO- prefix, no error)."""
    raw = (baq or "").strip()
    if raw.upper().startswith("AUTO-"):
        raw = raw[5:]
    if not raw:
        raw = "-".join(t["table_name"] for t in resolved) or "query"
    raw = _NAME_SANITIZE_RE.sub("-", raw).strip("-_") or "query"
    return raw[:25]


# ---------------------------------------------------------------------------
# Reuse of the legacy dashboard->BAQ engine (captured, NOT re-implemented) — the
# same shim pattern read.py uses to capture query_with_children.
# ---------------------------------------------------------------------------

def _capture_dashboard_fn(index, rbac, client):
    """Register ``epicor_dashboard_baq`` onto a capture shim and return its
    inner coroutine so ``action='dashboard'`` can drive the SAME
    dashboard-resolution + BAQ-execution logic without exposing it as a
    separate MCP tool."""
    captured: dict = {}

    class _Shim:
        def tool(self, *args, **kwargs):
            def deco(fn):
                captured["fn"] = fn
                return fn
            return deco

    try:
        _dash.register(_Shim(), index, rbac, client)
    except Exception:  # pragma: no cover
        logger.exception("failed to capture dashboard_baq engine fn")
    return captured.get("fn")


# ---------------------------------------------------------------------------
# Action bodies (module-level & dependency-injected so they are unit-testable
# without the MCP server / a live Epicor).
# ---------------------------------------------------------------------------

def _resolve_order_terms(
    order_by: str,
    resolved: list[dict[str, Any]],
    selected: list[tuple[str, str]],
    aggregates: list[dict[str, Any]],
    computed: list[dict[str, Any]],
) -> tuple[list[tuple[str, str]], dict | None]:
    """``([(sql_expr, direction)], error_envelope_or_None)`` for `order_by`.

    A term naming an aggregate/computed OUTPUT alias is emitted as ``[Out]``
    (the live parse accepts that form); otherwise it resolves to a real
    ``[Alias].[Col]``. An unresolvable term is an INV-1 error, never dropped.
    """
    terms, kind = parse_order_by(order_by or "")
    if not (order_by or "").strip():
        return [], None
    if kind == "expression" or not terms:
        return [], error_envelope(
            "order_expression_unsupported",
            "order_by takes plain column names only ('Col', 'Col desc') — not "
            "an expression. A saved BAQ's `order by` can reference only a real "
            "[Table].[Column]; name one of those. To rank by a computed "
            "measure, alias it in `fields` (\"sum(OrderQty) as TotalQty\") and "
            "order_by that alias — the tool then sorts the returned rows "
            "client-side and says so, because Epicor cannot sort on it.",
        )
    outs = {a["out"].lower(): a["out"] for a in aggregates}
    outs.update({c["out"].lower(): c["out"] for c in computed})
    by_col: dict[str, str] = {}
    for alias, real in selected:
        by_col.setdefault(real.lower(), f"[{alias}].[{real}]")
    for t in resolved:
        for low, real in t["cols"].items():
            by_col.setdefault(low, f"[{t['alias']}].[{real}]")
    out_terms: list[tuple[str, str]] = []
    unknown: list[str] = []
    for col, direction in terms:
        low = col.split(".")[-1].lower()
        if low in outs:
            out_terms.append((f"[{outs[low]}]", direction))
        elif low in by_col:
            out_terms.append((by_col[low], direction))
        else:
            unknown.append(col)
    if unknown:
        return [], error_envelope(
            "unknown_columns",
            "order_by names column(s) that do not exist on the resolved "
            f"tables: {', '.join(unknown)}. Use a real column or an output "
            "alias from `fields`.",
            valid={"did_you_mean": sorted(by_col)[:24]},
            retry_with={"unknown": unknown},
        )
    return out_terms, None


async def _do_create(
    *, session, rbac, client, baq_index, baq, tables, fields, where, limit,
    description: str = "", order_by: str = "",
) -> str:
    if not tables or not tables.strip():
        return json.dumps(error_envelope(
            "missing_tables",
            "create needs `tables` (business terms or Schema.Table names) so "
            "the BAQ SQL can be composed.",
        ))

    resolved, unresolved, ambiguous = _resolve_tables(baq_index, tables)
    if unresolved:
        return json.dumps(error_envelope(
            "unknown_tables",
            "Some table terms did not resolve. Re-call with names from "
            "`valid.tables` (or use action='find' to search the dictionary).",
            valid={"tables": [
                h.get("full_name")
                for term in [u["term"] for u in unresolved]
                for h in baq_index.search_tables(term, limit=5)
            ][:15]},
            retry_with={"unresolved": [u["term"] for u in unresolved]},
        ))
    if ambiguous:
        return json.dumps(error_envelope(
            "ambiguous_tables",
            "One or more table terms matched multiple tables with no clear "
            "winner. Re-call with a specific Schema.Table name from "
            "`candidates` (or use action='find' to inspect them).",
            candidates=[
                {"term": a["term"], "options": a["candidates"]}
                for a in ambiguous
            ],
            retry_with={"ambiguous": [a["term"] for a in ambiguous]},
        ))
    if not resolved:
        return json.dumps(error_envelope(
            "unknown_tables",
            "No tables resolved from the given terms. Use action='find' to "
            "search the data dictionary for the correct Schema.Table names.",
        ))

    selected, unknown_fields, valid_columns, did_you_mean, aggregates, computed = (
        _resolve_fields_ex(resolved, fields)
    )
    if not (fields or "").strip() and not (where or "").strip():
        # A BAQ with neither a column list nor criteria cannot encode a
        # business question. Blank `fields` means "auto-pick Company + strong
        # keys + up to 8 regex-interesting columns per table", so the
        # absence of both arguments can produce an unfiltered join if the
        # caller's intent arrived in an unsupported argument. This
        # guard must sit ahead of create_baq, which DeleteByID's the target id
        # before parsing — an under-specified retry destroys a good BAQ first.
        # Blank fields WITH a where stays legal: that is a deliberate "give me
        # the useful columns for these rows".
        auto = [real for (_alias, real) in selected]
        return json.dumps(error_envelope(
            "underspecified_create",
            "This create has neither `fields` nor `where`, so it would save an "
            "UNFILTERED join with a guessed column list — almost certainly not "
            "the question you were asked. Name the columns in `fields` "
            "(aggregate function calls allowed) and put ALL criteria in "
            "`where`. If you really do want the auto-picked columns, re-call "
            "with the explicit list in retry_with.fields.",
            valid={"columns": auto[:24], "total_columns": len(valid_columns)},
            retry_with={"action": "create", "baq": baq, "tables": tables,
                        "fields": ", ".join(auto[:24]), "where": ""},
        ))
    if unknown_fields:
        # TIGHT candidate set, not the full multi-table column union (which floods
        # the model and defeats the correction — same lesson as read's column_help).
        # Lead with the per-field did_you_mean hints, then the fields that DID
        # resolve, capped. total_columns tells the model more exist.
        tight: list[str] = []
        for hints in did_you_mean.values():
            for h in hints:
                if h not in tight:
                    tight.append(h)
        for _alias, real in selected:
            if real not in tight and len(tight) < 24:
                tight.append(real)
        return json.dumps(error_envelope(
            "unknown_columns",
            "Some field terms do not exist on the resolved tables. They were "
            "NOT guessed/substituted. Fix EACH using its suggestion in "
            "`valid.did_you_mean` and re-call — don't re-search or re-guess.",
            valid={"did_you_mean": did_you_mean, "columns": tight[:24],
                   "total_columns": len(valid_columns)},
            retry_with={"unknown": unknown_fields},
        ))
    if not selected and not aggregates and not computed:
        return json.dumps(error_envelope(
            "no_fields",
            "No columns were selected. Name the `fields` you want (business "
            "terms are fine) — see `valid.columns`.",
            valid={"columns": valid_columns},
        ))

    where_sql, unknown_where, unparsed_where = _translate_where(where, resolved)
    if unknown_where:
        return json.dumps(error_envelope(
            "unknown_columns",
            "The `where` filter references columns that do not exist on the "
            "resolved tables. Use names from `valid.columns`.",
            valid={"columns": valid_columns},
            retry_with={"unknown": unknown_where},
        ))

    order_terms, order_err = _resolve_order_terms(
        order_by, resolved, selected, aggregates, computed)
    if order_err is not None:
        return json.dumps(order_err)

    # Only [Table].[Col] terms may reach the persisted SQL; an alias/measure
    # term is peeled off and sorted client-side after the run (the _posugg
    # pattern for a backend that refuses the ordering).
    sql_order, client_order = _split_sql_order_terms(order_terms)
    sql, join_desc = _compose_baq_sql(
        resolved, selected, where_sql, aggregates, computed, sql_order)
    baq_name = _make_baq_name(baq, resolved)
    description = (description or "").strip() or (
        "Auto-composed: " + ", ".join(t["full_name"] for t in resolved))

    try:
        create_result = await create_baq(
            session=session, rbac=rbac, client=client,
            baq_name=baq_name, description=description[:250], sql=sql,
            replace=True, baq_index=baq_index,
        )
    except EpicorError as exc:
        # Epicor's BAQ parser 500s on SQL it dislikes (seen with 3-table
        # joins). Surface it as a retryable envelope, not internal_error.
        env = error_envelope(
            "epicor_rejected_sql",
            f"Epicor rejected the composed BAQ SQL: "
            f"{ErrorHandler.format_user_message(exc)}. This is usually the "
            "join shape, not your criteria — retry with fewer tables (drop "
            "the least-essential one) or simpler fields; you can join the "
            "extra data afterwards from the run results.",
            retry_with={"tables": tables, "fields": fields, "where": where},
        )
        env["composed_sql"] = sql
        if unparsed_where:
            # A fragment we could not parse was emitted VERBATIM, so its column
            # names are unqualified — far more likely the cause than the join.
            env["likely_cause"] = (
                "These `where` fragments could not be parsed and were passed "
                "through verbatim (their column names are unqualified, which "
                f"Epicor rejects): {unparsed_where}. Rewrite them as simple "
                "`Column op value` conditions.")
        return json.dumps(env)
    if "error" in create_result:
        # `success` goes AFTER the spread. Spread-last let create_baq's own
        # success:True overwrite the literal False, so a failure reported as a
        # win — see the run-failure branch below.
        return json.dumps({
            **create_result, "success": False,
            "composed_sql": sql, "joins": join_desc,
        }, indent=2)

    query_id = create_result["baq_id"]
    try:
        run_result = await run_baq_impl(
            session=session, rbac=rbac, client=client,
            baq_id=query_id, top=limit,
        )
    except EpicorError as exc:
        # `**create_result` LAST used to restore create_baq's own
        # success:True (_baq_helpers.py), so a BAQ that saved but 400s on
        # every run reported {"success": true, ..., "status_code": 400} and the
        # model handed the user a broken id.
        return json.dumps({
            **create_result,
            "success": False,
            "error": "baq_saved_but_run_failed",
            "composed_sql": sql, "joins": join_desc,
            "run_error": (
                f"BAQ '{query_id}' saved but the run failed: "
                f"{ErrorHandler.format_user_message(exc)}"
            ),
            "status_code": exc.status_code,
            "next_step": (
                f"The composed join or filter may need refinement. Re-call "
                f"create with adjusted tables/fields/where — the same "
                f"tables overwrite '{query_id}' in place, so do NOT delete "
                f"it first."
            ),
        }, indent=2)

    order_note = ""
    if client_order:
        run_result = dict(run_result)
        run_result["records"] = _sort_records_by(
            run_result.get("records"), client_order)
        shown = ", ".join(f"{e.strip('[]')} {d}" for e, d in client_order)
        # Honest about BOTH halves: these rows are sorted, the SAVED BAQ is not
        # — a later action='run' of this id returns them unordered.
        order_note = (
            f"{shown} (client-side: Epicor's BAQ `order by` cannot reference an "
            f"output alias or expression — a BAQ saved with one 400s on every "
            f"run, so it was omitted from the SQL). The rows below are sorted; "
            f"the SAVED BAQ '{query_id}' is NOT, so action='run' on it later "
            f"returns unsorted rows. Only the fetched page is ranked.")

    merged = {
        "success": True,
        "baq_id": query_id,
        "composed_sql": sql,
        "joins": join_desc,
        "resolved": {
            "tables": [t["full_name"] for t in resolved],
            "fields": [f"{a}.{f}" for (a, f) in selected],
            **({"aggregates": [
                (f"{agg['fn']}(*)" if agg.get("real") is None
                 else f"{agg['fn']}({'distinct ' if agg.get('distinct') else ''}"
                      f"{agg['alias']}.{agg['real']}) as {agg['out']}")
                for agg in aggregates
            ], "group_by": [f"{a}.{f}" for (a, f) in selected]}
               if aggregates else {}),
            **({"order": order_note} if order_note else {}),
        },
        **{k: v for k, v in run_result.items() if k != "baq_id"},
        "refine_hint": (
            f"BAQ '{query_id}' is saved in Epicor — KEEP it and give the "
            f"user the id. To adjust fields or filters, call "
            f"action='create' again with the same tables: it OVERWRITES "
            f"'{query_id}' in place. Never delete-and-recreate."
        ),
    }
    return format_response(merged, records_key="records", format="json")


# Epicor's mandatory-BAQ-parameter refusal, e.g.
# "Parameter 'ToDate' is configured as mandatory but no value is specified".
_MANDATORY_PARAM_RE = re.compile(
    r"parameter\s+'?(\w+)'?\s+is\s+configured\s+as\s+mandatory", re.IGNORECASE)

# Epicor's OTHER mask for the same class of run failure: a name it could not
# bind, e.g. "Could not find a property named 'NoSuchCol' on type ...". The
# named token X is the disambiguator — it may be a guessed parameter, a bad
# `order_by` column, or a bad `where` column. Blaming mandatory params on the
# strength of the DEFINITION alone (unsupplied params exist) misattributes a
# bad `order_by`/`where` to params, then to a `where` that was never passed —
# two false blames that send the model chasing phantoms (e.g.
# order_by='NoSuchCol' on a BAQ that has unsupplied-but-harmless params).
_PROPERTY_NOT_FOUND_RE = re.compile(
    r"could not find a property named\s+'?([\w.]+)'?", re.IGNORECASE)


def _coerce_baq_params(params) -> dict[str, Any]:
    """Accept the model's params as a dict or a JSON-object string."""
    if isinstance(params, dict):
        return params
    if isinstance(params, str) and params.strip():
        try:
            parsed = json.loads(params)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _describe_saved_baq(
    *, session, rbac, client, baq_id: str,
) -> dict[str, Any] | None:
    """The saved BAQ's execution parameters + result columns, or None.

    Reads the definition via ``Ice.BO.DynamicQuerySvc/GetByID``. Best-effort:
    tries the BAQ key, then a DynamicQuerySvc-scoped key; any failure returns
    None so callers degrade to a message without the parameter list.
    """
    keys: list[str] = []
    baq_access = rbac.check_baq_access(session.user_id)
    if baq_access.allowed and baq_access.api_key:
        keys.append(baq_access.api_key)
    svc = rbac.check_service_access(session.user_id, "Ice.BO.DynamicQuerySvc")
    if getattr(svc, "api_key", None) and svc.api_key not in keys:
        keys.append(svc.api_key)
    for api_key in keys:
        try:
            resp = await client.post(
                "Ice.BO.DynamicQuerySvc/GetByID", api_key,
                json_body={"queryID": baq_id})
        except Exception:  # noqa: BLE001 — describe is best-effort only
            continue
        obj = resp.get("returnObj") if isinstance(resp, dict) else None
        if not isinstance(obj, dict):
            continue
        parameters = [
            {"name": p.get("ParameterID"),
             "type": p.get("ParameterType") or "string",
             "mandatory": not p.get("SkipIfEmpty", False)}
            for p in (obj.get("QueryParameter") or [])
            if isinstance(p, dict) and p.get("ParameterID")
        ]
        columns = [
            f.get("Alias") or f.get("FieldName")
            for f in (obj.get("QueryField") or [])
            if isinstance(f, dict) and (f.get("Alias") or f.get("FieldName"))
        ]
        return {"parameters": parameters, "columns": columns}
    return None


def _params_example(parameters: list[dict]) -> dict[str, str]:
    """A fill-in template the retry can copy: {"FromDate": "<date>", ...}."""
    return {p["name"]: f"<{p.get('type', 'value')}>" for p in parameters}


def _project_baq_records(
    records, fields: str,
) -> tuple[Any, dict]:
    """Client-side column pruning for BAQ results; ``(rows, notes)``.

    The guard aliases ``select`` -> ``fields``, and run/dashboard then had no
    such parameter — so the coercion was ANNOUNCED and never applied, which is
    worse than the old silent drop (it asserts a pruning that did not happen).
    Pruning is done here because BAQ result columns are Alias_Field-shaped and
    unguessable: refusing would cost a round trip for rows already in hand.
    """
    wanted = [t.strip() for t in str(fields or "").split(",") if t.strip()]
    if not wanted or not isinstance(records, list) or not records:
        return records, {}
    first = records[0]
    if not isinstance(first, dict):
        return records, {}
    ci: dict[str, list[str]] = {}
    for k in first:
        ci.setdefault(str(k).lower(), []).append(k)
    keep: list[str] = []
    unknown: list[str] = []
    for term in wanted:
        if term in first:
            keep.append(term)
            continue
        hits = ci.get(term.lower(), [])
        # A multi-way case-insensitive hit is AMBIGUOUS — picking one is the
        # silent wrong guess this workstream exists to kill.
        if len(hits) == 1:
            keep.append(hits[0])
        else:
            suffix = [cols[0] for low, cols in ci.items()
                      if len(cols) == 1 and low.endswith("_" + term.lower())]
            if len(suffix) == 1:
                keep.append(suffix[0])
            else:
                unknown.append(term)
    notes: dict = {}
    if not keep:
        notes["unknown_fields"] = (
            f"None of {', '.join(wanted)} are columns of this BAQ, so NO "
            "pruning was applied — all columns are shown.")
        notes["valid_columns"] = list(first)
        return records, notes
    rows = [{k: r.get(k) for k in keep} for r in records if isinstance(r, dict)]
    notes["projected"] = (
        f"showed {len(keep)} of {len(first)} columns (fields/select)")
    if unknown:
        notes["unknown_fields"] = (
            f"{', '.join(unknown)} are not columns of this BAQ and were not "
            "applied.")
        notes["valid_columns"] = list(first)
    return rows, notes


async def _do_run(
    *, session, rbac, client, baq, where, limit, cursor, params=None,
    order_by: str = "", fields: str = "", dashboard_fn=None,
) -> str:
    if not baq or not baq.strip():
        return json.dumps(error_envelope(
            "missing_baq",
            "run needs `baq` set to a BAQ id (e.g. 'AUTO-open-pos').",
        ))
    # A saved BAQ id is always a single token (e.g. 'AUTO-open-orders'); a value with
    # whitespace or the word "dashboard" is a DASHBOARD name the model routed
    # to 'run' by mistake (e.g. run baq='Sample Sales dashboard' ->
    # baq_not_found, then the model needs a second hop
    # to reach action='dashboard'). Auto-route it to the dashboard engine
    # instead of failing — the tool picks the path (INV-2). The engine does
    # its own resolution and returns candidates if it isn't a dashboard
    # either, so a genuine typo still lands somewhere useful.
    name = baq.strip()
    if dashboard_fn is not None and (
            " " in name or re.search(r"\bdashboards?\b", name, re.IGNORECASE)):
        dash = re.sub(
            r"\bdashboards?\b", " ", name, flags=re.IGNORECASE).strip(" ,.-")
        return await _do_dashboard(
            dashboard_fn=dashboard_fn, baq=dash, where=where, limit=limit,
            fields=fields, order_by=order_by)
    try:
        skip = max(0, int(cursor)) if cursor else 0
    except ValueError:
        skip = 0
    fetch_top = min(skip + max(1, limit), 1000)
    odata_filter = _baq_filter_to_odata(where)
    baq_params = _coerce_baq_params(params)
    try:
        result = await run_baq_impl(
            session=session, rbac=rbac, client=client,
            baq_id=baq.strip(), filter=odata_filter, top=fetch_top,
            baq_params=baq_params, orderby=(order_by or "").strip(),
        )
    except EpicorError as exc:
        # Missing query id. Live Epicor answers BaqSvc/<id>/Data for a
        # nonexistent BAQ with 404 "Dynamic query is not found <id>"
        # (Ice.Api.Exceptions.ApiException); run_baq_impl has ALREADY retried
        # the AUTO-prefixed spelling before this propagates, so this verdict
        # is final. TERMINAL — no retry_with: retrying with another invented
        # id is the audit-log thrash this kills.
        msg = (exc.message or "").lower()
        not_found = exc.status_code == 404 or "not found" in msg
        # run_baq_impl's AUTO- retry can MASK a real 400 (e.g. bad filter on
        # an existing BAQ) behind the retry's 404 "not found AUTO-<id>".
        # The original error is chained on the retry's exception — if it was
        # NOT itself a not-found, this is a fixable call, not a missing BAQ.
        ctx = exc.__context__
        if (not_found and isinstance(ctx, EpicorError)
                and ctx.status_code != 404
                and "not found" not in (ctx.message or "").lower()):
            not_found = False
            exc = ctx
        if not_found:
            env = error_envelope(
                "baq_not_found",
                f"No saved BAQ named '{baq}' exists (the AUTO- prefixed "
                "spelling was also tried). BAQ ids cannot be guessed — do "
                "NOT retry action='run' with invented or varied names. If "
                "the user named a dashboard, use action='dashboard' with "
                "that name. Otherwise answer the question with epicor_read.",
            )
            env["terminal"] = True
            env["stop_hint"] = (
                "FINAL for this BAQ id. Only two moves remain: "
                "action='dashboard' (if the user gave a dashboard name) or "
                "epicor_read (for a data question)."
            )
            return json.dumps(env)
        # The BAQ exists but the run failed. Before blaming the filter, read
        # the BAQ's definition: a mandatory execution parameter the caller
        # didn't supply means NO variation of `where` can ever succeed —
        # $filter trims the RESULT; parameters feed the QUERY. This catches
        # both of Epicor's masks for the same root cause ("Parameter 'X' is
        # configured as mandatory..." on a plain run, and "Could not find a
        # property named 'X'..." when the parameter was guessed into the
        # filter), so the retry can supply parameters instead of guessing filters.
        desc = await _describe_saved_baq(
            session=session, rbac=rbac, client=client, baq_id=baq.strip())
        parameters = (desc or {}).get("parameters") or []
        columns = (desc or {}).get("columns") or []
        unsupplied = [p for p in parameters
                      if p.get("mandatory") and p["name"] not in baq_params]

        # Attribute the failure to what was ACTUALLY passed before falling back
        # to the definition-derived param guess. A bad `order_by` column masks
        # as "Could not find a property named 'X'" — identical to a guessed
        # param — so an unguarded param blame misfires whenever the BAQ also
        # happens to carry unsupplied params. Check order_by against the BAQ's
        # real result columns (or the not-found token when we have no columns).
        bad_prop = _PROPERTY_NOT_FOUND_RE.search(exc.message or "")
        bad_token = bad_prop.group(1) if bad_prop else ""
        order_terms, _ = parse_order_by(order_by or "")
        order_cols = [c for c, _dir in order_terms]
        if order_cols:
            lc_cols = {c.lower() for c in columns}
            offending = [c for c in order_cols
                         if (lc_cols and c.lower() not in lc_cols)
                         or (not lc_cols and c.lower() == bad_token.lower())]
            if offending:
                env = error_envelope(
                    "baq_bad_order_by",
                    f"order_by column(s) {', '.join(offending)} are not result "
                    f"columns of BAQ '{baq}', so the sort failed — this is NOT "
                    "a `where` or parameter problem. Sort by one of the BAQ's "
                    "own result columns (Alias_Field names), or drop order_by "
                    "and sort the returned rows yourself.",
                    valid={"columns": columns} if columns else None,
                    retry_with={"action": "run", "baq": baq, "order_by": ""},
                )
                return json.dumps(env)

        if unsupplied or (desc is None
                          and _MANDATORY_PARAM_RE.search(exc.message or "")):
            plist = (", ".join(
                f"{p['name']} ({p['type']}{', mandatory' if p['mandatory'] else ''})"
                for p in parameters) or "see Epicor's message")
            env = error_envelope(
                "baq_needs_params",
                f"BAQ '{baq}' requires BAQ execution parameters: {plist}. "
                "Pass them in `params` (a JSON object keyed by parameter "
                "name, dates as YYYY-MM-DD) — NOT in `where`: `where` "
                "filters the result AFTER the query runs; parameters are "
                "inputs the query needs to run at all. Ask the user for "
                "values if the question doesn't imply them.",
                valid={"parameters": parameters} if parameters else None,
                retry_with={
                    "action": "run", "baq": baq,
                    "params": _params_example(parameters) or
                    {"<ParameterID>": "<value>"},
                },
            )
            return json.dumps(env)
        # Real BAQ, parameters satisfied (or none), sort clean. Serve the BAQ's
        # real result columns so the retry uses exact Alias_Field names instead
        # of guessing: from the definition when we have it, else an unfiltered
        # top=1 probe. (`columns` was read from `desc` above.)
        if odata_filter and not columns:
            try:
                probe = await run_baq_impl(
                    session=session, rbac=rbac, client=client,
                    baq_id=baq.strip(), top=1, baq_params=baq_params,
                )
                recs = probe.get("records")
                if isinstance(recs, list) and recs and isinstance(recs[0], dict):
                    columns = [k for k in recs[0] if not k.startswith("Sys")]
            except Exception:  # noqa: BLE001 — probe is best-effort only
                pass
        # Only point at `where` when a `where` was actually passed — blaming a
        # filter the caller never sent is the second false blame that turns one
        # bad call into a multi-hop hunt.
        if where and where.strip():
            guidance = (
                "The BAQ exists — the `where` filter is the usual cause. Filter "
                "syntax is OData over the BAQ's result columns (Alias_Field "
                "names): operators eq/ne/gt/ge/lt/le, 'and'/'or', strings "
                "quoted, dates as YYYY-MM-DDT00:00:00Z (e.g. "
                "\"LaborDtl_ApprovedBy eq 'service-account' and LaborDtl_ClockInDate ge "
                "2026-06-01T00:00:00Z\"). Fix the filter using `valid.columns` "
                "and retry ONCE. If it is an AUTO- BAQ and the filter encodes "
                "the user's criteria, the reliable path is action='create' with "
                "the criteria in `where` — it overwrites the BAQ in place (do "
                "NOT delete it).")
            retry = {"baq": baq, "where": where}
        else:
            guidance = (
                "The BAQ exists and no `where` was passed, so the filter is not "
                "the cause — this is Epicor's own message about the run. Check "
                "`valid.columns` for the real result-column names; if the "
                "question needs different criteria, action='create' composes a "
                "new BAQ.")
            retry = {"baq": baq}
        env = error_envelope(
            "baq_run_failed",
            f"BAQ '{baq}' failed: {ErrorHandler.format_user_message(exc)}. "
            + guidance,
            retry_with=retry,
        )
        if odata_filter and odata_filter != where:
            env["filter_sent"] = odata_filter
        if columns:
            env["valid"] = {"columns": columns}
        return json.dumps(env)
    if "error" in result:
        return json.dumps(error_envelope(
            "baq_access_denied", result["error"], retry_with={"baq": baq},
        ))

    records = result.get("records")
    if isinstance(records, list):
        page = records[skip:skip + limit]
        next_cursor = ""
        if len(records) >= fetch_top and fetch_top < 1000:
            next_cursor = str(skip + limit)
        page, proj_notes = _project_baq_records(page, fields)
        out = {
            **{k: v for k, v in result.items() if k != "records"},
            "record_count": len(page),
            "records": page,
            **proj_notes,
        }
        if next_cursor:
            out["next_cursor"] = next_cursor
        return format_response(out, records_key="records", format="json")
    return format_response(result, records_key="records", format="json")


async def _do_dashboard(*, dashboard_fn, baq, where, limit, fields: str = "",
                        order_by: str = "") -> str:
    if dashboard_fn is None:
        env = error_envelope(
            "dashboard_unavailable",
            "The dashboard engine is not available on this server. Answer "
            "the question with epicor_read instead.",
        )
        env["terminal"] = True
        return json.dumps(env)
    # An empty or generic `baq` ("a dashboard", "which dashboards") is NOT an
    # error — the engine lists the available dashboards so the model can offer
    # real names instead of guessing one. A specific name is resolved
    # exact -> fuzzy. The captured legacy engine handles session, RBAC, lookup,
    # BAQ resolution, and execution; it already returns a JSON string.
    raw = await dashboard_fn(
        dashboard=(baq or "").strip(), filter=where, top=limit, execute=True,
    )
    if not ((fields or "").strip() or (order_by or "").strip()):
        return raw
    # Same reason as _do_run: the guard aliased `select` -> `fields`, so it has
    # to actually prune here or the announcement is a lie. `order_by` is in the
    # same position -- the tool description promises it applies to action='run',
    # and the run->dashboard auto-reroute used to void that promise silently.
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return raw
    if (order_by or "").strip():
        terms, kind = parse_order_by(order_by)
        if kind or not terms:
            env = error_envelope(
                "order_expression_unsupported",
                f"order_by='{order_by}' is an expression or aggregate. A "
                "dashboard's rows can only be ordered by a plain result-column "
                "name ('Col', 'Col desc'). Re-run with a column from the "
                "records below.")
            env["dashboard"] = payload.get("dashboard") or baq
            return json.dumps(env)
        # _sort_records_by silently `continue`s past a column it cannot
        # resolve, so resolve them HERE to know whether the sort really
        # happened -- otherwise the note would assert an ordering that was
        # never applied, which is the defect this fix exists to remove.
        cols = [str(k) for k in (payload["records"][0] or {})] \
            if payload["records"] else []
        low = {c.lower(): c for c in cols}
        unresolved = [
            c for c, _d in terms
            if c.strip("[]").split(".")[-1].strip("[]").lower() not in low
            and not any(k.endswith("_" + c.strip("[]").split(".")[-1].lower())
                        for k in low)
        ]
        if unresolved or not payload["records"]:
            payload["order"] = (
                f"order_by='{order_by}' was NOT applied — "
                + (f"{', '.join(unresolved)} is not a column of this "
                   f"dashboard's result. Available: {', '.join(cols)}."
                   if unresolved else "the dashboard returned no rows."))
        else:
            payload["records"] = _sort_records_by(payload["records"], terms)
            payload["order"] = (
                f"{order_by} (client-side, over the dashboard's returned rows)")
    if (fields or "").strip():
        rows, notes = _project_baq_records(payload["records"], fields)
        payload["records"] = rows
        payload.update(notes)
    return json.dumps(payload, indent=2)


def _do_find(*, baq_index, baq, limit) -> str:
    # A find whose search text is about DASHBOARDS ("dashboard", or a phrase
    # that ends in "dashboard") is a mis-routed dashboard ask — find searches
    # the data dictionary, never dashboards. Redirect to action='dashboard'
    # BEFORE the authoring gate so it works whether or not create is enabled
    # (otherwise a model burns `find query='dashboard'` +
    # `find baq='dashboard'` before reaching action='dashboard').
    if baq and re.search(r"\bdashboards?\b", baq, re.IGNORECASE):
        dash = re.sub(
            r"\bdashboards?\b", " ", baq, flags=re.IGNORECASE).strip(" ,.-")
        env = error_envelope(
            "use_dashboard_action",
            "You're looking for a DASHBOARD — find can't do that (it searches "
            "the data dictionary). Use epicor_baq action='dashboard': set "
            "`baq` to the dashboard name if the user gave one, or leave it "
            "EMPTY to list the available dashboards.",
            retry_with={"action": "dashboard", "baq": dash},
        )
        return json.dumps(env)
    # find searches the DATA DICTIONARY (tables/columns) for BAQ *authoring* —
    # it does NOT search saved BAQs, which cannot be discovered by searching.
    # With authoring disabled, find has no legitimate use: gate it exactly
    # like create/delete, terminally (this is the blind-search thrash killer).
    if not get_settings().enable_baq_create:
        env = error_envelope(
            "baq_find_disabled",
            "find searches data-dictionary TABLES for BAQ AUTHORING, which "
            "is disabled here and you cannot enable it. It does NOT search "
            "saved BAQs — saved BAQs cannot be found by searching or "
            "guessing, ever. Do NOT retry find with different keywords. For "
            "a data question use epicor_read. If the user named a dashboard, "
            "use action='dashboard' with that exact name.",
            valid={"actions": ["run", "dashboard", "schema"]},
        )
        env["terminal"] = True
        return json.dumps(env)
    if not baq or not baq.strip():
        return json.dumps(error_envelope(
            "missing_query",
            "find needs `baq` set to search text (keywords or a table name).",
        ))
    limit = max(1, min(limit, 50))
    tables = baq_index.search_tables(baq, limit=limit)
    if not tables:
        return json.dumps({
            "mode": "find", "query": baq, "results": [],
            "hint": "No tables matched. Try different keywords, or use "
                    "action='schema' with a known Schema.Table name.",
        })
    results = []
    for idx, tbl in enumerate(tables):
        full_name = tbl.get("full_name", "")
        fields = baq_index.get_fields(full_name)
        show_full = idx < 2
        visible = fields if show_full else fields[:10]
        results.append({
            "full_name": full_name,
            "description": tbl.get("description", ""),
            ("fields" if show_full else "key_fields"): [
                {"name": f.get("field_name", ""), "type": f.get("data_type", "")}
                for f in visible
            ],
            "total_fields": len(fields),
        })
    out: dict[str, Any] = {
        "mode": "find", "query": baq, "result_count": len(results),
        "results": results,
    }
    # Only steer into create when create is actually enabled (the gate above
    # normally guarantees it; keep this conditional in case the gate moves).
    if get_settings().enable_baq_create:
        out["note"] = ("Top 2 include full fields. Feed the resolved table "
                       "names straight into action='create'.")
    else:
        out["note"] = ("Top 2 include full fields. These are data-dictionary "
                       "tables, not saved BAQs.")
    return json.dumps(out, indent=2)


async def _do_schema(*, baq_index, tables, baq, session, rbac, client) -> str:
    raw = tables or baq or ""
    names = [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]
    if not names:
        return json.dumps(error_envelope(
            "missing_tables",
            "schema needs `tables` set to one or more Schema.Table names "
            "(e.g. 'Erp.OrderHed,Erp.OrderDtl'), or `baq` set to a saved "
            "BAQ id to describe its parameters and result columns.",
        ))
    # A saved-BAQ id in `baq` (schema of the BAQ itself, not a dictionary
    # table): describe its execution parameters + result columns so the model
    # learns what a run needs BEFORE failing one.
    if baq and baq.strip() and baq_index.get_table(baq.strip()) is None:
        baq_id = baq.strip()
        desc = await _describe_saved_baq(
            session=session, rbac=rbac, client=client, baq_id=baq_id)
        if desc is not None:
            parameters = desc.get("parameters") or []
            result: dict[str, Any] = {
                "mode": "schema",
                "baq": baq_id,
                "parameters": parameters,
                "result_columns": desc.get("columns") or [],
            }
            if parameters:
                result["hint"] = (
                    "This BAQ takes execution parameters — action='run' with "
                    "params=" + json.dumps(_params_example(parameters)) +
                    " (dates as YYYY-MM-DD). `where` cannot supply them.")
            else:
                result["hint"] = ("No parameters — action='run' with just "
                                  "`baq` (optional `where` filters the "
                                  "result columns above).")
            return json.dumps(result, indent=2)
    out: list[dict[str, Any]] = []
    not_found: list[str] = []
    for name in names[:10]:
        info = baq_index.get_table(name)
        if info is None:
            not_found.append(name)
            continue
        fields = baq_index.get_fields(name)
        out.append({
            "full_name": info.get("full_name", ""),
            "description": info.get("description", ""),
            "field_count": len(fields),
            "fields": [
                {"name": f.get("field_name", ""), "type": f.get("data_type", "")}
                for f in fields
            ],
        })
    result: dict[str, Any] = {"mode": "schema", "tables": out}
    if not_found:
        result["not_found"] = not_found
        # Only steer to action='find' when it is actually enabled — with BAQ
        # authoring off, find is gated and that hint would ping-pong.
        if get_settings().enable_baq_create:
            result["hint"] = ("Use action='find' with keywords to locate the "
                              "correct Schema.Table names.")
        else:
            result["hint"] = ("Check the Schema.Table spelling (e.g. "
                              "'Erp.OrderHed'); for data questions use "
                              "epicor_read instead.")
    return json.dumps(result, indent=2)


async def _do_delete(*, session, rbac, client, baq) -> str:
    baq = (baq or "").strip()
    if not baq:
        return json.dumps(error_envelope(
            "missing_baq", "delete needs `baq` set to the BAQ id to remove.",
        ))
    # Only AUTO- BAQs are deletable — auto-prefix rather than reject.
    if not baq.startswith("AUTO-"):
        baq = f"AUTO-{baq}"

    user_profile = rbac._user_map.get_user(session.user_id)
    has_baq_permission = (
        session.access_level == "read_write"
        or (user_profile and user_profile.can_write_baqs)
    )
    if not has_baq_permission:
        return json.dumps(error_envelope(
            "access_denied",
            "BAQ deletion requires BAQ Designer permissions or full write "
            "access. Contact your administrator.",
        ))
    if session.access_level == "read_write":
        api_key = rbac._user_map.get_write_key()
    else:
        api_key = rbac._user_map.get_baq_key()
    if not api_key:
        return json.dumps(error_envelope(
            "no_api_key", "No BAQ write API key configured.",
        ))
    try:
        await client.post(
            "Ice.BO.BAQDesignerSvc/DeleteByID", api_key,
            json_body={"queryID": baq},
        )
    except EpicorError as exc:
        # 404 => the BAQ is already gone. Delete is idempotent: report a clean
        # not-found rather than a scary failure so the caller doesn't retry.
        if exc.status_code == 404:
            logger.info("epicor_baq delete '%s' already absent (404)", baq)
            return json.dumps({
                "success": True, "deleted": baq, "already_absent": True,
                "message": f"BAQ '{baq}' does not exist (already deleted).",
            })
        return json.dumps(error_envelope(
            "delete_failed",
            f"Failed to delete BAQ '{baq}': "
            f"{ErrorHandler.format_user_message(exc)}",
        ))
    except Exception as exc:
        return json.dumps(error_envelope(
            "delete_failed", f"Failed to delete BAQ '{baq}': {exc}",
        ))
    logger.info("epicor_baq delete '%s' (user=%s)", baq, session.user_id)
    return json.dumps({
        "success": True, "deleted": baq,
        "message": f"BAQ '{baq}' has been deleted.",
    })


async def _epicor_baq_impl(
    *, session, rbac, client, baq_index,
    action: str, baq: str, tables: str, fields: str,
    where: str, limit: int, cursor: str,
    description: str = "", order_by: str = "",
    params=None, dashboard_fn=None,
) -> str:
    """Dispatch the requested BAQ action. Dependency-injected for testability."""
    act = (action or "").strip().lower()
    limit = max(1, min(limit, 1000))

    # BAQ authoring is disabled by default on the legacy tools (bias to business objects;
    # most users can't create BAQs and the model authors them poorly). Flip
    # EPICOR_MCP_ENABLE_BAQ_CREATE to re-enable once the authoring path is
    # hardened. (find is gated separately inside _do_find — it is an
    # authoring aid, not a saved-BAQ search.)
    if act in ("create", "delete") and not get_settings().enable_baq_create:
        return json.dumps(error_envelope(
            "baq_create_disabled",
            "Creating BAQs is disabled here and you cannot enable it. Do NOT retry "
            "and do NOT bounce back to epicor_read for the SAME request: if "
            "epicor_read already said the data needs a report/BAQ, that ranking "
            "simply isn't available — tell the user that plainly and stop. "
            "(epicor_baq here only runs a saved BAQ or dashboard the USER "
            "named by exact id.)",
            valid={"actions": ["run", "dashboard", "schema"]},
        ))

    if act == "create":
        return await _do_create(
            session=session, rbac=rbac, client=client, baq_index=baq_index,
            baq=baq, tables=tables, fields=fields, where=where, limit=limit,
            description=description, order_by=order_by,
        )
    if act == "run":
        return await _do_run(
            session=session, rbac=rbac, client=client,
            baq=baq, where=where, limit=limit, cursor=cursor, params=params,
            order_by=order_by, fields=fields, dashboard_fn=dashboard_fn,
        )
    if act == "dashboard":
        return await _do_dashboard(
            dashboard_fn=dashboard_fn, baq=baq, where=where, limit=limit,
            fields=fields, order_by=order_by,
        )
    if act == "find":
        return _do_find(baq_index=baq_index, baq=baq, limit=limit)
    if act == "schema":
        return await _do_schema(
            baq_index=baq_index, tables=tables, baq=baq,
            session=session, rbac=rbac, client=client,
        )
    if act == "delete":
        return await _do_delete(
            session=session, rbac=rbac, client=client, baq=baq,
        )
    return json.dumps(error_envelope(
        "unknown_action",
        f"Unknown action '{action}'. Use one of the valid actions.",
        valid={"actions": ["run", "dashboard", "find", "schema", "create",
                           "delete"]},
    ))


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    baq_index: "BAQSchemaIndex | None" = None,
    dataset_handler: "DatasetHandler | None" = None,
) -> None:
    """Bind the ``epicor_baq`` tool to *server*."""

    # Capture the legacy dashboard->BAQ engine once, at register time.
    dashboard_fn = _capture_dashboard_fn(index, rbac, client)

    # The description must match the runtime gate: advertise create/delete
    # only when the flag is on. (Settings are startup-static, so resolving
    # this once at register time is correct — the flag needs a restart.)
    description = (
        _DESCRIPTION_AUTHORING if get_settings().enable_baq_create
        else _DESCRIPTION
    )

    @server.tool(structured_output=False, description=description)
    async def epicor_baq(
        action: str,
        baq: str = "",
        tables: str | list[str] = "",
        fields: str | list[str] = "",
        where: str = "",
        description: str = "",
        order_by: str = "",
        limit: int = 50,
        cursor: str = "",
        params: dict[str, str] | str | None = None,
    ) -> str:
        """Run a saved BAQ / dashboard, or (authoring) find/schema/create/
        delete. See tool description."""
        # Normalise list / JSON-array forms to the comma-separated string every
        # internal signature expects (_epicor_baq_impl stays `tables: str`).
        tables = coerce_csv(tables)
        fields = coerce_csv(fields)
        try:
            session = get_current_session()
        except RuntimeError as exc:
            return json.dumps(error_envelope(
                "auth_required", f"Authentication required: {exc}",
            ))
        try:
            return await _epicor_baq_impl(
                session=session, rbac=rbac, client=client, baq_index=baq_index,
                action=action, baq=baq, tables=tables, fields=fields,
                where=where, description=description, order_by=order_by,
                limit=limit, cursor=cursor, params=params,
                dashboard_fn=dashboard_fn,
            )
        except Exception:
            logger.exception("epicor_baq failed")
            return json.dumps(error_envelope(
                "internal_error",
                "The BAQ tool hit an unexpected error. Re-check the action "
                "and arguments.",
            ))

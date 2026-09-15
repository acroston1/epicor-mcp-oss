"""Tool: epicor_query

Execute read-only queries against Epicor services.  Supports OData
``$filter``, ``$select``, ``$orderby``, ``$top``, and ``$expand``.

Automatically falls back to the service's ``GetRows`` method when the
OData entity set endpoint is not available (common for transactional
BOs like POSvc, JobEntrySvc, ARInvoiceSvc, etc.).
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import logging
import re
from typing import TYPE_CHECKING

from epicor_mcp.context import get_current_session
from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.response import format_response
from epicor_mcp.tools._aggregate import aggregate_records
from epicor_mcp.tools._inline_schema import build_query_description

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# SQL → OData filter translation (input side, for callers who default to SQL)
# -----------------------------------------------------------------------

# IN-clause:  Column in (a, b, c) | 'x','y'  →  (Column eq a or Column eq b ...)
_IN_RE = re.compile(
    r"""(?P<col>\[?[A-Za-z_][\w\.]*\]?)
        \s+in\s*\(
        (?P<vals>[^()]+)
        \)""",
    re.IGNORECASE | re.VERBOSE,
)

# LIKE:  Column like 'pattern'  →  contains/startswith/endswith/eq
_LIKE_RE = re.compile(
    r"""(?P<col>\[?[A-Za-z_][\w\.]*\]?)
        \s+like\s+'(?P<pat>[^']*)'""",
    re.IGNORECASE | re.VERBOSE,
)

# Infix CONTAINS:  Column contains 'value'  →  contains(Column, 'value').
# Models routinely emit the SQL-Server infix form, which is invalid in both
# OData (the function form is contains(Col,'x')) and Epicor's whereClause, so
# it 500s. Normalise it to the OData function call; the GetRows path then
# turns that into ``Col like '%value%'`` downstream.
_CONTAINS_INFIX_RE = re.compile(
    r"""(?P<col>\[?[A-Za-z_][\w\.]*\]?)
        \s+contains\s+'(?P<val>(?:''|[^'])*)'""",
    re.IGNORECASE | re.VERBOSE,
)


# BETWEEN:  Column between lo and hi  →  (Column ge lo and Column le hi).
# `between` was whitelisted in the column validator but never actually
# translated, so it reached the wire verbatim and 400'd.
_BETWEEN_RE = re.compile(
    r"""(?P<col>\[?[A-Za-z_][\w\.]*\]?)
        \s+between\s+(?P<lo>'[^']*'|[^\s]+)
        \s+and\s+(?P<hi>'[^']*'|[^\s]+)""",
    re.IGNORECASE | re.VERBOSE,
)


def _expand_contains_infix(match: re.Match) -> str:
    return f"contains({match.group('col')}, '{match.group('val')}')"


def _expand_between(
    match: re.Match, date_columns: "frozenset[str] | None" = None
) -> str:
    """``col between lo and hi`` -> an inclusive OData range.

    On a DATE column the naive ``col le hi`` is WRONG, not merely imprecise:
    the date normalizer pins a bare ``2025-01-31`` to ``2025-01-31T00:00:00Z``,
    so every row stamped later that day is dropped. Epicor's audit columns
    (CreatedOn/ChangedOn/LastUpdated/EntryDate) are all Edm.DateTimeOffset
    carrying a real time-of-day, and `between` is the phrasing that most
    strongly implies an inclusive range — a silently truncated final day would
    understate a month bucket by a full day with nothing in the response
    saying so. For a date bound we emit the half-open ``lt <hi + 1 day>``,
    which is exactly inclusive of the whole final day.
    """
    col = match.group("col")
    lo, hi = match.group("lo"), match.group("hi")
    bare = col.strip("[]").split(".")[-1].lower()
    # date_columns is None => the entity isn't indexed and the anchored-regex
    # normalizer governs alone; it pins a bare ISO bound to T00:00:00Z, i.e.
    # treats it as a date, so the same half-open correction applies.
    if date_columns is None or bare in date_columns:
        nxt = _next_day_literal(hi)
        if nxt:
            return f"({col} ge {lo} and {col} lt {nxt})"
    return f"({col} ge {lo} and {col} le {hi})"


def _next_day_literal(value: str) -> str | None:
    """``'2025-01-31'`` -> ``2025-02-01T00:00:00Z``; None if not a bare date.

    Only a DATE-only bound is advanced. A bound that already carries a time
    (``'2025-01-31 17:00'``) means what it says, so it is left alone.
    """
    m = _QUOTED_OR_BARE_ISO.match((value or "").strip())
    if not m or m.group("time"):
        return None
    try:
        day = _dt.date.fromisoformat(m.group("day")) + _dt.timedelta(days=1)
    except ValueError:
        return None
    return f"{day.isoformat()}T00:00:00Z"


def _expand_in_clause(match: re.Match) -> str:
    col = match.group("col")
    raw_vals = [v.strip() for v in match.group("vals").split(",") if v.strip()]
    parts = [f"{col} eq {v}" for v in raw_vals]
    return "(" + " or ".join(parts) + ")"


def _expand_like_clause(match: re.Match) -> str:
    col = match.group("col")
    pat = match.group("pat")
    starts = pat.endswith("%")
    ends = pat.startswith("%")
    inner = pat.strip("%")
    # Re-quote any single quotes the user typed inside the pattern.
    inner_lit = inner.replace("'", "''")
    if starts and ends:
        return f"contains({col}, '{inner_lit}')"
    if starts:
        return f"startswith({col}, '{inner_lit}')"
    if ends:
        return f"endswith({col}, '{inner_lit}')"
    return f"{col} eq '{inner_lit}'"


def _outside_quotes(s: str, fn) -> str:
    """Apply *fn* to every stretch of *s* that is NOT inside a quoted literal.

    Part numbers ('12345-6789-0001') and LIKE patterns ('%EXAMPLE%')
    live inside quotes and carry characters the arithmetic/date rewrites
    below would otherwise mangle.
    """
    parts = re.split(r"('(?:''|[^'])*')", s)
    return "".join(p if i % 2 else fn(p) for i, p in enumerate(parts))


# Arithmetic in an OData ``$filter`` must use the WORD operators. Against
# Erp.BO.PartSvc/Parts: ``UnitPrice mul 2 gt 100`` -> 200 and
# genuinely applied (col-x-col, div, add, sub all 200), while ``UnitPrice * 2 gt
# 100`` -> 400 "Syntax error at position 11". Arithmetic is also
# NOT accepted in ``$orderby`` (500 generic apology), ``$compute`` (400) or
# ``$apply`` (silently ignored, returning raw unaggregated rows) — so never
# extend this rewrite to the sort clause or push a rollup server-side.
_ARITH_TO_ODATA = {"*": "mul", "/": "div", "%": "mod", "+": "add", "-": "sub"}

# ISO date/datetime literal. Masked out before the arithmetic rewrite so the
# hyphens in ``2025-07-20`` can never be read as subtraction.
_ISO_LITERAL_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+Z?)?")


def _arith_to_odata(s: str) -> str:
    """Rewrite SQL arithmetic operators to their OData words, outside quotes.

    ``OnHandQty * AvgCost > 50000`` -> ``OnHandQty mul AvgCost gt 50000``.
    ``-`` is the sharp edge: it also lives inside ISO dates and negative
    literals, so it is masked (dates) and required to be a whitespace-delimited
    infix between two operands (``gt -5`` is left alone).
    """
    def _rewrite(seg: str) -> str:
        holes: list[str] = []

        def _stash(m: re.Match) -> str:
            holes.append(m.group(0))
            return f"\x00{len(holes) - 1}\x00"

        seg = _ISO_LITERAL_RE.sub(_stash, seg)
        for sym, word in _ARITH_TO_ODATA.items():
            gap = r"\s+" if sym == "-" else r"\s*"
            seg = re.sub(
                rf"(?<=[\w)]){gap}{re.escape(sym)}{gap}(?=[\w(])",
                f" {word} ",
                seg,
            )
        return re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], seg)

    return _outside_quotes(s, _rewrite)


# Anchored ISO date literal, quoted or bare, with an optional time part. The
# ``^``/``$`` anchors are LOAD-BEARING: they are the only thing standing
# between date coercion and a corrupted string filter — a part number
# ('12345-6789-0001') or a rev code ('REV-2025-07-20-A') cannot match an
# anchored pattern. Do NOT relax this to a search.
_QUOTED_OR_BARE_ISO = re.compile(
    r"^'?(?P<day>\d{4}-\d{2}-\d{2})(?P<time>[T ][\d:.]+Z?)?'?$")

# Edm types that take an ISO-Z literal rather than a quoted string.
_DATE_EDM_TYPES = frozenset({"Edm.DateTimeOffset", "Edm.DateTime", "Edm.Date"})


def _to_odata_datetime(value: str) -> str | None:
    """``'2025-07-20'`` / ``'2025-07-20 14:30'`` -> ``2025-07-20T14:30:00Z``.

    Returns ``None`` when *value* is not an anchored ISO date literal. Mirrors
    ``baq._baq_filter_to_odata``'s coercion for BaqSvc.
    """
    m = _QUOTED_OR_BARE_ISO.match((value or "").strip())
    if not m:
        return None
    time_part = (m.group("time") or "").replace(" ", "").lstrip("T")
    if not time_part:
        time_part = "00:00:00"
    if not time_part.endswith("Z"):
        time_part += "Z"
    return f"{m.group('day')}T{time_part}"


_QUOTED_CMP_RE = re.compile(
    r"(?P<col>[A-Za-z_]\w*)\s+(?P<op>eq|ne|ge|le|gt|lt)\s+(?P<val>'[^']*')",
    re.IGNORECASE,
)


def _normalize_dates_for_odata(
    s: str, date_columns: "frozenset[str] | None" = None
) -> str:
    """Normalize bare ``YYYY-MM-DD`` date literals to OData v4 form.

    Epicor's OData v4 layer rejects bare ``2024-01-01`` (it's parsed as
    arithmetic, ``2024-1-1 = 2022``) and rejects the legacy V3 form
    ``datetime'...'``. The accepted forms are an unquoted ISO-8601
    timestamp with ``Z`` (``2024-01-01T00:00:00Z``) for ``Edm.DateTime``
    /``Edm.DateTimeOffset`` columns.

    This rewrite only fires when a comparator (``eq`` / ``ne`` / ``ge``
    / ``le`` / ``gt`` / ``lt`` / ``=`` / ``>=`` / etc.) is immediately
    followed by a bare date, and only when the date isn't already
    quoted or already has a ``T`` time component. Idempotent.
    """
    # Strip the legacy V3 ``datetime'YYYY-MM-DDTHH:MM:SS'`` wrapper first
    # — vLLM-routed models occasionally emit it; OData v4 rejects it.
    s = re.sub(
        r"datetime'(\d{4}-\d{2}-\d{2}(?:T[^']*)?)'",
        lambda m: m.group(1) + ("Z" if "T" in m.group(1) and not m.group(1).endswith("Z") else ""),
        s,
        flags=re.IGNORECASE,
    )
    # Bare YYYY-MM-DD after a comparator → suffix T00:00:00Z. We require
    # the date NOT be preceded by a ``'`` or followed by ``T`` / digit /
    # ``'`` so we don't double-suffix already-good literals.
    s = re.sub(
        r"(\b(?:eq|ne|ge|le|gt|lt)\s+)(\d{4}-\d{2}-\d{2})(?![\dT'])",
        r"\1\2T00:00:00Z",
        s,
        flags=re.IGNORECASE,
    )

    # QUOTED ISO date after a comparator -> unquoted ISO-Z. This is the shape
    # the model reflexively emits (every OTHER value it writes is quoted), and
    # it was the one shape neither rewrite above handled: Epicor answers
    # ``TranDate ge '2025-07-20'`` with "A binary operator with incompatible
    # types was detected. Found operand types 'Edm.DateTimeOffset' and
    # 'Edm.String'". Runs AFTER operator translation, so it covers >=, <=, >,
    # <, =, != and the native OData words uniformly.
    #
    # Type-driven, never name-driven: *date_columns* comes from the index's
    # real Edm types. A name heuristic is disproven in both directions — 73
    # ``%Date`` fields are Edm.String, EnableDueDate/OvrDefTaxDate are
    # Edm.Boolean, while genuine date columns (CreatedOn, TaxPoint, Added,
    # LogUntil) don't end in ``Date`` at all. ``None`` means "entity not
    # indexed": fall back to the anchored regex alone.
    def _coerce(m: re.Match) -> str:
        col, op, val = m.group("col"), m.group("op"), m.group("val")
        if date_columns is not None and col.lower() not in date_columns:
            return m.group(0)
        iso = _to_odata_datetime(val)
        return f"{col} {op} {iso}" if iso else m.group(0)

    return _QUOTED_CMP_RE.sub(_coerce, s)


def _sql_to_odata_filter(
    s: str, *, date_columns: "frozenset[str] | None" = None
) -> str:
    """Translate common SQL operators in a filter string to OData v4 syntax.

    Pass-through when the filter already looks like OData (uses ``eq``/
    ``ne``/``ge``/``le``/``gt``/``lt`` and lacks SQL-only constructs).
    Handles: ``like``, ``in (...)``, ``between``, ``IS [NOT] NULL``,
    ``>=``/``<=``/``>``/``<``/``<>``/``=``, and arithmetic (``*`` -> ``mul``).
    Always runs arithmetic + date-literal normalization at the end so even
    already-OData filters get their operators and date literals fixed.

    *date_columns* is the set of lowercased Edm date column names on the target
    entity (see ``date_columns_for``); ``None`` disables the type gate and
    leaves the anchored-regex fallback in charge.
    """
    if not s:
        return s
    s_lower = s.lower()
    has_sql = (
        " like " in s_lower
        or re.search(r"\bin\s*\(", s_lower) is not None
        or _CONTAINS_INFIX_RE.search(s) is not None
        # `between` and `IS NULL` are SQL-only constructs that reach the wire
        # verbatim (and 400) unless we enter the translation branch — an
        # otherwise all-`eq` filter containing one still needs expanding.
        or re.search(r"\bbetween\b", s_lower) is not None
        or re.search(r"\bis\s+(?:not\s+)?null\b", s_lower) is not None
        or ">=" in s
        or "<=" in s
        or "<>" in s
        or "!=" in s
        # Bare > < = that aren't part of OData (OData uses ge/le/eq)
        or re.search(r"[^<>!=][<>=][^=]", " " + s + " ") is not None
    )
    if not has_sql:
        # Already-OData filter — still normalize arithmetic + dates.
        return _normalize_dates_for_odata(_arith_to_odata(s), date_columns)

    # IS [NOT] NULL first, BEFORE the `=` -> eq pass can corrupt it. OData
    # spells these `eq null` / `ne null`; there was no handling at all, so
    # `LastTranDate IS NULL` reached Epicor verbatim.
    s = re.sub(r"\bis\s+not\s+null\b", " ne null", s, flags=re.IGNORECASE)
    s = re.sub(r"\bis\s+null\b", " eq null", s, flags=re.IGNORECASE)
    # Infix CONTAINS → OData function form (before LIKE, so the quoted value
    # is consumed and later steps don't see a bare keyword).
    s = _CONTAINS_INFIX_RE.sub(_expand_contains_infix, s)
    # LIKE next (consumes single-quoted pattern, won't confuse later steps)
    s = _LIKE_RE.sub(_expand_like_clause, s)
    # BETWEEN after LIKE (its quoted bounds are then unambiguous) and before
    # IN, so the `and` it emits isn't re-parsed by an earlier stage.
    s = _BETWEEN_RE.sub(
        lambda m: _expand_between(m, date_columns), s)
    # IN (...) next
    s = _IN_RE.sub(_expand_in_clause, s)
    # <> / != → ne
    s = s.replace("<>", " ne ").replace("!=", " ne ")
    # Multi-char comparators
    s = s.replace(">=", " ge ").replace("<=", " le ")
    # Bare > < (not part of <= or >=, which we already replaced)
    s = re.sub(r"\s*>\s*", " gt ", s)
    s = re.sub(r"\s*<\s*", " lt ", s)
    # Bare = → eq, but skip == and ne/ge/le that contain `e`. We've already
    # collapsed >= / <=, so any remaining `=` is a SQL equals.
    s = re.sub(r"\s*=\s*", " eq ", s)
    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # Arithmetic → OData words, then dates → ISO-Z, both after the operators
    # are in OData form (so the date pass sees `ge`, not `>=`).
    s = _arith_to_odata(s)
    s = _normalize_dates_for_odata(s, date_columns)
    return s


# -----------------------------------------------------------------------
# OData → SQL filter translation for GetRows fallback
# -----------------------------------------------------------------------

def _quote_sql_dates(s: str) -> str:
    """Wrap unquoted date literals in single quotes for SQL whereClause.

    Applies in two passes: full ISO ``YYYY-MM-DDTHH:MM:SS...`` first,
    then bare ``YYYY-MM-DD``. Without the bare-date branch, GetRows
    reads ``>= 2024-01-01`` as arithmetic (``= 2022``) against a date
    column and 500s.
    """
    s = re.sub(
        r"([><=!]+\s*)(\d{4}-\d{2}-\d{2}T[^\s,)]*)",
        r"\1'\2'",
        s,
    )
    s = re.sub(
        r"([><=!]+\s*)(\d{4}-\d{2}-\d{2})(?![\dT'])",
        r"\1'\2'",
        s,
    )
    return s


# OData string-function call:  contains(Col, 'x') | startswith(Col, 'x') |
# endswith(Col, 'x')  →  SQL LIKE pattern. The value is captured verbatim
# (it was already single-quote-escaped upstream by _expand_like_clause), so
# we don't re-escape it here.
_ODATA_STRFUNC_RE = re.compile(
    r"""(?P<fn>contains|startswith|endswith)\s*\(
        \s*(?P<col>\[?[A-Za-z_][\w\.]*\]?)\s*,
        \s*'(?P<val>(?:''|[^'])*)'\s*
        \)""",
    re.IGNORECASE | re.VERBOSE,
)


def _odata_strfuncs_to_sql_like(s: str) -> str:
    """Rewrite OData ``contains``/``startswith``/``endswith`` calls as SQL LIKE.

    Epicor's GetRows ``whereClause`` parser speaks SQL, not OData — it has
    no ``contains()`` function, so a whereClause like
    ``contains(CustomerName, 'EXAMPLE')`` throws the generic "unexpected
    internal problem" 500. The user's original ``Col like '%EXAMPLE%'`` was
    translated to ``contains(...)`` for the (often-unavailable) OData path;
    on the GetRows fallback we must translate it back::

        contains(Col, 'EXAMPLE')    → Col like '%EXAMPLE%'
        startswith(Col, 'EXAMPLE')  → Col like 'EXAMPLE%'
        endswith(Col, 'EXAMPLE')    → Col like '%EXAMPLE'
    """
    def _repl(m: re.Match) -> str:
        fn = m.group("fn").lower()
        col = m.group("col")
        val = m.group("val")
        if fn == "startswith":
            pattern = f"{val}%"
        elif fn == "endswith":
            pattern = f"%{val}"
        else:  # contains
            pattern = f"%{val}%"
        return f"{col} like '{pattern}'"

    return _ODATA_STRFUNC_RE.sub(_repl, s)


_ODATA_ARITH_TO_SQL = {v: k for k, v in _ARITH_TO_ODATA.items()}


def _odata_arith_to_sql(s: str) -> str:
    """Inverse of ``_arith_to_odata`` — ``mul`` -> ``*`` outside quotes.

    Safe as a bare word substitution: no Epicor column is named mul/div/add/
    sub/mod (checked against the whole service index, 0 hits each).
    """
    def _rewrite(seg: str) -> str:
        for word, sym in _ODATA_ARITH_TO_SQL.items():
            seg = re.sub(rf"\b{word}\b", sym, seg, flags=re.IGNORECASE)
        return seg

    return _outside_quotes(s, _rewrite)


def _odata_to_sql_where(odata_filter: str) -> str:
    """Best-effort translation of an OData $filter to a SQL-style whereClause.

    Handles the most common OData operators.  Passes through filters that
    already look like SQL (contain ``=`` without ``eq``), but always
    applies date-literal quoting so the SQL form is consumable by
    Epicor's GetRows whereClause parser.

    Examples::

        "OpenOrder eq true"            → "OpenOrder = true"
        "VendorNum eq 1234"            → "VendorNum = 1234"
        "OrderDate ge 2024-01-01"      → "OrderDate >= '2024-01-01'"
        "OrderDate ge 2026-03-30T..."  → "OrderDate >= '2026-03-30T...'"
        "Name eq 'Acme'"               → "Name = 'Acme'"
        "OpenOrder = true"             → "OpenOrder = true"  (pass-through)
    """
    if not odata_filter:
        return ""

    s = odata_filter

    # Strip legacy V3 ``datetime'YYYY-MM-DDTHH:MM:SS'`` wrappers — neither
    # OData v4 nor GetRows accepts them.
    s = re.sub(
        r"datetime'(\d{4}-\d{2}-\d{2}(?:T[^']*)?)'",
        r"'\1'",
        s,
        flags=re.IGNORECASE,
    )

    # Convert OData string functions (contains/startswith/endswith) to SQL
    # LIKE before anything else — GetRows' whereClause parser has no such
    # functions and 500s on them. This is the GetRows counterpart to the
    # like→contains translation done on the OData input side.
    s = _odata_strfuncs_to_sql_like(s)

    # OData arithmetic words back to SQL symbols. GetRows' whereClause parser
    # is SQL: `OnHandQty * 2 > 100` -> 200 while
    # `OnHandQty mul 2 gt 100` -> 500. read.py translates to OData ONCE and
    # run_getrows re-translates to SQL, so without this the GetRows path breaks
    # for every filter the forward pass just fixed. Applied BEFORE the
    # early-return below, which a mixed filter (`Plant = '10' and OnHandQty mul
    # AvgCost gt 50000`) would otherwise take with `mul` still in place.
    s = _odata_arith_to_sql(s)

    # `eq null` / `ne null` are OData; SQL spells them IS [NOT] NULL.
    s = re.sub(r"\bne\s+null\b", "is not null", s, flags=re.IGNORECASE)
    s = re.sub(r"\beq\s+null\b", "is null", s, flags=re.IGNORECASE)

    # If it already looks like SQL (has = and NO OData comparator word), skip
    # the operator-translation pass but still apply date quoting. Testing for
    # ` eq ` alone was too narrow: a mixed filter like
    # `Plant = '10' and OnHandQty mul AvgCost gt 50000` took this branch and
    # shipped a bare `gt` into a SQL whereClause, which GetRows 500s on.
    if "=" in s and not re.search(r"\b(eq|ne|ge|le|gt|lt)\b", s, re.IGNORECASE):
        return _quote_sql_dates(s)

    # Replace OData comparison operators with SQL equivalents.
    # Process longer operators first to avoid partial replacements.
    s = re.sub(r"\b(ne)\b", "<>", s)
    s = re.sub(r"\b(ge)\b", ">=", s)
    s = re.sub(r"\b(le)\b", "<=", s)
    s = re.sub(r"\b(gt)\b", ">", s)
    s = re.sub(r"\b(lt)\b", "<", s)
    s = re.sub(r"\b(eq)\b", "=", s)

    return _quote_sql_dates(s)


# Services where OData entity set GET is known to fail.
# Populated at runtime so the OData attempt is skipped on repeat queries.
_getrows_services: set[str] = set()


# Child entities whose parent header carries the natural query keys.
# Filtering these directly by non-PK columns (e.g. GroupID, OrderDate)
# regularly throws Epicor's empty "We apologize" 500 because the column
# isn't indexed on the child table. When that error fires, we surface a
# pivot hint pointing at the parent.
# Mapping: child entity → (parent entity, [primary-key columns Claude
# should re-filter the child by]).
_CHILD_TO_PARENT_PIVOT: dict[str, tuple[str, list[str]]] = {
    "APInvDtl":  ("APInvHed",  ["VendorNum", "InvoiceNum"]),
    "InvcDtl":   ("InvcHead",  ["InvoiceNum"]),
    "OrderDtl":  ("OrderHed",  ["OrderNum"]),
    "OrderRel":  ("OrderHed",  ["OrderNum"]),
    "PODetail":  ("POHeader",  ["PONum"]),
    "PORel":     ("POHeader",  ["PONum"]),
    "JobOper":   ("JobHead",   ["JobNum"]),
    "JobMtl":    ("JobHead",   ["JobNum"]),
    "JobAsmbl":  ("JobHead",   ["JobNum"]),
    "RcvDtl":    ("RcvHead",   ["VendorNum", "PurPoint", "PackSlip"]),
    "ShipDtl":   ("ShipHead",  ["PackNum"]),
    "QuoteDtl":  ("QuoteHed",  ["QuoteNum"]),
    "QuoteQty":  ("QuoteHed",  ["QuoteNum"]),
    "PartWhse":  ("Part",      ["PartNum"]),
}


def _is_generic_epicor_apology(message: str) -> bool:
    """Detect Epicor's content-free internal-error message. Returned for
    many query plans Epicor's engine refuses (non-indexed filter, scope
    issues, etc.) — the message itself never contains the actual cause."""
    if not message:
        return False
    return "unexpected internal problem" in message.lower()


# Preferred detail child per parent header — used to build a concrete,
# copy-pasteable epicor_query_with_children example in the nudges below.
_PARENT_TO_PREFERRED_CHILD: dict[str, str] = {
    "OrderHed":  "OrderDtl",
    "POHeader":  "PODetail",
    "JobHead":   "JobOper",
    "InvcHead":  "InvcDtl",
    "APInvHed":  "APInvDtl",
    "QuoteHed":  "QuoteDtl",
    "RcvHead":   "RcvDtl",
    "ShipHead":  "ShipDtl",
}
_PIVOT_PARENTS: set[str] = {p for (p, _c) in _CHILD_TO_PARENT_PIVOT.values()}


def _child_query_enabled() -> bool:
    """True when the epicor_query_with_children tool is registered."""
    try:
        from epicor_mcp.config import get_settings

        return bool(get_settings().enable_child_query)
    except Exception:
        return False


def _resolve_parent_child(entity_set: str) -> tuple[str, str] | None:
    """Map a queried entity to its (parent, child) header/detail pair.

    Works whether *entity_set* is the header (e.g. ``OrderHed``) or one of
    its details (e.g. ``OrderDtl``). Returns ``None`` for entities that
    aren't part of a known parent/child dataset.
    """
    pivot = _CHILD_TO_PARENT_PIVOT.get(entity_set)
    if pivot is not None:
        parent = pivot[0]
        child = entity_set
    elif entity_set in _PIVOT_PARENTS:
        parent = entity_set
        child = _PARENT_TO_PREFERRED_CHILD.get(parent)
        if child is None:
            child = next(
                (c for c, (p, _k) in _CHILD_TO_PARENT_PIVOT.items()
                 if p == parent),
                None,
            )
    else:
        return None
    if not child:
        return None
    return parent, child


def _children_tool_nudge(service: str, entity_set: str) -> str | None:
    """One-line steer toward ``epicor_query_with_children`` when *entity_set*
    is a header or detail table in a parent/child dataset.

    This fires on *successful* queries (and counts) — precisely when Claude
    is probing/paging a header table or sampling its lines on the way to a
    per-record ``get_record`` loop. Returns ``None`` when the tool is
    disabled or the entity isn't part of a known dataset.
    """
    if not _child_query_enabled():
        return None
    pair = _resolve_parent_child(entity_set)
    if pair is None:
        return None
    parent, child = pair
    # Is the caller querying the CHILD/line table? That's the path that
    # silently fails: header attributes (Plant, customer, order date) are
    # NOT on the line, so filtering/selecting them here returns 0 rows or
    # errors. Say so explicitly — this is the single fact that stops the
    # "guess the plant field on the line table" probing loop.
    querying_child = entity_set in _CHILD_TO_PARENT_PIVOT
    lead = (
        (
            f"STOP probing '{child}': it is a DETAIL/line table. Header "
            f"attributes — Plant/site, CustNum, CustomerName, OrderDate, etc. "
            f"— live on the PARENT '{parent}', NOT on '{child}'. Filtering or "
            f"selecting them here returns 0 rows or an error (e.g. "
            f"OrderDtl.ECCPlant is blank, so 'plant 01' never matches the "
            f"line). "
        )
        if querying_child
        else (
            f"'{entity_set}' is the header of a parent/child dataset. "
        )
    )
    return (
        lead
        + f"For any question that needs '{parent}' and its '{child}' lines "
        f"together — 'broken down by / by customer / by month / by part / "
        f"summarize' — call epicor_query_with_children ONCE instead of "
        f"paging tables or looping epicor_get_record. It joins "
        f"{parent}+{child} and can group_by + aggregate. Put the header "
        f"condition (e.g. Plant eq '50' and OpenOrder eq true) in "
        f"parent_filter and the line condition (e.g. OpenLine eq true) in "
        f"child_filter. Example: epicor_query_with_children(service="
        f"\"{service}\", parent_entity=\"{parent}\", child_entity=\"{child}\""
        f", parent_filter=\"<header conditions>\", "
        f"child_filter=\"<line conditions>\", "
        f"parent_select=\"<header cols to keep>\", group_by=\"<cols>\", "
        f"aggregate=\"sum(<val>) as total, count(*)\")."
    )


# Denormalized customer/vendor lookup fields that ride on transactional
# headers (OrderHed, InvcHead, APInvHed, …) as copies of master-table
# values. Epicor's GetRows refuses to filter a transactional table by these
# — it throws the empty "We apologize" 500 (or pre-flight flags them as
# unknown when the column isn't on the table at all). The fix is always the
# same: resolve the name/code to the numeric FK on the master BO, then
# filter the header by that FK (CustNum/VendorNum — both indexed and fast).
_CUST_LOOKUP_FIELDS: frozenset[str] = frozenset({
    "customername", "customercustid", "custid", "btcustid",
    "shiptocustid", "shiptonum",
})
_VEND_LOOKUP_FIELDS: frozenset[str] = frozenset({
    "vendorname", "vendorvendorid", "vendorid",
})
# Don't fire on the master tables themselves — CustID is the right filter
# field on CustomerSvc, VendorID on VendorSvc.
_MASTER_ENTITY_SETS: frozenset[str] = frozenset({
    "customer", "customers", "vendor", "vendors",
})


def _name_resolution_hint(entity_set: str, filter_expr: str) -> dict | None:
    """Steer name→key resolution when a transactional table is filtered by a
    denormalized customer/vendor identifier.

    Filtering ``OrderHed``/``InvcHead`` by ``CustomerName`` or
    ``CustomerCustID = 'EXAMPLE01'`` is THE thrash loop: those fields are
    copied onto the header but aren't filterable through GetRows, so Epicor
    returns a content-free 500 and Claude guesses another field. The single
    fact that ends the loop: resolve the name on ``CustomerSvc`` to get
    ``CustNum``, then filter by ``CustNum`` (the indexed FK). Returns
    ``None`` when the filter doesn't reference a lookup field, or when the
    query is already against the master table.
    """
    if not filter_expr or entity_set.lower() in _MASTER_ENTITY_SETS:
        return None
    refs = {r.lower() for r in _extract_filter_identifiers(filter_expr)}
    if refs & _CUST_LOOKUP_FIELDS:
        return {
            "likely_cause": (
                f"'{entity_set}' carries customer fields (CustomerName, "
                "CustomerCustID) as denormalized copies of the Customer "
                "master. Epicor's query engine can't filter a transactional "
                "table by them — that's the empty 500 / unknown-column you "
                "got. CustNum (the numeric FK) IS filterable and fast."
            ),
            "fix": [
                "1. Resolve the customer to its CustNum FIRST. The value you "
                "have may be a NAME or a CustID (code) — users say both. Try "
                "the CustID exactly AND the name loosely in one call: "
                "epicor_query(service=\"Erp.BO.CustomerSvc\", "
                "entity_set=\"Customers\", filter=\"CustID eq '<value>' or "
                "Name like '%<value>%'\", select=\"CustNum,CustID,Name\"). "
                "Note a code like 'EXAMPLE01' is a CustID whose Name is "
                "'EXAMPLE AEROSPACE', so a Name-only search returns nothing.",
                f"2. Re-run this query filtered by the numeric key instead: "
                f"\"CustNum eq <CustNum from step 1>\" (combine with your "
                f"date range). Do NOT filter {entity_set} by CustomerName "
                f"or CustomerCustID.",
            ],
        }
    if refs & _VEND_LOOKUP_FIELDS:
        return {
            "likely_cause": (
                f"'{entity_set}' carries vendor fields as denormalized "
                "copies of the Vendor master, which Epicor can't filter a "
                "transactional table by. VendorNum (the numeric FK) is "
                "filterable and fast."
            ),
            "fix": [
                "1. Resolve the vendor to its VendorNum FIRST. The value you "
                "have may be a NAME or a VendorID (code) — try both in one "
                "call: epicor_query(service=\"Erp.BO.VendorSvc\", "
                "entity_set=\"Vendors\", filter=\"VendorID eq '<value>' or "
                "Name like '%<value>%'\", select=\"VendorNum,VendorID,Name\").",
                f"2. Re-run this query filtered by \"VendorNum eq <n>\" "
                f"instead of the vendor name/ID.",
            ],
        }
    return None


# Master tables where a textual ID/code field is the natural lookup key, used
# by the zero-result recovery: an exact ``Name eq`` that returns nothing is
# usually the user passing an ID (CustID 'EXAMPLE01' whose Name is actually
# 'EXAMPLE AEROSPACE') into the wrong field.
_NAME_ID_FIELD: dict[str, str] = {
    "customer": "CustID", "customers": "CustID",
    "vendor": "VendorID", "vendors": "VendorID",
    "part": "PartNum", "parts": "PartNum",
    "supplier": "VendorID", "suppliers": "VendorID",
}

# Matches a Name search in ANY form the filter may carry by the time the
# zero-result check runs: the SQL-ish input (``Name eq 'X'`` /
# ``Name like '%X%'``) OR the OData form it gets rewritten to before the GET
# (``contains(Name,'X')`` / ``startswith`` / ``endswith``). BOTH the eq and
# like/contains variants must trigger the recovery: the resolve_hint steers
# the model to a ``Name like`` lookup (→ ``contains(Name,'…')`` on the wire),
# so a recovery that only fired on ``Name eq`` would never fire on the path
# we push the model onto — exactly the code-searched-as-name thrash (a CustID searched
# as a Name: 0 rows, no guidance, loop forever).
_NAME_SEARCH_RE = re.compile(
    r"""(?:
            \bName\s+(?:eq|like)\s+'(?P<v1>(?:''|[^'])*)'
          | \b(?:contains|startswith|endswith)\s*\(\s*Name\s*,\s*
            '(?P<v2>(?:''|[^'])*)'\s*\)
        )""",
    re.IGNORECASE | re.VERBOSE,
)


def _zero_result_hint(entity_set: str, filter_expr: str) -> dict | None:
    """Recovery hint when a ``Name`` search (``eq`` or ``like``) returns zero.

    Zero rows from a Name lookup almost always means one of: the value is
    really an ID/code (the classic ``EXAMPLE01`` — that string is the
    *CustID*; the stored Name is ``EXAMPLE AEROSPACE``), or the stored name
    differs by spacing/punctuation/case. The single fact that ends the loop
    is to point at the ID field (CustID/VendorID/PartNum). Returns ``None``
    unless the filter contains a ``Name eq``/``Name like`` clause.
    """
    if not filter_expr:
        return None
    m = _NAME_SEARCH_RE.search(filter_expr)
    if not m:
        return None
    # Normalize the value: strip LIKE wildcards so the ID suggestion is clean.
    val = (m.group("v1") or m.group("v2") or "").strip("%").strip()
    token = val.split()[0] if val else val
    suggestions: list[str] = []
    id_field = _NAME_ID_FIELD.get(entity_set.lower())
    if id_field:
        # Lead with the ID field — for a code-shaped value this is the fix.
        suggestions.append(
            f"{id_field} eq '{val}'  — MOST LIKELY: '{val}' is the {id_field} "
            f"(code), not the name. Try this FIRST."
        )
    suggestions.append(
        f"Name like '%{token}%'  — partial match on just the first word "
        f"(case-insensitive), in case the full name differs"
    )
    return {
        "likely_cause": (
            f"A Name search for '{val}' returned 0 rows. This usually means "
            f"'{val}' is an ID/code (e.g. a CustID like 'EXAMPLE01') rather "
            f"than the stored Name, or the name differs by spacing/case."
        ),
        "try_instead": suggestions,
    }


def _child_pivot_hint(
    service: str,
    child_entity: str,
    failing_filter: str,
) -> dict | None:
    """Build a pivot hint when a child-table query 500s. Steers to the
    single-call ``epicor_query_with_children`` tool first (parent+child in
    one join), with the manual parent-then-child pattern as the fallback
    for callers without that tool. Returns ``None`` when the entity isn't
    in the pivot map."""
    pivot = _CHILD_TO_PARENT_PIVOT.get(child_entity)
    if pivot is None:
        return None
    parent, pk_cols = pivot
    pk_select = ",".join(pk_cols)
    hint: dict = {
        "likely_cause": (
            f"Epicor's query engine often refuses to filter the child "
            f"table '{child_entity}' by non-PK columns — the empty 500 "
            "you got is its way of saying 'this query plan is too wide.'"
        ),
    }
    if _child_query_enabled():
        hint["recommended_pattern"] = [
            f"BEST (one call): epicor_query_with_children(service="
            f"\"{service}\", parent_entity=\"{parent}\", "
            f"child_entity=\"{child_entity}\", parent_filter=\"<header "
            f"conditions>\", child_filter=\"{failing_filter}\") — pulls and "
            f"joins {parent}+{child_entity} in a single paged call; add "
            f"group_by/aggregate to roll it up.",
        ]
    else:
        hint["recommended_pattern"] = [
            f"1. epicor_query(service=\"{service}\", "
            f"entity_set=\"{parent}\", filter=\"{failing_filter}\", "
            f"select=\"{pk_select}\") — get the parent keys",
            f"2. For each row, epicor_query(service=\"{service}\", "
            f"entity_set=\"{child_entity}\", filter=\""
            + " and ".join(f"{c} eq <value>" for c in pk_cols)
            + "\") — fetch the matching child rows",
        ]
    return hint


# ---------------------------------------------------------------------------
# Pre-flight OData filter/select/orderby column validation
# ---------------------------------------------------------------------------
#
# Epicor's GetRows engine returns a generic ``400 We apologize, but an
# unexpected internal problem occurred`` whenever a whereClause references
# a column that doesn't exist. The OData layer does the same with a
# slightly less generic but still useless message. Catching unknown column
# names ourselves before the round-trip turns the loop-and-guess pattern
# into a single error with a precise ``did_you_mean`` suggestion.

# OData reserved words that look like field names but aren't. Keep this
# list tight — anything we miss becomes a false-positive that blocks a
# legal query. Also includes SQL operators that we translate to OData
# downstream (the validator runs before translation).
_ODATA_KEYWORDS: frozenset[str] = frozenset({
    "and", "or", "not", "eq", "ne", "ge", "le", "gt", "lt",
    "true", "false", "null",
    # String functions
    "contains", "startswith", "endswith", "tolower", "toupper",
    "trim", "length", "indexof", "substring", "concat", "replace",
    # Date functions
    "year", "month", "day", "hour", "minute", "second",
    "date", "datetime", "datetimeoffset",
    # Math functions
    "round", "floor", "ceiling",
    # Misc OData
    "asc", "desc", "any", "all",
    # SQL operators we translate to OData before sending — validator
    # runs first so we have to whitelist these here.
    "like", "in", "between", "is",
    # OData arithmetic words. Without these `_extract_filter_identifiers`
    # returns `mul` as a candidate column and the newly-working arithmetic
    # filter comes back as a FALSE unknown_columns. No real Epicor column is
    # named any of these (0 hits across the whole index).
    "mul", "div", "add", "sub", "mod",
})

# A bareword that looks like a candidate field reference. Matches any
# alphanumeric identifier (must start with a letter or underscore).
_IDENT_RE = re.compile(r"\b([A-Za-z_][\w]*)\b")


def _extract_filter_identifiers(filter_expr: str) -> set[str]:
    """Return the identifiers in *filter_expr* that look like field names.

    Strips:
      - String literals (``'foo'``), so quoted values aren't counted.
      - OData reserved words (operators, functions, true/false/null).
      - Bareword identifiers that immediately follow ``eq``/``ne``/etc.
        (those are the *values*, e.g. enum-like codes).

    Conservative — anything ambiguous stays in the set; the caller then
    intersects with the actual field list, so an extra spurious word
    just means "not in the schema, won't appear in did_you_mean."
    """
    if not filter_expr:
        return set()
    # Strip single-quoted literals (handle '' as escaped quote)
    stripped = re.sub(r"'(?:''|[^'])*'", " ", filter_expr)
    # Strip numeric literals — they're values, not field names
    stripped = re.sub(r"\b\d+(?:\.\d+)?\b", " ", stripped)
    # Strip date-ish literals (we already quoted these but be defensive)
    stripped = re.sub(r"\d{4}-\d{2}-\d{2}T?[^\s,)]*", " ", stripped)

    candidates: set[str] = set()
    for m in _IDENT_RE.finditer(stripped):
        tok = m.group(1)
        if tok.lower() in _ODATA_KEYWORDS:
            continue
        candidates.add(tok)
    return candidates


def _split_comma_list(s: str) -> list[str]:
    """Split a comma-separated identifier list, trimming whitespace and
    direction suffixes (``asc``/``desc``)."""
    if not s:
        return []
    out: list[str] = []
    for raw in s.split(","):
        token = raw.strip()
        if not token:
            continue
        # Drop trailing asc/desc on orderby entries
        parts = token.split()
        out.append(parts[0])
    return out


# Per-(service, entity) date-column sets. `get_fields` is a SQLite query and
# this now runs on every read, so cache it — one index per process.
_DATE_COLS_CACHE: dict[tuple[str, str], "frozenset[str] | None"] = {}


def date_columns_for(
    index: "ServiceIndex", service: str, entity_set: str
) -> "frozenset[str] | None":
    """Lowercased Edm date column names on the entity, or ``None`` if unindexed.

    ``None`` — NOT an empty frozenset — is the "we don't know this entity"
    signal: an empty set would silently disable date coercion, where ``None``
    tells ``_normalize_dates_for_odata`` to fall back to the anchored regex
    alone. An entity that genuinely has no date columns returns an empty set.
    """
    key = (service or "", entity_set or "")
    if key in _DATE_COLS_CACHE:
        return _DATE_COLS_CACHE[key]
    try:
        rows = index.get_fields(service, entity_set) or []
    except Exception:
        # A TRANSIENT index error must not be memoized: caching the resulting
        # None permanently disables the Edm type gate for this entity for the
        # whole process lifetime. Return the fallback WITHOUT storing it.
        return None
    if not rows:
        out: frozenset[str] | None = None
    else:
        out = frozenset(
            r["field_name"].lower() for r in rows
            if r.get("field_name") and (r.get("field_type") or "") in _DATE_EDM_TYPES
        )
    _DATE_COLS_CACHE[key] = out
    return out


def _validate_query_columns(
    index: "ServiceIndex",
    service: str,
    entity_set: str,
    filter_expr: str,
    select: str,
    orderby: str,
) -> list[dict]:
    """Return unknown-column diagnostics for the filter/select/orderby.

    Empty list means everything looks valid (or we couldn't validate
    because the entity set isn't in the index). Each diagnostic dict::

        {"ref": "SalesRepCode",
         "where": "filter",
         "did_you_mean": ["SalesRepList", "EntryPerson", ...]}
    """
    try:
        rows = index.get_fields(service, entity_set)
    except Exception:
        return []
    if not rows:
        return []  # Don't accuse what we can't verify

    known_names = [r.get("field_name", "") for r in rows if r.get("field_name")]
    known_lower = {n.lower(): n for n in known_names}

    diagnostics: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def check(name: str, where: str) -> None:
        # "*" is the select-all wildcard, not a column — never flag it.
        if name == "*":
            return
        key = (name.lower(), where)
        if key in seen:
            return
        seen.add(key)
        if name.lower() in known_lower:
            return
        suggestions = difflib.get_close_matches(name, known_names, n=5, cutoff=0.5)
        if not suggestions:
            nl = name.lower()
            suggestions = [n for n in known_names if nl in n.lower() or n.lower() in nl][:5]
        diagnostics.append({
            "ref": name,
            "where": where,
            "did_you_mean": suggestions,
        })

    for ident in _extract_filter_identifiers(filter_expr):
        check(ident, "filter")
    for col in _split_comma_list(select):
        check(col, "select")
    for col in _split_comma_list(orderby):
        check(col, "orderby")

    return diagnostics


def _suggest_entity_sets(
    index: "ServiceIndex",
    service: str,
    requested: str,
    n: int = 5,
) -> list[str]:
    """Return the closest entity-set names on *service* to *requested*.

    Handles common typo / pluralisation slips
    (``PODetails`` → ``PODetail``, ``Customers`` → ``Customer``). Always
    returns a list; empty if the service has no entity sets.
    """
    try:
        available = index.get_entity_sets(service) or []
    except Exception:
        return []
    if not available or not requested:
        return available[:n]
    matches = difflib.get_close_matches(requested, available, n=n, cutoff=0.5)
    if matches:
        return matches
    req_lower = requested.lower()
    matches = [a for a in available if req_lower in a.lower() or a.lower() in req_lower]
    return matches[:n] if matches else available[:n]


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
) -> None:
    """Bind the ``epicor_query`` tool to *server*."""

    description = build_query_description(index)

    @server.tool(structured_output=False, description=description)
    async def epicor_query(
        service: str,
        entity_set: str,
        filter: str = "",
        select: str = "",
        orderby: str = "",
        top: int = 10,
        skip: int = 0,
        expand: str = "",
        count_only: bool = False,
        group_by: str = "",
        aggregate: str = "",
        distinct: str = "",
        format: str = "json",
    ) -> str:
        """Query an Epicor entity set with optional in-process aggregation.

        Aggregation params (computed AFTER rows are fetched, so they're
        bounded by ``top``; raise ``top`` if you need totals over a wider
        scan):

        - ``distinct``: comma-separated columns. Returns unique value
          combinations with a per-combo ``count`` (e.g. ``"Plant"``).
        - ``group_by``: comma-separated columns to group on (e.g.
          ``"CustNum,PartNum"``). Defaults to ``count(*)`` per group when
          ``aggregate`` is empty.
        - ``aggregate``: comma-separated ``func(field) [as alias]`` —
          ``sum``, ``avg``, ``min``, ``max``, ``count``. E.g.
          ``"sum(ExtPrice) as total, count(*)"``. Without ``group_by``
          this returns a single summary row.
        """
        try:
            session = get_current_session()

            # --- RBAC check ------------------------------------------------
            allowed, msg = rbac.check_access(session.user_id, service)
            if not allowed:
                return json.dumps({"error": msg})

            svc_result = rbac.check_service_access(session.user_id, service)
            api_key = svc_result.api_key or ""

            # --- Pre-flight column validation ------------------------------
            # Reject references to columns that don't exist on the named
            # entity set BEFORE we forward to Epicor — its 400/500 errors
            # don't name the bad column, which sends Claude into a guess
            # loop. Skipped when the entity set isn't in the index.
            try:
                unknown = _validate_query_columns(
                    index, service, entity_set, filter, select, orderby,
                )
            except Exception:
                logger.exception("pre-flight column validation crashed; skipping")
                unknown = []
            if unknown:
                payload: dict = {
                    "error": "unknown_columns",
                    "service": service,
                    "entity_set": entity_set,
                    "message": (
                        "Your filter/select/orderby references columns "
                        "that don't exist on this entity set. Fix the "
                        "names (see did_you_mean) and retry. Caught "
                        "before sending to Epicor."
                    ),
                    "unknown_columns": unknown,
                }
                # Guessing a header field (Plant, CustNum, …) on a detail/line
                # table is THE wrong-field guess loop. Redirect to the join
                # tool before Claude burns more calls hunting the column.
                nudge = _children_tool_nudge(service, entity_set)
                if nudge:
                    payload["efficiency_hint"] = nudge
                # Filtering a header by CustID/CustomerName/etc. — resolve the
                # name to CustNum on CustomerSvc first instead of chasing a
                # did_you_mean that just 500s.
                resolve = _name_resolution_hint(entity_set, filter)
                if resolve:
                    payload["resolve_hint"] = resolve
                return json.dumps(payload)

            # --- Translate SQL-ish filter syntax to OData -------------------
            # Users frequently send "Col like '%x%'", "Col in (1,2,3)",
            # "Date >= '2026-01-01'". OData rejects those; translate.
            filter = _sql_to_odata_filter(filter)

            # --- Clamp top --------------------------------------------------
            top = max(1, min(top, 1000))

            # --- Auto-CSV at scale -----------------------------------------
            # CSV fits ~2-3x more rows in the response budget, and a truncated
            # JSON preview at top>=200 is usually re-run as CSV anyway. Skip when aggregating (result is small).
            if (
                format == "json"
                and top >= 200
                and not (group_by or aggregate or distinct or count_only)
            ):
                format = "csv"

            # --- Decide: OData vs GetRows -----------------------------------
            use_getrows = service in _getrows_services

            if not use_getrows:
                try:
                    return await _odata_query(
                        client, service, entity_set, api_key,
                        filter=filter, select=select, orderby=orderby,
                        top=top, skip=skip,
                        expand=expand, count_only=count_only,
                        group_by=group_by, aggregate=aggregate,
                        distinct=distinct, format=format,
                    )
                except EpicorError as exc:
                    msg_lower = exc.message.lower() if exc.message else ""
                    is_segment_404 = "resource not found for the segment" in msg_lower
                    if is_segment_404:
                        # Could be a service-wide "no OData collections" case
                        # (fall back to GetRows) OR a typo in entity_set
                        # (suggest correct names).
                        suggestions = _suggest_entity_sets(index, service, entity_set)
                        if entity_set in (index.get_entity_sets(service) or []):
                            _getrows_services.add(service)
                            use_getrows = True
                            logger.info(
                                "Service %s does not support OData entity sets; "
                                "falling back to GetRows.", service,
                            )
                        else:
                            return json.dumps({
                                "error": (
                                    f"Entity set '{entity_set}' not found on "
                                    f"service '{service}'."
                                ),
                                "did_you_mean": suggestions,
                                "hint": (
                                    "Entity-set names are case-sensitive and "
                                    "rarely pluralised. Try one of the suggestions, "
                                    "or call epicor_describe_service to list all sets."
                                ),
                            })
                    else:
                        raise

            if use_getrows:
                return await _getrows_query(
                    client, index, service, entity_set, api_key,
                    filter=filter, select=select, orderby=orderby,
                    top=top, skip=skip,
                    count_only=count_only,
                    group_by=group_by, aggregate=aggregate,
                    distinct=distinct, format=format,
                )

            return json.dumps({"error": "Unexpected query routing error."})

        except Exception:
            logger.exception("epicor_query failed")
            payload: dict = {
                "error": (
                    f"Query against {service}/{entity_set} failed. "
                    "Check the service name, entity set, and filter syntax."
                )
            }
            suggestions = _suggest_entity_sets(index, service, entity_set)
            if suggestions and entity_set not in (index.get_entity_sets(service) or []):
                payload["did_you_mean"] = suggestions
            nudge = _children_tool_nudge(service, entity_set)
            if nudge:
                payload["efficiency_hint"] = nudge
            return json.dumps(payload)

    # --- OData path --------------------------------------------------------

    async def _odata_query(
        client: "EpicorClient",
        service: str,
        entity_set: str,
        api_key: str,
        *,
        filter: str,
        select: str,
        orderby: str,
        top: int,
        skip: int = 0,
        expand: str,
        count_only: bool,
        group_by: str = "",
        aggregate: str = "",
        distinct: str = "",
        format: str,
    ) -> str:
        """Execute a direct OData GET on the entity set endpoint."""

        if count_only:
            count_url = f"{service}/{entity_set}/$count"
            count_params: dict[str, str] = {}
            if filter:
                count_params["$filter"] = filter
            response_raw = await client.get(count_url, api_key, params=count_params)
            if isinstance(response_raw, dict):
                count_value = response_raw.get("_raw", response_raw)
            else:
                count_value = response_raw
            try:
                count_int = int(str(count_value).strip())
            except (ValueError, TypeError):
                count_int = count_value
            count_payload: dict = {
                "count": count_int,
                "service": service,
                "entity_set": entity_set,
                "filter": filter or "(none)",
            }
            nudge = _children_tool_nudge(service, entity_set)
            if nudge:
                count_payload["efficiency_hint"] = nudge
            return format_response(count_payload, records_key=None)

        params: dict[str, str | int] = {"$top": top}
        if skip > 0:
            params["$skip"] = skip
        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = select
        if orderby:
            params["$orderby"] = orderby
        if expand:
            params["$expand"] = expand

        url = f"{service}/{entity_set}"
        response = await client.get(url, api_key, params=params)

        records = response.get("value", response)
        record_count = len(records) if isinstance(records, list) else None

        if (group_by or aggregate or distinct) and isinstance(records, list):
            try:
                agg_result = aggregate_records(
                    records,
                    group_by=group_by,
                    aggregate=aggregate,
                    distinct=distinct,
                )
            except ValueError as ve:
                return json.dumps({"error": str(ve)})
            if record_count == top:
                agg_result["note"] = (
                    f"Aggregated over the first {top} rows. Raise 'top' "
                    "(up to 1000) to widen the scan."
                )
            return format_response(agg_result, records_key="records", format=format)

        result: dict = {"records": records}
        if record_count is not None:
            result["record_count"] = record_count
        if record_count == top:
            result["note"] = (
                f"Result limited to {top} records. "
                "Increase 'top' or refine your filter to see more."
            )
        if record_count == 0:
            zero = _zero_result_hint(entity_set, filter)
            if zero:
                result["no_match_hint"] = zero
        nudge = _children_tool_nudge(service, entity_set)
        if nudge:
            result["efficiency_hint"] = nudge

        return format_response(result, records_key="records", format=format)

    # --- GetRows fallback path ---------------------------------------------

    async def _getrows_query(
        client: "EpicorClient",
        index: "ServiceIndex",
        service: str,
        entity_set: str,
        api_key: str,
        *,
        filter: str,
        select: str,
        orderby: str,
        top: int,
        skip: int = 0,
        count_only: bool,
        group_by: str = "",
        aggregate: str = "",
        distinct: str = "",
        format: str,
    ) -> str:
        """Fall back to the service's GetRows method.

        Builds table-specific ``whereClause{Table}`` params from the
        service index.  The target entity_set gets the filter; all
        other tables get ``1=0`` to exclude child data.

        Epicor's GetRows does not support OData ``$select``, so when the
        caller passed one we project the requested columns client-side
        after extracting the rows.  Without this projection the response
        carries every field on the target table (often 500+), defeating
        the caller's intent and routinely blowing past the response-size
        budget.
        """
        where_clause = _odata_to_sql_where(filter)

        # Append ordering to the whereClause if provided
        if orderby:
            # Translate OData orderby ("OrderDate desc") to SQL ("By OrderDate desc")
            order_sql = orderby.strip()
            if not order_sql.lower().startswith("by "):
                order_sql = f"By {order_sql}"
            if where_clause:
                where_clause = f"({where_clause}) {order_sql}"
            else:
                where_clause = order_sql

        # GetRows takes whereClauseX params for each table in the
        # service's primary DataSet — those are the entity sets the
        # index has FIELDS for.  Plural-only OData collection names
        # (e.g. "Vendors", "Customers", "Parts") come from a separate
        # source and are NOT valid GetRows targets; including them
        # causes Epicor to throw an internal error.
        all_entity_sets = index.get_entity_sets(service)
        real_tables = [
            es for es in all_entity_sets if index.get_fields(service, es)
        ]

        # If the caller passed an OData URL/plural form (no fields in
        # the index), translate it to the corresponding DataSet table
        # name by stripping the plural suffix.
        target_table = entity_set
        if entity_set not in real_tables:
            for stripped in (
                entity_set[:-1] if entity_set.endswith("s") else None,
                entity_set[:-2] if entity_set.endswith("es") else None,
            ):
                if stripped and stripped in real_tables:
                    target_table = stripped
                    break

        # absolutePage is 1-based and pages are sized by pageSize.
        # OData $skip is a row offset, so translate: page = floor(skip/top)+1.
        # When skip isn't an even multiple of top we still skip a few extra
        # rows client-side after the fetch.
        page = (skip // top) + 1 if skip > 0 else 1
        skip_remainder = skip - (page - 1) * top if skip > 0 else 0
        body: dict = {"pageSize": top, "absolutePage": page}
        matched_target = False
        for es in real_tables:
            if es == target_table:
                body[f"whereClause{es}"] = where_clause
                matched_target = True
            else:
                body[f"whereClause{es}"] = "1=0"

        if not matched_target:
            # Target table isn't in the index either — best-effort.
            body[f"whereClause{target_table}"] = where_clause

        url = f"{service}/GetRows"
        try:
            response = await client.post(url, api_key, json_body=body)
        except EpicorError as exc:
            payload: dict = {
                "error": (
                    f"Query against {service}/{entity_set} failed. "
                    f"Neither OData nor GetRows worked. "
                    f"GetRows error: {exc.message}"
                )
            }
            suggestions = _suggest_entity_sets(index, service, entity_set)
            if suggestions and entity_set not in (index.get_entity_sets(service) or []):
                payload["did_you_mean"] = suggestions
            # Empty-500 against a known child entity? Point at the parent.
            if _is_generic_epicor_apology(exc.message or ""):
                pivot = _child_pivot_hint(service, target_table, filter)
                if pivot is not None:
                    payload["pivot_hint"] = pivot
                # Empty-500 because the header was filtered by a denormalized
                # customer/vendor name field — steer to name→CustNum resolution.
                resolve = _name_resolution_hint(entity_set, filter)
                if resolve is not None:
                    payload["resolve_hint"] = resolve
            # An error on a header/detail table is the strongest "wrong path"
            # signal — surface the single-call join tool here too, not just on
            # success, so a failed probe redirects immediately.
            nudge = _children_tool_nudge(service, entity_set)
            if nudge:
                payload["efficiency_hint"] = nudge
            return json.dumps(payload)

        # Extract the dataset from the response
        data = response.get("returnObj", response)

        # Find the target table's rows.
        # Only fall through to "first non-empty list" when the requested
        # entity_set isn't present in the dataset at all — do NOT fall
        # through when the target table is present but empty, because
        # that legitimately means "no matching rows" and falling through
        # would silently return unrelated data (e.g. a TaxConnectStatus
        # metadata row when QuoteDtl matched nothing).
        records = []
        if isinstance(data, dict):
            # GetRows returns rows keyed by DataSet table name (singular),
            # which is target_table after any plural→singular translation.
            if target_table in data:
                records = data.get(target_table) or []
            else:
                for key, value in data.items():
                    if isinstance(value, list) and value:
                        records = value
                        break

        # Trim the page-alignment remainder when skip isn't a multiple
        # of top (GetRows pages by absolutePage * pageSize).
        if skip_remainder > 0 and len(records) > skip_remainder:
            records = records[skip_remainder:]

        if select:
            wanted = [c.strip() for c in select.split(",") if c.strip()]
            if wanted:
                records = [
                    {k: row.get(k) for k in wanted}
                    for row in records
                    if isinstance(row, dict)
                ]

        record_count = len(records)

        if count_only:
            count_payload: dict = {
                "count": record_count,
                "service": service,
                "entity_set": entity_set,
                "filter": filter or "(none)",
                "note": "Count via GetRows (limited by pageSize).",
            }
            nudge = _children_tool_nudge(service, entity_set)
            if nudge:
                count_payload["efficiency_hint"] = nudge
            return format_response(count_payload, records_key=None)

        if group_by or aggregate or distinct:
            try:
                agg_result = aggregate_records(
                    records,
                    group_by=group_by,
                    aggregate=aggregate,
                    distinct=distinct,
                )
            except ValueError as ve:
                return json.dumps({"error": str(ve)})
            if record_count == top:
                agg_result["note"] = (
                    f"Aggregated over the first {top} rows. Raise 'top' "
                    "(up to 1000) to widen the scan."
                )
            return format_response(agg_result, records_key="records", format=format)

        result: dict = {"records": records, "record_count": record_count}
        if record_count == top:
            result["note"] = (
                f"Result limited to {top} records. "
                "Increase 'top' or refine your filter to see more."
            )
        if record_count == 0:
            zero = _zero_result_hint(entity_set, filter)
            if zero:
                result["no_match_hint"] = zero
        nudge = _children_tool_nudge(service, entity_set)
        if nudge:
            result["efficiency_hint"] = nudge

        return format_response(result, records_key="records", format=format)

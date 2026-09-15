"""Post-query aggregation for epicor_query and epicor_run_baq.

Callers can request totals or distinct values from fetched rows. Performing
that aggregation in-process keeps the response in budget.

Aggregation is performed AFTER rows have been fetched from Epicor, so the
``top`` parameter still bounds how many rows are scanned. For totals over a
whole table, raise ``top`` accordingly (or use a BAQ with a SQL GROUP BY).
"""

from __future__ import annotations

import datetime as _dt
import difflib
import re
from typing import Any

from epicor_mcp.tools._inline_schema import (
    best_date_column,
    order_terms_to_clause,
    parse_order_by,
)
# `_resolve` imports only `_inline_schema` at module level, so this is not a
# cycle — `_aggregate` owns the rollup logic and borrows the INV-1 builder.
from epicor_mcp.tools._resolve import error_envelope

# Matches "func(field)" or "func(field) as alias" — case-insensitive,
# tolerates whitespace, bracketed names, a synonym function (total/average/
# cnt/...), and a qualified field (OrderDtl.OrderQty — the qualifier is
# stripped in parse_aggregates since the rollup runs over flattened rows keyed
# by bare column name).
# The single-identifier alternative stays FIRST so every existing spec parses
# bit-identically; the expression alternative only catches what used to raise.
_AGG_RE = re.compile(
    r"""^\s*
        (?P<func>[A-Za-z]+)
        \s*\(\s*
        (?:
            (?P<field>\*|\[?[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?\]?)
          | (?P<expr>[A-Za-z_0-9.\s*/+\-()]+)
        )
        \s*\)\s*
        (?:as\s+(?P<alias>\[?[A-Za-z_][\w]*\]?)\s*)?
        $""",
    re.IGNORECASE | re.VERBOSE,
)
# The "field:func" shorthand the model reflexively types (OrderQty:sum). Same
# output shape as _AGG_RE so parse_aggregates handles either uniformly.
_COLON_AGG_RE = re.compile(
    r"""^\s*
        (?P<field>\*|\[?[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?\]?)
        \s*:\s*
        (?P<func>[A-Za-z]+)\s*
        (?:as\s+(?P<alias>\[?[A-Za-z_][\w]*\]?)\s*)?
        $""",
    re.IGNORECASE | re.VERBOSE,
)
# Fold aggregate-function synonyms to the canonical five.
_AGG_SYNONYMS = {
    "count": "count", "cnt": "count",
    "sum": "sum", "total": "sum",
    "avg": "avg", "average": "avg", "mean": "avg",
    "min": "min", "minimum": "min",
    "max": "max", "maximum": "max",
}


def _strip_brackets(name: str) -> str:
    return name.strip().lstrip("[").rstrip("]")


# ---------------------------------------------------------------------------
# Restricted arithmetic expressions  (sum(OnHandQty * AvgCost))
# ---------------------------------------------------------------------------
# "Inventory value = quantity x cost" had no route through either tool: the
# aggregate parser accepted a single identifier only, and the error said just
# NO, naming no path to the goal — so the model substituted a DIFFERENT column
# (ExtCost for AvgCost) and returned a plausible wrong number.
#
# Rollups are ALREADY entirely client-side (read.py forces route_getrows for
# any group_by/aggregate and aggregate_records computes in-process; a live
# probe confirmed $apply is silently IGNORED by this Epicor instance, which
# would return raw unaggregated rows presented as a total). So evaluating
# `A * B` per row costs nothing extra — the rows are already in memory. The
# only requirement is that BOTH component columns ride on the projection,
# which is _rollup_columns' job in read.py.
_EXPR_TOKEN_RE = re.compile(
    r"\s*(?P<tok>\d+\.\d+|\d+|[A-Za-z_]\w*|\*\*|[*/+\-()])")


def _is_operand(tok: str) -> bool:
    return tok[0].isalnum() or tok[0] == "_"


def _tokenize_expr(expr: str) -> list[str] | None:
    """Tokens of a restricted arithmetic expression, or None if it has anything
    outside identifiers / numbers / ``* / + -`` / parens. No ``eval``, ever."""
    tokens: list[str] = []
    pos = 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        m = _EXPR_TOKEN_RE.match(expr, pos)
        if not m or m.group("tok") == "**":
            return None
        tokens.append(m.group("tok"))
        pos = m.end()
    if not tokens:
        return None
    # Two operands in a row is not arithmetic — it is SQL we don't speak
    # (`CASE WHEN x THEN y END` tokenizes as bare identifiers and would
    # otherwise evaluate to None on every row, i.e. a silent wrong total).
    if any(_is_operand(a) and _is_operand(b)
           for a, b in zip(tokens, tokens[1:])):
        return None
    return tokens


def parse_expr_fields(expr: str) -> list[str]:
    """The column identifiers an expression references, in order, deduped."""
    out: list[str] = []
    for tok in (_tokenize_expr(expr) or []):
        if tok[0].isalpha() or tok[0] == "_":
            if tok not in out:
                out.append(tok)
    return out


def _eval_tokens(tokens: list[str], row: dict[str, Any]) -> float | None:
    """Recursive-descent evaluation of the token list against one row.

    Any identifier that is missing/non-numeric makes the WHOLE row's value
    ``None``. Never coerce a missing component to 0 — that is the silent-zero
    the ``aggregate_warning`` machinery exists to prevent.
    """
    pos = 0

    def parse_expr() -> float | None:
        nonlocal pos
        val = parse_term()
        while pos < len(tokens) and tokens[pos] in ("+", "-"):
            op = tokens[pos]
            pos += 1
            rhs = parse_term()
            if val is None or rhs is None:
                val = None
            else:
                val = val + rhs if op == "+" else val - rhs
        return val

    def parse_term() -> float | None:
        nonlocal pos
        val = parse_atom()
        while pos < len(tokens) and tokens[pos] in ("*", "/"):
            op = tokens[pos]
            pos += 1
            rhs = parse_atom()
            if val is None or rhs is None:
                val = None
            elif op == "*":
                val = val * rhs
            else:
                val = None if rhs == 0 else val / rhs
        return val

    def parse_atom() -> float | None:
        nonlocal pos
        if pos >= len(tokens):
            return None
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            val = parse_expr()
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return val
        if tok == "-":
            pos += 1
            inner = parse_atom()
            return None if inner is None else -inner
        pos += 1
        if tok[0].isdigit():
            return float(tok)
        if tok[0].isalpha() or tok[0] == "_":
            return _to_number(row.get(tok))
        return None

    return parse_expr()


def eval_expr(expr: str, row: dict[str, Any]) -> float | None:
    tokens = _tokenize_expr(expr)
    if not tokens:
        return None
    return _eval_tokens(tokens, row)


# group_by may wrap a date column in a bucketing function, e.g.
# "year(OrderDate)", "month(InvoiceDate)". Without this the column was looked
# up literally — r.get("year(OrderDate)") is always None, silently collapsing
# every row into one bogus group (the "YoY came back as a single total" bug).
_GROUP_FN_RE = re.compile(
    r"^\s*(?P<fn>year|quarter|month|day|date)\s*\(\s*"
    r"(?P<field>\[?[A-Za-z_][\w]*\]?)\s*\)\s*$",
    re.IGNORECASE,
)

_ISO_DATE_HEAD = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _eval_date_fn(fn: str, value: Any) -> Any:
    """Apply a date bucketing function to an Epicor ISO date value.

    ``year``→int year, ``quarter``→1-4, ``month``→"YYYY-MM" (trend-friendly),
    ``day``→int day-of-month, ``date``→"YYYY-MM-DD". Returns ``None`` for a
    null/unparseable value so it groups distinctly from real dates.
    """
    if value is None or value == "":
        return None
    m = _ISO_DATE_HEAD.match(str(value))
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    fn = fn.lower()
    if fn == "year":
        return year
    if fn == "quarter":
        return (month - 1) // 3 + 1
    if fn == "month":
        return f"{m.group(1)}-{m.group(2)}"
    if fn == "day":
        return day
    if fn == "date":
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _compile_group_col(spec: str):
    """Return ``(label, resolver)`` for one group_by column.

    A bare field resolves with ``row.get(field)``; a ``fn(field)`` spec
    resolves by applying the date function. The label is the original spec
    (so ``year(OrderDate)`` shows as that in the output rows).
    """
    spec = spec.strip()
    m = _GROUP_FN_RE.match(spec)
    if m:
        fn = m.group("fn").lower()
        field = _strip_brackets(m.group("field"))
        return spec, (lambda r, fn=fn, field=field: _eval_date_fn(fn, r.get(field)))
    field = _strip_brackets(spec)
    return field, (lambda r, field=field: r.get(field))


def group_by_base_fields(group_by: str) -> list[str]:
    """Return the underlying column names a group_by references.

    Unwraps date functions: ``"year(OrderDate),CustNum"`` → ``["OrderDate",
    "CustNum"]``. Used by callers to validate group_by against a real field
    list without tripping over the function syntax.
    """
    out: list[str] = []
    for raw in group_by.split(","):
        c = raw.strip()
        if not c:
            continue
        m = _GROUP_FN_RE.match(c)
        out.append(_strip_brackets(m.group("field")) if m else _strip_brackets(c))
    return out


def parse_aggregates(spec: str) -> list[dict[str, str]]:
    """Parse ``"sum(ExtPrice) as total, count(*)"`` -> list of agg dicts.

    Each agg is ``{"func": "sum", "field": "ExtPrice", "alias": "total"}``.
    Raises ``ValueError`` on malformed input.
    """
    if not spec:
        return []
    parts = [p for p in (s.strip() for s in spec.split(",")) if p]
    out: list[dict[str, str]] = []
    for part in parts:
        m = _AGG_RE.match(part) or _COLON_AGG_RE.match(part)
        if not m:
            raise ValueError(
                f"Invalid aggregate '{part}'. Use e.g. "
                "'sum(ExtPrice) as total', 'count(*)', 'avg(UnitPrice)', an "
                "arithmetic expression 'sum(OnHandQty * AvgCost) as "
                "InventoryValue' — or the shorthand 'ExtPrice:sum'. For "
                "anything more complex (CASE, sub-selects, cross-table math), "
                "use epicor_baq action='create' with "
                "fields=\"PartNum, (OnHandQty * AvgCost) as InventoryValue\", "
                "which is real SQL."
            )
        func = _AGG_SYNONYMS.get(m.group("func").lower())
        if func is None:
            raise ValueError(
                f"Unknown aggregate function in '{part}'. Use "
                "count/sum/avg/min/max (or synonyms total/average/cnt/...)."
            )
        expr = (m.groupdict().get("expr") or "").strip()
        if expr:
            # Arithmetic form: sum(OnHandQty * AvgCost). Strip table qualifiers
            # the same way the single-field branch does — the rollup runs over
            # flattened rows keyed by bare column name.
            expr = re.sub(r"\b[A-Za-z_]\w*\.([A-Za-z_]\w*)\b", r"\1", expr)
            components = parse_expr_fields(expr)
            if (not components or _tokenize_expr(expr) is None
                    or not any(op in expr for op in "*/+-")):
                raise ValueError(
                    f"Could not parse the expression in '{part}'. Only "
                    "identifiers, numbers, * / + - and parentheses are "
                    "supported here. For anything more complex use "
                    "epicor_baq action='create'."
                )
            if func == "count":
                raise ValueError(
                    f"'{part}' is not meaningful — count() takes a column or "
                    "*, not an expression. Use sum/avg/min/max for arithmetic."
                )
            alias = m.group("alias")
            alias = (_strip_brackets(alias) if alias
                     else f"{func}_{'_'.join(components)}")
            out.append({"func": func, "field": "", "alias": alias,
                        "expr": expr})
            continue
        field = _strip_brackets(m.group("field"))
        if "." in field:  # OrderDtl.OrderQty -> OrderQty (flattened row key)
            field = field.rsplit(".", 1)[-1]
        if field == "*" and func != "count":
            raise ValueError(
                f"'{part}' is not meaningful — only count(*) works without a "
                "column; sum/avg/min/max need a field name."
            )
        alias = m.group("alias")
        if alias:
            alias = _strip_brackets(alias)
        else:
            alias = "count" if (func == "count" and field == "*") else f"{func}_{field}"
        # No ``expr`` key on the plain form: the dict shape of every
        # pre-existing aggregate stays byte-identical. Readers use
        # ``agg.get("expr")``.
        out.append({"func": func, "field": field, "alias": alias})
    return out


def _to_number(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _apply_aggs_to_group(
    rows: list[dict[str, Any]],
    aggs: list[dict[str, str]],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for agg in aggs:
        func = agg["func"]
        field = agg["field"]
        alias = agg["alias"]
        if func == "count" and field == "*":
            out[alias] = len(rows)
            continue
        expr = agg.get("expr") or ""
        vals = ([eval_expr(expr, r) for r in rows] if expr
                else [r.get(field) for r in rows])
        if func == "count":
            out[alias] = sum(1 for v in vals if v is not None and v != "")
        elif func in ("sum", "avg"):
            nums = [n for n in (_to_number(v) for v in vals) if n is not None]
            if func == "sum":
                out[alias] = sum(nums)
            else:
                out[alias] = (sum(nums) / len(nums)) if nums else None
        elif func == "min":
            non_null = [v for v in vals if v is not None]
            out[alias] = min(non_null) if non_null else None
        elif func == "max":
            non_null = [v for v in vals if v is not None]
            out[alias] = max(non_null) if non_null else None
    return out


def _unknown_agg_fields(
    records: list[dict[str, Any]],
    aggs: list[dict[str, str]],
) -> list[str]:
    """Aggregate field names that exist on NONE of the scanned rows.

    Summing a field the rows don't carry (e.g. ``sum(InvcAmt)`` when the real
    column is ``InvoiceAmt``) silently yields 0 — a wrong answer that reads
    as authoritative. GetRows rows share one schema, so a field absent from a
    sample of rows is absent everywhere; we sample the first 50 to stay cheap
    on large scans. ``count(*)`` (field ``*``) is never flagged.
    """
    if not records:
        return []
    known: set[str] = set()
    for r in records[:50]:
        known.update(r.keys())
    seen: list[str] = []
    for agg in aggs:
        # An expression agg has no single field — check EVERY component, else
        # a typo'd component silently yields a column of None and a total that
        # still reads authoritative.
        names = (parse_expr_fields(agg["expr"]) if agg.get("expr")
                 else [agg["field"]])
        for field in names:
            if field and field != "*" and field not in known and field not in seen:
                seen.append(field)
    return seen


def _attach_agg_field_warning(
    result: dict[str, Any],
    records: list[dict[str, Any]],
    aggs: list[dict[str, str]],
) -> None:
    """Add an ``aggregate_warning`` if any aggregate field is unknown.

    Turns the silent-zero into a loud, actionable message with a
    ``did_you_mean`` drawn from the fields the rows actually carry — so a
    wrong field name is fixed in one retry instead of shipping a bogus 0.
    """
    unknown = _unknown_agg_fields(records, aggs)
    if not unknown:
        return
    known = sorted({k for r in records[:50] for k in r.keys()})
    suggestions: dict[str, list[str]] = {}
    for field in unknown:
        close = difflib.get_close_matches(field, known, n=4, cutoff=0.5)
        # Fall back to substring matches (get_close_matches misses
        # InvcAmt→DocInvoiceAmt because of the prefix) so the real field
        # still surfaces.
        if not close:
            low = field.lower()
            close = [k for k in known if low in k.lower() or k.lower() in low][:4]
        suggestions[field] = close
    result["aggregate_warning"] = (
        "These aggregate field(s) don't exist on the returned rows, so their "
        f"totals are 0/None and NOT correct: {', '.join(unknown)}. Re-run with "
        "a real field name (see did_you_mean); do not report these values."
    )
    result["aggregate_unknown_fields"] = suggestions


# A post-aggregation threshold ("...over 50k"). Matches either the aggregate's
# ALIAS (InventoryValue > 50000) or its full spec (sum(A * B) > 50000) so the
# model can express it whichever way it thinks of it.
_HAVING_RE = re.compile(
    r"^\s*(?P<lhs>.+?)\s*(?P<op>>=|<=|<>|!=|=|>|<)\s*(?P<rhs>-?\d+(?:\.\d+)?)\s*$")


def _apply_having(
    rows: list[dict[str, Any]], having: str, aggs: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], str]:
    """Filter grouped *rows* by a threshold on an aggregate. Returns
    ``(rows, applied)``; ``applied`` is "" when the predicate didn't parse."""
    m = _HAVING_RE.match(having or "")
    if not m:
        return rows, ""
    lhs = m.group("lhs").strip().strip("[]")
    alias = None
    for agg in aggs:
        spec = (f"{agg['func']}({agg.get('expr') or agg['field']})").lower()
        if lhs.lower() in (agg["alias"].lower(), spec):
            alias = agg["alias"]
            break
    if alias is None:
        return rows, ""
    op, rhs = m.group("op"), float(m.group("rhs"))
    tests = {
        ">": lambda v: v > rhs, "<": lambda v: v < rhs,
        ">=": lambda v: v >= rhs, "<=": lambda v: v <= rhs,
        "=": lambda v: v == rhs, "<>": lambda v: v != rhs,
        "!=": lambda v: v != rhs,
    }
    test = tests[op]
    kept = [r for r in rows
            if isinstance(r.get(alias), (int, float)) and test(r[alias])]
    return kept, f"{alias} {op} {m.group('rhs')}"


def resolve_sort_terms(
    sort_terms: list[tuple[str, str]], valid_sort,
) -> tuple[list[tuple[str, str]], list[str]]:
    """``(resolved_terms, unresolvable_names)`` for a caller sort clause.

    Matching is case-INSENSITIVE and bracket-tolerant on purpose. The exact,
    case-sensitive ``in`` test this replaces silently discarded the whole
    clause for ``order_by="totalqty asc"`` against alias ``TotalQty`` and fell
    through to the first-aggregate-DESC default — returning the exact inverse
    of the request with no warning. The model writes aliases in whatever case
    it wrote them in the `aggregate` string; a case difference must never
    change the ranking.
    """
    lookup: dict[str, str] = {}
    for v in valid_sort:
        lookup.setdefault(_strip_brackets(str(v)).strip().lower(), str(v))
    resolved: list[tuple[str, str]] = []
    bad: list[str] = []
    for col, direction in sort_terms:
        real = lookup.get(_strip_brackets(col).strip().lower())
        if real is None:
            bad.append(col)
        else:
            resolved.append((real, direction))
    return resolved, bad


def _sort_key(value):
    """Stable ordering key. Missing/non-numeric values sort LAST rather than
    being coerced to 0 — a coerced 0 fabricates a rank."""
    if value is None or value == "":
        return (1, 0.0, "")
    if isinstance(value, bool):
        return (0, float(value), "")
    if isinstance(value, (int, float)):
        return (0, float(value), "")
    return (0, 0.0, str(value))


def aggregate_records(
    records: list[dict[str, Any]],
    *,
    group_by: str = "",
    aggregate: str = "",
    distinct: str = "",
    having: str = "",
    order_by: str = "",
) -> dict[str, Any]:
    """Apply group_by / aggregate / distinct to a row list.

    Returns ``{"records": [...], "record_count": N, "aggregated": True,
    "scanned_rows": <input row count>, "group_by": [...], "aggregates": [...]}``.

    - ``distinct``: comma-separated columns. Returns unique combinations
      with a per-combination ``count``.
    - ``group_by``: comma-separated columns. Defaults to ``count(*)`` per
      group if no ``aggregate`` is provided.
    - ``aggregate`` without ``group_by``: returns a single summary row.
    """
    scanned = len(records)

    if distinct:
        compiled = [_compile_group_col(c) for c in distinct.split(",") if c.strip()]
        cols = [label for label, _ in compiled]
        resolvers = [res for _, res in compiled]
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for r in records:
            key = tuple(res(r) for res in resolvers)
            groups.setdefault(key, []).append(r)
        out_rows = [
            {**{cols[i]: k[i] for i in range(len(cols))}, "count": len(v)}
            for k, v in groups.items()
        ]
        out_rows.sort(key=lambda r: r["count"], reverse=True)
        result = {
            "records": out_rows,
            "record_count": len(out_rows),
            "aggregated": True,
            "mode": "distinct",
            "scanned_rows": scanned,
            "distinct_columns": cols,
        }
        _warn_having_ignored(result, having, "distinct")
        return result

    aggs = parse_aggregates(aggregate) if aggregate else []

    if group_by:
        compiled = [_compile_group_col(c) for c in group_by.split(",") if c.strip()]
        cols = [label for label, _ in compiled]
        resolvers = [res for _, res in compiled]
        if not aggs:
            aggs = [{"func": "count", "field": "*", "alias": "count"}]
        groups2: dict[tuple, list[dict[str, Any]]] = {}
        for r in records:
            key = tuple(res(r) for res in resolvers)
            groups2.setdefault(key, []).append(r)
        out_rows = []
        for k, group_rows in groups2.items():
            row = {cols[i]: k[i] for i in range(len(cols))}
            row.update(_apply_aggs_to_group(group_rows, aggs))
            out_rows.append(row)
        # HAVING applies AFTER the aggregates exist and BEFORE the sort.
        having_applied = ""
        if having:
            out_rows, having_applied = _apply_having(out_rows, having, aggs)
        # A caller `order_by` ranks the GROUPS (valid keys: the group-key
        # labels and the aggregate aliases — NOT the entity's raw columns).
        # Correct because this orders the result of a completed 20x1000 scan,
        # never a page. Without one, the historic first-aggregate-desc default
        # stands byte-identical.
        sort_terms, _kind = parse_order_by(order_by)
        valid_sort = list(cols) + [a["alias"] for a in aggs]
        resolved_sort, bad_sort = resolve_sort_terms(sort_terms, valid_sort)
        applied_order = ""
        if sort_terms and not bad_sort:
            for col, direction in reversed(resolved_sort):
                out_rows.sort(
                    key=lambda r, c=col: _sort_key(r.get(c)),
                    reverse=(direction == "desc"),
                )
            applied_order = order_terms_to_clause(resolved_sort)
        elif aggs:
            first_alias = aggs[0]["alias"]
            out_rows.sort(
                key=lambda r: (
                    r.get(first_alias) if isinstance(r.get(first_alias), (int, float)) else 0
                ),
                reverse=True,
            )
        result = {
            "records": out_rows,
            "record_count": len(out_rows),
            "aggregated": True,
            "mode": "group_by",
            "scanned_rows": scanned,
            "group_by": cols,
            "aggregates": [a["alias"] for a in aggs],
        }
        if having:
            if having_applied:
                result["having"] = having_applied
            else:
                # Say so loudly. A silently-ignored threshold returns MORE rows
                # than asked for and reads as the filtered answer.
                result["having_warning"] = (
                    f"Could not apply having={having!r} — it must compare an "
                    "aggregate alias or spec to a number, e.g. "
                    f"\"{aggs[0]['alias']} > 50000\". ALL groups are shown.")
        if order_by:
            # Same posture as `having` above: an unapplied ranking reads as the
            # ranked answer, which is the worst failure class — it looks right.
            if applied_order:
                result["order"] = applied_order
                result["order_applied"] = True
            else:
                result["order_applied"] = False
                result["order_warning"] = (
                    f"Could not apply order_by={order_by!r} — "
                    f"{', '.join(bad_sort) or 'the clause'} is neither a "
                    "group-key nor an aggregate alias. A rollup can only be "
                    f"ranked by one of {valid_sort}. Rows are ranked by "
                    f"{aggs[0]['alias']} DESC (the default) instead — this is "
                    "NOT the ordering you asked for.")
        _attach_agg_field_warning(result, records, aggs)
        return result

    if aggs:
        summary = _apply_aggs_to_group(records, aggs)
        rows = [summary]
        having_applied = ""
        if having:
            # A `having` over a grand total is a THRESHOLD on the single
            # summary row: it either passes or the answer is "nothing meets
            # it". Returning the row regardless read as the filtered answer.
            rows, having_applied = _apply_having(rows, having, aggs)
        result = {
            "records": rows,
            "record_count": len(rows),
            "aggregated": True,
            "mode": "summary",
            "scanned_rows": scanned,
            "aggregates": [a["alias"] for a in aggs],
        }
        if having:
            if having_applied:
                result["having"] = having_applied
            else:
                _warn_having_ignored(result, having, "summary", aggs)
        _attach_agg_field_warning(result, records, aggs)
        return result

    result = {"records": records, "record_count": scanned}
    _warn_having_ignored(result, having, "plain")
    return result


def _warn_having_ignored(
    result: dict[str, Any], having: str, mode: str,
    aggs: list[dict[str, str]] | None = None,
) -> None:
    """Say loudly that a `having` threshold was NOT applied.

    Only the group_by branch used to warn; summary, distinct and plain reads
    dropped `having` in silence and returned MORE rows than were asked for,
    which reads as the filtered answer. That is the exact failure the group_by
    branch's own comment describes — the other three paths just didn't say it.
    """
    if not having:
        return
    example = (f'"{aggs[0]["alias"]} > 50000"' if aggs
               else '"sum(OnHandQty) as total" + having="total > 50000"')
    if mode == "plain":
        detail = ("`having` filters AGGREGATE groups and needs `group_by` "
                  "and/or `aggregate`; this read had neither, so ALL matching "
                  "rows are shown UNFILTERED. Either move the condition into "
                  "`where` (which filters rows and keeps the scan small), or "
                  "add a group_by/aggregate.")
    elif mode == "distinct":
        detail = ("`having` is not supported alongside `distinct` — ALL "
                  "distinct rows are shown UNFILTERED. Use `group_by` with an "
                  "`aggregate` to threshold groups.")
    else:
        detail = (f"it must compare an aggregate alias to a number, e.g. "
                  f"{example}. The unfiltered total is shown.")
    result["having_warning"] = f"Could not apply having={having!r} — {detail}"
    result["having_applied"] = False


# --------------------------------------------------------------------------- #
# Unbounded-rollup guard (INV-1)
# --------------------------------------------------------------------------- #
# A group_by/aggregate rollup is entirely CLIENT-side: the engine pages the
# scanned table (20 pages x 1000 rows plain, 20 x 500 parent pages joined) and
# buckets the rows in-process. With a bounding `where` that is cheap and exact.
# Without a bounding filter, a rollup such as
#
#     target=OrderDtl, group_by=PartNum, aggregate="sum(DocExtPrice) as revenue"
#
# can spend minutes scanning a large transaction table. The scan still stops
# at the page ceiling, so the final total may be incomplete despite the cost.
#
# This is the same trade `order_scan_unbounded` already refuses on the join
# sort path: refuse in ~300ms and name the paths that DO work, rather than
# spend minutes producing a number that has to be labelled partial.
#
# Deliberately NOT keyed on `is_heavy()`: that set carries the Customer, Vendor
# and Part masters, whose rollups ("customers by territory") finish in one page
# and must stay available. The signal is the SCANNED TABLE — a curated set of
# Epicor transaction tables, the only ones big enough for this to bite.
BIG_ROLLUP_TABLES: frozenset[str] = frozenset({
    "orderhed", "orderdtl", "orderrel",
    "quotehed", "quotedtl",
    "invchead", "invcdtl",
    "apinvhed", "apinvdtl",
    "poheader", "podetail", "porel",
    "jobhead", "jobasmbl", "joboper", "jobmtl", "jobprod",
    "labordtl",
    "parttran",
    "rcvhead", "rcvdtl",
    "shiphead", "shipdtl",
    "gljrndtl",
})

# One year back. Long enough that a "by month" report still has 12 buckets,
# short enough to bound the scan on every table in BIG_ROLLUP_TABLES.
_ROLLUP_WINDOW_DAYS = 365


def is_big_rollup_scan(table: str) -> bool:
    """True when an unfiltered rollup over *table* is a multi-minute scan."""
    return (table or "").strip().lower() in BIG_ROLLUP_TABLES


def _parsed_aggs(aggregate: str) -> list[dict[str, str]]:
    """``parse_aggregates`` that never raises — a malformed spec is reported
    by the rollup engine itself, and the cost guard must not pre-empt it."""
    try:
        return parse_aggregates(aggregate)
    except Exception:  # noqa: BLE001
        return []


def unbounded_rollup_refusal(
    *,
    target: str,
    scanned: str,
    group_by: str,
    aggregate: str,
    where: str,
    bounded: bool,
    date_columns=(),
    scan_rows: int = 20_000,
    baq_tables: str = "",
    today: "_dt.date | None" = None,
) -> dict | None:
    """INV-1 envelope for a rollup that would scan an unbounded big table.

    ``None`` — the common case — means the rollup is fine and must run
    untouched. A refusal is returned only when all three hold:

    * the call really is a rollup (``group_by`` or ``aggregate``);
    * ``bounded`` is False — no conjunct lands on the table being SCANNED
      (a child-only filter does not bound a parent scan, exactly as
      ``order_scan_unbounded`` treats an empty ``p_parts``);
    * ``scanned`` is one of ``BIG_ROLLUP_TABLES``.

    ``retry_with`` is a runnable call, not a template: the caller's own
    group_by/aggregate plus a concrete date window on the best real date
    column of the scanned table. When the table has no indexed date column the
    window degrades to a named placeholder rather than a guessed filter.
    """
    if not (group_by.strip() or aggregate.strip()):
        return None
    if bounded or not is_big_rollup_scan(scanned):
        return None
    aggs = _parsed_aggs(aggregate)

    # A grand-total COUNT with no group_by needs no scan at all: `count_only`
    # is a true server-side $count, one call. Sending this one away to add a
    # date window would answer a DIFFERENT question, and letting it run would
    # return the page ceiling as if it were the row count.
    if (not group_by.strip() and aggs
            and all(a.get("func") == "count" for a in aggs)):
        retry = {"target": target, "count_only": True}
        if where.strip():
            retry["where"] = where.strip()
        return error_envelope(
            "rollup_scan_unbounded",
            f"aggregate={aggregate!r} with no `group_by` is a grand total over "
            f"all of {scanned}: as a rollup it pages the table client-side, "
            f"stops at ~{scan_rows:,} rows, and reports THAT as the count. Use "
            "count_only=true instead — Epicor answers it with a true "
            "server-side $count in one call (retry_with does exactly that).",
            retry_with=retry,
        )

    since = (today or _dt.date.today()) - _dt.timedelta(days=_ROLLUP_WINDOW_DAYS)
    date_col = best_date_column(date_columns)
    if date_col:
        bound = f"{date_col} >= '{since.isoformat()}'"
        bound_advice = (
            f"a date window on {scanned}.{date_col} is the cheapest, and "
            "retry_with already carries one (widen or replace it)")
    else:
        bound = f"<a {scanned} condition>"
        bound_advice = f"any condition on {scanned} will do"
    retry_where = f"{where.strip()} and {bound}" if where.strip() else bound

    # Date buckets (month()/quarter()/...) have no saved-BAQ equivalent — the
    # BAQ composer groups by RAW columns only — and neither does an arithmetic
    # measure (`sum(OnHandQty * AvgCost)`), which only the in-process evaluator
    # understands. Promising a server-side GROUP BY that cannot be composed
    # would just cost another failed hop.
    baq_ok = "(" not in group_by and not any(a.get("expr") for a in aggs)
    paths = [
        f"(1) bound the scan with `where` — {bound_advice}",
    ]
    if baq_ok:
        paths.append(
            "(2) push the GROUP BY server-side with epicor_baq action='create', "
            "which aggregates in SQL over the WHOLE table and returns a "
            "complete rollup (see server_side_alternative)")
    paths.append(
        f"({len(paths) + 1}) roll up a narrower entity.")

    env = error_envelope(
        "rollup_scan_unbounded",
        f"This rollup over {scanned} has no {scanned}-side condition, so it "
        "would page the whole table client-side and could still stop at "
        f"~{scan_rows:,} rows, returning a TRUNCATED total. "
        "Refusing the unbounded scan before it starts. "
        "Do ONE of: " + "; ".join(paths),
        valid={"bounding_date_columns": sorted(
            {str(c) for c in (date_columns or ())})},
        retry_with={
            "target": target,
            "group_by": group_by,
            "aggregate": aggregate,
            "where": retry_where,
        },
    )
    if baq_ok:
        fields = ", ".join(p for p in (group_by.strip(), aggregate.strip()) if p)
        env["server_side_alternative"] = {
            "tool": "epicor_baq",
            "action": "create",
            "tables": baq_tables or f"Erp.{scanned}",
            "fields": fields,
        }
    return env

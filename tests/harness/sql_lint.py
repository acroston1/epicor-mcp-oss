"""Regression coverage: sql lint."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterator

import sqlglot
from sqlglot import exp

DIALECT = "tsql"


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


class Severity(str, Enum):
    ADVISORY = "ADVISORY"
    REPAIRABLE = "REPAIRABLE"
    UNSUPPORTED = "UNSUPPORTED"


class Verdict(str, Enum):
    CLEAN = "CLEAN"
    REPAIRABLE = "REPAIRABLE"
    UNSUPPORTED = "UNSUPPORTED"
    UNPARSEABLE = "UNPARSEABLE"


class Category(str, Enum):
    #: 200 + rows + a WRONG answer. The class no error channel reports.
    SILENT_WRONG = "silent_wrong"
    #: HTTP 400 at ParseFromSQL.
    PARSE_ERROR = "parse_error"
    #: HTTP 200 + returnObj.Errors at Execute.
    RUN_ERROR = "run_error"
    #: Works on Epicor; the server refuses it or bounds it.
    POLICY = "policy"
    #: Works, but the output shape is awkward / unstable.
    COSMETIC = "cosmetic"
    #: Correct SQL that can still produce a wrong number (fan-out).
    GRAIN = "grain"
    
    UNTESTED = "untested"


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    category: Category
    message: str
    evidence: str
    rewrite: str | None = None
    rewrite_status: str = "NONE"  # VERIFIED | INFERRED | NONE
    requires_schema: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "category": self.category.value,
            "message": self.message,
            "evidence": self.evidence,
            "rewrite": self.rewrite,
            "rewrite_status": self.rewrite_status,
            "requires_schema": self.requires_schema,
            "detail": self.detail,
        }


@dataclass
class LintResult:
    sql: str
    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)
    parse_error: str | None = None
    statement_count: int = 1

    @property
    def rules(self) -> list[str]:
        return [f.rule for f in self.findings]

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is not Severity.ADVISORY]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "rules": self.rules,
            "parse_error": self.parse_error,
            "statement_count": self.statement_count,
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# Text masking — string literals and comments blanked, offsets preserved
# --------------------------------------------------------------------------- #

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_SINGLE_QUOTED = re.compile(r"'(?:[^']|'')*'")
_BRACKETED = re.compile(r"\[[^\]]*\]")


def mask_literals(sql: str, *, mask_identifiers: bool = False) -> str:
    """Blank out string literals and comments, preserving length and offsets.

    Without this, ``where [T].[GroupID] = 'order by 1'`` would trip the ordinal
    rule, and ``-- use top 100 not top (100)`` would trip the paren rule. Same
    class of bug as the legacy ``unknown_columns`` envelope blaming a filter for a quoted
    literal that happened to look like a column name.
    """

    def blank(m: re.Match) -> str:
        return " " * (m.end() - m.start())

    def blank_string(m: re.Match) -> str:
        return "'" + " " * (m.end() - m.start() - 2) + "'"

    out = _BLOCK_COMMENT.sub(blank, sql)
    out = _LINE_COMMENT.sub(blank, out)
    out = _SINGLE_QUOTED.sub(blank_string, out)
    if mask_identifiers:
        out = _BRACKETED.sub(lambda m: "[" + " " * (m.end() - m.start() - 2) + "]", out)
    return out


# --------------------------------------------------------------------------- #
# Parse context
# --------------------------------------------------------------------------- #


#: ``EXCEPT`` / ``INTERSECT`` are ``SetOperation`` but NOT ``Union``. Reading only
#: ``exp.Union`` made every root-select rule skip them and made ``r_statement_shape``
#: refuse them as non-SELECT. Fall back on older sqlglot builds that lack the base.
SETOP = getattr(exp, "SetOperation", exp.Union)


@dataclass
class Ctx:
    sql: str
    masked: str
    tree: exp.Expression | None
    statements: list[exp.Expression]
    parse_error: str | None
    require_row_bound: bool = True

    @property
    def root_selects(self) -> list[exp.Select]:
        """Top-level SELECTs — the statement itself, or each set-operation branch."""
        if self.tree is None:
            return []
        node = self.tree
        if isinstance(node, exp.Select):
            return [node]
        if isinstance(node, SETOP):
            return [s for s in node.flatten() if isinstance(s, exp.Select)]
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select):
            return [node.this]
        return []

    @property
    def setop_trailing_orders(self) -> set[int]:
        """Regression coverage: setop trailing orders."""
        out: set[int] = set()
        if self.tree is None:
            return out
        for setop in self.tree.find_all(SETOP):
            if _ancestor_of(setop, (SETOP,)):
                continue  # inner node of a chain; the outermost owns the branches
            branches = [s for s in setop.flatten() if isinstance(s, exp.Select)]
            if not branches:
                continue
            order = branches[-1].args.get("order")
            if order is not None:
                out.add(id(order))
        return out

    @property
    def cte_names(self) -> set[str]:
        if self.tree is None:
            return set()
        names: set[str] = set()
        for with_ in self.tree.find_all(exp.With):
            for cte in with_.expressions:
                alias = cte.args.get("alias")
                if alias is not None and alias.this is not None:
                    names.add(alias.this.name.lower())
        return names


def _parse(sql: str) -> tuple[exp.Expression | None, list[exp.Expression], str | None]:
    try:
        stmts = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except Exception as exc:  # noqa: BLE001 — sqlglot raises several types
        return None, [], f"{type(exc).__name__}: {exc}"[:400]
    if not stmts:
        return None, [], "empty_statement"
    return stmts[0], stmts, None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_AGG_NAMES = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.AggFunc)


def _is_agg(node: exp.Expression) -> bool:
    return isinstance(node, exp.AggFunc)


def _has_agg(node: exp.Expression) -> bool:
    return any(_is_agg(n) for n in node.walk())


def _select_alias_map(select: exp.Select) -> dict[str, exp.Expression]:
    """``{lowercase output alias: underlying expression}`` for one SELECT."""
    out: dict[str, exp.Expression] = {}
    for item in select.expressions:
        if isinstance(item, exp.Alias):
            out[item.alias.lower()] = item.this
    return out


_QUOTE_CHARS = re.compile(r"[\[\]\"`]")


def _norm(node: exp.Expression) -> str:
    """Comparable text for one expression, insensitive to identifier quoting.

    ``[P].[ClassID]`` and ``P.ClassID`` are the same column and must compare equal
    — a GROUP BY written one way and a SELECT item the other is otherwise scored as
    an ungrouped column. Only identifier quoting is stripped; ``'…'`` literals keep
    their delimiters.
    """
    try:
        text = node.sql(dialect=DIALECT)
    except Exception:  # noqa: BLE001
        return repr(node)
    return _QUOTE_CHARS.sub("", text).lower().replace(" ", "")


def _enclosing_select(node: exp.Expression) -> exp.Select | None:
    """The nearest SELECT above ``node`` — i.e. the scope the node belongs to."""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            return parent
        parent = parent.parent
    return None


def _own_nodes(select: exp.Select, *types: type) -> Iterator[exp.Expression]:
    """Nodes of ``types`` under ``select`` whose own scope IS ``select``.

    ``select.find_all`` descends into subqueries, which is what made
    ``unqualified_column_on_join`` scope-blind: a bare column inside a legal
    single-table subquery was counted against the outer join's source count.
    """
    for node in select.find_all(*types):
        if _enclosing_select(node) is select:
            yield node


def _own_has_agg(select: exp.Select, node: exp.Expression) -> bool:
    """Regression coverage:  own has agg."""
    for agg in node.find_all(exp.AggFunc):
        if _enclosing_select(agg) is select:
            return True
    return isinstance(node, exp.AggFunc) and _enclosing_select(node) is select


def _under_agg(node: exp.Expression, stop: exp.Expression) -> bool:
    """Is ``node`` inside an aggregate call, looking no further up than ``stop``?"""
    parent = node.parent
    while parent is not None and parent is not stop:
        if isinstance(parent, exp.AggFunc):
            return True
        parent = parent.parent
    return False


_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _cross_table_comparisons(on: exp.Expression) -> list[exp.Binary]:
    """Comparisons in an ON clause between columns of two DIFFERENT tables."""
    out: list[exp.Binary] = []
    for node in on.find_all(*_COMPARISONS):
        left, right = node.this, node.expression
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        if not (left.table and right.table):
            continue
        if left.table.lower() == right.table.lower():
            continue
        out.append(node)
    return out


def _on_fully_qualified(on: exp.Expression) -> bool:
    """Every column in the ON carries a table qualifier.

    Without this guard the join rules cannot tell "no cross-table key" from "I
    could not see the key", and an unqualified ON would be reported as a cartesian.
    ``unqualified_column_on_join`` owns that case.
    """
    return all(bool(c.table) for c in on.find_all(exp.Column))


def _setop_of(node: exp.Expression | None) -> exp.Expression | None:
    """The set operation directly behind a FROM/JOIN source, if any."""
    if node is None:
        return None
    this = getattr(node, "this", None)
    if isinstance(this, exp.Subquery) and isinstance(this.this, SETOP):
        return this.this
    if isinstance(node, exp.Subquery) and isinstance(node.this, SETOP):
        return node.this
    return None


def _from_setop(select: exp.Select) -> exp.Expression | None:
    """The derived set operation on this select's FROM side, if there is one."""
    return _setop_of(_from_clause(select))


def _setop_branches(setop: exp.Expression) -> list[exp.Select]:
    return [s for s in setop.flatten() if isinstance(s, exp.Select)]


def _cte_setop_names(tree: exp.Expression | None) -> set[str]:
    """Lowercased names of CTEs whose body is a set operation."""
    names: set[str] = set()
    if tree is None:
        return names
    for with_ in tree.find_all(exp.With):
        for cte in with_.expressions:
            alias = cte.args.get("alias")
            if alias is None or alias.this is None:
                continue
            if isinstance(cte.this, SETOP):
                names.add(alias.this.name.lower())
    return names


def _from_clause(select: exp.Select) -> exp.Expression | None:
    """sqlglot 30 stores the FROM under ``from_``; older builds used ``from``.

    Reading the wrong key returns ``None`` silently, which makes every join look
    like a single-table query and switches the join rules off — so this accessor
    exists rather than an inline ``args.get``.
    """
    return select.args.get("from_") or select.args.get("from")


def _source_tables(select: exp.Select) -> list[exp.Table]:
    tables: list[exp.Table] = []
    from_ = _from_clause(select)
    if from_ is not None and isinstance(getattr(from_, "this", None), exp.Table):
        tables.append(from_.this)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            tables.append(join.this)
    return tables


def _source_count(select: exp.Select) -> int:
    """Number of FROM/JOIN sources including derived tables."""
    n = 1 if _from_clause(select) is not None else 0
    n += len(select.args.get("joins") or [])
    return n


# --------------------------------------------------------------------------- #
# Rules — raw text (must NOT be moved to the AST; see module docstring)
# --------------------------------------------------------------------------- #

RuleFn = Callable[[Ctx], Iterator[Finding]]
RAW_RULES: list[RuleFn] = []
AST_RULES: list[RuleFn] = []


def raw_rule(fn: RuleFn) -> RuleFn:
    RAW_RULES.append(fn)
    return fn


def ast_rule(fn: RuleFn) -> RuleFn:
    AST_RULES.append(fn)
    return fn


_TOP_PAREN_RE = re.compile(r"\btop\s*\(", re.I)
_TOP_PERCENT_RE = re.compile(r"\btop\s+\(?\s*\d+\s*\)?\s+percent\b", re.I)
_TOP_ZERO_RE = re.compile(r"\btop\s+\(?\s*0\s*\)?(?!\d)", re.I)
_LIMIT_RE = re.compile(r"\blimit\s+\d+", re.I)
_FETCH_RE = re.compile(r"\bfetch\s+(first|next)\b", re.I)
_OFFSET_RE = re.compile(r"\boffset\s+\d+\s+rows?\b", re.I)
_PARAM_RE = re.compile(r"(?<![\w@])@[A-Za-z_]\w*")
_ODBC_DATE_RE = re.compile(r"\{\s*(d|t|ts)\s+'", re.I)
_BARE_ISO_RE = re.compile(r"(?<![\w'\-])\d{4}-\d{2}-\d{2}(?![\w'\-])")
_NOLOCK_RE = re.compile(r"\bwith\s*\(\s*nolock", re.I)
_PIVOT_RE = re.compile(r"\b(un)?pivot\s*\(", re.I)


#: never as supported and never as refused.
_UNTESTED_RE = {
    "merge": re.compile(r"\bmerge\s+into\b", re.I),
    "apply": re.compile(r"\b(cross|outer)\s+apply\b", re.I),
    "tablesample": re.compile(r"\btablesample\b", re.I),
    "for_xml_json": re.compile(r"\bfor\s+(xml|json)\b", re.I),
    "string_agg": re.compile(r"\bstring_agg\s*\(", re.I),
    "try_cast": re.compile(r"\btry_(cast|convert|parse)\s*\(", re.I),
    "iif": re.compile(r"\biif\s*\(", re.I),
    "recursive_cte": re.compile(r"\bwith\s+recursive\b", re.I),
}


@raw_rule
def r_top_parenthesised(ctx: Ctx) -> Iterator[Finding]:
    if _TOP_PAREN_RE.search(ctx.masked) and not _TOP_PERCENT_RE.search(ctx.masked):
        yield Finding(
            rule="top_parenthesised",
            severity=Severity.REPAIRABLE,
            category=Category.SILENT_WRONG,
            message="`top (N)` is treated as NO LIMIT AT ALL — 200, rows, no error. Write `top N`.",
            evidence="parses to SelectListClause='All'; returned 500 rows under PageSize=500",
            rewrite="strip the parentheses: `top (N)` -> `top N`",
            rewrite_status="VERIFIED",
            detail="Microsoft's own recommended form. sqlglot normalises it away, so this rule reads raw text.",
        )


@raw_rule
def r_top_percent(ctx: Ctx) -> Iterator[Finding]:
    if _TOP_PERCENT_RE.search(ctx.masked):
        yield Finding(
            rule="top_percent",
            severity=Severity.UNSUPPORTED,
            category=Category.SILENT_WRONG,
            message="`top N percent` returns N PERCENT of the table with no error, not N rows.",
            evidence="TopInPercent=true applies a percentage rather than a row bound.",
            rewrite="no mechanical equivalent — the intended row count is unknowable. Re-ask for `top N`.",
            rewrite_status="NONE",
        )


@raw_rule
def r_top_zero(ctx: Ctx) -> Iterator[Finding]:
    if _TOP_ZERO_RE.search(ctx.masked):
        yield Finding(
            rule="top_zero",
            severity=Severity.UNSUPPORTED,
            category=Category.SILENT_WRONG,
            message="`top 0` means UNLIMITED on Epicor, not zero rows.",
            evidence="TopRowExpr=0 == no limit; returned 500 rows",
            rewrite="`where 1 = 0` would return zero rows, but nobody means `top 0` — refuse instead.",
            rewrite_status="INFERRED",
        )


@raw_rule
def r_limit_clause(ctx: Ctx) -> Iterator[Finding]:
    if _LIMIT_RE.search(ctx.masked):
        yield Finding(
            rule="limit_clause",
            severity=Severity.REPAIRABLE,
            category=Category.PARSE_ERROR,
            message="`limit N` is rejected by the BAQ parser. Epicor uses `select top N`.",
            evidence="`SQL cannot be parsed: Incorrect syntax near 'limit'.`",
            rewrite="move the bound to the select list: `select top N …`",
            rewrite_status="VERIFIED",
            detail="sqlglot silently normalises `limit N` to `TOP N`, so this rule reads raw text.",
        )


@raw_rule
def r_fetch_clause(ctx: Ctx) -> Iterator[Finding]:
    if _FETCH_RE.search(ctx.masked) and not _OFFSET_RE.search(ctx.masked):
        yield Finding(
            rule="fetch_clause",
            severity=Severity.REPAIRABLE,
            category=Category.PARSE_ERROR,
            message="`fetch first N rows only` without an `offset` is rejected. Use `top N`.",
            evidence="`Incorrect syntax near 'first'`; `order by … offset 0 rows fetch next N rows only` DOES work",
            rewrite="`select top N …`, or add `order by … offset 0 rows` before the fetch",
            rewrite_status="VERIFIED",
        )


@raw_rule
def r_parameter_marker(ctx: Ctx) -> Iterator[Finding]:
    hits = sorted({m.group(0) for m in _PARAM_RE.finditer(ctx.masked)})
    if hits:
        yield Finding(
            rule="parameter_marker",
            severity=Severity.UNSUPPORTED,
            category=Category.RUN_ERROR,
            message=f"There is no parameter binding on the ad-hoc path: {', '.join(hits)}.",
            evidence='`Must declare the scalar variable "@OrderNum".`',
            rewrite="inline the literal value",
            rewrite_status="INFERRED",
            detail=", ".join(hits),
        )


@raw_rule
def r_odbc_date_escape(ctx: Ctx) -> Iterator[Finding]:
    if _ODBC_DATE_RE.search(ctx.masked):
        yield Finding(
            rule="odbc_date_escape",
            severity=Severity.REPAIRABLE,
            category=Category.RUN_ERROR,
            message="ODBC date escapes `{d '…'}` fail. A quoted ISO literal works directly.",
            evidence="`Operand type clash: date is incompatible with int`",
            rewrite="`{d '2025-01-01'}` -> `'2025-01-01'`",
            rewrite_status="VERIFIED",
        )


@raw_rule
def r_unquoted_date(ctx: Ctx) -> Iterator[Finding]:
    hits = sorted({m.group(0) for m in _BARE_ISO_RE.finditer(ctx.masked)})
    if hits:
        yield Finding(
            rule="unquoted_date_literal",
            severity=Severity.REPAIRABLE,
            category=Category.RUN_ERROR,
            message=f"Unquoted date literal(s) {', '.join(hits)} fail — quote them.",
            evidence="`>= 2025-01-01` -> `Operand type clash: date is incompatible with int`",
            rewrite="wrap in single quotes: `2025-01-01` -> `'2025-01-01'`",
            rewrite_status="VERIFIED",
            detail=", ".join(hits),
        )


@raw_rule
def r_nolock(ctx: Ctx) -> Iterator[Finding]:
    if _NOLOCK_RE.search(ctx.masked):
        yield Finding(
            rule="table_hint_nolock",
            severity=Severity.ADVISORY,
            category=Category.COSMETIC,
            message="`with (nolock)` is accepted and silently ignored.",
            evidence="The parser does not support this SQL construct.",
            rewrite="drop the hint",
            rewrite_status="INFERRED",
        )


@raw_rule
def r_pivot(ctx: Ctx) -> Iterator[Finding]:
    if _PIVOT_RE.search(ctx.masked):
        yield Finding(
            rule="pivot",
            severity=Severity.UNSUPPORTED,
            category=Category.PARSE_ERROR,
            message="PIVOT is refused at parse time.",
            evidence="`Pivot expressions are not currently supported.`",
            rewrite="`sum(case when … then … end)` per bucket",
            rewrite_status="INFERRED",
        )


@raw_rule
def r_untested(ctx: Ctx) -> Iterator[Finding]:
    for name, rx in _UNTESTED_RE.items():
        if rx.search(ctx.masked):
            yield Finding(
                rule=f"untested_{name}",
                severity=Severity.ADVISORY,
                category=Category.UNTESTED,
                message=f"`{name}` was never probed — its behaviour on Epicor is UNKNOWN, not supported.",
                evidence="only specific constructs were probed, not the whole language",
                rewrite=None,
                rewrite_status="NONE",
            )


# --------------------------------------------------------------------------- #
# Rules — AST
# --------------------------------------------------------------------------- #


@ast_rule
def r_statement_shape(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    if len(ctx.statements) > 1:
        yield Finding(
            rule="multiple_statements",
            severity=Severity.UNSUPPORTED,
            category=Category.PARSE_ERROR,
            message=f"{len(ctx.statements)} statements — the BAQ parser takes exactly one.",
            evidence="`Only one SQL statement can be processed at a time for BAQ generation.`",
            rewrite_status="NONE",
        )
    # SETOP covers UNION / EXCEPT / INTERSECT. Reading only `exp.Union` refused a
    # top-level EXCEPT as "only SELECT statements can be processed" — a message
    
    # INTERSECT live on Epicor.
    if not isinstance(ctx.tree, (exp.Select, SETOP, exp.Subquery)):
        yield Finding(
            rule="non_select_statement",
            severity=Severity.UNSUPPORTED,
            category=Category.POLICY,
            message=f"`{type(ctx.tree).__name__.upper()}` is refused — only SELECT reaches the BAQ generator.",
            evidence="`Only SELECT statements can be processed for BAQ generation.`",
            rewrite_status="NONE",
        )


def _is_projection_star(item: exp.Expression) -> bool:
    """``*`` or ``[T].*`` in a SELECT list — but NOT the ``*`` inside ``count(*)``."""
    if isinstance(item, exp.Star):
        return True
    return isinstance(item, exp.Column) and isinstance(item.this, exp.Star)


def _ancestor_of(node: exp.Expression, types: tuple[type, ...]) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, types):
            return True
        parent = parent.parent
    return False


@ast_rule
def r_select_star(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    starred = any(
        _is_projection_star(item)
        for select in ctx.tree.find_all(exp.Select)
        for item in select.expressions
    )
    if starred:
        yield Finding(
            rule="select_star",
            severity=Severity.REPAIRABLE,
            category=Category.POLICY,
            message="`select *` is refused by the server — name the columns.",
            evidence="Wildcard projection is refused by policy at parsed-dataset validation.",
            rewrite="expand to the table's real column list",
            rewrite_status="INFERRED",
            requires_schema=True,
        )


@ast_rule
def r_count_distinct(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    for node in ctx.tree.find_all(exp.AggFunc):
        if any(isinstance(c, exp.Distinct) for c in node.iter_expressions()):
            fn = type(node).__name__.lower()
            yield Finding(
                rule="count_distinct",
                severity=Severity.UNSUPPORTED,
                category=Category.SILENT_WRONG,
                message=(
                    f"`{fn}(distinct …)` — Epicor SILENTLY DROPS the DISTINCT and returns a wrong "
                    "number with an EMPTY Errors array."
                ),
                evidence="DISTINCT inside an aggregate can be silently ignored, producing an incorrect count.",
                rewrite=(
                    "count(*) over a `select distinct` derived table: "
                    "`select count(*) as [N] from (select distinct [T].[C] as [C] from Erp.T as [T]) as [t]`"
                ),
                rewrite_status="VERIFIED",
                detail=(
                    "count(distinct) INSIDE a derived table is still wrong and is not a workaround. "
                    "The query policy refuses rather than silently rewriting aggregate semantics."
                ),
            )
            break


@ast_rule
def r_distinct_with_top(ctx: Ctx) -> Iterator[Finding]:
    for select in ctx.root_selects:
        if select.args.get("distinct") is not None and select.args.get("limit") is not None:
            yield Finding(
                rule="distinct_with_top",
                severity=Severity.UNSUPPORTED,
                category=Category.SILENT_WRONG,
                message="`select distinct top N` silently returns duplicates.",
                evidence="Aggregate safety: (`top N distinct` does not even parse)",
                rewrite="drop the `top` and keep `distinct`, or wrap the distinct in a derived table",
                rewrite_status="INFERRED",
            )
            break


@ast_rule
def r_missing_row_bound(ctx: Ctx) -> Iterator[Finding]:
    if not ctx.require_row_bound:
        return
    for select in ctx.root_selects:
        exprs = select.expressions
        grand_total = bool(
            exprs
            and select.args.get("group") is None
            and all(_own_has_agg(select, e) for e in exprs)
        )
        if grand_total:
            continue  # one row by construction, whatever the source is
        # A select over a derived SET OPERATION is a special case in BOTH
        
        # returned 200), and per-branch bounds DO bind. So the outer `limit` proves
        # nothing here and the branches decide.
        setop = _from_setop(select)
        if setop is not None:
            branches = _setop_branches(setop)
            if branches and all(b.args.get("limit") is not None for b in branches):
                continue  # bounded the only way Epicor honours — see r_setop_outer_bound
            yield Finding(
                rule="missing_row_bound",
                severity=Severity.REPAIRABLE,
                category=Category.POLICY,
                message=(
                    "No effective row bound — a `top N` on a select that wraps a set "
                    "operation is IGNORED, so the bound must go on each branch."
                ),
                evidence="outer `top 7` over an unbounded union -> 100 rows (the whole PageSize window)",
                rewrite="put `top N` on EACH branch of the set operation, not on the wrapper",
                rewrite_status="VERIFIED",
                detail="set_operation_source",
            )
            break
        # The grand-total exemption above uses `_own_has_agg`: `_has_agg` descended
        # into subqueries, so `select (select max(x) from …) as [N] from Erp.Part`
        # scored as a grand total and shipped unbounded — 25 rows at PageSize=25
        
        if select.args.get("limit") is not None:
            continue
        yield Finding(
            rule="missing_row_bound",
            severity=Severity.REPAIRABLE,
            category=Category.POLICY,
            message="No `top N` — an unbounded read of a production ERP table.",
            evidence="An unbounded read can exceed the MCP response size limit.",
            rewrite="inject `top <default_limit>` into the outermost select (PageSize is still sent regardless)",
            rewrite_status="VERIFIED",
        )
        break


@ast_rule
def r_order_by_ordinal(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    trailing = ctx.setop_trailing_orders
    for order in ctx.tree.find_all(exp.Order):
        if id(order) in trailing:
            continue  # union_order_by_discarded owns it; substitution does not fix it
        for ordered in order.expressions:
            target = ordered.this
            if isinstance(target, exp.Literal) and not target.args.get("is_string"):
                yield Finding(
                    rule="order_by_ordinal",
                    severity=Severity.REPAIRABLE,
                    category=Category.SILENT_WRONG,
                    message=f"`order by {target.name}` is SILENTLY DISCARDED — rows come back unordered.",
                    evidence=(
                        "`order by 1 desc` and `order by 1 asc` returned byte-identical "
                        "rows, identical to no ORDER BY; parse leaves TableID='' FieldName='1'"
                    ),
                    rewrite="substitute the SELECT item at that position and repeat the expression",
                    rewrite_status="VERIFIED",
                    detail=f"ordinal {target.name}",
                )
                return


@ast_rule
def r_group_by_ordinal(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    for group in ctx.tree.find_all(exp.Group):
        for e in group.expressions:
            if isinstance(e, exp.Literal) and not e.args.get("is_string"):
                yield Finding(
                    rule="group_by_ordinal",
                    severity=Severity.REPAIRABLE,
                    category=Category.RUN_ERROR,
                    message=f"`group by {e.name}` fails at run time.",
                    evidence=(
                        "`Each GROUP BY expression must contain at least one column "
                        "that is not an outer reference.`"
                    ),
                    rewrite="repeat the SELECT expression at that position",
                    rewrite_status="VERIFIED",
                )
                return


def _alias_reference_findings(
    select: exp.Select,
    clause: exp.Expression | None,
    rule: str,
    where: str,
    evidence: str,
) -> Iterator[Finding]:
    """Regression coverage:  alias reference findings."""
    if clause is None:
        return
    aliases = _select_alias_map(select)
    if not aliases:
        return
    for col in clause.find_all(exp.Column):
        if col.table:
            continue
        underlying = aliases.get(col.name.lower())
        if underlying is None:
            continue
        if isinstance(underlying, exp.Column) and underlying.name.lower() == col.name.lower():
            continue  # alias == its own column name; harmless
        yield Finding(
            rule=rule,
            severity=Severity.REPAIRABLE,
            category=Category.RUN_ERROR,
            message=(
                f"`{where} [{col.name}]` references a SELECT output alias — Epicor answers "
                f"`Invalid column name '{col.name}'.`"
            ),
            evidence=evidence,
            rewrite=f"repeat the expression: {where} {underlying.sql(dialect=DIALECT)}",
            rewrite_status="VERIFIED",
            detail=col.name,
        )
        return


@ast_rule
def r_order_by_alias(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    trailing = ctx.setop_trailing_orders
    for select in ctx.tree.find_all(exp.Select):
        order = select.args.get("order")
        if order is not None and id(order) in trailing:
            continue  # union_order_by_discarded owns it; expression-repeat does not fix it
        yield from _alias_reference_findings(
            select,
            order,
            "order_by_alias",
            "order by",
            "aggregate alias, plain-field output alias and computed alias ALL fail at run time",
        )


@ast_rule
def r_having_alias(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    for select in ctx.tree.find_all(exp.Select):
        yield from _alias_reference_findings(
            select,
            select.args.get("having"),
            "having_alias",
            "having",
            "`having [Cnt] > 100` -> `Invalid column name 'Cnt'.`",
        )


@ast_rule
def r_exists(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    if any(isinstance(n, exp.Exists) for n in ctx.tree.walk()):
        yield Finding(
            rule="exists_predicate",
            severity=Severity.UNSUPPORTED,
            category=Category.RUN_ERROR,
            message="EXISTS / NOT EXISTS is dead in every form — the BAQ generator rewrites it into a malformed IN.",
            evidence="`Incorrect syntax near the keyword 'in'.` for all five forms tested",
            rewrite="`where col in (select …)`, or `left outer join … where child.key is null` for NOT EXISTS",
            rewrite_status="INFERRED",
            detail="correlated multi-key EXISTS has no single mechanical equivalent — this is why it is UNSUPPORTED, not REPAIRABLE",
        )


@ast_rule
def r_window(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    if any(isinstance(n, exp.Window) for n in ctx.tree.walk()):
        yield Finding(
            rule="window_function",
            severity=Severity.UNSUPPORTED,
            category=Category.RUN_ERROR,
            message="Window functions fail — the parser strips the OVER clause.",
            evidence="`The function 'row_number' must have an OVER clause.`",
            rewrite="`top N` + `order by <expression>` is a true global top-N; per-group ranking needs a derived table",
            rewrite_status="INFERRED",
        )


@ast_rule
def r_select_into(ctx: Ctx) -> Iterator[Finding]:
    for select in ctx.root_selects:
        if select.args.get("into") is not None:
            yield Finding(
                rule="select_into",
                severity=Severity.UNSUPPORTED,
                category=Category.SILENT_WRONG,
                message="`select … into` is silently DROPPED — rows come back and nothing is created.",
                evidence="(adjacent to the write path; the server must never emit it)",
                rewrite=None,
                rewrite_status="NONE",
            )
            break


@ast_rule
def r_cross_join(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    for join in ctx.tree.find_all(exp.Join):
        kind = (join.args.get("kind") or "").upper()
        side = (join.args.get("side") or "").upper()
        # CROSS/OUTER APPLY is a LATERAL join, not a comma join. Reporting it as
        # "a comma join with no ON predicate is a cross join" names a construct the
        
        
        if isinstance(join.this, exp.Lateral):
            continue
        if kind == "CROSS":
            yield Finding(
                rule="cross_join",
                severity=Severity.UNSUPPORTED,
                category=Category.POLICY,
                message="CROSS JOIN is refused by the server — it is unbounded by construction.",
                evidence="Unsupported by the read-only SQL policy.",
                rewrite="join on a real predicate",
                rewrite_status="NONE",
            )
            return
        if not kind and not side and join.args.get("on") is None and join.args.get("using") is None:
            yield Finding(
                rule="comma_join_no_predicate",
                severity=Severity.UNSUPPORTED,
                category=Category.POLICY,
                message="Comma join with no ON predicate is a cross join — refused.",
                evidence="(comma joins parse and run); the governor refuses the unbounded shape",
                rewrite="`inner join … on [A].[Company] = [B].[Company] and [A].[Key] = [B].[Key]`",
                rewrite_status="INFERRED",
            )
            return


@ast_rule
def r_missing_schema_prefix(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    ctes = ctx.cte_names
    bad: list[str] = []
    for table in ctx.tree.find_all(exp.Table):
        if table.db:
            continue
        name = table.name
        if not name or name.lower() in ctes:
            continue
        if name.startswith("#"):
            continue
        bad.append(name)
    if bad:
        yield Finding(
            rule="missing_schema_prefix",
            severity=Severity.REPAIRABLE,
            category=Category.PARSE_ERROR,
            message=f"Table(s) without a schema prefix: {', '.join(sorted(set(bad)))}. `from Part` is a hard 400.",
            evidence=(
                "`References to inaccessible tables detected: subquery 'Main', "
                "table '.Part' with alias 'Part'`"
            ),
            rewrite="prefix with the owning schema — `Erp.Part`, `Ice.Menu`",
            rewrite_status="VERIFIED",
            requires_schema=True,
            detail=", ".join(sorted(set(bad))),
        )


@ast_rule
def r_unqualified_column_on_join(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    for select in ctx.tree.find_all(exp.Select):
        if _source_count(select) < 2:
            continue
        aliases = set(_select_alias_map(select))
        bad: list[str] = []
        # SCOPE-AWARE: only columns whose own scope is THIS select. `find_all`
        # descends into subqueries, so the statement-wide count flagged the legal
        # `… = (select top 1 CustNum from Erp.Customer where Name = 'AcmeIndustrial')` shape,
        # whose unqualified column belongs to a single-source subquery, as a break.
        for col in _own_nodes(select, exp.Column):
            if col.table or isinstance(col.this, exp.Star):
                continue
            # An unqualified name in ORDER BY / HAVING that matches an output
            # alias is the alias rules' business, not this one.
            if col.name.lower() in aliases and _ancestor_of(col, (exp.Order, exp.Having)):
                continue
            bad.append(col.name)
        if bad:
            yield Finding(
                rule="unqualified_column_on_join",
                severity=Severity.REPAIRABLE,
                category=Category.RUN_ERROR,
                message=(
                    f"Unqualified column(s) on a join: {', '.join(sorted(set(bad)))}. "
                    "They resolve against the FIRST table only and then fail."
                ),
                evidence="unqualified column on a JOIN -> `Invalid column name 'OnHandQty'.`",
                rewrite="qualify with the owning table alias",
                rewrite_status="VERIFIED",
                requires_schema=True,
                detail=", ".join(sorted(set(bad))),
            )
            return


@ast_rule
def r_missing_output_alias(ctx: Ctx) -> Iterator[Finding]:
    for select in ctx.root_selects:
        plain: list[str] = []
        computed = 0
        for item in select.expressions:
            if isinstance(item, (exp.Alias, exp.Star)):
                continue
            if isinstance(item, exp.Column):
                plain.append(item.sql(dialect=DIALECT))
            else:
                computed += 1
        if computed:
            yield Finding(
                rule="missing_alias_computed",
                severity=Severity.REPAIRABLE,
                category=Category.COSMETIC,
                message=f"{computed} computed/aggregate SELECT item(s) without `as [Name]` — keys become Calculated_Field1, 2, …",
                evidence="no alias on an aggregate/computed column -> `Calculated_Field1`",
                rewrite="add `as [Name]`",
                rewrite_status="VERIFIED",
            )
        if plain:
            yield Finding(
                rule="missing_alias_plain",
                severity=Severity.ADVISORY,
                category=Category.COSMETIC,
                message=f"{len(plain)} plain column(s) without an alias — keys become Table_Column.",
                evidence="no output alias -> `Part_PartNum`",
                rewrite="add `as [Name]`",
                rewrite_status="VERIFIED",
                detail=", ".join(plain[:6]),
            )
        break


@ast_rule
def r_join_missing_company(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    for join in ctx.tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        if not any(c.name.lower() == "company" for c in on.find_all(exp.Column)):
            yield Finding(
                rule="join_missing_company",
                severity=Severity.ADVISORY,
                category=Category.POLICY,
                message="Join predicate has no `Company` key.",
                evidence=(
                    "row counts are IDENTICAL with and without Company in a "
                    "single-company install — correctness impact is UNKNOWN in general"
                ),
                rewrite="add `[A].[Company] = [B].[Company]` to the ON clause",
                rewrite_status="INFERRED",
            )
            return


@ast_rule
def r_join_on_company_only(ctx: Ctx) -> Iterator[Finding]:
    """Regression coverage: r join on company only."""
    if ctx.tree is None:
        return
    for join in ctx.tree.find_all(exp.Join):
        if isinstance(join.this, exp.Lateral):
            continue
        on = join.args.get("on")
        if on is None or not _on_fully_qualified(on):
            continue
        keys = _cross_table_comparisons(on)
        equalities = [k for k in keys if isinstance(k, exp.EQ)]
        if equalities and all(
            k.this.name.lower() == "company" and k.expression.name.lower() == "company"
            for k in equalities
        ):
            yield Finding(
                rule="join_on_company_only",
                severity=Severity.UNSUPPORTED,
                category=Category.SILENT_WRONG,
                message=(
                    "Join predicate keys ONLY on `Company` — that is a cartesian product "
                    "against the whole target table, returned with no error."
                ),
                evidence="`inner join Erp.Customer as c on jh.Company = c.Company` multiplies rows unless a business key also joins the tables",
                rewrite=(
                    "no mechanical repair — the business key cannot be invented. "
                    "Name the missing key and re-ask, or drop the join."
                ),
                rewrite_status="NONE",
                requires_schema=True,
                detail=on.sql(dialect=DIALECT)[:120],
            )
            return
        if not keys:
            yield Finding(
                rule="join_no_key_predicate",
                severity=Severity.UNSUPPORTED,
                category=Category.SILENT_WRONG,
                message=(
                    "Join ON clause compares no column of one table to a column of the "
                    "other — a filter predicate is not a join key, so this is a cartesian."
                ),
                evidence="`full outer join Erp.LaborDtl l on l.ScrapQty > 0` — parses, runs, and is a cartesian",
                rewrite=(
                    "join on the real keys, or express the two populations as a "
                    "`union all` if they are disjoint"
                ),
                rewrite_status="NONE",
                requires_schema=True,
                detail=on.sql(dialect=DIALECT)[:120],
            )
            return


@ast_rule
def r_union_order_by(ctx: Ctx) -> Iterator[Finding]:
    """A set operation's trailing ``ORDER BY`` is SILENTLY DISCARDED."""
    if not ctx.setop_trailing_orders:
        return
    yield Finding(
        rule="union_order_by_discarded",
        severity=Severity.REPAIRABLE,
        category=Category.SILENT_WRONG,
        message=(
            "`order by` after a set operation is SILENTLY DISCARDED — rows come back "
            "unordered, with no error."
        ),
        evidence=(
            "`desc` and `asc` returned byte-identical rows "
            "and the parsed DS carried `QuerySortBy == []`; parsing alone does not verify ordering"
        ),
        rewrite=(
            "put the set operation in a CTE and sort + bound OUTSIDE it: "
            "`with [u] as ( … union all … ) select top N [u].[C] from [u] order by [u].[C] desc`"
        ),
        rewrite_status="VERIFIED",
        detail=(
            "The QUALIFIED form is discarded too, so the expression-repeat rewrite every "
            "other sort trap uses does NOT work here. Use the CTE wrap, NOT a derived-table "
            "wrap with per-branch bounds: the CTE form returns the "
            "known top-5 exactly, while the branch-bounded derived wrap ranked a TRUNCATED "
            "sample and returned a different, wrong top-5."
        ),
    )


@ast_rule
def r_aggregate_over_derived_setop(ctx: Ctx) -> Iterator[Finding]:
    """Regression coverage: r aggregate over derived setop."""
    if ctx.tree is None:
        return
    for select in ctx.tree.find_all(exp.Select):
        if _from_setop(select) is None:
            continue
        aggregated = any(_own_has_agg(select, e) for e in select.expressions)
        if not (aggregated or select.args.get("group") is not None):
            continue
        yield Finding(
            rule="aggregate_over_derived_setop",
            severity=Severity.UNSUPPORTED,
            category=Category.RUN_ERROR,
            message=(
                "Epicor cannot aggregate or GROUP BY over a DERIVED set operation — "
                "`Bad SQL statement.` The same set operation behind a CTE works."
            ),
            evidence=(
                "`select count(*) from "
                "(A union all B) as [u]` -> `Bad SQL statement.`; the CTE control returns "
                "the aggregate over the full set. `group by` over one -> `Number of display fields in "
                "subquery … type UnionAll, differs`"
            ),
            rewrite=(
                "move the set operation into a CTE: "
                "`with [u] as ( … union all … ) select count(*) as [N] from [u]`"
            ),
            rewrite_status="VERIFIED",
        )
        return


@ast_rule
def r_setop_branch_arity(ctx: Ctx) -> Iterator[Finding]:
    """Regression coverage: r setop branch arity."""
    if ctx.tree is None:
        return
    for setop in ctx.tree.find_all(SETOP):
        if _ancestor_of(setop, (SETOP,)):
            continue
        branches = _setop_branches(setop)
        if len(branches) < 2:
            continue
        if any(any(_is_projection_star(e) for e in b.expressions) for b in branches):
            continue  # `*` is uncountable without a schema — not a clean bill
        widths = [len(b.expressions) for b in branches]
        if len(set(widths)) > 1:
            yield Finding(
                rule="setop_branch_arity_mismatch",
                severity=Severity.UNSUPPORTED,
                category=Category.RUN_ERROR,
                message=f"Set-operation branches project {widths} columns — they must match.",
                evidence=(
                    "`Number of display fields in subquery "
                    "'…', type UnionAll, differs from subquery …` (3 live instances)"
                ),
                rewrite="pad the narrower branch with typed `null as [Name]` placeholders",
                rewrite_status="INFERRED",
                detail=",".join(str(w) for w in widths),
            )
            return


@ast_rule
def r_setop_outer_bound(ctx: Ctx) -> Iterator[Finding]:
    """A ``top N`` on a select that WRAPS a set operation is SILENTLY IGNORED."""
    if ctx.tree is None:
        return
    for select in ctx.tree.find_all(exp.Select):
        if select.args.get("limit") is None:
            continue
        if _from_setop(select) is not None:
            yield Finding(
                rule="setop_outer_bound_ignored",
                severity=Severity.REPAIRABLE,
                category=Category.SILENT_WRONG,
                message=(
                    "`top N` on a select that WRAPS a set operation is IGNORED — the "
                    "declared bound does not hold and no error is raised."
                ),
                evidence=(
                    "an outer `top N` over a wrapped set operation is ignored: the row "
                    "count follows the branches and PageSize, not N"
                ),
                rewrite=(
                    "move the set operation into a CTE and keep `top N` on the outer select: "
                    "`with [u] as ( … union all … ) select top N [u].[C] from [u]`"
                ),
                rewrite_status="VERIFIED",
                detail=(
                    "the CTE form returns exactly N rows "
                    "and the correct global top-N; the same set operation as a DERIVED TABLE "
                    "returns the whole PageSize window. Per-branch bounds also bound the read, "
                    "but they truncate the input before any outer sort and can produce "
                    "a wrong ranking, so prefer the CTE."
                ),
            )
            return
        
        # so there is deliberately no finding for that shape.


@ast_rule
def r_aggregate_in_where(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    for where in ctx.tree.find_all(exp.Where):
        for node in where.find_all(exp.AggFunc):
            # An aggregate inside a subquery in the WHERE is legal.
            parent = node.parent
            in_subquery = False
            while parent is not None and parent is not where:
                if isinstance(parent, (exp.Subquery, exp.Select)):
                    in_subquery = True
                    break
                parent = parent.parent
            if in_subquery:
                continue
            yield Finding(
                rule="aggregate_in_where",
                severity=Severity.REPAIRABLE,
                category=Category.RUN_ERROR,
                message="An aggregate in WHERE fails — move it to HAVING.",
                evidence=(
                    "`An aggregate may not appear in the WHERE clause unless it is in "
                    "a subquery contained in a HAVING clause or a select list…`"
                ),
                rewrite="move the predicate into `having`",
                rewrite_status="INFERRED",
            )
            return


@ast_rule
def r_nested_aggregate(ctx: Ctx) -> Iterator[Finding]:
    if ctx.tree is None:
        return
    for node in ctx.tree.find_all(exp.AggFunc):
        for child in node.iter_expressions():
            if any(_is_agg(n) for n in child.walk()):
                yield Finding(
                    rule="nested_aggregate",
                    severity=Severity.UNSUPPORTED,
                    category=Category.RUN_ERROR,
                    message="Nested aggregates fail.",
                    evidence=(
                        "`Cannot perform an aggregate function on an expression "
                        "containing an aggregate or a subquery.`"
                    ),
                    rewrite="aggregate once in a derived table, then aggregate the derived table",
                    rewrite_status="INFERRED",
                )
                return


@ast_rule
def r_plain_column_not_grouped(ctx: Ctx) -> Iterator[Finding]:
    for select in ctx.root_selects:
        items = select.expressions
        if not any(_own_has_agg(select, i) for i in items):
            continue
        group = select.args.get("group")
        keys = {_norm(e) for e in (group.expressions if group is not None else [])}
        offenders: list[str] = []
        for item in items:
            body = item.this if isinstance(item, exp.Alias) else item
            if _own_has_agg(select, body):
                continue
            if _norm(body) in keys:
                continue
            # COMPONENT-AWARE: T-SQL allows any expression built out of grouped
            # columns and constants. Demanding the whole expression appear in the
            # GROUP BY flagged `oh.Company + ': ' + c.Name` against
            # `group by oh.Company, c.Name`, and flagged a bare `'DMR' as [Src]`
            # constant (no columns at all) as an ungrouped column.
            for col in body.find_all(exp.Column):
                if _enclosing_select(col) is not select:
                    continue  # belongs to a scalar subquery, not to this GROUP BY
                if _under_agg(col, body):
                    continue
                if _norm(col) in keys:
                    continue
                offenders.append(col.sql(dialect=DIALECT))
        if offenders:
            yield Finding(
                rule="plain_column_not_grouped",
                severity=Severity.REPAIRABLE,
                category=Category.RUN_ERROR,
                message=(
                    f"Non-aggregated SELECT item(s) missing from GROUP BY: {', '.join(offenders[:6])}."
                ),
                evidence=(
                    "`Column 'Erp.Part.PartNum' is invalid in the select list because "
                    "it is not contained in either an aggregate function or the GROUP BY clause.`"
                ),
                rewrite="repeat each expression in `group by`",
                rewrite_status="VERIFIED",
                detail=", ".join(offenders),
            )
        break


@ast_rule
def r_derived_order_without_top(ctx: Ctx) -> Iterator[Finding]:
    """A derived table / CTE with its own ORDER BY needs its own TOP."""
    if ctx.tree is None:
        return
    inner: list[exp.Select] = []
    for sub in ctx.tree.find_all(exp.Subquery):
        if isinstance(sub.this, exp.Select):
            inner.append(sub.this)
    for with_ in ctx.tree.find_all(exp.With):
        for cte in with_.expressions:
            if isinstance(cte.this, exp.Select):
                inner.append(cte.this)
    roots = {id(s) for s in ctx.root_selects}
    for select in inner:
        if id(select) in roots:
            continue
        if select.args.get("order") is not None and select.args.get("limit") is None:
            yield Finding(
                rule="derived_order_without_top",
                severity=Severity.REPAIRABLE,
                category=Category.PARSE_ERROR,
                message="A derived table / CTE with `order by` must also carry its own `top N`.",
                evidence=(
                    "`The ORDER BY clause is not valid in views, inline functions, "
                    "derived tables, sub-queries, and common table expressions, unless TOP … is also specified.`"
                ),
                rewrite="add `top N` to the inner select, or drop the inner `order by`",
                rewrite_status="VERIFIED",
            )
            return


def _parent_alias(select: exp.Select) -> str:
    from_ = _from_clause(select)
    this = getattr(from_, "this", None)
    if isinstance(this, exp.Table):
        return (this.alias or this.name or "").lower()
    if isinstance(this, exp.Subquery):
        return (this.alias or "").lower()
    return ""


def _parent_side_aggregates(select: exp.Select) -> list[str]:
    """Aggregates whose argument columns come from the FROM-side (parent) table.

    THE canonical fan-out signature: `sum([OH].[OrderAmt])` with a join to
    `OrderDtl` multiplies the header value by the line count. `count(*)` is
    excluded — counting the joined rows is usually what was meant.
    """
    parent = _parent_alias(select)
    if not parent:
        return []
    out: list[str] = []
    for item in select.expressions:
        body = item.this if isinstance(item, exp.Alias) else item
        aggs = [body] if isinstance(body, exp.AggFunc) else list(body.find_all(exp.AggFunc))
        for agg in aggs:
            if _enclosing_select(agg) is not select:
                continue
            if isinstance(agg, exp.Count) and isinstance(agg.this, (exp.Star, type(None))):
                continue
            tables = {c.table.lower() for c in agg.find_all(exp.Column) if c.table}
            if parent in tables:
                out.append(agg.sql(dialect=DIALECT)[:60])
    return out


@ast_rule
def r_fanout_risk(ctx: Ctx) -> Iterator[Finding]:
    for select in ctx.root_selects:
        joins = len(select.args.get("joins") or [])
        if joins < 1:
            continue
        # The `>= 2` trigger missed the CANONICAL shape: a header joined to its
        # lines, where a header-side aggregate is multiplied by the line count.
        # The common case is one join, so a `>= 2` trigger could not fire on it.
        parent_aggs = _parent_side_aggregates(select)
        if not parent_aggs and not (joins >= 2 and any(_own_has_agg(select, i) for i in select.expressions)):
            continue
        if parent_aggs:
            why = (
                f"Aggregate(s) over the FROM-side table across {joins} join(s): "
                f"{', '.join(parent_aggs[:3])} — the parent value is repeated once per "
                "matching child row, so the total comes back several times too large."
            )
            detail = "parent_side_aggregate"
        else:
            why = (
                f"Aggregate over a {joins + 1}-table join — a sum on the many side of two "
                "joins returns a number several times too large, with NO error."
            )
            detail = "multi_join_aggregate"
        yield Finding(
            rule="fanout_risk",
            severity=Severity.ADVISORY,
            category=Category.GRAIN,
            message=why + " No error channel reports it.",
            evidence=(
                "`sum([OH].[OrderAmt])` joined to OrderDtl returns each header amount "
                "multiplied by its order-line count"
            ),
            rewrite="aggregate the child in a derived table first, then join the derived table",
            rewrite_status="INFERRED",
            detail=detail,
        )
        break


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

_SEVERITY_TO_VERDICT = {
    Severity.UNSUPPORTED: Verdict.UNSUPPORTED,
    Severity.REPAIRABLE: Verdict.REPAIRABLE,
}


def lint(sql: str, *, require_row_bound: bool = True) -> LintResult:
    """Classify ``sql`` against the Epicor BAQ dialect rules. No network."""
    sql = (sql or "").strip()
    if not sql:
        return LintResult(sql="", verdict=Verdict.UNPARSEABLE, parse_error="empty")

    masked = mask_literals(sql)
    tree, statements, parse_error = _parse(sql)
    ctx = Ctx(
        sql=sql,
        masked=masked,
        tree=tree,
        statements=statements,
        parse_error=parse_error,
        require_row_bound=require_row_bound,
    )

    findings: list[Finding] = []
    for rule in RAW_RULES:
        findings.extend(rule(ctx))
    if tree is not None:
        for rule in AST_RULES:
            try:
                findings.extend(rule(ctx))
            except Exception as exc:  # noqa: BLE001 — one bad rule must not sink the run
                findings.append(
                    Finding(
                        rule=f"lint_internal_error:{rule.__name__}",
                        severity=Severity.ADVISORY,
                        category=Category.UNTESTED,
                        message=f"lint rule crashed: {type(exc).__name__}: {exc}",
                        evidence="harness bug — not a property of the SQL",
                    )
                )

    # De-duplicate on (rule, detail) keeping first occurrence.
    seen: set[tuple[str, str]] = set()
    unique: list[Finding] = []
    for f in findings:
        key = (f.rule, f.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    if tree is None:
        verdict = Verdict.UNPARSEABLE
    elif any(f.severity is Severity.UNSUPPORTED for f in unique):
        verdict = Verdict.UNSUPPORTED
    elif any(f.severity is Severity.REPAIRABLE for f in unique):
        verdict = Verdict.REPAIRABLE
    else:
        verdict = Verdict.CLEAN

    order = {Severity.UNSUPPORTED: 0, Severity.REPAIRABLE: 1, Severity.ADVISORY: 2}
    unique.sort(key=lambda f: (order[f.severity], f.rule))

    return LintResult(
        sql=sql,
        verdict=verdict,
        findings=unique,
        parse_error=parse_error,
        statement_count=len(statements) or 1,
    )


def classify(sql: str) -> str:
    """Convenience: just the verdict string."""
    return lint(sql).verdict.value


#: Rule metadata, for the README and for a coverage test over the trap list.
def rule_registry() -> list[str]:
    return sorted({fn.__name__[2:] for fn in RAW_RULES + AST_RULES})


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    text = sys.stdin.read() if not sys.argv[1:] else " ".join(sys.argv[1:])
    res = lint(text)
    print(json.dumps(res.to_dict(), indent=2))

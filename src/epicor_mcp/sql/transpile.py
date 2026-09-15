"""Plain T-SQL  ->  Epicor BAQ SQL.  Deterministic, server-side, unit-testable.

WHY THIS EXISTS
---------------
The model writes plain, standard T-SQL. Epicor's BAQ SQL is a *subset* that
diverges from standard T-SQL in a handful of places, and several of those
divergences are **silent** — HTTP 200, a populated result set, and a wrong
answer. This module moves the burden of knowing them off a probabilistic model
and onto deterministic code with a known-answer test behind every rewrite.

THREE OUTCOMES, NEVER A SILENT GUESS
------------------------------------
``REWRITTEN``  the SQL was changed; every change is itemised in
               ``result.transformations`` for the response's assumptions channel.
``UNCHANGED``  nothing needed changing; ``result.sql`` is the input **byte for
               byte** (the transpiler never regenerates a statement it did not
               have to touch — that keeps the blast radius at zero for the
               majority case).
``REFUSED``    the construct has no proven Epicor equivalent, or repairing it
               would require a guess. ``result.error`` is an INV-1 envelope
               naming the construct and the supported alternative.

COMPATIBILITY BOUNDARY
----------------------
A successful HTTP response does not prove that Epicor honored the SQL. Distinct
aggregates can return incorrect counts, DISTINCT with TOP can retain duplicates,
and ORDER BY on a UNION can be discarded. Rewrites therefore need known-answer
coverage, not merely successful execution. Transformations carry a ``proof``
marker and an explanation of the relevant behavior. Constructs without a safe,
general equivalent are refused. The deterministic regression suites are
``tests/test_transpile.py`` and ``tests/test_transpile_ship_gate.py``.

WHAT THIS MODULE IS NOT
-----------------------
It is **not an authorization boundary**. Do not use a hand-rolled SQL parser
for the security gate — Epicor's own ``ParseFromSQL`` output
(``QueryTable`` rows with ``TableType == 'DB'``) is the authority on which tables
a statement reads. ``result.tables_referenced`` is for *diagnostics and cost
estimation only* and must never be the input to an allow/deny decision.
``inject_predicate`` is the seam a future row-level-security pass will use; the
decision itself lives in the RBAC enforcer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Mapping, Sequence

import sqlglot
from sqlglot import exp

__all__ = [
    "Outcome",
    "Transformation",
    "Advisory",
    "RowBound",
    "TableSchema",
    "Policy",
    "WEDGE_POLICY",
    "TranspileResult",
    "transpile",
    "DIALECT",
]

DIALECT = "tsql"

#: The identifier the union-wrap rewrite gives its derived table.
_WRAP_ALIAS = "u"


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


class Outcome(str, Enum):
    REWRITTEN = "REWRITTEN"
    UNCHANGED = "UNCHANGED"
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class Transformation:
    """One applied rewrite, itemised for the assumptions channel."""

    rule: str
    message: str
    evidence: str
    proof: str = "VERIFIED"  # VERIFIED | INFERRED
    before: str = ""
    after: str = ""

    def to_dict(self) -> dict:
        d = {
            "rule": self.rule,
            "message": self.message,
            "evidence": self.evidence,
            "proof": self.proof,
        }
        if self.before or self.after:
            d["before"] = self.before
            d["after"] = self.after
        return d


@dataclass(frozen=True)
class Advisory:
    """Something worth telling the caller that this module will NOT change."""

    rule: str
    message: str
    evidence: str
    detail: str = ""

    def to_dict(self) -> dict:
        d = {"rule": self.rule, "message": self.message, "evidence": self.evidence}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass(frozen=True)
class RowBound:
    """How the returned statement is bounded. ``PageSize`` is ALWAYS also sent.

    ``kind``   ``top`` | ``fetch`` | ``page_size_only``
    ``source`` ``caller`` | ``injected`` | ``clamped`` | ``per_branch``
    """

    kind: str
    value: int | None
    source: str
    note: str = ""

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "value": self.value, "source": self.source}
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class TranspileResult:
    outcome: Outcome
    sql: str | None
    transformations: list[Transformation] = field(default_factory=list)
    advisories: list[Advisory] = field(default_factory=list)
    error: dict | None = None
    row_bound: RowBound | None = None
    tables_referenced: list[str] = field(default_factory=list)

    @property
    def rewritten(self) -> bool:
        return self.outcome is Outcome.REWRITTEN

    @property
    def refused(self) -> bool:
        return self.outcome is Outcome.REFUSED

    @property
    def rules(self) -> list[str]:
        return [t.rule for t in self.transformations]

    @property
    def assumptions(self) -> list[str]:
        """One line per applied rewrite — for the response's assumptions bag."""
        return [t.message for t in self.transformations]

    def to_dict(self) -> dict:
        d: dict = {
            "outcome": self.outcome.value,
            "sql": self.sql,
            "transformations": [t.to_dict() for t in self.transformations],
            "advisories": [a.to_dict() for a in self.advisories],
        }
        if self.row_bound is not None:
            d["row_bound"] = self.row_bound.to_dict()
        if self.error is not None:
            d["error"] = self.error
        if self.tables_referenced:
            d["tables_referenced"] = self.tables_referenced
        return d


class _Refusal(Exception):
    """Internal control flow: abandon the rewrite and return an INV-1 envelope."""

    def __init__(self, envelope: dict) -> None:
        super().__init__(envelope.get("error", "refused"))
        self.envelope = envelope


def _envelope(
    error: str,
    message: str,
    *,
    evidence: str,
    valid: dict | None = None,
    retry_with: dict | None = None,
    detail: dict | None = None,
) -> dict:
    """INV-1 self-correcting envelope. Never a bare reason code (that invites a guess loop).

    Mirrors ``tools/_resolve.error_envelope`` so the shape is uniform across the
    server, plus an ``evidence`` key: a refusal that cannot cite a measurement is
    a refusal nobody can audit.
    """
    env: dict = {"error": error, "message": message, "evidence": evidence}
    if detail is not None:
        env["detail"] = detail
    if valid is not None:
        env["valid"] = valid
    if retry_with is not None:
        env["retry_with"] = retry_with
    return env


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


class TableSchema:
    """Case-insensitive table -> column lookup.

    Accepts either bare (``Part``) or schema-qualified (``Erp.Part``) keys and
    resolves either form, because the model writes ``Erp.Part`` and the field
    corpus is keyed both ways.
    """

    def __init__(self, tables: Mapping[str, Iterable[str]] | None = None) -> None:
        self._by_full: dict[str, frozenset[str]] = {}
        self._by_bare: dict[str, frozenset[str]] = {}
        self._display: dict[str, str] = {}
        #: lower(table) -> the column names AS SUPPLIED, so an INV-1 envelope
        #: hands back a name the caller can paste back verbatim.
        self._cased: dict[str, list[str]] = {}
        for name, cols in (tables or {}).items():
            cols = list(cols)
            colset = frozenset(c.lower() for c in cols)
            key = name.lower()
            self._by_full[key] = colset
            self._cased[key] = cols
            self._display[key] = name
            bare = key.rsplit(".", 1)[-1]
            # A bare name shared by two schemas is not a safe lookup key.
            if bare in self._by_bare and self._by_bare[bare] != colset:
                self._by_bare[bare] = frozenset()
                self._cased[bare] = []
            else:
                self._by_bare[bare] = colset
                self._cased.setdefault(bare, cols)
                self._display.setdefault(bare, name)

    def column_names(self, table: str) -> list[str]:
        """Column names in their original casing (for INV-1 envelopes)."""
        k = table.lower()
        return self._cased.get(k) or self._cased.get(k.rsplit(".", 1)[-1]) or []

    def __bool__(self) -> bool:
        return bool(self._by_full)

    def known(self, table: str) -> bool:
        k = table.lower()
        return k in self._by_full or k.rsplit(".", 1)[-1] in self._by_bare

    def columns(self, table: str) -> frozenset[str]:
        k = table.lower()
        if k in self._by_full:
            return self._by_full[k]
        return self._by_bare.get(k.rsplit(".", 1)[-1], frozenset())

    def has(self, table: str, column: str) -> bool:
        return column.lower() in self.columns(table)

    def display(self, table: str) -> str:
        k = table.lower()
        return self._display.get(k) or self._display.get(k.rsplit(".", 1)[-1]) or table


@dataclass(frozen=True)
class Policy:
    """SQL compatibility and row-bound options."""

    #: Injected when a statement carries no row bound of its own.
    default_limit: int = 100
    #: Hard ceiling; a caller `top N` above this is clamped down.
    max_rows: int = 1000
    #: Wildcard projection is refused; the recovery lists explicit columns.
    refuse_select_star: bool = True
    #: Refuse a statement whose FROM/JOIN tables are not all in the schema.
    #: OFF by default: this module is NOT the authorization boundary (resolved-dataset authorization rule).
    refuse_unknown_tables: bool = False

    # --- rewrite-safety policy SHIP GATE -------------------------------------------- #
    # Three rewrite passes can cause a silent wrong answer. They are
    # gated explicitly so a caller turns them off on purpose, rather than by
    # happening not to supply a schema. Defaults are UNCHANGED (True) so the
    # tests that exercise these passes keep exercising them; the production
    # entry point uses WEDGE_POLICY, which turns all three off, and
    # tests/test_transpile_ship_gate.py asserts that it does.
    #: S1 — ``_pass_qualify`` attaches a bare column to the WRONG table when a
    #: source is a derived table or a CTE (a subquery source maps to "", so it
    #: is a candidate for neither qualification nor the ambiguity refusal).
    #: The rewrite can then return 0.0 where the input returns a non-zero quantity.
    enable_qualify_pass: bool = True
    #: S2 — the union wrap injects a per-branch ``top N``, so the outer ORDER BY
    #: ranks a truncated sample rather than the complete result.
    enable_setop_order_wrap: bool = True
    #: S3 — ``_guard_alias_shadow`` is defeated by a derived-table source, and
    #: SUPPLYING THE SCHEMA is what defeats it (without a schema it correctly
    #: refuses). Any guard whose safety decreases with more information is
    #: inverted. False => always use the no-schema fallback, which refuses
    #: correctly.
    schema_derived_shadow_owners: bool = True


#: The production transpiler uses only the validated subset: safety-class
#: limit normalisation, the ``order by <alias>`` repair and the row-bound seam.
#: The three silent-wrong passes are OFF. Feature E13 (Phase 1) either re-enables
#: them with a known-answer proof or deletes them; until then this is the only
#: policy a production call site may use.
#:
#: ``refuse_select_star`` is False here and that is NOT a relaxation: ``select *``
#: is still refused, one parse later, by ``sql/lint.py`` — which can hand back the
#: table's REAL column list, because Epicor has by then expanded the star into
#: ``QueryField`` rows. Both transpilation and parsed lint must offer that recovery
#: (*"refuse, serve the column list"*), and this module holds no schema with
#: which to serve it. Refusing here would be slightly faster and strictly less
#: useful. ``tests/test_wedge_query_pipe.py`` asserts the refusal still happens.
WEDGE_POLICY = Policy(
    enable_qualify_pass=False,
    enable_setop_order_wrap=False,
    schema_derived_shadow_owners=False,
    refuse_select_star=False,
)


#: A hook for a future row-level-security pass. Given the parsed statement and
#: the set of ``schema.table`` names it reads, return extra WHERE conjuncts as
#: SQL text keyed by table alias. NOT implemented here — authorization gate owns the gate.
PredicateInjector = Callable[[exp.Expression, Sequence[str]], Mapping[str, str]]


# --------------------------------------------------------------------------- #
# Text masking — literals and comments blanked, offsets preserved
# --------------------------------------------------------------------------- #

#: ONE left-to-right pass, literals FIRST in the alternation. See :func:`_mask`.
_MASKABLE = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)


def _mask(sql: str) -> str:
    """Blank string literals and comments, preserving length.

    Without this ``where [T].[GroupID] = 'top (100)'`` would trip the paren rule
    — the same class of bug as the legacy ``unknown_columns`` envelope blaming a filter for a
    quoted literal that happened to look like a column name.

    **Masked in ONE pass on purpose.** The previous version blanked
    line comments *before* string literals, so a ``--`` inside a literal blanked
    the rest of the line and every text-based rule went blind past it. The same
    defect in ``sql/lint.py`` let a set operation's silently-discarded ORDER BY
    reach Execute; here it blinded the ``top (N)`` / ``limit`` / multi-statement
    guards, which have AST backstops but should not have to rely on them. The
    literal alternative comes first so a quote that opens before a dash wins,
    and leftmost-match semantics give the reverse case to the comment.
    """

    def blank(m: re.Match) -> str:
        text = m.group(0)
        width = m.end() - m.start()
        if text.startswith("'"):
            return "'" + " " * (width - 2) + "'"
        return " " * width

    return _MASKABLE.sub(blank, sql)


# These three are INVISIBLE in the AST: sqlglot normalises `top (100)` and
# `top 100` to byte-identical trees and rewrites `limit 5` into `TOP 5`. Detect
# them on the masked TEXT or they are silently "already fixed" and never
# announced — and `top (100)` is the single most dangerous shape in the dialect.
_RE_TOP_PAREN = re.compile(r"\btop\s*\(\s*\d+\s*\)", re.I)
_RE_LIMIT = re.compile(r"\blimit\s+\d+", re.I)
_RE_TOP_THEN_DISTINCT = re.compile(r"\btop\s+\(?\s*\d+\s*\)?\s+distinct\b", re.I)
_RE_STATEMENT_SPLIT = re.compile(r";\s*\S")


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #


def _own_nodes(select: exp.Select, kind: type) -> list:
    """Nodes of ``kind`` whose nearest enclosing SELECT is ``select``.

    sqlglot's ``Scope.columns`` lifts a correlated subquery's columns into the
    OUTER scope, which makes a naive walk report spurious
    ``unqualified_column_on_join`` findings for a perfectly legal single-table
    subquery (``select top 1 [CustNum] from Erp.Customer …``). A finding must
    belong to the SELECT that owns the node.
    """
    out = []
    for node in select.find_all(kind):
        parent = node.parent
        while parent is not None and not isinstance(parent, exp.Select):
            parent = parent.parent
        if parent is select:
            out.append(node)
    return out


def _sources(select: exp.Select) -> dict[str, str]:
    """alias -> table name for this SELECT's own FROM and JOINs.

    A derived table or CTE reference maps to ``""`` (unknown physical table).
    """
    out: dict[str, str] = {}

    def add(node) -> None:
        if isinstance(node, exp.Table):
            alias = node.alias or node.name
            out[alias] = ".".join(p for p in (node.db, node.name) if p)
        elif isinstance(node, exp.Subquery):
            out[node.alias or ""] = ""

    # sqlglot renamed the FROM arg key from "from" to "from_" in v26+. Read both
    # so a dependency bump cannot silently turn every join into a single-source
    # select — which is exactly what disables the qualification pass.
    frm = select.args.get("from") or select.args.get("from_")
    if frm is not None:
        add(frm.this)
    for join in select.args.get("joins") or []:
        add(join.this)
    return out


def _alias_map(select: exp.Select) -> dict[str, exp.Expression]:
    """lower(output alias) -> the expression it stands for."""
    out: dict[str, exp.Expression] = {}
    for item in select.expressions:
        if isinstance(item, exp.Alias):
            out[item.alias.lower()] = item.this
    return out


def _select_output_names(select: exp.Select) -> list[str] | None:
    """Output column names, or None if any item is unnameable."""
    names: list[str] = []
    for item in select.expressions:
        if isinstance(item, exp.Alias):
            names.append(item.alias)
        elif isinstance(item, exp.Column) and not isinstance(item.this, exp.Star):
            names.append(item.name)
        else:
            return None
    return names or None


def _union_branches(node: exp.Expression) -> list[exp.Select]:
    out: list[exp.Select] = []
    if isinstance(node, exp.SetOperation):
        out.extend(_union_branches(node.left))
        out.extend(_union_branches(node.right))
    elif isinstance(node, exp.Select):
        out.append(node)
    return out


def _last_branch(node: exp.Expression) -> exp.Select | None:
    branches = _union_branches(node)
    return branches[-1] if branches else None


def _setop_has_order(root: exp.Expression) -> bool:
    """True when a set operation carries an ORDER BY, on the node or the last branch.

    The tsql parser attaches a trailing ORDER BY to the LAST BRANCH, not to the
    ``Union`` node — checking only the node misses every real case.
    """
    if root.args.get("order") is not None:
        return True
    last = _last_branch(root)
    return last is not None and last.args.get("order") is not None


def _is_agg(node: exp.Expression) -> bool:
    return isinstance(node, exp.AggFunc) or bool(list(node.find_all(exp.AggFunc)))


def _sql(node: exp.Expression) -> str:
    return node.sql(dialect=DIALECT)


# --------------------------------------------------------------------------- #
# The passes
# --------------------------------------------------------------------------- #


def _pass_text_guards(sql: str, masked: str, tx: list[Transformation]) -> None:
    """Refusals and detections that only exist in the raw text."""
    if _RE_STATEMENT_SPLIT.search(masked):
        raise _Refusal(
            _envelope(
                "sql_multiple_statements",
                "Send ONE select statement. Epicor refuses a stacked batch outright.",
                evidence="Epicor behavior: `Only one SQL statement can be processed at a time "
                "for BAQ generation.`",
            )
        )


def _pass_shape_refusals(root: exp.Expression, masked: str, policy: Policy) -> None:
    """Constructs with no proven Epicor equivalent, or refused by server policy."""
    if not isinstance(root, (exp.Select, exp.SetOperation, exp.Subquery)):
        raise _Refusal(
            _envelope(
                "sql_not_a_select",
                f"Only SELECT is accepted; this is {type(root).__name__.upper()}. "
                "This server is read-only.",
                evidence="Epicor behavior: `Only SELECT statements can be processed for BAQ "
                "generation.`",
            )
        )

    if root.find(exp.Into) is not None:
        raise _Refusal(
            _envelope(
                "sql_select_into",
                "`select … into` is SILENTLY DROPPED by Epicor — rows come back and nothing "
                "is created. Remove the `into`.",
                evidence="Epicor can return rows while ignoring SELECT INTO",
            )
        )

    if root.find(exp.Exists) is not None:
        raise _Refusal(
            _envelope(
                "sql_exists_unsupported",
                "EXISTS / NOT EXISTS is dead in every form — the BAQ generator rewrites it "
                "into a malformed IN and the SQL never compiles. There is no mechanical "
                "equivalent for a correlated multi-key EXISTS, so this is refused rather "
                "than guessed.",
                evidence="Epicor behavior: `Incorrect syntax near the keyword 'in'.` for all "
                "five forms tested",
                valid={
                    "alternatives": [
                        "where [A].[Key] in (select [B].[Key] from Erp.B as [B] where …)",
                        "left outer join Erp.B as [B] on … where [B].[Key] is null   -- NOT EXISTS",
                    ]
                },
            )
        )

    if root.find(exp.Window) is not None:
        raise _Refusal(
            _envelope(
                "sql_window_unsupported",
                "Window functions fail: Epicor's parser strips the OVER clause and then "
                "reports the function must have one.",
                evidence="Epicor behavior: `The function 'row_number' must have an OVER clause.`",
                valid={
                    "alternatives": [
                        "`top N` + `order by <expression>` is a TRUE global top-N",
                        "per-group ranking needs a derived table with its own aggregate",
                    ]
                },
            )
        )

    if root.find(exp.Pivot) is not None:
        raise _Refusal(
            _envelope(
                "sql_pivot_unsupported",
                "PIVOT is refused at parse time by Epicor.",
                evidence="Epicor behavior: `Error parsing SQL to BAQ - Pivot expressions are "
                "not currently supported.`",
                valid={"alternatives": ["sum(case when … then … else 0 end) as [Bucket]"]},
            )
        )

    if root.find(exp.Parameter) is not None or re.search(r"(?<![\w@])@\w+", masked):
        raise _Refusal(
            _envelope(
                "sql_parameter_marker",
                "There is no parameter binding on the ad-hoc path — inline the literal value.",
                evidence='Epicor behavior: `Must declare the scalar variable "@OrderNum".`',
            )
        )

    for node in root.find_all(exp.AggFunc):
        if any(isinstance(c, exp.Distinct) for c in node.iter_expressions()):
            fn = type(node).__name__.lower()
            raise _Refusal(
                _envelope(
                    "sql_distinct_in_aggregate",
                    f"`{fn}(distinct …)` — Epicor SILENTLY DROPS the DISTINCT and returns a "
                    "wrong number with an EMPTY Errors array. It is refused, not rewritten: "
                    "the only proven replacement answers ONE distinct count and cannot carry "
                    "the other aggregates in the same select.",
                    evidence='Engine compatibility behavior; validate against the configured Epicor version and local data.',
                    valid={
                        "replacement": "select count(*) as [N] from (select distinct "
                        "[T].[C] as [C] from Erp.T as [T]) as [t]"
                    },
                    detail={
                        "aggregate": _sql(node),
                        "why_not_rewritten": "count(distinct) INSIDE a derived table is still "
                        "wrong, and the derived-table form does not compose with a sibling "
                        "sum()/avg() in the same grouped select — which is the shape the model "
                        "typically emits.",
                    },
                )
            )

    for join in root.find_all(exp.Join):
        kind = (join.args.get("kind") or "").upper()
        side = (join.args.get("side") or "").upper()
        if kind == "CROSS":
            raise _Refusal(
                _envelope(
                    "sql_cross_join",
                    "CROSS JOIN is refused — it is unbounded by construction.",
                    evidence="The public SQL dialect refuses table-valued function sources",
                    valid={"alternatives": ["join on Company plus the business key"]},
                )
            )
        if not kind and not side and not join.args.get("on") and not join.args.get("using"):
            raise _Refusal(
                _envelope(
                    "sql_comma_join_no_predicate",
                    "A comma join with no ON predicate is a cross join — refused.",
                    evidence="Comma joins may execute, but the SQL dialect policy refuses the "
                    "unbounded shape",
                    valid={
                        "alternatives": [
                            "inner join Erp.B as [B] on [A].[Company] = [B].[Company] "
                            "and [A].[Key] = [B].[Key]"
                        ]
                    },
                )
            )

    if policy.refuse_select_star:
        for select in root.find_all(exp.Select):
            for item in select.expressions:
                star = isinstance(item, exp.Star) or (
                    isinstance(item, exp.Column) and isinstance(item.this, exp.Star)
                )
                if star:
                    raise _Refusal(
                        _envelope(
                            "sql_select_star",
                            "`select *` is refused — name the columns you need. "
                            "Wide tables can exceed the response-size limit when every "
                            "column is returned.",
                            evidence="Wildcard projection returns every column and prevents explicit response-size control",
                            detail={"item": _sql(item)},
                        )
                    )

    # `top N percent` and `top 0` mean something Epicor cannot do. Rewriting
    # either one would be a GUESS about the caller's intended row count.
    for select in root.find_all(exp.Select):
        limit = select.args.get("limit")
        if isinstance(limit, exp.Limit):
            opts = limit.args.get("limit_options")
            if opts is not None and opts.args.get("percent"):
                raise _Refusal(
                    _envelope(
                        "sql_top_percent",
                        "`top N percent` returns N PERCENT of the table with no error — on a "
                        "16,000-row table that is 800 rows, not N. State an absolute row "
                        "count instead. It is refused rather than converted because "
                        "`top 5 percent` -> `top 5` would silently shrink the answer ~160x.",
                        evidence="A percentage bound depends on table size and cannot be replaced by a fixed row count",
                        valid={"replacement": "select top 100 …"},
                    )
                )
            val = limit.expression
            if isinstance(val, exp.Literal) and not val.args.get("is_string"):
                if int(val.name) == 0:
                    raise _Refusal(
                        _envelope(
                            "sql_top_zero",
                            "`top 0` is not 'no rows' on Epicor — it parses to TopRowExpr=0, "
                            "which is NO LIMIT. State a positive row count.",
                            evidence="TopRowExpr=0 does not enforce a zero-row limit and can fail when combined with ordering",
                            valid={"replacement": "select top 100 …"},
                        )
                    )


def _pass_distinct_with_bound(root: exp.Expression) -> None:
    """`distinct` + `top` in one select — silently wrong, so refused.

    Both keyword orders are broken and they break DIFFERENTLY:
      `select top N distinct …`  -> hard parse 400 (loud, recoverable)
      `select distinct top N …`  -> HTTP 200, N rows, DISTINCT dropped (silent)

    A repair loop can turn the first into the second when it is fed a correct
    diagnostic — trading a loud failure for a silent wrong answer.
    A rewrite pass must not have that degree of freedom, and it must not
    "normalise the keyword order" either, because the target is the silent one.
    """
    for select in root.find_all(exp.Select):
        limit = select.args.get("limit")
        if select.args.get("distinct") is None or limit is None:
            continue
        if isinstance(limit, exp.Fetch):
            # OFFSET/FETCH is a different mechanism whose behaviour with DISTINCT
            # is unconfirmed. Not refused, but not claimed either — see advisories.
            continue
        n = limit.expression.name if isinstance(limit.expression, exp.Literal) else "N"
        inner = select.copy()
        inner.set("limit", None)
        inner.set("order", None)
        raise _Refusal(
            _envelope(
                "sql_distinct_with_top",
                "`distinct` and `top` in the same select SILENTLY RETURN DUPLICATES — the "
                "DISTINCT is dropped and no error is raised. Wrap the distinct in a derived "
                "table and bound the OUTER select, or drop the `top` and let PageSize bound "
                "it (PageSize preserves the DISTINCT).",
                evidence="Epicor behavior: `select distinct top N` returns N rows with the "
                "DISTINCT dropped, so duplicates fill the page; the derived-table form "
                "returns the true distinct set. `select top N distinct` is a hard parse "
                "400.",
                valid={
                    "replacement": "select top N [t].[C] as [C] from (select distinct "
                    "[T].[C] as [C] from Erp.T as [T]) as [t]",
                    "also_correct": "drop the `top` — PageSize alone bounds a `select "
                    "distinct` AND preserves it",
                },
                detail={"top": n, "distinct_select": _sql(inner)[:400]},
            )
        )


def _pass_ordinals(root: exp.Expression, tx: list[Transformation]) -> None:
    """`order by 1` is SILENTLY DISCARDED; `group by 1` fails at run time.

    Both are repaired by substituting the SELECT item at that position — which
    is what the ordinal *means*, so the substitution is definitional, not a
    guess.
    """
    for select in root.find_all(exp.Select):
        items = select.expressions
        for clause_key, label, ev in (
            (
                "order",
                "order by",
                "Epicor behavior: `order by 2 desc` and `order by 2 asc` "
                "returned byte-identical rows, identical to no ORDER BY",
            ),
            (
                "group",
                "group by",
                "Epicor behavior: `Each GROUP BY expression must contain at least one column "
                "that is not an outer reference.`",
            ),
        ):
            clause = select.args.get(clause_key)
            if clause is None:
                continue
            for node in list(clause.expressions):
                target = node.this if isinstance(node, exp.Ordered) else node
                if not (isinstance(target, exp.Literal) and not target.args.get("is_string")):
                    continue
                idx = int(target.name)
                if idx < 1 or idx > len(items):
                    raise _Refusal(
                        _envelope(
                            "sql_ordinal_out_of_range",
                            f"`{label} {idx}` names SELECT item {idx}, but the select list has "
                            f"{len(items)}.",
                            evidence=ev,
                        )
                    )
                item = items[idx - 1]
                repl = item.this if isinstance(item, exp.Alias) else item
                before = _sql(node)
                target.replace(repl.copy())
                tx.append(
                    Transformation(
                        rule=f"{clause_key}_by_ordinal",
                        message=f"`{label} {idx}` replaced with the expression it names "
                        f"(`{_sql(repl)}`) — Epicor discards a positional sort silently.",
                        evidence=ev,
                        before=before,
                        after=_sql(node),
                    )
                )


def _pass_setop_order(
    root: exp.Expression,
    tx: list[Transformation],
    policy: Policy | None = None,
    advisories: list[Advisory] | None = None,
) -> exp.Expression:
    """An ORDER BY attached to a UNION is SILENTLY DISCARDED — wrap it.

    Successful execution alone misses the ordering failure: `desc` and
    `asc` returned byte-identical rows and the parsed DS carried
    ``QuerySortBy == []``. The QUALIFIED form is discarded too, so repeating the
    expression — the fix for every other sort trap — does NOT work here.

    The one form that truly sorts is the union wrapped in a derived table with
    the sort on the OUTER select, which supplies a real ``QuerySortBy`` row.
    """
    if not isinstance(root, exp.SetOperation):
        return root
    if policy is not None and not policy.enable_setop_order_wrap:
        # Set-operation safety: the wrap itself is right, but the per-branch bound this
        # module then injects makes the outer ORDER BY rank a truncated sample —
        # this transform is disabled. The statement is
        # left alone and `sql/lint.py::setop_order_by_discarded` refuses it at
        # the parsed DS, which is rewrite-safety policy's own stated alternative.
        if advisories is not None and _setop_has_order(root):
            advisories.append(
                Advisory(
                    rule="setop_order_by_discarded_not_rewritten",
                    message="This set operation carries an ORDER BY, which Epicor SILENTLY "
                    "DISCARDS. The wrap that would fix it is disabled by the rewrite-safety policy ship "
                    "gate (it ranked a per-branch truncated sample"
                    "). Wrap the union in a derived table yourself and sort the "
                    "wrapper.",
                    evidence="Per-branch row bounds can change a wrapped set operation; the automatic wrap is disabled",
                )
            )
        return root

    order = root.args.get("order")
    holder: exp.Expression = root
    if order is None:
        # sqlglot (tsql) attaches a trailing ORDER BY to the LAST branch.
        last = _last_branch(root)
        if last is not None and last.args.get("order") is not None:
            order, holder = last.args["order"], last
    if order is None:
        return root

    branches = _union_branches(root)
    names = _select_output_names(branches[0]) if branches else None
    if not names:
        raise _Refusal(
            _envelope(
                "sql_union_order_unnameable",
                "The ORDER BY on this UNION is silently discarded by Epicor, and it cannot be "
                "repaired automatically because the first branch has a SELECT item with no "
                "output alias. Give every SELECT item an alias, or wrap the union yourself.",
                evidence="Epicor behavior: union + `order by` desc and asc returned byte-identical "
                "rows; the parsed DS carried QuerySortBy == []",
                valid={
                    "replacement": "select top N [u].[Col] as [Col] from ( <union> ) as [u] "
                    "order by [u].[Col] desc"
                },
            )
        )

    lower = {n.lower(): n for n in names}
    keys: list[exp.Ordered] = []
    for node in order.expressions:
        target = node.this if isinstance(node, exp.Ordered) else node
        if not isinstance(target, exp.Column) or isinstance(target.this, exp.Star):
            raise _Refusal(
                _envelope(
                    "sql_union_order_expression",
                    "The ORDER BY on this UNION is silently discarded by Epicor, and its sort "
                    f"key (`{_sql(target)}`) is an expression rather than one of the union's "
                    "output columns, so it cannot be lifted onto a wrapper automatically.",
                    evidence="ORDER BY on a set operation is discarded in both alias and qualified forms",
                    valid={"union_output_columns": names},
                )
            )
        real = lower.get(target.name.lower())
        if real is None:
            raise _Refusal(
                _envelope(
                    "sql_union_order_unknown_column",
                    f"`order by {target.name}` does not name one of the union's output columns, "
                    "and Epicor discards a union's ORDER BY silently rather than reporting it.",
                    evidence="ORDER BY on a set operation is discarded in both alias and qualified forms",
                    valid={"union_output_columns": names},
                )
            )
        # COPY the parser's own Ordered node and swap only its target. Building
        # a fresh `exp.Ordered(desc=False)` looks equivalent and is not: sqlglot's
        # tsql generator emits a NULLS-ordering emulation
        # (`ORDER BY CASE WHEN x IS NULL THEN 1 ELSE 0 END, x ASC`) for any
        # Ordered whose `desc` is False rather than None, and Epicor answers that
        # anonymous CASE sort key with "An object or column name is missing or
        # empty." This was caught by EXECUTING the output, not by parsing it —
        # the exact trap this project keeps re-learning.
        key = node.copy() if isinstance(node, exp.Ordered) else exp.Ordered(this=node.copy())
        key.set("this", exp.column(real, table=_WRAP_ALIAS))
        keys.append(key)

    before = _sql(root)
    holder.set("order", None)
    wrapper = exp.Select(
        expressions=[
            exp.alias_(exp.column(n, table=_WRAP_ALIAS), n, quoted=False) for n in names
        ]
    ).from_(
        exp.Subquery(this=root, alias=exp.TableAlias(this=exp.to_identifier(_WRAP_ALIAS)))
    )
    wrapper.set("order", exp.Order(expressions=keys))
    tx.append(
        Transformation(
            rule="union_order_by_discarded",
            message="The UNION's `order by` was moved onto a wrapping select — Epicor "
            "SILENTLY DISCARDS a sort attached to a set operation (in both the alias and "
            "the qualified form), so the rows would have come back unordered with no error.",
            evidence="Epicor behavior: desc and asc returned byte-identical rows and "
            "QuerySortBy == []; the wrapped form returned different rows with n_sortby == 1",
            before=before[:300],
            after=_sql(wrapper)[:300],
        )
    )
    return wrapper


def _pass_alias_sorts(
    root: exp.Expression,
    schema: TableSchema,
    tx: list[Transformation],
    policy: Policy | None = None,
) -> None:
    """`order by <alias>` / `having <alias>` — repeat the expression.

    Epicor resolves a bare name in ORDER BY / HAVING against the SOURCE COLUMNS
    ONLY, never against the SELECT's output aliases. When no source column
    matches, it fails loudly (``Invalid column name 'Revenue'.``). When one DOES
    match, it silently sorts by that column instead. So the
    substitution is only safe when the alias cannot be a source column, and the
    shadowed case must ERROR rather than pick an interpretation.
    """
    # Alias-owner safety: the shadow guard's schema-derived owner search is blind to
    # a derived-table source, and SUPPLYING the schema is what defeats it —
    # without one the fallback scans the statement's own qualified columns and
    # correctly refuses. Gated OFF => always take that no-schema path.
    guard_schema = schema
    if policy is not None and not policy.schema_derived_shadow_owners:
        guard_schema = TableSchema(None)
    for select in root.find_all(exp.Select):
        aliases = _alias_map(select)
        if not aliases:
            continue
        sources = _sources(select)
        for clause_key, label, ev in (
            (
                "order",
                "order by",
                'Engine compatibility behavior; validate against the configured Epicor version and local data.',
            ),
            (
                "having",
                "having",
                "Epicor behavior: `having [Cnt] > 100` -> `Invalid column name 'Cnt'.`",
            ),
        ):
            clause = select.args.get(clause_key)
            if clause is None:
                continue
            for col in list(clause.find_all(exp.Column)):
                if col.table or isinstance(col.this, exp.Star):
                    continue
                underlying = aliases.get(col.name.lower())
                if underlying is None:
                    continue
                # alias == its own source column name: legal as written, and
                # substituting would be a no-op that only adds noise.
                if isinstance(underlying, exp.Column) and (
                    underlying.name.lower() == col.name.lower()
                ):
                    continue
                _guard_alias_shadow(
                    col.name, underlying, select, sources, guard_schema, label
                )
                before = _sql(col)
                col.replace(underlying.copy())
                tx.append(
                    Transformation(
                        rule=f"{clause_key}_by_alias",
                        message=f"`{label} {before}` replaced with the expression the alias "
                        f"stands for (`{_sql(underlying)}`) — Epicor never resolves a SELECT "
                        "output alias in this clause.",
                        evidence=ev,
                        before=before,
                        after=_sql(underlying),
                    )
                )


def _guard_alias_shadow(
    name: str,
    underlying: exp.Expression,
    select: exp.Select,
    sources: Mapping[str, str],
    schema: TableSchema,
    label: str,
) -> None:
    """Refuse when the alias ALSO names a real column of a source table.

    The alias collision can change the selected sort column:
        select top 5 [PW].[PartNum] as [OnHandQty], [PW].[OnHandQty] as [Q]
        from Erp.PartWhse as [PW] order by [OnHandQty] desc
    T-SQL sorts by the alias (PartNum, text). Epicor returned the rows sorted by
    the COLUMN OnHandQty — byte-identical to the `order by [PW].[OnHandQty]`
    control and completely different from the `order by [PW].[PartNum]` control.
    Substituting the alias's expression would therefore CHANGE the answer, and
    leaving it alone means the caller gets a sort they did not ask for. Neither
    is defensible, so it errors and names both readings.
    """
    owners: list[str] = []
    if schema:
        for alias, table in sources.items():
            if table and schema.known(table) and schema.has(table, name):
                owners.append(f"{alias}.{name}" if alias else f"{table}.{name}")
    else:
        # No schema: fall back to the columns the statement itself references.
        # Weaker, but it catches KA5's exact shape and never guesses.
        for col in select.find_all(exp.Column):
            if col.table and col.name.lower() == name.lower():
                owners.append(f"{col.table}.{col.name}")
    if not owners:
        return
    raise _Refusal(
        _envelope(
            "sql_ambiguous_sort_alias",
            f"`{label} {name}` is ambiguous: `{name}` is BOTH an output alias standing for "
            f"`{_sql(underlying)}` AND a real column ({', '.join(sorted(set(owners)))}). "
            "Standard T-SQL sorts by the alias; Epicor sorts by the COLUMN. Qualify the sort "
            "key so there is only one reading.",
            evidence="Epicor behavior: with `[PW].[PartNum] as [OnHandQty]`, `order by "
            "[OnHandQty] desc` returned rows byte-identical to `order by [PW].[OnHandQty] "
            "desc` and completely different from `order by [PW].[PartNum] desc`",
            valid={
                "sort_by_the_alias_expression": f"{label} {_sql(underlying)}",
                "sort_by_the_column": f"{label} {sorted(set(owners))[0]}",
            },
            detail={"alias": name, "candidates": sorted(set(owners))},
        )
    )


def _pass_qualify(
    root: exp.Expression, schema: TableSchema, tx: list[Transformation],
    advisories: list[Advisory], policy: Policy | None = None,
) -> None:
    """Qualify a bare column on a join. Ambiguity ERRORS; it is never guessed.

    Names such as ``PartNum`` and ``OnHandQty`` occur on many tables — guessing
    an owner here re-creates the exact failure class this server exists to eliminate.
    """
    # Unresolved-source safety: an unresolvable source (derived table / CTE) can never be a
    # candidate owner, so with exactly one PHYSICAL table carrying the name the
    # pass qualifies confidently — and wrongly, which can turn a non-zero answer
    # into 0.0. Gated OFF => take the no-schema branch, which advises and
    # changes nothing.
    if policy is not None and not policy.enable_qualify_pass:
        schema = TableSchema(None)
    for select in root.find_all(exp.Select):
        sources = _sources(select)
        if len(sources) < 2:
            continue
        aliases = set(_alias_map(select))
        bare = [
            c
            for c in _own_nodes(select, exp.Column)
            if not c.table and not isinstance(c.this, exp.Star)
        ]
        bare = [c for c in bare if c.name.lower() not in aliases]
        if not bare:
            continue
        if not schema:
            advisories.append(
                Advisory(
                    rule="unqualified_column_no_schema",
                    message="Unqualified column(s) on a join: "
                    f"{', '.join(sorted({c.name for c in bare}))}. Epicor resolves them "
                    "against the FIRST table only and then fails. No schema was supplied, "
                    "so they were NOT qualified — that would be a guess.",
                    evidence="Epicor behavior: unqualified column on a JOIN -> `Invalid column "
                    "name 'OnHandQty'.`",
                    detail=", ".join(sorted({c.name for c in bare})),
                )
            )
            continue
        for col in bare:
            owners = [
                alias
                for alias, table in sources.items()
                if table and schema.known(table) and schema.has(table, col.name)
            ]
            if len(owners) == 1:
                before = _sql(col)
                col.set("table", exp.to_identifier(owners[0]))
                tx.append(
                    Transformation(
                        rule="unqualified_column_on_join",
                        message=f"`{before}` qualified as `{_sql(col)}` — on a join an "
                        "unqualified column resolves against the FIRST table only.",
                        evidence="Epicor behavior: `Invalid column name 'OnHandQty'.`",
                        before=before,
                        after=_sql(col),
                    )
                )
            elif len(owners) > 1:
                raise _Refusal(
                    _envelope(
                        "sql_ambiguous_column",
                        f"`{col.name}` is unqualified and {len(owners)} joined tables carry it. "
                        "Qualify it — the transpiler will not pick one.",
                        evidence="An unqualified column on a join resolves "
                        "against the FIRST table only; common names such as PartNum do not identify an owner",
                        valid={
                            "candidates": [f"[{a}].[{col.name}]" for a in sorted(owners)],
                            "tables": {a: schema.display(sources[a]) for a in sorted(owners)},
                        },
                        detail={"column": col.name},
                    )
                )
            else:
                known = [a for a, t in sources.items() if t and schema.known(t)]
                if not known:
                    continue  # nothing to be authoritative with
                raise _Refusal(
                    _envelope(
                        "sql_unknown_column",
                        f"`{col.name}` is not a column of any table in this statement.",
                        evidence="schema card / GetFieldList (authoritative physical columns)",
                        valid={
                            "columns_by_table": {
                                schema.display(sources[a]): sorted(
                                    schema.column_names(sources[a])
                                )[:60]
                                for a in known
                            }
                        },
                        detail={"column": col.name},
                    )
                )


def _setop_under(select: exp.Expression) -> exp.SetOperation | None:
    """The set operation this SELECT's sole source wraps, if any."""
    if not isinstance(select, exp.Select) or select.args.get("joins"):
        return None
    frm = select.args.get("from") or select.args.get("from_")
    if frm is None or not isinstance(frm.this, exp.Subquery):
        return None
    inner = frm.this.this
    return inner if isinstance(inner, exp.SetOperation) else None


#: sqlglot's arg key for a leading CTE list: ``with_`` from v30, ``with`` before.
_WITH_KEY = "with_" if "with_" in exp.Select.arg_types else "with"


def _is_star_item(node: exp.Expression) -> bool:
    """Is this projection ITEM a star (``*`` or ``[T].*``)?

    **Must not be `node.find(exp.Star)`**: `count(*)` contains an `exp.Star`, so
    the descendant search reads every grand-total aggregate as `select *`. That
    is the same over-broad-scope defect as the whole-statement `top` regex this
    change removes from ``lint.py`` — a signal about the OUTER item answered by
    searching everything underneath it. ``lint.star_projection_item`` gets this
    right on masked text by splitting at depth 0; this is the AST equivalent.
    """
    if isinstance(node, exp.Alias):
        node = node.this
    if isinstance(node, exp.Star):
        return True
    return isinstance(node, exp.Column) and isinstance(node.this, exp.Star)


def _setop_word(node: exp.SetOperation) -> str:
    """``UNION ALL`` / ``UNION`` / ``INTERSECT`` / ``EXCEPT`` for a message."""
    if isinstance(node, exp.Intersect):
        return "INTERSECT"
    if isinstance(node, exp.Except):
        return "EXCEPT"
    return "UNION ALL" if node.args.get("distinct") is False else "UNION"


def _cte_rewrite(root: exp.Select) -> str | None:
    """The caller's statement with the derived table turned into a CTE.

    Purely structural: ``select … from (<setop>) as [w] …`` becomes
    ``with [w] as (<setop>) select … from [w] …``. The alias is preserved, so
    every ``[w].[Col]`` reference in the projection, WHERE, GROUP BY and ORDER BY
    stays valid untouched — which is what makes this safe to hand back as a
    runnable recovery rather than a shape to imitate.

    A CTE preserves the outer row bound and aggregation structure for the
    supported recovery shape; a derived-table wrapper may not survive Epicor's
    parser with those semantics intact.

    ``with`` is spelled ``with_`` in sqlglot >= 30, exactly as ``from`` became
    ``from_`` in >= 26 (accept both keys for compatibility).
    Setting the wrong key is SILENT — the arg is stored, ``repr`` shows it, and
    the generator drops it — so the returned statement would have been a CTE
    reference with no CTE. :data:`_WITH_KEY` is resolved from the class itself
    rather than guessed, and a rewrite that fails to round-trip is discarded.
    """
    try:
        new = root.copy()
        frm = new.args.get("from") or new.args.get("from_")
        sub = frm.this
        alias = sub.alias
        if not alias:
            return None
        ident = exp.to_identifier(alias, quoted=True)
        cte = exp.CTE(this=sub.this.copy(), alias=exp.TableAlias(this=ident.copy()))
        frm.set("this", exp.Table(this=ident.copy()))
        existing = new.args.get(_WITH_KEY)
        if isinstance(existing, exp.With):
            existing.set("expressions", [*existing.expressions, cte])
        else:
            new.set(_WITH_KEY, exp.With(expressions=[cte]))
        out = _sql(new)
        # A rewrite that lost the CTE is worse than no rewrite: it names an
        # alias nothing defines. Prove it survived generation before serving it.
        return out if re.search(r"(?<![\w.])with(?![\w])", out, re.I) else None
    except Exception:  # noqa: BLE001 - a recovery we cannot build is simply omitted
        return None


def _pass_row_bound(
    root: exp.Expression, policy: Policy, tx: list[Transformation],
    advisories: list[Advisory],
) -> RowBound:
    """THE enforcement seam for row limits (requirement: safety, not convenience).

    `top (N)` and `top 0` remove the only query-level bound on a production ERP
    read and do it with no error, so this pass is kept even though models
    rarely emit either form. `PageSize` is sent
    unconditionally regardless of what this returns (row-bounding policy).
    """




























    inner_setop = _setop_under(root)
    if inner_setop is not None:
        reasons: list[str] = []
        if root.args.get("limit") is not None:
            reasons.append(
                "the outer `top` is IGNORED (Epicor returns the whole wrapped result)"
            )
        if root.args.get("group") is not None:
            reasons.append(
                'Engine compatibility behavior; validate against the configured Epicor version and local data.'
            )
        elif any(_is_agg(e) for e in root.expressions):
            reasons.append(
                "an aggregate over the wrapper makes its projection wider than the "
                "branches', which Epicor rejects outright"
            )
        if reasons:
            # A `retry_with` another gate refuses is not a recovery. The caller's
            # own `[w].*` survives a purely structural rewrite, and `select *` is
            # refused one parse later by `lint.select_star` (which serves the
            # real column list — the reason it is refused THERE and not here).
            # So on a star, hand back the SHAPE and say the columns must be
            # named, rather than a statement that dies at the next gate.
            starred = any(_is_star_item(e) for e in root.expressions)
            replacement = None if starred else _cte_rewrite(root)
            if starred:
                reasons.append(
                    "its projection is `*`, which is refused separately — name the "
                    "columns in the CTE rewrite"
                )
            raise _Refusal(
                _envelope(
                    "sql_setop_wrapper_unsafe",
                    "This statement wraps a "
                    f"{_setop_word(inner_setop)} in a derived table — `from (<set "
                    "operation>) as [alias]` — and Epicor does not evaluate that shape "
                    "correctly: " + "; and ".join(reasons) + ". Use a CTE instead: "
                    "`with [u] as (<your set operation>) select … from [u] …`. The CTE "
                    "honours the `top` and the GROUP BY exactly. Nothing else about your "
                    "statement needs to change — the alias and every column reference stay "
                    "as they are."
                    + (
                        f" Your statement rewritten that way: {replacement}"
                        if replacement
                        else ""
                    ),
                    evidence='Engine compatibility behavior; validate against the configured Epicor version and local data.',
                    valid={
                        "shape": "with [u] as ( <your set operation> ) select top N "
                        "[u].[Col] as [Col], sum([u].[Amt]) as [Total] from [u] "
                        "group by [u].[Col] order by sum([u].[Amt]) desc"
                    },
                    retry_with=({"sql": replacement} if replacement else None),
                    detail={
                        "set_operation": _setop_word(inner_setop).lower(),
                        "unsafe_because": reasons,
                    },
                )
            )
        # A plain projection over the wrap with no bound of its own is the ONE
        # shape that behaves EQUIVALENTLY to the CTE (same rows, same order).
        # It is left exactly as written and PageSize bounds it — the
        # `page_size_only` posture `select distinct` already uses. Injecting a
        # `top` here would declare a bound Epicor then ignores, which is the
        # defect this branch exists to remove.
        advisories.append(
            Advisory(
                rule="setop_wrapper_bounded_by_page_size_only",
                message="No `top` was injected: this select wraps a set operation in a "
                "derived table, and Epicor IGNORES a `top` on that wrapper and returns "
                "every row. PageSize is the real bound. To get a row "
                "bound that is honoured, wrap the set operation in a CTE instead — "
                "`with [u] as (<your set operation>) select top N … from [u]`.",
                evidence="Epicor behaviour: an outer `top N` over a derived-table-wrapped "
                "union bounds nothing, so the row count follows the union's size rather "
                "than N; the CTE form returns N rows",
            )
        )
        return RowBound(
            "page_size_only",
            None,
            "injected",
            "set operation wrapped in a derived table — Epicor ignores a `top` on the "
            "wrapper, so PageSize is the bound. Use a CTE for a real row bound.",
        )

    selects = _union_branches(root) if isinstance(root, exp.SetOperation) else [root]
    if not isinstance(root, exp.SetOperation):
        if not isinstance(root, exp.Select):
            inner = root.find(exp.Select)
            selects = [inner] if inner is not None else []
    elif any(isinstance(n, (exp.Intersect, exp.Except)) for n in root.walk()):
        advisories.append(
            Advisory(
                rule="setop_branch_bound_narrows_result",
                message="Row bounds are applied PER BRANCH because Epicor does not honour an "
                "outer `top` over a set operation. For INTERSECT / EXCEPT that can OMIT rows "
                "which are in the true result — bound the inputs with a `where` instead.",
                evidence="An outer TOP over a set operation is misapplied; "
                "the narrowing itself is INFERRED from set semantics, not measured",
            )
        )

    bounds: list[RowBound] = []
    for select in selects:
        bounds.append(_bound_one(select, policy, tx, advisories))

    if not bounds:
        return RowBound("page_size_only", None, "injected", "no SELECT found to bound")
    if len(bounds) == 1:
        return bounds[0]
    return _combine_branch_bounds(bounds)


def _combine_branch_bounds(bounds: list[RowBound]) -> RowBound:
    if not bounds:
        return RowBound("page_size_only", None, "injected", "no branch to bound")
    total = 0
    for b in bounds:
        if b.value is None:
            return RowBound(
                "page_size_only",
                None,
                "per_branch",
                f"{len(bounds)} set-operation branches, at least one of which cannot carry a "
                "`top` (DISTINCT); PageSize is the bound.",
            )
        total += b.value
    return RowBound(
        "top",
        total,
        "per_branch",
        f"bounded PER BRANCH: {len(bounds)} set-operation branches, up to {total} rows in "
        "total. An outer `top` over a set operation is NOT honoured by Epicor.",
    )


def _bound_one(
    select: exp.Select, policy: Policy, tx: list[Transformation],
    advisories: list[Advisory],
) -> RowBound:
    limit = select.args.get("limit")

    if isinstance(limit, exp.Fetch):
        count = limit.args.get("count")
        n = int(count.name) if isinstance(count, exp.Literal) else None
        if n is not None and n > policy.max_rows:
            limit.set("count", exp.Literal.number(policy.max_rows))
            tx.append(
                Transformation(
                    rule="row_bound_clamped",
                    message=f"`fetch next {n} rows only` clamped to {policy.max_rows}.",
                    evidence="row-bounding policy: row bounding is mandatory and unconditional",
                    before=f"fetch next {n} rows only",
                    after=f"fetch next {policy.max_rows} rows only",
                )
            )
            return RowBound("fetch", policy.max_rows, "clamped")
        if select.args.get("distinct") is not None:
            advisories.append(
                Advisory(
                    rule="distinct_with_fetch_untested",
                    message="`select distinct … offset/fetch` is unconfirmed on Epicor. "
                    "`distinct` + `top` IS silently wrong; treat this row "
                    "count as unconfirmed.",
                    evidence="OFFSET/FETCH without DISTINCT does not establish the behavior of DISTINCT with a row bound",
                )
            )
        return RowBound("fetch", n, "caller")

    if isinstance(limit, exp.Limit):
        val = limit.expression
        if isinstance(val, exp.Literal) and not val.args.get("is_string"):
            n = int(val.name)
            if n > policy.max_rows:
                limit.set("expression", exp.Literal.number(policy.max_rows))
                tx.append(
                    Transformation(
                        rule="row_bound_clamped",
                        message=f"`top {n}` clamped to the server maximum "
                        f"`top {policy.max_rows}`.",
                        evidence="row-bounding policy: row bounding is mandatory and unconditional",
                        before=f"top {n}",
                        after=f"top {policy.max_rows}",
                    )
                )
                return RowBound("top", policy.max_rows, "clamped")
            return RowBound("top", n, "caller")
        return RowBound("top", None, "caller", "non-literal TOP expression")

    # No bound at all.
    if select.args.get("distinct") is not None:
        # Injecting `top` here manufactures a silently wrong answer.
        advisories.append(
            Advisory(
                rule="distinct_bounded_by_page_size",
                message="No `top` was injected because this is a `select distinct`: adding one "
                "makes Epicor drop the DISTINCT and return duplicates with no error. PageSize "
                "bounds it correctly AND preserves the DISTINCT.",
                evidence="Epicor behavior: `select distinct top N` returns N rows with the "
                "DISTINCT dropped, so duplicates fill them; the same select bounded only "
                "by PageSize returns distinct rows, up to the true distinct count",
            )
        )
        return RowBound(
            "page_size_only", None, "injected", "DISTINCT select — PageSize is the bound"
        )

    # A grand-total aggregate with no GROUP BY returns exactly one row.
    exprs = select.expressions
    if exprs and select.args.get("group") is None and all(_is_agg(e) for e in exprs):
        return RowBound("top", 1, "caller", "grand-total aggregate returns one row")

    select.set("limit", exp.Limit(expression=exp.Literal.number(policy.default_limit)))
    tx.append(
        Transformation(
            rule="row_bound_injected",
            message=f"`top {policy.default_limit}` injected — the statement had no row bound "
            "and an unbounded read of a production ERP table is not permitted.",
            evidence="An unbounded read can exceed the MCP response limit; a positive `top N` bounds returned rows",
            before="(no row bound)",
            after=f"top {policy.default_limit}",
        )
    )
    return RowBound("top", policy.default_limit, "injected")


def _pass_advisories(
    root: exp.Expression, schema: TableSchema, advisories: list[Advisory]
) -> None:
    """Things worth saying that this module deliberately will NOT change."""
    # A join predicate with no Company term. Earlier checks returned identical
    # row counts with and without it on a single-company install, so inventing
    # the predicate would be a guess dressed as a fix.
    missing: list[str] = []
    company_only: list[str] = []
    for join in root.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        cols = {c.name.lower() for c in on.find_all(exp.Column)}
        if "company" not in cols:
            missing.append(_sql(join.this)[:60])
        elif cols == {"company"}:
            company_only.append(_sql(join.this)[:60])
    if missing:
        advisories.append(
            Advisory(
                rule="join_missing_company",
                message="Join predicate(s) with no `Company` term. Not required on a "
                "single-company install, but Company is the leading key of every Epicor index "
                "and the only form that stays correct if a second company is added.",
                evidence='Engine compatibility behavior; validate against the configured Epicor version and local data.',
                detail=", ".join(sorted(set(missing))),
            )
        )
    if company_only:
        advisories.append(
            Advisory(
                rule="join_on_company_only",
                message="Join predicate(s) whose ONLY term is `Company` — on a single-company "
                "install that is a CARTESIAN product against the whole table. The business key "
                "is missing and the transpiler cannot invent it.",
                evidence="Company alone is constant within a single-company query and does not identify the related business record",
                detail=", ".join(sorted(set(company_only))),
            )
        )

    # Fan-out: an aggregate over a header joined to two independent children.
    for select in root.find_all(exp.Select):
        joins = select.args.get("joins") or []
        if len(joins) >= 2 and any(_is_agg(e) for e in select.expressions):
            advisories.append(
                Advisory(
                    rule="fanout_risk",
                    message="An aggregate over a join of 3+ tables. If two of them are "
                    "independent children of the same parent, every sum() is MULTIPLIED with "
                    "no error. Aggregate one child per query, or aggregate the child in a "
                    "derived table first.",
                    evidence="Joining independent child collections multiplies aggregate inputs even when the SQL is syntactically valid",
                )
            )
            break

    # Output aliases: NOT auto-added. Two source tables both carrying PartNum
    # would collide on one output key, which is a silently wrong projection.
    for select in _union_branches(root) if isinstance(root, exp.SetOperation) else [root]:
        if not isinstance(select, exp.Select):
            continue
        plain = [
            _sql(i)
            for i in select.expressions
            if not isinstance(i, exp.Alias) and not isinstance(i, exp.Star)
        ]
        if plain:
            advisories.append(
                Advisory(
                    rule="missing_output_alias",
                    message="SELECT item(s) with no output alias. Epicor names them "
                    "`Table_Column` (or `Calculated_Field1` for a computed column). Aliases "
                    "are NOT auto-added: two joined tables both carrying `PartNum` would "
                    "collide on one output key.",
                    evidence="Unaliased projections use Epicor-generated names and can collide across joined tables",
                    detail=", ".join(plain[:8]),
                )
            )
            break


#: sqlglot's tsql generator emulates NULLS FIRST/LAST with an anonymous CASE in
#: the ORDER BY. Epicor rejects that sort key ("An object or column name is
#: missing or empty."), and the statement still PARSES — so nothing but an
#: Execute catches it. Any generator emulation this module did not ask for is a
#: bug in this module, not in the caller's SQL.
_RE_NULLS_EMULATION = re.compile(
    r"order\s+by[^;]*?case\s+when\s+.+?\s+is\s+null\s+then\s+1\s+else\s+0\s+end", re.I | re.S
)


def _guard_generator_artifacts(before: str, after: str) -> None:
    if _RE_NULLS_EMULATION.search(_mask(after)) and not _RE_NULLS_EMULATION.search(
        _mask(before)
    ):
        raise _Refusal(
            _envelope(
                "sql_transpiler_internal",
                "The rewrite could not be emitted safely: the SQL generator introduced a "
                "NULLS-ordering emulation in the ORDER BY that Epicor rejects. The original "
                "statement was left untouched. This is a transpiler defect, not a defect in "
                "the submitted SQL.",
                evidence="Epicor generator behavior: Epicor answers an anonymous "
                "`CASE WHEN x IS NULL THEN 1 ELSE 0 END` sort key with `An object or column "
                "name is missing or empty.` while the statement still PARSES cleanly",
            )
        )


def _collect_tables(root: exp.Expression) -> list[str]:
    """Diagnostics ONLY. Never an authorization input — see the module docstring."""
    out: list[str] = []
    ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    for t in root.find_all(exp.Table):
        if t.name.lower() in ctes and not t.db:
            continue
        out.append(".".join(p for p in (t.db, t.name) if p))
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# UD mirror joins — `_c` columns re-routed to `<Table>_UD`
# --------------------------------------------------------------------------- #

#: Explanation attached to every `ud_mirror_join` rewrite. The deterministic
#: cases live in tests/test_ud_column_rewrite.py.
_UD_JOIN_EVIDENCE = (
    'Engine compatibility behavior; validate against the configured Epicor version and local data.'
)


def _pass_ud_mirror_joins(
    root: exp.Expression, ud_mirrors, ud_catalogue, tx: list[Transformation]
) -> None:
    """Splice ``left outer join Erp.<T>_UD`` for `_c` refs on a base table.

    The engine — grammar, conservatism and the all-or-nothing rule — lives in
    ``validate_columns.splice_ud_joins`` so that layer 1's ``retry_with.sql``
    and this pass can never disagree about what is spliceable. A decline here
    mutates NOTHING and announces NOTHING: the statement falls through to E14,
    whose envelope names the mirror and (where the same engine allows) hands
    back the spliced statement as the recovery. Inert unless the caller
    supplies a mirror map, so every existing call site is byte-identical.
    """
    if not ud_mirrors or not isinstance(root, exp.Select):
        return
    # Local import: only statements that actually carry a mirror map pay for
    # the validate_columns module (which sql/__init__ deliberately does not
    # load at package import).
    from epicor_mcp.sql.validate_columns import splice_ud_joins

    for s in splice_ud_joins(root, mirrors=ud_mirrors, catalogue=ud_catalogue):
        cols = ", ".join(f"`{c}`" for c in s.columns)
        plural = len(s.columns) > 1
        tx.append(
            Transformation(
                rule="ud_mirror_join",
                message=(
                    f"{cols} {'are user-defined (_c) columns' if plural else 'is a user-defined (_c) column'}: "
                    f"at the SQL layer {'they live' if plural else 'it lives'} on the mirror table "
                    f"{s.mirror}, not on Erp.{s.parent}. Added `left outer join {s.mirror} as "
                    f"[{s.alias}] on [{s.qualifier}].[SysRowID] = [{s.alias}].[ForeignSysRowID]` and "
                    f"re-qualified the reference{'s' if plural else ''} to read from the mirror — "
                    "referencing a _c column on the base table parses and then fails at execution."
                ),
                evidence=_UD_JOIN_EVIDENCE,
                proof="VERIFIED",
                before=s.before,
                after=s.after,
            )
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def transpile(
    sql: str,
    *,
    schema: TableSchema | Mapping[str, Iterable[str]] | None = None,
    policy: Policy | None = None,
    inject_predicate: PredicateInjector | None = None,
    ud_mirrors=None,
    ud_catalogue=None,
) -> TranspileResult:
    """Rewrite plain T-SQL into SQL Epicor's BAQ engine answers correctly.

    ``schema`` is optional. Without it every rewrite that needs to know which
    table owns a column degrades to an ADVISORY — it never guesses.

    ``inject_predicate`` is the seam for a future row-level-security pass and is
    not implemented here (authorization gate owns the gate).

    ``ud_mirrors`` / ``ud_catalogue`` enable the announced
    ``ud_mirror_join`` rewrite — a ``validate_columns.UdMirrorMap`` (ALWAYS the
    deny-filtered one from ``load_ud_mirrors``) and E14's deny-filtered
    ``ColumnCatalogue``. Both default to ``None``, which disables the pass and
    keeps every existing call site byte-identical.
    """
    policy = policy or Policy()
    if not isinstance(schema, TableSchema):
        schema = TableSchema(schema)

    tx: list[Transformation] = []
    advisories: list[Advisory] = []

    if not sql or not sql.strip():
        return TranspileResult(
            Outcome.REFUSED,
            None,
            error=_envelope(
                "sql_empty",
                "No SQL was supplied.",
                evidence="n/a",
            ),
        )

    masked = _mask(sql)

    try:
        _pass_text_guards(sql, masked, tx)

        try:
            statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        except Exception as exc:  # noqa: BLE001 - sqlglot raises several types
            raise _Refusal(
                _envelope(
                    "sql_unparseable",
                    f"The statement could not be parsed as T-SQL: {exc}",
                    evidence="local parse (sqlglot, tsql dialect) — no Epicor call was made",
                )
            ) from exc

        if len(statements) > 1:
            raise _Refusal(
                _envelope(
                    "sql_multiple_statements",
                    "Send ONE select statement.",
                    evidence="Epicor behavior: `Only one SQL statement can be processed at a "
                    "time for BAQ generation.`",
                )
            )
        if not statements:
            # `;` and a lone comment both parse to nothing. Indexing here raised
            # IndexError, which no caller of a validation function expects.
            raise _Refusal(
                _envelope(
                    "sql_empty",
                    "No SQL statement was found — the input is empty, a comment, or only "
                    "punctuation.",
                    evidence="local parse (sqlglot, tsql dialect) — no Epicor call was made",
                )
            )
        root = statements[0]

        _pass_shape_refusals(root, masked, policy)
        # UD mirror joins run FIRST among the rewrites, so every later pass —
        # alias sorts, the row bound, the advisories — sees the final FROM set.
        _pass_ud_mirror_joins(root, ud_mirrors, ud_catalogue, tx)
        _pass_distinct_with_bound(root)
        _pass_ordinals(root, tx)
        root = _pass_setop_order(root, tx, policy, advisories)
        _pass_alias_sorts(root, schema, tx, policy)
        _pass_qualify(root, schema, tx, advisories, policy)
        row_bound = _pass_row_bound(root, policy, tx, advisories)
        _pass_advisories(root, schema, advisories)

        # Detections that live only in the raw text: sqlglot NORMALISES these
        # away, so without this block the most dangerous shape in the dialect
        # would be silently repaired and never announced.
        if _RE_TOP_PAREN.search(masked):
            tx.append(
                Transformation(
                    rule="top_parenthesised",
                    message="`top (N)` rewritten as `top N` — the parenthesised form is "
                    "Microsoft's own recommended syntax and Epicor treats it as NO LIMIT AT "
                    "ALL, with no error.",
                    evidence="Epicor behavior: `top (7)` parsed to "
                    "SelectListClause='All', TopRowExpr=0 and returned the whole PageSize "
                    "window; `top 7` returned exactly 7",
                    before="top (N)",
                    after="top N",
                )
            )
        if _RE_LIMIT.search(masked):
            tx.append(
                Transformation(
                    rule="limit_clause",
                    message="`limit N` rewritten as `top N` — Epicor's parser rejects `limit`.",
                    evidence="Epicor behavior: `SQL cannot be parsed: Incorrect syntax near "
                    "'limit'.`",
                    before="limit N",
                    after="top N",
                )
            )

        if inject_predicate is not None:
            raise _Refusal(
                _envelope(
                    "sql_predicate_injection_not_implemented",
                    "Row-level security predicate injection is a declared seam but is not "
                    "implemented in the transpiler. The authorization decision belongs to the "
                    "RBAC enforcer, evaluated against Epicor's OWN resolved table list.",
                    evidence="resolved-dataset authorization rule: never hand-roll a SQL parser for the security gate",
                )
            )

        out = root.sql(dialect=DIALECT, pretty=False)
        _guard_generator_artifacts(sql, out)
        if not tx:
            # Nothing needed changing: hand back the caller's own text, byte for
            # byte. A statement the transpiler did not have to touch must not be
            # re-serialised — that is free blast radius for zero benefit.
            return TranspileResult(
                Outcome.UNCHANGED,
                sql,
                transformations=[],
                advisories=advisories,
                row_bound=row_bound,
                tables_referenced=_collect_tables(root),
            )
        return TranspileResult(
            Outcome.REWRITTEN,
            out,
            transformations=tx,
            advisories=advisories,
            row_bound=row_bound,
            tables_referenced=_collect_tables(root),
        )

    except _Refusal as ref:
        return TranspileResult(
            Outcome.REFUSED,
            None,
            transformations=tx,
            advisories=advisories,
            error=ref.envelope,
        )

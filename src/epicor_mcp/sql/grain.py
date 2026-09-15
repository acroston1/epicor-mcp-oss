'Grain / fan-out detection — the wrong answers that are NOT zero (grain-analysis policy).'

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import sqlglot
from sqlglot import exp

__all__ = [
    "DIALECT",
    "GrainFinding",
    "GrainReport",
    "Severity",
    "TABLE_KEYS",
    "analyse_grain",
    "apply_verification",
    "candidate_keys",
    "duplicate_collapse",
    "verification_plan",
]

DIALECT = "tsql"

#: Package data: ``{table: [[key column, ...], ...]}``. See the module docstring.
KEYS_PATH = Path(__file__).with_name("table_keys.json")

#: On the key of every Erp table and constant across a single-company install, so
#: it identifies nothing on its own. It still counts toward key COVERAGE (a join
#: really does have to carry it); it is excluded only from the "did the caller
#: name a specific parent" tests, where treating it as identifying would make
#: every statement look pinned.
_UNINFORMATIVE = frozenset({"company"})


class Severity:
    """Same two levels ``sql/lint.py`` uses, for the same reason."""

    REFUSE = "refuse"
    WARN = "warn"


def _load_keys(
    path: Path = KEYS_PATH,
) -> tuple[dict[str, tuple[frozenset[str], ...]], dict[str, str]]:
    """``({table_lower: (frozenset(cols lower), ...)}, {col_lower: OriginalCase})``.

    Comparison is case-insensitive because callers write ``PONum``, ``PONUM`` and
    ``ponum`` for the same Epicor column; dictionary casing can also differ
    between tables. The casing map exists purely so
    the SQL this module *emits* is written the way a human would write it —
    Epicor's parser accepts the all-lowercase form, so this is legibility,
    not correctness.

    A missing or corrupt file degrades the module to "every table is
    unjudgeable" — no findings, no crash. It must never break module import.
    """
    try:
        raw = json.loads(path.read_text()).get("tables", {})
    except Exception:  # noqa: BLE001 - a metadata file must not take the server down
        return {}, {}
    out: dict[str, tuple[frozenset[str], ...]] = {}
    casing: dict[str, str] = {}
    for table, keys in raw.items():
        sets = []
        for key in keys or []:
            if not key:
                continue
            sets.append(frozenset(c.lower() for c in key))
            for col in key:
                casing.setdefault(col.lower(), col)
        if sets:
            out[str(table).lower()] = tuple(sets)
    return out, casing


TABLE_KEYS, _KEY_CASING = _load_keys()


def configure_keys(path: Path) -> None:
    """Merge explicitly supplied key metadata into the small core examples."""
    global TABLE_KEYS, _KEY_CASING
    defaults, casing = _load_keys()
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        tables = raw.get("tables") if isinstance(raw, dict) else None
        if not isinstance(tables, dict):
            raise ValueError("Table key file must contain a tables object")
        for name, candidates in tables.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Table key file names must be unqualified identifiers")
            if not isinstance(candidates, list) or any(not isinstance(key, list) or not key or any(not isinstance(c, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c) for c in key) for key in candidates):
                raise ValueError("Table key file values must be lists of nonempty column lists")
        custom, custom_casing = _load_keys(path)
        defaults.update(custom)
        casing.update(custom_casing)
    TABLE_KEYS, _KEY_CASING = defaults, casing



def candidate_keys(table: str) -> tuple[frozenset[str], ...]:
    """Candidate keys of *table* (schema prefix optional), or ``()`` if unknown."""
    name = str(table or "").rsplit(".", 1)[-1].strip("[]").lower()
    return TABLE_KEYS.get(name, ())


def _cased(col: str) -> str:
    return _KEY_CASING.get(col.lower(), col)


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GrainFinding:
    rule: str
    severity: str
    message: str
    evidence: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "evidence": self.evidence,
        }
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass(frozen=True)
class GrainReport:
    findings: tuple[GrainFinding, ...] = ()
    #: Bounded statements that would MEASURE the suspicion instead of guessing.
    verifications: tuple[dict[str, Any], ...] = ()
    #: Sources in scope whose keys are not knowable — the measurable residual.
    unjudgeable: tuple[str, ...] = ()

    @property
    def rules(self) -> list[str]:
        return [f.rule for f in self.findings]

    def to_dicts(self) -> list[dict[str, Any]]:
        return [f.to_dict() for f in self.findings]


# --------------------------------------------------------------------------- #
# AST helpers — deliberately local (independent-checker rule: no cross-module private reuse)
# --------------------------------------------------------------------------- #


def _from_arg(select: exp.Select) -> Any:
    # sqlglot renamed the key from "from" to "from_" in v26+. Read both, or every
    # join looks like a single-source select.
    return select.args.get("from") or select.args.get("from_")


def _table_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Table):
        parts = [p for p in (node.text("catalog"), node.text("db"), node.name) if p]
        return ".".join(parts) if parts else None
    return None


@dataclass
class _Scope:
    """Everything about one SELECT that the cardinality test needs."""

    select: exp.Select
    #: alias -> table name, for sources whose keys can be looked up
    tables: dict[str, str] = field(default_factory=dict)
    #: alias -> candidate keys, including keys DERIVED from a grouped subquery
    keys: dict[str, tuple[frozenset[str], ...]] = field(default_factory=dict)
    #: aliases whose grain is not knowable at all
    opaque: list[str] = field(default_factory=list)
    #: the FROM-side alias, which every join hangs off
    driver: str | None = None
    #: (alias, col) == (alias, col) pairs from ON clauses and the WHERE
    edges: list[tuple[tuple[str, str], tuple[str, str]]] = field(default_factory=list)
    #: Lower-cased alias/column -> the caller's own spelling. Matching is
    #: case-insensitive; everything this module EMITS is echoed back in the
    #: caller's casing, so a suggested fix can be pasted straight in.
    casing: dict[str, str] = field(default_factory=dict)
    #: (alias, col) pinned to a constant
    constants: set[tuple[str, str]] = field(default_factory=set)




    flags: set[tuple[str, str]] = field(default_factory=set)


def _derived_keys(sub: exp.Subquery) -> tuple[frozenset[str], ...]:
    """A grouped derived table's output IS unique on its GROUP BY list.

    ``(select ... from ... group by [JH].[Company], [JH].[JobNum]) as [JC]``
    yields at most one row per ``(Company, JobNum)``, so a join that binds those
    two columns cannot fan out. Without this, a statement that
    pre-aggregates costs in exactly this shape is unjudgeable, and the whole
    point of writing it that way is lost on the detector.

    The names are the derived table's OUTPUT names (the alias when there is one),
    because that is what the outer query can reference.
    """
    inner = sub.this
    if not isinstance(inner, exp.Select):
        return ()
    group = inner.args.get("group")
    if group is None or not group.expressions:
        return ()
    grouped = {g.sql(dialect=DIALECT).lower() for g in group.expressions}
    out: set[str] = set()
    for item in inner.expressions:
        body = item.this if isinstance(item, exp.Alias) else item
        if body.sql(dialect=DIALECT).lower() in grouped:
            out.add((item.alias if isinstance(item, exp.Alias) else body.name).lower())
    # Every group key must survive into the projection, or the output is not
    # unique on the names the outer query can see.
    if len(out) != len(grouped):
        return ()
    return (frozenset(out),) if out else ()


def _cte_keys(select: exp.Select) -> dict[str, tuple[frozenset[str], ...]]:
    """``{cte name: keys}`` for every CTE visible from *select*.

    Two jobs, and the first is a correctness guard, not a feature. A CTE is
    referenced by a **bare name** — ``from YearlySales as [y]`` — which parses to
    the same ``exp.Table`` shape as ``from Erp.OrderDtl``. Without this map a CTE
    that happens to be called ``OrderDtl`` would be judged with the real
    ``Erp.OrderDtl``'s primary key, which is a silent-wrong of exactly the class
    this module exists to catch. A CTE name therefore always shadows the
    dictionary, and gets a key only if its own GROUP BY earns one.
    """
    out: dict[str, tuple[frozenset[str], ...]] = {}
    node: exp.Expression | None = select
    while node is not None:
        # Read BOTH keys: sqlglot >= 26 renamed `with` to `with_`, the same
        # rename used for `from`/`from_`. Reading only
        # `with` finds zero CTEs, and the shadowing guard then silently never
        # runs — a control that reports success while doing nothing.
        with_ = node.args.get("with") or node.args.get("with_")
        for cte in getattr(with_, "expressions", None) or []:
            name = (cte.alias_or_name or "").lower()
            if name:
                out[name] = _derived_keys(cte)
        node = node.parent
    return out


def _build_scope(select: exp.Select) -> _Scope:
    scope = _Scope(select=select)
    ctes = _cte_keys(select)

    def add(node: exp.Expression, is_driver: bool = False) -> None:
        target = node.this if isinstance(node, exp.Alias) else node
        if not isinstance(target, (exp.Table, exp.Subquery)):
            return
        raw_alias = target.alias_or_name or ""
        alias = raw_alias.lower()
        if not alias:
            return
        scope.casing.setdefault(alias, raw_alias)
        if is_driver:
            scope.driver = alias
        if isinstance(target, exp.Table):
            name = _table_name(target)
            # A CTE name ALWAYS shadows the dictionary — see `_cte_keys`.
            keys = ctes.get((name or "").lower(), candidate_keys(name or ""))
            if name and keys:
                scope.tables[alias] = name
                scope.keys[alias] = keys
            else:
                scope.opaque.append(name or alias)
            return
        derived = _derived_keys(target)
        if derived:
            scope.tables[alias] = f"({alias})"
            scope.keys[alias] = derived
        else:
            scope.opaque.append(alias)

    frm = _from_arg(select)
    if frm is not None:
        expressions = list(getattr(frm, "expressions", None) or [frm.this])
        for i, src in enumerate(expressions):
            add(src, is_driver=(i == 0))
    for join in select.args.get("joins") or []:
        add(join.this)

    for col in select.find_all(exp.Column):
        if col.name:
            scope.casing.setdefault(col.name.lower(), col.name)

    predicates: list[exp.Expression] = []
    for join in select.args.get("joins") or []:
        predicates.extend(_conjuncts(join.args.get("on")))
    where = select.args.get("where")
    predicates.extend(_conjuncts(where.this if where is not None else None))
    for pred in predicates:
        if not isinstance(pred, exp.EQ):
            continue
        left, right = pred.this, pred.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            la, ra = (left.table or "").lower(), (right.table or "").lower()
            if la and ra and la != ra:
                scope.edges.append(((la, left.name.lower()), (ra, right.name.lower())))
        elif isinstance(left, exp.Column) and _is_constant(right) and left.table:
            ref = (left.table.lower(), left.name.lower())
            scope.constants.add(ref)
            if _is_flag(right):
                scope.flags.add(ref)
        elif isinstance(right, exp.Column) and _is_constant(left) and right.table:
            ref = (right.table.lower(), right.name.lower())
            scope.constants.add(ref)
            if _is_flag(left):
                scope.flags.add(ref)
    return scope


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    """Top-level AND-ed predicates. **Stops at OR** on purpose.

    Under an ``OR`` nothing is guaranteed to hold, so an equality inside one
    cannot be used to argue a join binds a key — using it would make a fanning
    join look safe, which is the one direction this module must never fail in.
    """
    out: list[exp.Expression] = []
    stack = [node] if node is not None else []
    while stack:
        cur = stack.pop()
        if cur is None:
            continue
        if isinstance(cur, exp.Paren):
            stack.append(cur.this)
        elif isinstance(cur, exp.And):
            stack.extend([cur.this, cur.expression])
        elif isinstance(cur, exp.Connector) and not isinstance(cur, exp.And):
            continue  # OR / XOR: nothing under it is guaranteed
        else:
            out.append(cur)
    return out


def _is_constant(node: exp.Expression) -> bool:
    return isinstance(node, (exp.Literal, exp.Boolean, exp.Null)) or (
        isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal)
    )


def _is_flag(node: exp.Expression) -> bool:
    """``true``/``false``/unquoted ``0``/``1`` — a population filter, not an id.

    The quoting matters and is not pedantry: ``[JH].[Plant] = '10'`` is a real
    entity selector whose value happens to be numeric-looking, while
    ``[PR].[Approved] = 1`` is a flag. sqlglot keeps ``is_string``, so the two
    are distinguishable without guessing from the column name.
    """
    if isinstance(node, exp.Boolean):
        return True
    return (
        isinstance(node, exp.Literal)
        and not node.args.get("is_string")
        and str(node.this) in {"0", "1"}
    )


# --------------------------------------------------------------------------- #
# The cardinality test — directed equality propagation
# --------------------------------------------------------------------------- #


def _closure(scope: _Scope, fixed: Iterable[str]) -> tuple[set[tuple[str, str]], set[str]]:
    """``(columns known, aliases whose ROW is determined)`` once *fixed* is fixed.

    Two interleaved steps, run to a fixpoint, and **both are required**:

    1. *equality propagates a column* — a known column makes anything equated to
       it known;
    2. **a table whose whole candidate key is known is DETERMINED, and then all
       of its columns are known.**

    Step 2 is not an optimisation. Without it, ``sum([OD].[OrderQty])`` over
    ``OrderHed ⋈ OrderDtl ⋈ Customer`` reports ``Customer`` as a multiplier: an
    ``OrderDtl`` row determines ``OrderHed``'s key, but ``Customer`` is joined on
    ``[OH].[CustNum]``, which is a column of ``OrderHed`` that no equality
    mentions. Only "OrderHed's row is now fixed, therefore ``OH.CustNum`` is
    fixed" closes the chain and avoids falsely flagging child-to-header-to-master
    joins as aggregate multipliers.
    """
    determined = {a.lower() for a in fixed}
    known: set[tuple[str, str]] = set(scope.constants)

    def is_known(ref: tuple[str, str]) -> bool:
        return ref[0] in determined or ref in known

    changed = True
    while changed:
        changed = False
        for a, b in scope.edges:
            for src, dst in ((a, b), (b, a)):
                if is_known(src) and not is_known(dst):
                    known.add(dst)
                    changed = True
        for alias, keys in scope.keys.items():
            if alias in determined:
                continue
            if any(all(is_known((alias, col)) for col in key) for key in keys):
                determined.add(alias)
                changed = True
    return known, determined


def _fanning(scope: _Scope, fixed: Iterable[str]) -> list[str]:
    """Aliases that multiply the rows of *fixed*, in statement order.

    A table with no known keys is **unjudgeable** and never appears here — the
    module never asserts a fan-out it cannot argue for. Neither does a table
    whose only unbound key column is ``Company``: see :func:`_split_fanning`.
    """
    return _split_fanning(scope, fixed)[0]


def _split_fanning(scope: _Scope, fixed: Iterable[str]) -> tuple[list[str], list[str]]:
    """``([genuinely fans out], [only Company is unbound])``.

    A join that omits the ``Company`` predicate — ``on jh.PersonID = p.PersonID``
    — leaves ``Company`` free on every Erp key, so the pure cardinality test
    calls it a fan-out. In a **single-company install that is not what happens**:
    the extra rows do not exist, the answer is right, and warning about a
    multiplier is a false alarm. It is also not nothing — in a multi-company
    install it is a cross-company cartesian, which is why the shape gets its own
    accurate finding (``join_missing_company``) instead of being ignored.

    """
    fixed_set = {a.lower() for a in fixed}
    known, determined = _closure(scope, fixed_set)
    fans: list[str] = []
    company_only: list[str] = []
    for alias in scope.keys:
        if alias in determined:
            continue
        gaps = [
            {c for c in key if not (alias, c) in known}
            for key in scope.keys.get(alias, ())
        ]
        best = min(gaps, key=len) if gaps else set()
        (company_only if best and best <= _UNINFORMATIVE else fans).append(alias)
    return fans, company_only


def _free_key_columns(scope: _Scope, alias: str, fixed: Iterable[str]) -> list[str]:
    """The key columns still free — the REASON the join can multiply."""
    known, determined = _closure(scope, fixed)
    gaps = [
        sorted(_cased(c) for c in key if not (alias in determined or (alias, c) in known))
        for key in scope.keys.get(alias, ())
    ]
    return min(gaps, key=len) if gaps else []


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #

_IMMUNE_AGGS = (exp.Min, exp.Max)


def _agg_kind(agg: exp.AggFunc) -> str:
    """``'immune' | 'count_star' | 'affected'``.

    ``min``/``max`` are **fan-out immune** — duplicating a row cannot move an
    extreme. So is any aggregate over a DISTINCT argument. ``sum``/``avg``/
    ``count`` over a duplicated row are all wrong, ``avg`` included: the weights
    move even when the values do not.

    ``count_star`` is reported separately but **treated like ``immune``**, and
    that is a measured decision, not an oversight. ``count(*)`` over a join has
    no attributable measure table — "how many joined rows" is frequently exactly
    what was asked. Without an attributable measure table, a fan-out claim
    would conflate the number of joined rows with a parent-level count.
    """
    if isinstance(agg, _IMMUNE_AGGS):
        return "immune"
    if isinstance(agg.this, exp.Distinct) or agg.args.get("distinct"):
        return "immune"
    if isinstance(agg, exp.Count) and isinstance(agg.this, (exp.Star, type(None))):
        return "count_star"
    return "affected"


def _enclosing_select(node: exp.Expression) -> exp.Select | None:
    cur = node.parent
    while cur is not None:
        if isinstance(cur, exp.Select):
            return cur
        cur = cur.parent
    return None


def _own_aggregates(select: exp.Select) -> list[exp.AggFunc]:
    """Aggregates belonging to THIS select — not to a nested one."""
    candidates: list[exp.Expression] = list(select.expressions)
    having = select.args.get("having")
    if having is not None:
        candidates.append(having.this if having.this is not None else having)
    order = select.args.get("order")
    if order is not None:
        candidates.extend(order.expressions or [])
    out: list[exp.AggFunc] = []
    for item in candidates:
        body = item.this if isinstance(item, exp.Alias) else item
        found = [body] if isinstance(body, exp.AggFunc) else list(body.find_all(exp.AggFunc))
        for agg in found:
            if _enclosing_select(agg) is select and agg not in out:
                out.append(agg)
    return out


def _column_aliases(node: exp.Expression) -> set[str]:
    return {c.table.lower() for c in node.find_all(exp.Column) if c.table}


def _projection_aliases(select: exp.Select) -> set[str]:
    out: set[str] = set()
    for item in select.expressions:
        body = item.this if isinstance(item, exp.Alias) else item
        out |= _column_aliases(body)
    return out


def _projected_columns(select: exp.Select) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for item in select.expressions:
        body = item.this if isinstance(item, exp.Alias) else item
        cols = [body] if isinstance(body, exp.Column) else list(body.find_all(exp.Column))
        for col in cols:
            if isinstance(col, exp.Column) and col.table:
                out.add((col.table.lower(), col.name.lower()))
    return out


def _has_distinct(select: exp.Select) -> bool:
    return bool(select.args.get("distinct"))


# --------------------------------------------------------------------------- #
# Evidence strings — every finding cites a measured number
# --------------------------------------------------------------------------- #

_EV_AGG = (
    'Engine compatibility behavior; validate against the configured Epicor version and local data.'
)
_EV_SIBLING = "Joining independent child collections multiplies their rows; aggregate each child at the parent grain before joining."
_EV_DUP = (
    'Engine compatibility behavior; validate against the configured Epicor version and local data.'
)
_EV_SCOPE = (
    'Engine compatibility behavior; validate against the configured Epicor version and local data.'
)


def _spell(scope: "_Scope", name: str) -> str:
    """The caller's own spelling of *name*, else the dictionary's, else as given."""
    return scope.casing.get(name.lower()) or _cased(name)


def _qualified(scope: "_Scope", alias: str, cols: Sequence[str]) -> str:
    a = _spell(scope, alias)
    return ", ".join(f"[{a}].[{_spell(scope, c)}]" for c in cols)


def _named(scope: _Scope, aliases: Iterable[str]) -> str:
    return ", ".join(scope.tables.get(a, a) for a in aliases)


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #


def _rule_aggregate_fanout(
    scope: _Scope,
) -> tuple[list[GrainFinding], list[dict[str, Any]]]:
    findings: list[GrainFinding] = []
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for agg in _own_aggregates(scope.select):
        if _agg_kind(agg) != "affected":
            continue
        measure = _column_aliases(agg) & set(scope.keys)
        if not measure:
            continue
        if _column_aliases(agg) - set(scope.keys):
            continue  # the measure reads an unjudgeable source; stand down
        fanning = _fanning(scope, measure)
        if not fanning:
            continue
        text = agg.sql(dialect=DIALECT)
        if text in seen:
            continue
        seen.add(text)
        multipliers = [
            {
                "table": scope.tables.get(a, a),
                "alias": a,
                "key_columns_left_free": _free_key_columns(scope, a, measure),
            }
            for a in fanning
        ]
        free = "; ".join(
            f"{m['table']}.{'+'.join(m['key_columns_left_free']) or '(nothing bound)'}"
            for m in multipliers
        )
        measured = _named(scope, sorted(measure))
        names = _named(scope, fanning)
        findings.append(
            GrainFinding(
                rule="aggregate_fanout",
                severity=Severity.WARN,
                message=(
                    f"GRAIN WARNING: `{text}` measures {measured}, but fixing one {measured} row "
                    f"does not fix {names} — these key columns are still free: {free}. So every "
                    f"{measured} row is REPEATED once per matching {names} row and the total "
                    "comes back several times too large. Epicor reports nothing; the answer is "
                    "HTTP 200 and wrong. Either aggregate "
                    f"{names} in a derived table and join THAT, or de-duplicate first: "
                    "`select sum([d].[Measure]) from (select distinct <key columns>, <measure> "
                    "from ...same joins...) as [d]`."
                ),
                evidence=_EV_AGG,
                detail={
                    "aggregate": text,
                    "measure_tables": sorted(scope.tables.get(a, a) for a in measure),
                    "multiplies_by": multipliers,
                },
            )
        )
        plan = _dedup_plan(scope, agg, measure)
        if plan:
            plans.append(plan)
    return findings, plans


def _rule_sibling_child_cross(scope: _Scope) -> list[GrainFinding]:
    """Two children of one parent, joined to the parent and not to each other.

    Every output row is one row of child A paired with one row of child B, and
    those pairings are **invented by the join** — no such combination exists in
    the data: m rows of A and n rows of B under one parent become m x n invented
    rows. This is wrong whether
    or not anything is aggregated, which is why it does not look at aggregates.
    """
    driver = scope.driver
    if not driver or driver not in scope.keys:
        return []
    fanning = _fanning(scope, {driver})
    if len(fanning) < 2:
        return []
    crossed: list[tuple[str, str]] = []
    for i, a in enumerate(fanning):
        for b in fanning[i + 1 :]:
            # Fixing the driver AND one sibling must still leave the other free,
            # in BOTH directions. A chain (parent -> child -> grandchild) fails
            # this test and is correctly left alone: it yields one row per
            # grandchild, not an invented cross product.
            if b in _fanning(scope, {driver, a}) and a in _fanning(scope, {driver, b}):
                crossed.append((a, b))
    if not crossed:
        return []
    pairs = "; ".join(
        f"{scope.tables.get(a, a)} x {scope.tables.get(b, b)}" for a, b in crossed
    )
    return [
        GrainFinding(
            rule="sibling_child_cross",
            severity=Severity.WARN,
            message=(
                f"GRAIN WARNING: {scope.tables.get(driver, driver)} is joined to two independent "
                f"children ({pairs}) that are not joined to EACH OTHER, so the result is their "
                "CROSS PRODUCT: every row pairs one with the other and no such pairing exists in "
                "the data. Counts and sums over it are the product of the two child counts. Query "
                "one child at a time, or pre-aggregate each child in its own derived table and "
                "join those."
            ),
            evidence=_EV_SIBLING,
            detail={
                "parent": scope.tables.get(driver, driver),
                "crossed": [
                    [scope.tables.get(a, a), scope.tables.get(b, b)] for a, b in crossed
                ],
            },
        )
    ]


def _rule_duplicate_projection(scope: _Scope) -> list[GrainFinding]:
    """A join used as a FILTER, projected without DISTINCT — duplicated rows.

    The projection reads only tables P; another table fans P out; nothing from
    that table reaches the output. Every output row is therefore emitted once per
    matching hidden row, and the caller counts the duplicates as data.
    """
    if _has_distinct(scope.select) or _own_aggregates(scope.select):
        return []
    projected = _projection_aliases(scope.select) & set(scope.keys)
    if not projected:
        return []
    invisible = [a for a in _fanning(scope, projected) if a not in projected]
    if not invisible:
        return []
    return [
        GrainFinding(
            rule="duplicate_projection",
            severity=Severity.WARN,
            message=(
                f"GRAIN WARNING: nothing from {_named(scope, invisible)} appears in your "
                f"projection, but joining it multiplies the {_named(scope, sorted(projected))} "
                "rows — so the SAME row comes back once per match and the row count is not a "
                "count of anything. If the join is only there to filter, add `distinct` (or move "
                "it into a `where ... in (select ...)`); if you meant the detail, project a "
                f"column from {_named(scope, invisible)} so the rows are distinguishable."
            ),
            evidence=_EV_DUP,
            detail={
                "projected_from": sorted(scope.tables.get(a, a) for a in projected),
                "hidden_multiplier": [scope.tables.get(a, a) for a in invisible],
            },
        )
    ]


def _rule_unlabelled_parent_scope(
    scope: _Scope,
) -> tuple[list[GrainFinding], list[dict[str, Any]]]:
    'Children of MANY parents, where the parent was chosen by\n    an ATTRIBUTE rather than by identity.'
    driver = scope.driver
    if not driver or driver not in scope.keys or _own_aggregates(scope.select):
        return [], []
    keys = scope.keys[driver]
    identifying = min(keys, key=len) - _UNINFORMATIVE
    if not identifying:
        return [], []
    pinned = {c for (a, c) in scope.constants if a == driver}
    if pinned & identifying:
        return [], []  # a key column is pinned: one parent, or one coherent sub-tree
    flagged = {c for (a, c) in scope.flags if a == driver}
    selector = sorted(_spell(scope, c) for c in pinned - _UNINFORMATIVE - flagged)
    if not selector:
        # No selector at all, or only flags. `[PR].[Approved] = 1` narrows a
        # population and says nothing about wanting ONE parent; treating it as an
        # entity selector fires on every `select distinct <child> ... where
        # <parent>.<flag> = 1`, which is the correct shape for that question.
        return [], []
    fanning = _fanning(scope, {driver})
    if not fanning:
        return [], []
    cols = sorted(_spell(scope, c) for c in identifying)
    parent = scope.tables.get(driver, driver)
    labelled = _parent_is_labelled(scope, driver, identifying)
    advice = (
        f"The rows do carry {_qualified(scope, driver, cols)}, so count the DISTINCT values you got "
        f"before reading this as one {parent}'s worth of data"
        if labelled
        else f"And no column in your projection says which {parent} each row came from, so the "
        f"mixture is invisible — add {_qualified(scope, driver, cols)} to see the spread"
    )
    finding = GrainFinding(
        rule="unlabelled_parent_scope",
        severity=Severity.WARN,
        message=(
            f"GRAIN WARNING: this is not one {parent}'s worth of rows. You selected {parent} by "
            f"an attribute ({', '.join(selector)}), not by identity — {_qualified(scope, driver, cols)} "
            f"is not fixed — so the result is every matching {_named(scope, fanning)} row of "
            f"EVERY matching {parent}, concatenated. {advice}. To get one "
            f"{parent}'s worth, pin {_qualified(scope, driver, cols)} in the WHERE clause."
        ),
        evidence=_EV_SCOPE,
        detail={
            "parent_table": parent,
            "selected_by": selector,
            "parent_key_not_pinned": cols,
            "parent_identified_in_output": labelled,
            "children": [scope.tables.get(a, a) for a in fanning],
        },
    )
    return [finding], [p for p in [_parent_count_plan(scope, driver, cols)] if p]


def _parent_is_labelled(scope: _Scope, driver: str, identifying: frozenset[str]) -> bool:
    """Does the projection let the caller tell the parents apart?

    A column equality-bound to a parent key column counts: ``[OD].[OrderNum]``
    names the ``OrderHed`` row just as well as ``[OH].[OrderNum]`` does.
    """
    reachable = _projected_columns(scope.select)
    changed = True
    while changed:
        changed = False
        for a, b in scope.edges:
            for src, dst in ((a, b), (b, a)):
                if src in reachable and dst not in reachable:
                    reachable.add(dst)
                    changed = True
    return all((driver, col) in reachable for col in identifying)


def _rule_join_missing_company(scope: _Scope) -> list[GrainFinding]:
    """A join predicate that omits ``Company`` — accurate, and not a fan-out.

    Split out of the cardinality rules because omitting Company need not
    multiply rows in a single-company installation. It still warrants a warning
    because a multi-company installation can match another company's records.
    """
    if not scope.driver:
        return []
    gaps = sorted(_split_fanning(scope, {scope.driver})[1])
    if not gaps:
        return []
    return [
        GrainFinding(
            rule="join_missing_company",
            severity=Severity.WARN,
            message=(
                f"The join to {_named(scope, gaps)} lacks a Company predicate. "
                "This can join records belonging to different companies. Include Company and the business key."
            ),
            evidence=(
                "Join cardinality: Company is on "
                "the key of every Erp table, so omitting it from a join predicate is a "
                "cross-company product with no error channel"
            ),
            detail={"tables": [scope.tables.get(a, a) for a in gaps]},
        )
    ]


# --------------------------------------------------------------------------- #
# Cheap verification — measure it instead of guessing
# --------------------------------------------------------------------------- #


def _from_and_joins_sql(select: exp.Select) -> str:
    frm = _from_arg(select)
    parts = [frm.sql(dialect=DIALECT)] if frm is not None else []
    parts += [j.sql(dialect=DIALECT) for j in select.args.get("joins") or []]
    where = select.args.get("where")
    if where is not None:
        parts.append(where.sql(dialect=DIALECT))
    return "\n".join(p for p in parts if p)


def _dedup_plan(
    scope: _Scope, agg: exp.AggFunc, measure: Iterable[str]
) -> dict[str, Any] | None:
    'A bounded statement that returns the SAME aggregate at the right grain.'
    measured = agg.this
    if isinstance(measured, (exp.Star, type(None))):
        return None
    inner_terms: list[str] = []
    for alias in sorted(measure):
        keys = scope.keys.get(alias)
        if not keys:
            return None
        inner_terms.append(_qualified(scope, alias, sorted(min(keys, key=len))))
    inner_terms.append(f"{measured.sql(dialect=DIALECT)} as [Measure]")

    group = scope.select.args.get("group")
    group_terms = [g.sql(dialect=DIALECT) for g in (group.expressions if group else [])]
    outer_select: list[str] = []
    outer_group = ""
    if group_terms:
        labels = [f"[G{i}]" for i in range(len(group_terms))]
        inner_terms = [f"{g} as {lab}" for g, lab in zip(group_terms, labels)] + inner_terms
        outer_select = [f"[d].{lab} as {lab}" for lab in labels]
        outer_group = "\ngroup by " + ", ".join(f"[d].{lab}" for lab in labels)
    fn = type(agg).__name__.lower()
    outer_select.append(f"{fn}([d].[Measure]) as [CorrectedValue]")
    stmt = (
        "select top 100 "
        + ", ".join(outer_select)
        + "\nfrom (\nselect distinct "
        + ", ".join(inner_terms)
        + "\n"
        + _from_and_joins_sql(scope.select)
        + "\n) as [d]"
        + outer_group
    )
    return {
        "kind": "dedup_aggregate",
        "for_rule": "aggregate_fanout",
        "aggregate": agg.sql(dialect=DIALECT),
        "why": (
            "Runs the same aggregate over a de-duplicated derived table. If the two numbers "
            "differ, the join was multiplying rows and THIS is the right answer."
        ),
        "sql": stmt,
    }


def _parent_count_plan(
    scope: _Scope, driver: str, key_cols: Sequence[str]
) -> dict[str, Any] | None:
    inner = (
        "select distinct "
        + _qualified(scope, driver, key_cols)
        + "\n"
        + _from_and_joins_sql(scope.select)
    )
    return {
        "kind": "parent_cardinality",
        "for_rule": "unlabelled_parent_scope",
        "why": (
            "Counts the distinct parent rows this listing actually spans. `1` means it is one "
            "entity's worth of rows; anything larger means it is several concatenated and the "
            "caller was not told."
        ),
        "sql": "select count(*) as [DistinctParents]\nfrom (\n" + inner + "\n) as [d]",
    }


def verification_plan(report: GrainReport) -> list[dict[str, Any]]:
    """The bounded statements that would turn a suspicion into a measurement."""
    return list(report.verifications)


def apply_verification(
    finding: GrainFinding,
    *,
    reported_value: float | None,
    corrected_value: float | None,
    tolerance: float = 0.005,
) -> GrainFinding:
    """Fold a MEASURED verification back into the finding.

    The only place a grain finding is allowed to stop hedging. With both numbers
    in hand the message states the multiple and the corrected value instead of
    describing a risk — and when the two agree it **withdraws**, which is what
    stops a warning that has been disproved from riding along as noise. A warning
    nobody can clear is a warning everybody learns to ignore.
    """
    if reported_value is None or corrected_value is None:
        return finding
    detail = dict(finding.detail)
    detail.update({"reported_value": reported_value, "corrected_value": corrected_value})
    scale = max(abs(reported_value), abs(corrected_value), 1e-9)
    if abs(reported_value - corrected_value) <= tolerance * scale:
        detail["verified"] = "no_fanout"
        return GrainFinding(
            rule=finding.rule,
            severity=Severity.WARN,
            message=(
                "GRAIN CHECKED — no fan-out: the same aggregate over a de-duplicated derived "
                f"table returns the same value ({corrected_value:g}), so this join did not "
                "multiply the measure. The number is at the grain you asked for."
            ),
            evidence=finding.evidence,
            detail=detail,
        )
    multiple = reported_value / corrected_value if corrected_value else float("inf")
    detail["verified"] = "fanout_measured"
    detail["multiple"] = multiple
    return GrainFinding(
        rule=finding.rule,
        severity=Severity.REFUSE,
        message=(
            f"WRONG GRAIN, MEASURED: this statement returns {reported_value:g}; the same "
            f"aggregate over a de-duplicated derived table returns {corrected_value:g} — "
            f"{multiple:.4g}x. The join repeats each measured row, so the number you were about "
            "to show someone is that many times too large. Use the de-duplicated form."
        ),
        evidence=finding.evidence,
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# Post-execution: the free half
# --------------------------------------------------------------------------- #


def duplicate_collapse(
    rows: Sequence[Mapping[str, Any]], *, min_rows: int = 10, ratio: float = 0.5
) -> GrainFinding | None:
    'Rows already in hand that collapse under DISTINCT — costs nothing.'
    if len(rows) < min_rows:
        return None
    seen = {tuple(sorted((str(k), str(v)) for k, v in row.items())) for row in rows}
    distinct = len(seen)
    if distinct >= max(1, int(len(rows) * ratio)):
        return None
    return GrainFinding(
        rule="duplicate_rows_returned",
        severity=Severity.WARN,
        message=(
            f"GRAIN WARNING: {len(rows)} rows came back but only {distinct} of them are "
            "distinct — a join is repeating rows. Any total added up from this page is about "
            f"{len(rows) / distinct:.3g}x too large, and read as a list it is {distinct} items, "
            f"not {len(rows)}. Project the column that distinguishes the duplicates, or "
            "de-duplicate with `select distinct`."
        ),
        evidence=(
            'Engine compatibility behavior; validate against the configured Epicor version and local data.'
        ),
        detail={"rows": len(rows), "distinct_rows": distinct},
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def _analyse_select(select: exp.Select) -> tuple[list[GrainFinding], list[dict[str, Any]], list[str]]:
    if not (select.args.get("joins") or []):
        return [], [], []
    scope = _build_scope(select)
    if not scope.keys:
        return [], [], scope.opaque
    findings: list[GrainFinding] = []
    plans: list[dict[str, Any]] = []
    f, p = _rule_aggregate_fanout(scope)
    findings += f
    plans += p
    findings += _rule_sibling_child_cross(scope)
    findings += _rule_duplicate_projection(scope)
    f, p = _rule_unlabelled_parent_scope(scope)
    findings += f
    plans += p
    findings += _rule_join_missing_company(scope)
    return findings, plans, scope.opaque


def analyse_grain(
    sql: str, *, rows: Sequence[Mapping[str, Any]] | None = None
) -> GrainReport:
    """Grain findings for *sql*, plus the bounded statements that would prove them.

    Never raises. A parse failure, an unknown table, a derived-table source or a
    corrupt key file all produce FEWER findings — never an exception, never a
    refusal. The pipeline's safety controls live elsewhere (cost-governor policy, denylist policy);
    this is a correctness control and it fails open, exactly like ``sql/lint.py``.
    """
    findings: list[GrainFinding] = []
    plans: list[dict[str, Any]] = []
    unjudgeable: list[str] = []
    try:
        root = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:  # noqa: BLE001 - sqlglot raises several types
        root = None
    if root is not None:
        try:
            for select in root.find_all(exp.Select):
                f, p, u = _analyse_select(select)
                findings += f
                plans += p
                unjudgeable += u
        except Exception:  # noqa: BLE001 - a detector bug must never 500 the tool
            findings, plans = [], []
    if rows is not None:
        dup = duplicate_collapse(rows)
        if dup is not None:
            findings.append(dup)
    return GrainReport(
        findings=tuple(findings),
        verifications=tuple(plans),
        unjudgeable=tuple(sorted(set(unjudgeable))),
    )

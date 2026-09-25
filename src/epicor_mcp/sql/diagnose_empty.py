'Feature **E15b** — turn a silent zero into a self-correcting answer.'

from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from epicor_mcp.sql import denylist
from epicor_mcp.sql.governor import BIG_TABLES

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DOMAIN_TTL_S",
    "DEFAULT_PROBE_BUDGET",
    "DOMAIN_LIMIT",
    "DomainCache",
    "ProbeResult",
    "diagnose_empty",
    "unverified_text_match",
]

DIALECT = "tsql"

#: Hard cap on EXTRA Epicor calls per diagnosis. Shared by every probe kind.
DEFAULT_PROBE_BUDGET = 5

#: How many distinct values count as "an enumerable domain". 26 are fetched so
#: that returning 26 proves the column has MORE than 25 and the list is a sample,
#: not a domain — the difference between "your value is not in this list" and
#: "here are some values", which is the difference between a diagnosis and a
#: false alarm.
DOMAIN_LIMIT = 25

#: Confidence floor for a fuzzy correction. Same value the legacy ``_correct_column``
#: uses, for the same reason: below it, a suggestion is a guess.
_FUZZY_CUTOFF = 0.82


# --------------------------------------------------------------------------- #
# Curated knowledge — small domains checked by bounded runtime queries
# --------------------------------------------------------------------------- #

#: Columns whose authoritative domain lives on a DIFFERENT, tiny table. Without
#: this, ``JobHead.Plant`` would be enumerated off a BIG table and skipped.
_DOMAIN_AUTHORITY: dict[str, tuple[str, str, str]] = {
    # column (lowercased) -> (schema.table, code column, label column)
    "plant": ("Erp.Plant", "Plant", "Name"),
    "siteid": ("Erp.Plant", "Plant", "Name"),
}




# The configured REST company is request context, not proof that a SQL table
# contains only that company. Enumerate/probe rather than invent a singleton.
_INSTALL_CONSTANT_COLUMNS: frozenset[str] = frozenset()







_SIBLING_HINTS: dict[tuple[str, str], dict[str, str]] = {
    ("erp.apinvhed", "invoicenum"): {
        "table": "Erp.InvcHead",
        "text": (
            "Erp.APInvHed holds SUPPLIER (A/P) invoices. Customer (A/R) invoices are a "
            "different table: Erp.InvcHead, keyed by InvoiceNum as well. A number that is "
            "not an A/P invoice is very often an A/R one."
        ),
    },
    ("erp.apinvdtl", "invoicenum"): {
        "table": "Erp.InvcDtl",
        "text": (
            "Erp.APInvDtl holds SUPPLIER (A/P) invoice lines. Customer (A/R) invoice lines "
            "are Erp.InvcDtl."
        ),
    },
    ("erp.invchead", "invoicenum"): {
        "table": "Erp.APInvHed",
        "text": (
            "Erp.InvcHead holds CUSTOMER (A/R) invoices. Supplier (A/P) invoices are a "
            "different table: Erp.APInvHed."
        ),
    },
}

_NUMERIC_LITERAL = re.compile(r"^-?\d+(\.\d+)?$")
_DIGITS = re.compile(r"\d+")


# --------------------------------------------------------------------------- #
# Probe plumbing
# --------------------------------------------------------------------------- #


@dataclass
class ProbeResult:
    """What one bounded probe returned. ``ok`` False is never fatal."""

    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    ms: float = 0.0

    @property
    def first(self) -> dict[str, Any]:
        return self.rows[0] if self.rows else {}


#: ``async (sql: str) -> ProbeResult``. Injected so the deterministic suite can
#: drive the whole diagnostician with no Epicor, and so the production probe
#: keeps running the full deny-list pipe.
ProbeFn = Callable[[str], Awaitable[ProbeResult]]


#: How long a measured domain stays authoritative. Observed behavior: the
#: cache was process-lifetime, shared across every session and user, with no TTL
#: and no timestamp — and because "the live probe wins" over E15a's dated
#: snapshot, a cached domain of unknown age silently outranked a snapshot that at
#: least carries `as_of`. An entry cached before a new site/code is created will
#: accuse the first correct query that uses it. One working day.
DEFAULT_DOMAIN_TTL_S = 8 * 60 * 60


class DomainCache:
    """Cache repeated column-domain probes, expiring on a TTL.

    Keyed by ``(schema.table, column, kind)``. Holds only what a domain probe
    already returned to a caller, so it cannot widen anyone's access; a denied
    column never reaches a probe, so it can never reach the cache either.

    Every entry is stamped ``measured_at`` and expires after :data:`ttl_s`, and
    the stamp rides into the served ``domain`` so a reader can see how old the
    measurement behind an accusation is.
    """

    def __init__(
        self, max_entries: int = 256, ttl_s: float = DEFAULT_DOMAIN_TTL_S
    ) -> None:
        self._data: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        self._max = max_entries
        self.ttl_s = float(ttl_s)
        self.hits = 0
        self.misses = 0
        self.expired = 0

    def get(self, table: str, column: str, kind: str) -> dict[str, Any] | None:
        key = (table.lower(), column.lower(), kind)
        hit = self._data.get(key)
        if hit is None:
            self.misses += 1
            return None
        stamped, value = hit
        if self.ttl_s > 0 and (time.time() - stamped) > self.ttl_s:
            self._data.pop(key, None)
            self.expired += 1
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, table: str, column: str, kind: str, value: dict[str, Any]) -> None:
        if len(self._data) >= self._max:
            self._data.clear()
        now = time.time()
        stamped = dict(value)
        stamped.setdefault(
            "measured_at",
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        )
        self._data[(table.lower(), column.lower(), kind)] = (now, stamped)

    def clear(self) -> None:
        self._data.clear()


#: The default shared cache. A caller may pass its own.
_DEFAULT_CACHE = DomainCache()


# --------------------------------------------------------------------------- #
# Decomposition — the sqlglot AST, never text matching
# --------------------------------------------------------------------------- #


@dataclass
class Conjunct:
    """One top-level AND-term of the outermost WHERE."""

    text: str
    node: Any
    aliases: frozenset[str]
    alias: str | None = None
    column: str | None = None
    table: str | None = None
    kind: str = "other"          # eq | in | like | null | cmp | other
    literal: Any = None
    literal_is_numeric: bool = False
    suspicion: int = 0
    #: Filled in as the diagnosis runs.
    verdict: str = "untested"    # untested | matches | matches_nothing | skipped
    matched_rows: int | None = None
    reason: str = ""

    @property
    def probeable(self) -> bool:
        return bool(self.table and self.column and len(self.aliases) <= 1)


def _bool_back(node: Any) -> Any:
    """sqlglot's tsql generator writes ``true`` as ``1``.

    Epicor accepts both (``= true``, ``= false``, ``= 1``, ``= 0`` — SQL dialect policy
    WHERE), but a diagnosis that quotes back ``[SugPoDtl].[Buy] = 1`` at a caller
    who wrote ``= true`` is asking them to find a predicate that is not in their
    statement. Rendering, and the probe SQL, both keep the caller's spelling.
    """
    if isinstance(node, exp.Boolean):
        return exp.Literal(this="true" if node.this else "false", is_string=False)
    return node


def _render(node: Any) -> str:
    try:
        return node.transform(_bool_back, copy=True).sql(dialect=DIALECT)
    except Exception:  # noqa: BLE001
        return node.sql(dialect=DIALECT)


def _fmt(value: Any) -> str:
    """A literal as the CALLER wrote it, for a message."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value)
    return repr(str(value))


def _from_source(select: Any) -> Any:
    """``Select.args["from"]`` is ``"from_"`` in some sqlglot builds."""
    return select.args.get("from") or select.args.get("from_")


def _alias_map(select: Any) -> dict[str, str]:
    """alias -> ``Schema.Table``, for REAL tables only.

    A CTE reference or a derived table is deliberately absent: there is no
    single physical table to probe, so its conjuncts stay ``not probeable``
    rather than being probed against a guess.
    """
    out: dict[str, str] = {}

    def add(node: Any) -> None:
        if not isinstance(node, exp.Table):
            return
        db = node.db or ""
        name = node.name or ""
        # A SCHEMA is mandatory in this dialect (SQL dialect policy MUST 1: `from Part`
        # fails), so a bare name is a CTE reference, not a physical table.
        # Probing `from c as [c]` would be nonsense SQL built on a guess.
        if not name or not db:
            return
        out[node.alias or name] = f"{db}.{name}"

    source = _from_source(select)
    if source is not None:
        add(source.this)
    for join in select.args.get("joins") or []:
        add(join.this)
    return out


def _classify(node: Any) -> tuple[str, Any, bool]:
    """(kind, literal, literal_is_numeric) for one predicate node."""
    if isinstance(node, (exp.Is,)):
        return "null", None, False
    if isinstance(node, exp.In):
        values = [
            v.this if isinstance(v, exp.Literal) else None
            for v in (node.args.get("expressions") or [])
        ]
        return "in", [v for v in values if v is not None], False
    right = node.args.get("expression") if hasattr(node, "args") else None
    if isinstance(node, exp.Like) or isinstance(node, exp.ILike):
        lit = right.this if isinstance(right, exp.Literal) else None
        return "like", lit, False
    if isinstance(node, exp.EQ):
        if isinstance(right, exp.Boolean):
            return "eq", bool(right.this), False
        if isinstance(right, exp.Literal):
            numeric = not right.args.get("is_string") and bool(
                _NUMERIC_LITERAL.match(str(right.this))
            )
            return "eq", right.this, numeric
        return "eq", None, False
    if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ)):
        # `< -1000000` parses as LT(Neg(Literal)), not LT(Literal). Reading only
        # the bare Literal made every negative bound invisible.
        if isinstance(right, exp.Neg) and isinstance(right.this, exp.Literal):
            return "cmp", f"-{right.this.this}", True
        if isinstance(right, exp.Literal):
            numeric = not right.args.get("is_string") and bool(
                _NUMERIC_LITERAL.match(str(right.this))
            )
            return "cmp", right.this, numeric
        return "cmp", None, False
    return "other", None, False


def _suspicion(conj: Conjunct, company_id: str | None) -> int:
    """How likely is THIS predicate to be the invented literal?

    Ordering only — it decides where a bounded budget is spent, never whether a
    predicate is accused. Accusation always requires a probe that came back 0.
    Weights prioritize cheap, discriminating checks; they do not prove a mistake.
    """
    col = (conj.column or "").lower()
    if col in _INSTALL_CONSTANT_COLUMNS and company_id:
        return 100
    if col in _DOMAIN_AUTHORITY:
        return 92                      # bounded lookup on an authoritative master
    if conj.kind == "eq" and isinstance(conj.literal, bool):
        return 80                      # boolean equality has a small domain
    if conj.kind == "eq" and conj.literal is not None and not conj.literal_is_numeric:
        return 75                      # a string code: ResourceType = 'S'
    if conj.kind == "in":
        return 70
    if conj.kind == "eq" and conj.literal_is_numeric:
        return 60                      # OrderDtl.CustNum = 10000
    if conj.kind == "like":
        return 50
    if conj.kind == "null":
        return 40                      # SugPoDtl.PONUM is null
    if conj.kind == "cmp":
        return 25
    return 10


def _decompose(sql: str, company_id: str | None) -> tuple[Any, list[Conjunct], str]:
    """Parse and split the outermost WHERE into AND-terms. Never raises."""
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception as exc:  # noqa: BLE001 - a parse failure is not an error here
        return None, [], f"the statement could not be re-parsed locally ({exc})"
    select = tree
    if not isinstance(select, exp.Select):
        select = tree.find(exp.Select)
    if select is None:
        return None, [], "no SELECT was found in the statement"

    where = select.args.get("where")
    if where is None or where.this is None:
        return select, [], ""

    node = where.this
    parts = list(node.flatten()) if isinstance(node, exp.And) else [node]
    aliases = _alias_map(select)
    out: list[Conjunct] = []
    for part in parts:
        # An OR-group is one term: relaxing half of it answers a different
        # question, so it is reported whole and never probed piecewise.
        cols = list(part.find_all(exp.Column))
        # Only aliases bound by the OUTER select count. A scalar sub-select
        # brings its own scope — `od.CustNum = (select top 1 [c].[CustNum] from
        # Erp.Customer as [c] where ...)` is a predicate ABOUT `od`. Counting
        # the inner alias `c` would incorrectly make the aliased spelling
        # unprobeable while allowing the equivalent unaliased spelling.
        used = frozenset(c.table for c in cols if c.table and c.table in aliases)
        conj = Conjunct(text=_render(part), node=part, aliases=used)
        if not isinstance(part, exp.Or) and len(used) == 1:
            alias = next(iter(used))
            outer_cols = [c for c in cols if c.table == alias]
            conj.alias = alias
            # The column the predicate is ABOUT is the outer one, left-most.
            conj.column = outer_cols[0].name
            conj.table = aliases.get(alias)
            conj.kind, conj.literal, conj.literal_is_numeric = _classify(part)
        elif isinstance(part, exp.Or):
            conj.kind = "or_group"
        conj.suspicion = _suspicion(conj, company_id)
        out.append(conj)
    return select, out, ""


# --------------------------------------------------------------------------- #
# Probe construction — bounded, single-table, deny-checked
# --------------------------------------------------------------------------- #


def _probe_blocked(table: str, column: str) -> str:
    """Non-empty reason when a probe on *table*.*column* must not be built."""
    if denylist.is_denied_table(table):
        return f"{table} is on the deny-list"
    if denylist.is_denied_column(table, column):
        return f"{table}.{column} is a denied (compensation/PII) column"
    return ""


def _satisfiability_sql(conj: Conjunct) -> str:
    return (
        f"select top 5 count(*) as [n] from {conj.table} as [{conj.alias}] "
        f"where {conj.text}"
    )


def _enumeration_sql(table: str, alias: str, code: str, label: str | None) -> str:
    label_item = f", [{alias}].[{label}] as [label]" if label else ""
    group_extra = f", [{alias}].[{label}]" if label else ""
    return (
        f"select top {DOMAIN_LIMIT + 1} [{alias}].[{code}] as [value]{label_item}, "
        f"count(*) as [n] from {table} as [{alias}] "
        f"group by [{alias}].[{code}]{group_extra} order by count(*) desc"
    )


def _sql_literal(value: Any) -> str:
    """A literal as SQL. Single quotes are doubled — a value read out of the
    caller's own already-parsed statement is not hostile, but a generated probe
    must still be well-formed."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _NUMERIC_LITERAL.match(text):
        return text
    return "'" + text.replace("'", "''") + "'"


def _range_sql(table: str, alias: str, column: str, *, with_count: bool) -> str:
    tail = ", count(*) as [n]" if with_count else ""
    return (
        f"select top 5 min([{alias}].[{column}]) as [lo], "
        f"max([{alias}].[{column}]) as [hi]{tail} from {table} as [{alias}]"
    )


def _is_big(table: str) -> bool:
    return (table or "").rsplit(".", 1)[-1].lower() in BIG_TABLES


def _as_int(value: Any) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Correction candidates — E15b. Announced, never applied.
# --------------------------------------------------------------------------- #


def _correction(literal: Any, values: Sequence[str], labels: Mapping[str, str]) -> dict | None:
    """A concrete replacement for *literal*, or None. Uniqueness is required.

    Three bases, most confident first. Every one of them names itself in
    ``match_basis`` so the caller can judge the suggestion instead of trusting
    it — announced-normalization rule: *announce every normalisation, never apply one silently*.

    **A correction is never the caller's own value.** Suggesting an unchanged
    value produces an identical retry and can make clients repeat the same
    empty query indefinitely. Every matching branch checks this invariant,
    including numeric normalization and fuzzy matching.
    """
    if literal is None or not values:
        return None
    text = str(literal).strip()
    if not text:
        return None
    low = text.lower()

    def ok(value: Any, basis: str) -> dict | None:
        if str(value).strip() == text:
            return None
        return {"value": value, "match_basis": basis}

    exact = [v for v in values if str(v).strip().lower() == low]
    if len(exact) == 1:
        return ok(exact[0], "case-insensitive exact match")

    # The caller wrote the LABEL where the CODE belongs: 'Example Site' vs '10'
    # (label "your organization - Example Site Division"). Containment, and it must
    # be unique — two divisions containing the word would be a guess.
    if labels:
        contains = [v for v, lbl in labels.items() if low and low in str(lbl).lower()]
        if len(contains) == 1:
            return ok(
                contains[0],
                f"the label {labels[contains[0]]!r} contains {text!r}",
            )

    # The caller wrapped the code in prose: 'Site 10' -> '10'.
    for chunk in _DIGITS.findall(text):
        hits = [v for v in values if str(v).strip() == chunk]
        if len(hits) == 1:
            candidate = ok(hits[0], f"the digits in {text!r} are a real value")
            if candidate:
                return candidate

    close = difflib.get_close_matches(text, [str(v) for v in values], n=2, cutoff=_FUZZY_CUTOFF)
    if len(close) == 1:
        candidate = ok(close[0], f"closest value (>= {_FUZZY_CUTOFF})")
        if candidate:
            return candidate
    if labels:
        close_lbl = difflib.get_close_matches(
            text, [str(v) for v in labels.values()], n=2, cutoff=_FUZZY_CUTOFF
        )
        if len(close_lbl) == 1:
            back = [v for v, lbl in labels.items() if str(lbl) == close_lbl[0]]
            if len(back) == 1:
                return ok(back[0], f"closest label {close_lbl[0]!r}")
    return None


def _where_terms(select: Any) -> list[Any]:
    where = select.args.get("where")
    if where is None or where.this is None:
        return []
    node = where.this
    return list(node.flatten()) if isinstance(node, exp.And) else [node]


def _rewrite_where(sql: str, target_text: str, replacement: Any | None) -> str | None:
    """Rebuild *sql* with the conjunct rendering as *target_text* replaced or removed.

    The WHERE is rebuilt from its remaining terms, never by transforming a node
    to ``None`` in place: dropping one arm of an ``And`` that way leaves a
    half-built node the generator then crashes on (measured while writing this).
    """
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:  # noqa: BLE001
        return None
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        return None
    terms = _where_terms(select)
    if not terms:
        return None
    kept: list[Any] = []
    hit = False
    for term in terms:
        if not hit and _render(term) == target_text:
            hit = True
            if replacement is not None:
                kept.append(replacement)
            continue
        kept.append(term)
    if not hit:
        return None
    if kept:
        rebuilt = kept[0]
        for term in kept[1:]:
            rebuilt = exp.and_(rebuilt, term)
        select.set("where", exp.Where(this=rebuilt))
    else:
        select.set("where", None)
    try:
        return _render(tree)
    except Exception:  # noqa: BLE001
        return None


def _rewrite_literal(sql: str, conj: Conjunct, new_value: Any) -> str | None:
    """The statement with *conj*'s literal replaced. AST, not text."""
    node = conj.node
    if not isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return None
    replacement = node.copy()
    replacement.set("expression", exp.Literal.string(str(new_value)))
    return _rewrite_where(sql, conj.text, replacement)


def _drop_predicate(sql: str, conj: Conjunct) -> str | None:
    'The statement with *conj* removed entirely.'
    return _rewrite_where(sql, conj.text, None)


# --------------------------------------------------------------------------- #
# The diagnosis
# --------------------------------------------------------------------------- #


async def diagnose_empty(
    sql: str,
    *,
    probe: ProbeFn,
    budget: int = DEFAULT_PROBE_BUDGET,
    cache: DomainCache | None = None,
    company_id: str | None = None,
    table_is_big: Callable[[str], bool] = _is_big,
) -> dict[str, Any]:
    """Explain a 0-row result. Returns an annotation; **never raises**.

    ``probe`` is an ``async (sql) -> ProbeResult`` that MUST run the same
    deny-list pipe the caller's query ran through.
    """
    try:
        return await _diagnose(
            sql,
            probe=probe,
            budget=max(0, int(budget)),
            cache=cache if cache is not None else _DEFAULT_CACHE,
            company_id=(str(company_id).strip() if company_id else None),
            table_is_big=table_is_big,
        )
    except Exception:  # noqa: BLE001 - a diagnosis must never break a good answer
        logger.warning("zero-row diagnosis failed; returning the plain result", exc_info=True)
        return {
            "verdict": "undetermined",
            "likely_mistake": False,
            "message": (
                "0 rows. The server could not analyse why, so treat this as a plain empty "
                "result: it may well be the correct answer."
            ),
            "probes_used": 0,
            "probe_budget": int(budget),
            "probes": [],
        }


async def _diagnose(
    sql: str,
    *,
    probe: ProbeFn,
    budget: int,
    cache: DomainCache,
    company_id: str | None,
    table_is_big: Callable[[str], bool],
) -> dict[str, Any]:
    select, conjuncts, parse_note = _decompose(sql, company_id)
    probes: list[dict[str, Any]] = []

    async def run(kind: str, probe_sql: str) -> ProbeResult:
        result = await probe(probe_sql)
        probes.append(
            {
                "kind": kind,
                "sql": probe_sql,
                "ok": result.ok,
                "rows": len(result.rows),
                "ms": round(result.ms, 1),
                "error": result.error[:300] if result.error else "",
            }
        )
        return result

    if select is None:
        return _undetermined(parse_note, budget, probes)

    joins = list(select.args.get("joins") or [])
    having = select.args.get("having")

    # ---- 0. no predicates at all: nothing to bisect, and say so plainly -----
    if not conjuncts:
        return {
            "verdict": "no_predicates",
            "likely_mistake": False,
            "message": _no_predicate_message(bool(joins), having is not None),
            "killing_predicates": [],
            "satisfied_predicates": [],
            "untested_predicates": [],
            "probes_used": 0,
            "probe_budget": budget,
            "probes": probes,
        }

    ordered = sorted(
        range(len(conjuncts)), key=lambda i: (-conjuncts[i].suspicion, i)
    )

    # ---- 1. install constants cost ZERO probes ------------------------------
    for idx in ordered:
        conj = conjuncts[idx]
        if (
            company_id
            and (conj.column or "").lower() in _INSTALL_CONSTANT_COLUMNS
            and conj.kind == "eq"
            and conj.literal is not None
            and str(conj.literal).strip() != company_id
        ):
            conj.verdict = "matches_nothing"
            conj.matched_rows = 0
            conj.reason = "install_constant"

    # ---- 2. satisfiability probes, most suspicious first --------------------
    # One probe is held back so the top suspect can still get a domain.
    sat_cap = max(0, budget - 1) if budget else 0
    for idx in ordered:
        conj = conjuncts[idx]
        if conj.verdict != "untested":
            continue
        if not conj.probeable:
            conj.verdict = "skipped"
            conj.reason = (
                "spans more than one table" if len(conj.aliases) > 1
                else "is an OR group" if conj.kind == "or_group"
                else "has no single physical column to probe"
            )
            continue
        blocked = _probe_blocked(conj.table or "", conj.column or "")
        if blocked:
            conj.verdict = "skipped"
            conj.reason = f"not probed: {blocked}"
            continue
        if len(probes) >= sat_cap:
            continue
        result = await run("satisfiability", _satisfiability_sql(conj))
        if not result.ok:
            conj.verdict = "skipped"
            conj.reason = f"the probe did not run ({result.error[:120]})"
            continue
        count = _as_int(result.first.get("n"))
        if count is None:
            # The probe ran and returned something we cannot read as a count.
            # Calling that "matches" would let a broken probe manufacture a
            # `genuinely_empty` verdict — a confident wrong answer built on no
            # measurement at all.
            conj.verdict = "skipped"
            conj.reason = "the probe returned no readable count"
            continue
        conj.matched_rows = count
        conj.verdict = "matches_nothing" if count == 0 else "matches"

    untested = [c for c in conjuncts if c.verdict == "untested"]
    dead = [c for c in conjuncts if c.verdict == "matches_nothing"]
    alive = [c for c in conjuncts if c.verdict == "matches"]
    skipped = [c for c in conjuncts if c.verdict == "skipped"]

    # ---- 3. a domain for each dead predicate, most suspicious first ---------
    findings: list[dict[str, Any]] = []
    for conj in sorted(dead, key=lambda c: -c.suspicion):
        finding = await _domain_for(
            conj,
            sql=sql,
            run=run,
            remaining=lambda: budget - len(probes),
            cache=cache,
            company_id=company_id,
            table_is_big=table_is_big,
            sole_predicate=len(conjuncts) == 1,
        )
        findings.append(finding)

    if findings:
        return _killing_verdict(
            findings, alive, untested, skipped, budget, probes, joins=len(joins)
        )

    # ---- 4. nothing individually dead: the combination, or HAVING -----------
    if having is not None and budget - len(probes) > 0:
        relaxed = _without_having(sql)
        if relaxed:
            result = await run("having_relaxation", relaxed)
            if result.ok and _as_int(result.first.get("n")):
                return {
                    "verdict": "killing_having",
                    "likely_mistake": True,
                    "message": (
                        "0 rows, and the WHERE clause is not the cause: every predicate "
                        "matches rows on its own, and the same query WITHOUT its HAVING "
                        f"returns {_as_int(result.first.get('n'))} group(s). Your HAVING "
                        "threshold is what removed them — lower it, or drop it to see the "
                        "real distribution."
                    ),
                    "killing_predicates": [],
                    "satisfied_predicates": [c.text for c in alive],
                    "untested_predicates": [c.text for c in untested],
                    "skipped_predicates": [
                        {"predicate": c.text, "reason": c.reason} for c in skipped
                    ],
                    "probes_used": len(probes),
                    "probe_budget": budget,
                    "probes": probes,
                }

    return _genuinely_empty(
        alive, untested, skipped, joins, having is not None, budget, probes
    )


# --------------------------------------------------------------------------- #
# Domain resolution for one dead predicate
# --------------------------------------------------------------------------- #


async def _domain_for(
    conj: Conjunct,
    *,
    sql: str,
    run: Callable[[str, str], Awaitable[ProbeResult]],
    remaining: Callable[[], int],
    cache: DomainCache,
    company_id: str | None,
    table_is_big: Callable[[str], bool],
    sole_predicate: bool = False,
) -> dict[str, Any]:
    """Fetch (or recall) the real domain of *conj*'s column and build a finding."""
    finding: dict[str, Any] = {
        "predicate": conj.text,
        "column": f"{conj.table}.{conj.column}",
        "compared_to": conj.literal if conj.kind != "null" else None,
        "comparison": conj.kind,
        "matched_rows": 0,
        "likely_mistake": False,
    }
    column_low = (conj.column or "").lower()
    table = conj.table or ""

    # -- the install constant: certain, and it costs nothing -----------------
    if conj.reason == "install_constant" and company_id:
        finding.update(
            domain={"values": [company_id], "complete": True, "source": "install constant"},
            likely_mistake=True,
            correction={"value": company_id, "match_basis": "the company id of this install"},
            note=(
                f"Company is the id of this Epicor install and is always {company_id!r}. "
                "It is never a company NAME."
            ),
        )
        finding["retry_sql"] = _rewrite_literal(sql, conj, company_id)
        return finding

    if conj.kind == "null":
        finding["note"] = (
            "This is a NULL test, not a value comparison — the column has no NULL rows. "
            "In Epicor a numeric column is normally 0 rather than NULL, and a text column "
            "is normally the empty string; `= 0` or `= ''` is usually what was meant."
        )
        return finding

    # -- where the domain actually lives -------------------------------------
    authority = _DOMAIN_AUTHORITY.get(column_low)
    if authority:
        dom_table, code_col, label_col = authority
    else:
        dom_table, code_col, label_col = table, conj.column or "", None

    # Both columns the enumeration will SELECT are deny-checked, not just the
    # code one. Observed behavior: `_enumeration_sql` also projects
    # `label_col`, which nothing examined. Unexploitable today — `label_col` is
    # non-None only for the two `_DOMAIN_AUTHORITY` entries and is hardcoded to
    # `Erp.Plant.Name` — and a landmine for the next entry, whose label could be
    # a person name or a rate.
    blocked = _probe_blocked(dom_table, code_col) or (
        _probe_blocked(dom_table, label_col) if label_col else ""
    )
    if blocked:
        finding["note"] = f"The domain was not listed: {blocked}."
        return finding







    hint = _SIBLING_HINTS.get((table.lower(), column_low))
    if hint:
        finding["sibling_hint"] = hint["text"]
        sibling = hint["table"]
        if (
            conj.kind == "eq"
            and conj.literal is not None
            and remaining() > 0
            and not _probe_blocked(sibling, conj.column or "")
        ):
            sib = await run(
                "sibling_table",
                f"select top 5 count(*) as [n] from {sibling} as [D] "
                f"where [D].[{conj.column}] = {_sql_literal(conj.literal)}",
            )
            found = _as_int(sib.first.get("n")) if sib.ok else None
            if found:
                finding["sibling_evidence"] = {
                    "table": sibling,
                    "matching_rows": found,
                    "verified": True,
                }
                finding["sibling_hint"] = (
                    f"{hint['text']} Checked: {sibling} has {found} row(s) with "
                    f"{conj.column} = {_fmt(conj.literal)}."
                )
            elif found == 0:
                finding["sibling_evidence"] = {
                    "table": sibling,
                    "matching_rows": 0,
                    "verified": True,
                }
                finding["sibling_hint"] = (
                    f"{hint['text']} Checked: {sibling} has no row with "
                    f"{conj.column} = {_fmt(conj.literal)} either."
                )

    # A `>`/`<`/`>=`/`<=` predicate is answered by the column's RANGE, whatever
    # the literal's type. The minimum and maximum dates explain an empty date
    # window; enumerating every date would be expensive and unhelpful.
    kind = (
        "range"
        if (not authority and (conj.literal_is_numeric or conj.kind == "cmp"))
        else "enumeration"
    )
    cached = cache.get(dom_table, code_col, kind)
    if cached is not None:
        return _apply_domain(
            finding, conj, cached, sql, from_cache=True,
            sole_predicate=sole_predicate,
        )

    if remaining() <= 0:
        finding["note"] = (
            "The probe budget was spent before this column's real values could be listed. "
            "Re-run one narrow query against it to see them."
        )
        return finding

    alias = "D"
    if kind == "range":
        big = table_is_big(dom_table)
        result = await run("range", _range_sql(dom_table, alias, code_col, with_count=not big))
        row = result.first if result.ok else {}
        # ANY non-empty bound is usable — Epicor returns a datetime as
        # a US-style date string, which is neither numeric nor ISO. Requiring
        # a number would discard valid bounds for a date-window question.
        # `min([Buy])` on a `bit` fails, which is the
        # case the fallback exists for.
        usable = result.ok and str(row.get("lo") or "") and str(row.get("hi") or "")
        if usable:
            domain = {
                "kind": "range",
                "min": row.get("lo"),
                "max": row.get("hi"),
                "row_count": _as_int(row.get("n")),
                "source": f"{dom_table}.{code_col}",
                "complete": True,
            }
            cache.put(dom_table, code_col, kind, domain)
            return _apply_domain(
                finding, conj, domain, sql, sole_predicate=sole_predicate
            )







        finding["range_probe"] = (
            "unavailable" if not result.ok else "returned non-numeric bounds"
        )
        if remaining() <= 0 or (table_is_big(dom_table) and not authority):
            finding["note"] = (
                f"The value range of {finding['column']} could not be read"
                + (f" ({result.error[:120]})" if result.error else "")
                + ", and its distinct values were not enumerated, so the domain is "
                "unknown. The predicate matching nothing is still the finding."
            )
            return finding
        cached = cache.get(dom_table, code_col, "enumeration")
        if cached is not None:
            return _apply_domain(
                finding, conj, cached, sql, from_cache=True,
                sole_predicate=sole_predicate,
            )

    if table_is_big(dom_table) and not authority:
        finding["note"] = (
            f"{dom_table} is one of the large transaction tables, so its distinct "
            f"{code_col} values were NOT enumerated — that scan is not worth running "
            "against production for a diagnostic. The predicate matching nothing is the "
            "finding; it may simply be a value that does not exist."
        )
        return finding

    result = await run(
        "enumeration", _enumeration_sql(dom_table, alias, code_col, label_col)
    )
    if not result.ok:
        finding["note"] = f"The column's values could not be listed ({result.error[:160]})."
        return finding
    rows = result.rows
    complete = len(rows) <= DOMAIN_LIMIT
    values = [r.get("value") for r in rows[:DOMAIN_LIMIT]]
    counts = {str(r.get("value")): _as_int(r.get("n")) for r in rows[:DOMAIN_LIMIT]}
    labels = (
        {str(r.get("value")): r.get("label") for r in rows[:DOMAIN_LIMIT]}
        if label_col
        else {}
    )
    domain = {
        "kind": "enumeration",
        "values": values,
        "counts": counts,
        "labels": labels or None,
        "complete": complete,
        "distinct_at_least": len(rows),
        "source": f"{dom_table}.{code_col}",
    }
    cache.put(dom_table, code_col, kind, domain)
    return _apply_domain(finding, conj, domain, sql, sole_predicate=sole_predicate)


def _apply_domain(
    finding: dict[str, Any],
    conj: Conjunct,
    domain: Mapping[str, Any],
    sql: str,
    *,
    from_cache: bool = False,
    sole_predicate: bool = False,
) -> dict[str, Any]:
    """Attach *domain* to *finding* and decide ``likely_mistake`` — the FP gate."""
    finding["domain"] = dict(domain) | ({"from_cache": True} if from_cache else {})

    if domain.get("kind") == "range":
        lo, hi = domain.get("min"), domain.get("max")
        total = domain.get("row_count")
        outside = _outside_range(conj.literal, lo, hi)
        head = f"{finding['column']} ranges from {lo} to {hi}" + (
            f" over {total} rows" if total else ""
        )
        if conj.kind == "cmp":





            finding["note"] = (
                head + f"; the bound {_fmt(conj.literal)} lies "
                + ("outside" if outside else "inside")
                + " that, so 0 rows may well be the correct answer."
            )
            finding["likely_mistake"] = False
            return finding
        # An EQUALITY to a value outside the real range is a mistake with
        # evidence — but ONLY where the range is a SMALL DENSE CODE SET.
        # An absent sequential document number may simply not have been issued.
        # Treat that like an absent text identifier, not a mistaken filter.
        # `_span_is_a_sequence` distinguishes it from a small, dense code set
        # where an out-of-range value is useful evidence for a correction.
        sequence = _span_is_a_sequence(lo, hi)
        finding["note"] = head + (
            (
                f"; {_fmt(conj.literal)} is OUTSIDE that range. "
                + (
                    "That range is a wide numbering SEQUENCE, so a value past its high-water "
                    "mark normally means that document has not been issued — 0 rows is very "
                    "likely the correct answer."
                    if sequence
                    else "No row can carry it."
                )
            )
            if outside
            else f"; {_fmt(conj.literal)} is inside that range but no row carries it, "
            "which may simply mean that record does not exist."
        )
        # A value inside the range that simply has no row is an ordinary missing
        # record, and calling that a mistake is exactly the false alarm this
        # module refuses.
        finding["likely_mistake"] = bool(outside and not sequence)
        return finding

    values = [str(v) for v in (domain.get("values") or [])]
    labels = {str(k): v for k, v in (domain.get("labels") or {}).items() if v is not None}
    source = str(domain.get("source") or "")
    same_table = not source or source.lower() == str(finding["column"]).lower()
    # Name what was actually MEASURED. When the domain came off the curated
    # authority (Erp.Plant) rather than the caller's own table, saying
    # "Erp.JobHead.Plant contains exactly these 9" asserts something no probe
    # checked — the honesty rule this whole project runs on.
    subject = (
        finding["column"]
        if same_table
        else f"{source} (the authoritative list for {finding['column']})"
    )

    # ---- THE AUTHORITY-TABLE CARVE-OUT (avoid blaming a valid master-data value) --------
    # The `complete` -> `likely_mistake` rule below is sound only when the
    # domain was enumerated on the CALLER'S OWN table: the satisfiability probe
    # already proved `Col = X` matches nothing there, so X cannot appear in that
    # table's own enumeration. `_DOMAIN_AUTHORITY` breaks exactly that
    # precondition — the enumeration runs on `Erp.Plant` while the emptiness was
    # measured on `Erp.JobHead`, and a site can perfectly well exist in the
    # master with no rows in the transaction table.
    # A valid site with no jobs must not be called a mistaken site code merely
    # because the transaction query is empty. If the master list contains it,
    # preserve the empty result instead of proposing the same query again.
    present = [v for v in values if _norm(v) == _norm(conj.literal)]
    # On the caller's OWN table a case-only difference is still a correctable
    # mistake (`_correction`'s case-insensitive-exact branch), so the carve-out
    # there requires an EXACT match. Off an authority table it does not: the
    # value is real wherever it is spelled, and the emptiness was measured
    # somewhere else.
    exact_present = any(str(v).strip() == str(conj.literal).strip() for v in present)
    if present and (exact_present or not same_table):
        total = (domain.get("counts") or {}).get(str(conj.literal).strip())
        finding["likely_mistake"] = False
        finding.pop("correction", None)
        finding.pop("retry_sql", None)
        finding["note"] = (
            f"{_fmt(conj.literal)} IS a real value of {source or subject}"
            + (f" ({total} row(s) there)" if total else "")
            + f", but no row of {finding['column'].rsplit('.', 1)[0]} carries it. "
            "0 rows is a real answer here, not a typo."
        )
        finding["value_exists_elsewhere"] = True
        return finding

    correction = _correction(conj.literal, values, labels)
    if correction:
        finding["correction"] = correction
        finding["retry_sql"] = _rewrite_literal(sql, conj, correction["value"])

    if domain.get("complete") and len(values) == 1:

















        only = values[0]
        total = (domain.get("counts") or {}).get(only)
        head = (
            f"{finding['column']} holds ONE value across this whole install: {only!r}"
            + (f" on all {total} rows" if total else "")
            + f". Comparing it to {_fmt(conj.literal)} can never match"
        )
        finding.pop("correction", None)
        if sole_predicate:
            finding["likely_mistake"] = False
            finding.pop("retry_sql", None)
            finding["note"] = (
                head + ", so there are NO such rows and 0 is the correct, complete "
                "answer to the query as written. (Removing the predicate would return "
                f"all {total or 'the'} rows of {finding['column'].rsplit('.', 1)[0]}, "
                "which answers a different question — do that only if the flag was not "
                "the point.)"
            )
            finding["suggested_action"] = "none"
            # The alternative statement is still worth HAVING, and it is
            # deliberately NOT `retry_with`. `retry_with` means "the fix for
            # your query", and the whole point of this branch is that the
            # caller's query is not broken. Filters such as
            # `SugPoDtl.Buy = true` and `Customer.Inactive = true`
            # are the SAME SQL shape with opposite intents and no server can
            # read the intent — so the alternative is offered under its own
            # name, labelled as a different question, and the model chooses.
            # Calling it `retry_with` would mislabel a broader query as a repair
            # and could answer a different business question.
            broader = _drop_predicate(sql, conj)
            if broader:
                finding["broader_query"] = {
                    "sql": broader,
                    "answers": (
                        "a DIFFERENT question — every row of "
                        f"{finding['column'].rsplit('.', 1)[0]} with no filter at all"
                        + (f" ({total} rows)" if total else "")
                        + f", not the {_fmt(conj.literal)} ones. Run it ONLY if the "
                        "flag was not what was being asked about."
                    ),
                }
            return finding
        finding["likely_mistake"] = True
        finding["note"] = (
            head + ", and the column cannot discriminate anything — remove this "
            "predicate rather than inverting it. The other predicates still carry "
            "the question."
        )
        finding["retry_sql"] = _drop_predicate(sql, conj)
        finding["suggested_action"] = "drop_predicate"
        return finding

    if domain.get("complete"):
        # An ENUMERABLE domain the value is missing from is real evidence: the
        # caller can see at a glance that their value is not one of these. The
        # value-is-present case never reaches here — it returned above.
        finding["likely_mistake"] = True
        listed = _listable(source or str(finding["column"]))
        if listed:
            shown = ", ".join(_fmt(v) for v in values[:12]) or "(no values at all)"
            more = "" if len(values) <= 12 else f" (+{len(values) - 12} more)"
            tail = f": {shown}{more}"
        else:
            tail = " (not listed here — this column identifies people)"
        finding["note"] = (
            f"{subject} contains exactly {len(values)} distinct value(s){tail}. "
            f"{_fmt(conj.literal)} is not one of them."
        )
    else:
        # A SAMPLE is not a domain. Saying "your value is not in this list" when
        # the list is 25 of thousands is the false alarm this module refuses.
        finding["likely_mistake"] = bool(correction)
        listed = _listable(source or str(finding["column"]))
        # Do not volunteer people names as examples for an unmatched value.
        # Cardinality explains the diagnostic without listing unrelated people.
        shown = (
            f": {', '.join(_fmt(v) for v in values[:8])}"
            if listed
            else " (not listed here — this column identifies people)"
        )
        finding["note"] = (
            f"{finding['column']} has more than {DOMAIN_LIMIT} distinct values, so this is "
            f"a SAMPLE of the most common ones, not the domain{shown}. "
            f"{_fmt(conj.literal)} not appearing may simply mean that record does not "
            "exist, which would make 0 rows the correct answer."
        )
    return finding


def _norm(value: Any) -> str:
    """A literal or a domain value, comparably. Case- and space-insensitive, and
    ``'70'`` from a text column equals ``70`` from a numeric one."""
    text = str(value if value is not None else "").strip().lower()
    if _NUMERIC_LITERAL.match(text):
        try:
            number = float(text)
        except ValueError:
            return text
        return str(int(number)) if number == int(number) else str(number)
    return text


#: A wide integer span is a document NUMBERING SEQUENCE (`PONum` 1000..9000),
#: where "past the maximum" means "not issued yet". A narrow one is a code set
#: or a small master, where a value far outside it may be an invalid code;
#: either case needs more evidence.
_SEQUENCE_SPAN = 1000


def _span_is_a_sequence(lo: Any, hi: Any) -> bool:
    low, high = _as_int(lo), _as_int(hi)
    if low is None or high is None:
        # Not a readable integer range at all — a date, a decimal, a string
        # bound. Never claim a value outside it was invented.
        return True
    return (high - low) >= _SEQUENCE_SPAN


#: Tables whose rows ARE people. A domain listing off one of these is a staff
#: roster, so the diagnosis reports its CARDINALITY and not its values
#:. Bare names, schema-insensitive.
_PERSON_TABLES: frozenset[str] = frozenset(
    {
        "empbasic", "empbasicsearch", "employee", "empexpense", "laborhed",
        "person", "personper", "personcontact", "perscon", "userfile",
    }
)


def _listable(source: str) -> bool:
    """False when naming this column's values would enumerate people."""
    table = str(source or "").rsplit(".", 1)[0] if "." in str(source or "") else ""
    bare = table.rsplit(".", 1)[-1].strip().lower()
    return bare not in _PERSON_TABLES


_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
#: Epicor may return US-style dates as well as ISO dates. Parse both so date
#: ranges are compared chronologically rather than lexicographically.
_US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")


def _to_date(value: Any) -> tuple[int, int, int] | None:
    text = str(value or "").strip()
    iso = _ISO_DATE.match(text)
    if iso:
        return int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
    us = _US_DATE.match(text)
    if us:
        return int(us.group(3)), int(us.group(1)), int(us.group(2))
    return None


def _outside_range(literal: Any, lo: Any, hi: Any) -> bool:
    """True only when the comparison is SAFE to make. Unknown ⇒ False."""
    try:
        value = float(str(literal))
        return value < float(str(lo)) or value > float(str(hi))
    except (TypeError, ValueError):
        pass
    dates = [_to_date(literal), _to_date(lo), _to_date(hi)]
    if all(d is not None for d in dates):
        value, low, high = dates  # type: ignore[misc]
        return value < low or value > high
    return False


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


def _undetermined(note: str, budget: int, probes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "verdict": "undetermined",
        "likely_mistake": False,
        "message": (
            "0 rows. The server could not analyse the statement to say why"
            + (f" — {note}" if note else "")
            + ". Treat this as a plain empty result: it may be the correct answer."
        ),
        "killing_predicates": [],
        "satisfied_predicates": [],
        "untested_predicates": [],
        "probes_used": len(probes),
        "probe_budget": budget,
        "probes": probes,
    }


def _no_predicate_message(has_joins: bool, has_having: bool) -> str:
    if has_having:
        return (
            "0 rows, and there is no WHERE clause — every row was removed by the HAVING "
            "threshold (or the table is empty). Drop the HAVING to see the real "
            "distribution before setting one."
        )
    if has_joins:
        return (
            "0 rows, and there is no WHERE clause at all — so nothing was filtered out. "
            "The JOIN is what returned nothing: no row of the first table matched the "
            "join condition. Check the join keys (both sides need Company AND the "
            "business key, and the business key must be the same field on both tables)."
        )
    return (
        "0 rows, and there is no WHERE clause and no join: this table is empty. That is "
        "the complete and correct answer."
    )


def unverified_text_match(finding: Mapping[str, Any]) -> bool:
    """True when a dead predicate is an exact TEXT match that nothing proved absent.

    ``=`` / ``in`` against a non-numeric literal, where the column's values were
    NOT fully enumerated (a sample of the top 25, or no enumeration at all on a
    big table). That is the one dead-predicate shape where "no row has this
    value" and "the user's words are spelled differently in the data" look
    identical to every probe this module runs — `Vendor.Name = 'Acme'` against
    `ACME TOOLING INC`. A complete enumeration or a measured numeric range
    DOES prove absence, and is not this.
    """
    if finding.get("likely_mistake") or finding.get("comparison") not in ("eq", "in"):
        return False
    if finding.get("sibling_hint"):
        # The diagnosis already names WHERE the value most likely lives (the
        # AP<->AR look-alike tables); a LIKE on the wrong table is not the lead.
        return False
    value = finding.get("compared_to")
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        float(value)
        return False
    except ValueError:
        pass
    domain = finding.get("domain")
    return not (isinstance(domain, Mapping) and domain.get("complete") is True)


def _killing_verdict(
    findings: list[dict[str, Any]],
    alive: list[Conjunct],
    untested: list[Conjunct],
    skipped: list[Conjunct],
    budget: int,
    probes: list[dict[str, Any]],
    joins: int = 0,
) -> dict[str, Any]:
    likely = any(f.get("likely_mistake") for f in findings)
    head = findings[0]
    lead = (
        f"0 rows, and the cause is identified: the predicate `{head['predicate']}` matches "
        f"NO row"
    )
    if len(findings) > 1:
        lead += f" (and so do {len(findings) - 1} other predicate(s))"
    parts = [lead + "."]
    for finding in findings:
        if finding.get("note"):
            parts.append(finding["note"])
        if finding.get("correction"):
            parts.append(
                f"Did you mean {_fmt(finding['correction']['value'])}? "
                f"({finding['correction']['match_basis']})"
            )
        if finding.get("sibling_hint"):
            parts.append(finding["sibling_hint"])
    if alive:
        parts.append(
            f"The other {len(alive)} WHERE predicate(s) each match rows on their own."
        )
    if joins:
        # Individual predicates can match rows while the join still yields no
        # result. These probes test one table each, so a suggested predicate
        # correction cannot promise that the joined query will return rows.
        parts.append(
            f"NOTE: this statement has {joins} join(s). Every probe above tests ONE "
            "table, so the joins themselves were NOT tested — correcting the predicate "
            "below may still return 0 rows if the join condition is what is empty."
        )
    if untested:
        parts.append(
            f"{len(untested)} predicate(s) were NOT tested — the {budget}-probe budget was "
            "spent first: " + "; ".join(c.text for c in untested[:4])
        )
    if not likely:
        inexact = [f for f in findings if unverified_text_match(f)]
        if inexact:
            parts.append(
                "Only an EXACT match was tested and the column's values were not fully "
                "listed, so this cannot tell a record that does not exist from one "
                "spelled differently (a longer legal name, a suffix like INC, a partial "
                f"id). A like '%…%' on {inexact[0]['predicate'].split('=')[0].strip()} "
                "would settle it."
            )
        else:
            parts.append(
                "This may still be the correct answer: a value that does not exist is a "
                "real result, not necessarily a mistake."
            )
    return {
        "verdict": "killing_predicate",
        "likely_mistake": likely,
        "message": " ".join(parts),
        "killing_predicates": findings,
        "satisfied_predicates": [c.text for c in alive],
        "untested_predicates": [c.text for c in untested],
        "skipped_predicates": [{"predicate": c.text, "reason": c.reason} for c in skipped],
        "retry_with": _retry_with(findings, joins=joins),
        "probes_used": len(probes),
        "probe_budget": budget,
        "probes_exhausted": bool(untested),
        "probes": probes,
    }


def _retry_with(
    findings: list[dict[str, Any]], *, joins: int = 0
) -> dict[str, Any] | None:
    """A RUNNABLE next call, not a template (error-envelope contract). One correction only —
    stacking two AST rewrites onto one statement is not verified here, and an
    unverified rewrite handed back as ``retry_with`` is the silent-wrong class
    this module exists to remove."""
    for finding in findings:
        if not finding.get("retry_sql"):
            continue
        if finding.get("suggested_action") == "drop_predicate":
            changed = f"removed the predicate `{finding['predicate']}`"
        else:
            changed = (
                f"{finding['column']}: {_fmt(finding.get('compared_to'))} -> "
                f"{_fmt(finding['correction']['value'])}"
            )
        out = {"sql": finding["retry_sql"], "changed": changed}
        if joins:
            out["scope"] = (
                "patches the NAMED predicate only. The join conditions were not tested "
                "(every probe is single-table), so this may still return 0 rows."
            )
        return out
    return None


def _genuinely_empty(
    alive: list[Conjunct],
    untested: list[Conjunct],
    skipped: list[Conjunct],
    joins: list[Any],
    has_having: bool,
    budget: int,
    probes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Every conjunct is individually satisfiable. Say THAT, as an answer."""
    tested = len(alive)
    if untested or (not alive and (untested or skipped)):
        verdict = "undetermined"
        message = (
            f"0 rows. {tested} predicate(s) were checked and each matches rows on its own, "
            f"but {len(untested) + len(skipped)} could not be checked"
            + (f" (budget {budget} probes)" if untested else "")
            + ", so the cause is not established. This may well be the correct answer."
        )
        likely = False
    else:
        verdict = "genuinely_empty"
        likely = False
        bits = [
            f"0 rows, and this is the correct answer: all {tested} predicate(s) match data "
            "individually, so none of them is a typo or an invented value — the "
            "COMBINATION simply has no rows."
        ]
        if joins:
            bits.append(
                f"With {len(joins)} join(s) in the statement, 'the combination' includes "
                "the join: no row satisfies the filters AND has a match on the other "
                "table(s)."
            )
        if has_having:
            bits.append("The HAVING threshold was checked and is not the cause.")
        message = " ".join(bits)
    return {
        "verdict": verdict,
        "likely_mistake": likely,
        "message": message,
        "killing_predicates": [],
        "satisfied_predicates": [c.text for c in alive],
        "untested_predicates": [c.text for c in untested],
        "skipped_predicates": [{"predicate": c.text, "reason": c.reason} for c in skipped],
        "probes_used": len(probes),
        "probe_budget": budget,
        "probes_exhausted": bool(untested),
        "probes": probes,
    }


def _without_having(sql: str) -> str | None:
    """The same statement with HAVING removed, projected to a bounded count."""
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:  # noqa: BLE001
        return None
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None or select.args.get("having") is None:
        return None
    select.set("having", None)
    inner = select.sql(dialect=DIALECT)
    return f"select top 5 count(*) as [n] from ({inner}) as [t]"

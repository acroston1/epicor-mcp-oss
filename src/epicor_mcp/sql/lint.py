'The free parse-lint (parse-lint policy) — run on the DS we already hold, before Execute.'

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "Finding",
    "Severity",
    "lint_parsed",
    "mask_sql",
    "top_level_subquery",
    "db_tables",
    "qualify_tables",
]


class Severity:
    """Two levels, because the caller does exactly two things with a finding."""

    REFUSE = "refuse"  #: never Execute — the answer would be wrong or unbounded
    WARN = "warn"  #: Execute, but the response must carry the caveat


@dataclass(frozen=True)
class Finding:
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


# --------------------------------------------------------------------------- #
# Masked raw text — literals and comments blanked, offsets preserved
# --------------------------------------------------------------------------- #

#: ONE left-to-right pass. The alternation order matters and so does the fact
#: that there is only one pass — see :func:`mask_sql`.
_MASKABLE = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)


def mask_sql(sql: str) -> str:
    """Blank string literals and comments, preserving length.

    **Masking must be a single pass.** This function
    used to substitute in three passes — block comments, then line comments,
    then literals — so a ``--`` *inside a string literal* was blanked as a
    comment **before** the literal masker ever saw it, taking the rest of the
    line with it::

        input : … where [P].[C] = 'FG--x' union all select … order by [P].[U] desc
        masked: … where [P].[C] = 'FG                                (rest blanked)

    Every rule that reads masked TEXT then went blind past the dash.
    In the failure case, the clean-literal version was
    REFUSED ``sql_silently_wrong`` (a set operation's ORDER BY is discarded by
    Epicor), and the byte-identical query with ``'FG--x'`` **executed and
    returned unsorted rows presented as a ranking**. A dash inside a literal is
    ordinary (``'REV--2025'``, ``like '%--%'``, any description field), so this
    was reachable without trying.

    Scanning once with the literal alternative FIRST is what makes precedence
    correct in both directions: a quote that opens before a dash consumes the
    dash, and a dash that opens before a quote consumes the quote.

    Deliberately duplicated from ``transpile.py`` and ``tests/harness/sql_lint``
    (independent-checker rule): the scorer must not share code with the thing it scores, and
    the runtime gate must not depend on the scorer. **Both copies carry this
    fix** — a divergence between them is a silent-wrong waiting to happen.
    """

    def blank(m: re.Match) -> str:
        text = m.group(0)
        width = m.end() - m.start()
        if text.startswith("'"):
            # Keep the quotes so the token still reads as a literal.
            return "'" + " " * (width - 2) + "'"
        return " " * width

    return _MASKABLE.sub(blank, sql)


#: DELETED: `_RE_TOP = re.compile(r"(?<![\w.])top\s*\(?\s*\d")`, a
#: whole-statement search whose result was compared against the TOP-LEVEL
#: subquery's `SelectListClause`. Mismatched scopes; see
#: :func:`outer_select_declares_top`. It is deleted rather than left unused so
#: the convenient-looking wrong tool is not sitting next to the right one.
#: A `select` keyword. Used to walk the OUTER selects only — see
#: :func:`outer_select_declares_top`.
_RE_SELECT_KW = re.compile(r"(?<![\w.])select(?![\w])", re.I)
#: A `top` clause sitting immediately at a select head (after an optional
#: `distinct`). Anchored at the head, so an inner subquery's `top` cannot match.
_RE_TOP_AT_HEAD = re.compile(r"\s*(?:distinct\s+)?top\s*\(?\s*\d", re.I)
#: Start of a projection list: `select`, optionally `distinct`, optionally a
#: `top` clause. What FOLLOWS is split on top-level commas by
#: :func:`_projection_has_star`, because a star may sit in ANY slot.
_RE_SELECT_HEAD = re.compile(
    r"(?<![\w.])select\s+(?:distinct\s+)?(?:top\s*\(?\s*\d+\s*\)?\s*(?:percent\s*)?)?",
    re.I,
)
#: One projection ITEM that is a star: `*` or `[P].*` / `P.*`. Anchored at both
#: ends, so `[A].[Qty] * [A].[Price]` (multiplication) can never match.
_RE_STAR_ITEM = re.compile(r"^\s*(?:\[?\w+\]?\s*\.\s*)?\*\s*$")
_RE_FROM = re.compile(r"(?<![\w.])from(?![\w])", re.I)
_RE_SETOP = re.compile(r"(?<![\w.])(union|intersect|except)(?![\w])", re.I)
_RE_ORDER_BY = re.compile(r"(?<![\w.])order\s+by(?![\w])", re.I)
_RE_AGG_CALL = re.compile(r"(?<![\w.])(count|sum|avg|min|max)\s*\(", re.I)
#: `count(distinct x)` — DISTINCT *inside* an aggregate, which is the shape
#: Epicor silently drops. A bare `select distinct` (and a `select distinct`
#: inside a derived table, which is SQL dialect policy's own CORRECT replacement for
#: `count(distinct)`) must NOT match: firing there refuses the recipe the tool
#: description tells the model to use.
_RE_DISTINCT_IN_AGG = re.compile(
    r"(?<![\w.])(count|sum|avg|min|max)\s*\(\s*distinct(?![\w])", re.I
)


# --------------------------------------------------------------------------- #
# DS accessors — every one tolerates a missing array
# --------------------------------------------------------------------------- #


def _projection_items(masked: str, start: int) -> list[str]:
    """Split one SELECT's projection list into items, at paren/bracket depth 0.

    Stops at the statement's own ``from`` (or the closing paren of the subquery
    it lives in). Depth tracking is what keeps a scalar subquery's ``from`` and a
    ``count(*)``'s parentheses from ending the list early.
    """
    items: list[str] = []
    current: list[str] = []
    depth = bracket = 0
    i, n = start, len(masked)
    while i < n:
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif ch == "," and depth == 0 and bracket == 0:
            items.append("".join(current))
            current = []
            i += 1
            continue
        elif depth == 0 and bracket == 0 and _RE_FROM.match(masked, i):
            break
        current.append(ch)
        i += 1
    items.append("".join(current))
    return items


def star_projection_item(masked: str) -> str | None:
    """The first ``*`` / ``[T].*`` PROJECTION ITEM in *masked*, or ``None``.

    Epicor behavior: the rule this replaces was a single regex anchored to the FIRST
    projection slot, so ``select [P].[PartNum], [P].*`` was invisible to it.
    That shape can expose every column past the only ``select *`` gate the
    wedge has: ``WEDGE_POLICY.refuse_select_star`` is False so that this rule
    can serve the permitted column list back as the recovery.

    Items are split at depth 0, so ``count(*)`` is not a star (its ``*`` is
    inside parens and the item is ``count(*)``, which does not match), and
    ``[A].[Qty] * [A].[Price]`` is not a star (the item has operands on both
    sides of the ``*`` and :data:`_RE_STAR_ITEM` is anchored).
    """
    for head in _RE_SELECT_HEAD.finditer(masked):
        for item in _projection_items(masked, head.end()):
            if _RE_STAR_ITEM.match(item):
                return item.strip()
    return None


def outer_select_declares_top(masked: str) -> bool:
    """Does an **OUTER** select in *masked* declare a ``top``?

    Rule 1 compares this against ``top_level_subquery(ds)["SelectListClause"]``,
    which describes the TOP-LEVEL select and nothing else. The two sides must
    therefore be scoped the same way. They were not: the old test was
    a bare ``top`` regex over the WHOLE statement, so a ``top`` on any
    inner select — a derived table, a CTE body, an ``in (select …)``, a UNION
    branch — was compared against a top-level clause that legitimately reads
    ``'All'``, and the statement was REFUSED as a dropped row bound.

    All four of these are legal (three of them run and return correct rows),
    and the whole-statement test REFUSED all four:

    * ``select [t].[x] from (select top 10 … ) as [t]``            — no union at all
    * ``select … where [P].[PartNum] in (select top 5 … )``
    * ``select … from (<union with top on each branch>) as [w] group by …``
    * ``with [w] as (<union with top on each branch>) select … from [w]``

    The last two are how the model gets *out* of a set-operation refusal, so the
    false positive closed the only exits and made the refusal a LOOP.

    "Outer" means paren depth 0, which is exactly the set of selects the
    top-level subquery covers: every derived table, CTE body, scalar subquery
    and ``in (select …)`` is parenthesised by construction. A **bare** set
    operation has all of its branches at depth 0 and any of them may carry the
    ``top`` Epicor then drops, so ANY depth-0 select counts — the bare
    union with the ``top`` on its SECOND branch parses to ``'All'``/``0.0`` and
    is still correctly refused.

    Bracket depth is tracked as well, so a column literally named ``[select]``
    or ``[top 5]`` cannot open a phantom select head.

    Scoping loses no true positive from the inner-``top`` class, because the
    transpiler already normalises ``top (N)`` -> ``TOP N`` on EVERY select and
    refuses ``top 0`` anywhere, both verified against this same statement set
    before the lint ever runs.
    """
    depth = bracket = 0
    i, n = 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif depth == 0 and bracket == 0:
            m = _RE_SELECT_KW.match(masked, i)
            if m:
                if _RE_TOP_AT_HEAD.match(masked, m.end()):
                    return True
                i = m.end()
                continue
        i += 1
    return False


def _rows(ds: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    rows = ds.get(name) or ds.get(f"{name}Designer") or []
    return [r for r in rows if isinstance(r, Mapping)]


def top_level_subquery(ds: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``TopLevel`` QuerySubQuery row, or the first one, or ``{}``."""
    subs = _rows(ds, "QuerySubQuery")
    for s in subs:
        if s.get("Type") == "TopLevel":
            return s
    return subs[0] if subs else {}


def db_tables(ds: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """``QueryTable`` rows Epicor resolved to a real DB table (resolved-table contract).

    ``SQ`` (derived table / CTE) rows carry a GUID and blank schema; ``TT``
    (calculated) rows carry both blank. Neither is attributable to a BO, and the
    base tables of a CTE appear as their own ``DB`` rows, so filtering to ``DB``
    loses nothing.
    """
    return [t for t in _rows(ds, "QueryTable") if t.get("TableType") == "DB"]


def _table_type_by_id(ds: Mapping[str, Any]) -> dict[str, str]:
    return {t.get("TableID"): t.get("TableType") for t in _rows(ds, "QueryTable")}


# --------------------------------------------------------------------------- #
# The schema catalogue — the RECOVERY for an unresolved table, loaded lazily
# --------------------------------------------------------------------------- #





_CATALOGUE_PATH = Path("data/schema_catalogue.json")

#: Override for a deployment whose catalogue lives elsewhere (and the seam the
#: tests use to prove BOTH the present and the absent path).
_CATALOGUE_ENV = "EPICOR_MCP_SCHEMA_CATALOGUE"


@dataclass(frozen=True)
class _SchemaIndex:
    '``bare table name -> (schema, display name, physical column set)``.'

    schemas: Mapping[str, str]
    display: Mapping[str, str]
    columns: Mapping[str, frozenset[str]]
    authoritative: bool
    generated: str = ""

    def knows(self, table: str) -> bool:
        return table.rsplit(".", 1)[-1].lower() in self.schemas

    def schema_of(self, table: str) -> str:
        return self.schemas.get(table.rsplit(".", 1)[-1].lower(), "")

    def display_of(self, table: str) -> str:
        bare = table.rsplit(".", 1)[-1]
        return self.display.get(bare.lower(), bare)

    def full_name(self, table: str) -> str:
        schema = self.schema_of(table)
        return f"{schema}.{self.display_of(table)}" if schema else ""

    def has_column(self, table: str, column: str) -> bool | None:
        """``True``/``False``, or ``None`` when this index cannot judge."""
        cols = self.columns.get(table.rsplit(".", 1)[-1].lower())
        if not cols:
            return None
        return column.lower() in cols

    def close_names(self, table: str) -> list[str]:
        if not self.authoritative:
            return []
        bare = table.rsplit(".", 1)[-1].lower()
        hits = difflib.get_close_matches(bare, list(self.schemas), n=3, cutoff=0.8)
        return [f"{self.schemas[h]}.{self.display[h]}" for h in hits]


def _card_index() -> _SchemaIndex:
    """The 30-table card as a NON-authoritative index (static-table-card contract)."""
    from epicor_mcp.sql.card import CARD_SCHEMA

    return _SchemaIndex(
        schemas={t.lower(): s for t, s in CARD_SCHEMA.items()},
        display={t.lower(): t for t in CARD_SCHEMA},
        columns={},
        authoritative=False,
    )


@lru_cache(maxsize=4)
def _load_schema_index(signature: tuple[str, float] | None) -> _SchemaIndex:
    """Build the index once per (path, mtime). Never raises."""
    if signature is None:
        return _card_index()
    try:
        from epicor_mcp.sql.validate_columns import _physical_tables

        raw = json.loads(Path(signature[0]).read_text())
        tables = _physical_tables(raw)
        schemas: dict[str, str] = {}
        display: dict[str, str] = {}
        columns: dict[str, frozenset[str]] = {}
        for name, meta in tables.items():
            if not isinstance(meta, Mapping):
                continue
            bare = str(name).rsplit(".", 1)[-1]
            key = bare.lower()
            schemas[key] = str(meta.get("schema") or "")
            display[key] = bare
            fields = meta.get("fields") or []
            names = {
                str(f.get("name")).lower()
                for f in fields
                if isinstance(f, Mapping) and f.get("name")
            }
            if names:
                columns[key] = frozenset(names)
        if not schemas:
            return _card_index()
        return _SchemaIndex(
            schemas=schemas,
            display=display,
            columns=columns,
            authoritative=True,
            generated=str(raw.get("generated") or ""),
        )
    except Exception:  # noqa: BLE001 - a missing/bad catalogue must never refuse differently
        logger.warning("schema catalogue: could not read %s", signature[0], exc_info=True)
        return _card_index()


def _schema_index() -> _SchemaIndex:
    """The catalogue if it is on disk, else the card. Lazy, cached, silent.

    Loaded **only on the error path** (a table that did not resolve), so the
    happy path never pays the catalogue parse. The card fallback is what keeps this
    working on a clean checkout, where the catalogue is gitignored.
    """
    override = os.environ.get(_CATALOGUE_ENV)
    path = (Path(override) if override else _CATALOGUE_PATH).resolve()
    try:
        signature: tuple[str, float] | None = (str(path), path.stat().st_mtime)
    except OSError:
        signature = None
    return _load_schema_index(signature)


# --------------------------------------------------------------------------- #
# The lint
# --------------------------------------------------------------------------- #


def lint_parsed(
    sql: str, ds: Mapping[str, Any], *, fanout_warning: bool = False
) -> list[Finding]:
    """Return every parse-lint policy finding for *sql* and its parsed DS.

    Never raises: a malformed DS yields no findings rather than taking the
    request down, because the governor and the deny-list are the safety
    controls and this one is a correctness control.

    ``fanout_warning`` re-enables the key-BLIND fan-out warning, which is OFF by
    default — see :func:`_fanout_findings`.
    """
    try:
        return _lint(sql, ds, fanout_warning=fanout_warning)
    except Exception as exc:  # noqa: BLE001 - a lint bug must never 500 the tool
        out = [
            Finding(
                "lint_unavailable",
                Severity.WARN,
                "The pre-execution lint could not run, so silent-wrong shapes were NOT "
                f"checked for this statement ({type(exc).__name__}). The row bound and the "
                "authorization gate are unaffected.",
                evidence="fail-open by design: parse-lint policy is a correctness control, not a "
                "safety control; cost-governor policy and denylist policy are the safety controls and they fail closed",
            )
        ]
        # ...with ONE exception. `select *` is a COST control as much as a
        # correctness one: unprojected pages can exceed response-size limits,
        # and under WEDGE_POLICY this rule is the only gate on it. So it is
        # re-run here on masked text alone — no DS, nothing that can fail the
        # way the main pass just did — and it REFUSES. A control whose only
        # instance fails open on a bug is not a control.
        try:
            star = star_projection_item(mask_sql(sql))
        except Exception:  # noqa: BLE001
            star = None
        if star:
            out.append(
                Finding(
                    "select_star",
                    Severity.REFUSE,
                    f"`select {star}` is refused — name the columns. (The lint's other rules "
                    "could not run on this statement, so the real column list is not "
                    "available; this refusal stands on the text alone.)",
                    evidence='Engine compatibility behavior; validate against the configured Epicor version and local data.',
                    detail={"item": star, "degraded": True},
                )
            )
        return out


def _lint(sql: str, ds: Mapping[str, Any], *, fanout_warning: bool = False) -> list[Finding]:
    masked = mask_sql(sql)
    out: list[Finding] = []
    top = top_level_subquery(ds)
    # SCOPED TO THE OUTER SELECT, because `clause` describes only the outer
    # select. See :func:`outer_select_declares_top` for the four legal shapes
    # the whole-statement regex used to refuse.
    has_top = outer_select_declares_top(masked)
    clause = top.get("SelectListClause")

    # --- 1. the row bound Epicor dropped ---------------------------------
    if has_top and clause != "Top":























        setop = _RE_SETOP.search(masked)
        if setop:
            message = (
                f"The `top` on this {setop.group(1).upper()} is SILENTLY DISCARDED — "
                f"Epicor compiled the statement to NO LIMIT (SelectListClause={clause!r}), "
                "so the bound you wrote does not exist and the rows come back capped only "
                "by PageSize. Re-writing the `top` will NOT fix it: the set operation is "
                "the cause. Put the set operation in a CTE and bound the select that reads "
                "it — `with [u] as (<your set operation>) select top 100 [u].[Col] as "
                "[Col] from [u]`. Name the columns; do NOT write `[u].*`, which is refused "
                "separately. A CTE is also the fix for an ORDER BY on a set operation. Do "
                "NOT wrap the set operation in a derived table (`from (<your union>) as "
                "[w]`) — Epicor ignores the `top` on that wrapper too."
            )
            evidence = (
                'Engine compatibility behavior; validate against the configured Epicor version and local data.'
            )
        else:
            message = (
                "The statement declares a `top` but Epicor compiled it to NO LIMIT "
                f"(SelectListClause={clause!r}). Write `top 100` — never `top (100)`, "
                "`top 0` or `top N percent`."
            )
            evidence = (
                'Engine compatibility behavior; validate against the configured Epicor version and local data.'
            )
        out.append(
            Finding(
                "row_bound_dropped",
                Severity.REFUSE,
                message,
                evidence=evidence,
                detail={
                    "select_list_clause": clause,
                    "set_operation": setop.group(1).lower() if setop else None,
                },
            )
        )
    elif has_top and top.get("TopInPercent"):
        out.append(
            Finding(
                "top_percent",
                Severity.REFUSE,
                "`top N percent` is not a row bound in Epicor — it returns the whole "
                "PageSize window. Use `top N`.",
                evidence="Epicor behavior: `top 5 percent` returned the full PageSize window",
                detail={"top_row_expr": top.get("TopRowExpr")},
            )
        )
    elif has_top and clause == "Top" and not top.get("TopRowExpr"):
        out.append(
            Finding(
                "top_zero",
                Severity.REFUSE,
                "`top 0` means UNLIMITED in Epicor, not zero rows. With an `order by` it "
                "is a hard run failure instead. Use `top N` with N >= 1.",
                evidence="Without ORDER BY -> unbounded (the full PageSize "
                "window); with ORDER BY -> a hard run failure",
                detail={"top_row_expr": top.get("TopRowExpr")},
            )
        )

    # --- 2. select * ------------------------------------------------------
    # parse-lint policy reads a bare `SelectListClause == 'All'` as `select *`. That
    # proxy is wrong in BOTH directions:
    #   * `select count(distinct [OD].[PartNum]) as [N] from Erp.OrderDtl` parses
    #     to 'All' with no star anywhere            -> false POSITIVE
    #   * `select top 200 [P].* from Erp.Part`      -> 'Top', a QueryField row per column
    #     ...which is what the row-bound injection produces, so the signal is
    #     blind to exactly the shape that reaches Epicor -> false NEGATIVE
    # The star in the masked projection is the actual signal; the DS supplies
    # the recovery (the real column list), which is what parse-lint policy asks for.
    # A star in ANY projection slot, not just the first.
    star = star_projection_item(masked)
    if star:
        cols = [
            f.get("DBFieldName")
            for f in _rows(ds, "QueryField")
            if f.get("DBFieldName")
        ]
        out.append(
            Finding(
                "select_star",
                Severity.REFUSE,
                f"`select {star}` is refused — name the columns. Epicor expanded it to "
                f"{len(cols)} columns; wide pages can exceed response-size limits "
                "and add transport overhead.",
                evidence='Engine compatibility behavior; validate against the configured Epicor version and local data.',
                detail={"columns": cols[:400], "column_count": len(cols), "item": star},
            )
        )

    # --- 3. sorts Epicor will discard or mis-resolve -----------------------
    tabtype = _table_type_by_id(ds)
    fields = _rows(ds, "QueryField")
    invented = {
        f.get("FieldName")
        for f in fields
        if tabtype.get(f.get("TableID")) != "DB" and f.get("FieldName")
    }
    real = {(f.get("TableID"), f.get("DBFieldName")) for f in fields if f.get("DataType")}
    for s in _rows(ds, "QuerySortBy"):
        tid, fname = s.get("TableID"), s.get("FieldName") or ""
        if not tid:
            if fname.isdigit():
                out.append(
                    Finding(
                        "sort_ordinal",
                        Severity.REFUSE,
                        f"`order by {fname}` is a positional sort. Epicor accepts it and "
                        "then IGNORES it — the rows come back unsorted with no error. "
                        "Repeat the column or the expression.",
                        evidence="Epicor behavior: `order by 2 desc` and "
                        "`order by 2 asc` returned byte-identical rows, identical to no "
                        "ORDER BY at all",
                        detail={"ordinal": fname},
                    )
                )
        elif tabtype.get(tid) == "DB" and fname in invented and (tid, fname) not in real:
            out.append(
                Finding(
                    "sort_invented_alias",
                    Severity.REFUSE,
                    f"`order by {fname}` sorts by a SELECT output alias. Epicor resolves a "
                    "bare sort name against SOURCE COLUMNS only, so this fails at run time "
                    "with `Invalid column name`. Repeat the expression instead.",
                    evidence="Epicor behavior: the alias form fails "
                    "with `Invalid column name '<alias>'.`; the repeated expression returns "
                    "the independently known answer",
                    detail={"alias": fname, "table_id": tid},
                )
            )

    # --- 4. a TABLE that did not resolve, and a column that does not exist --
    # These two are ONE rule because Epicor reports them identically, and
    # telling them apart is the point of the rule. See `_resolution_findings`.
    out.extend(_resolution_findings(ds, tabtype, fields))

    # --- 5. DISTINCT silently dropped inside an aggregate ------------------
    if _RE_DISTINCT_IN_AGG.search(masked):
        for f in fields:
            if not f.get("IsCalculated"):
                continue
            formula = (f.get("Formula") or "").lower()
            if not _RE_AGG_CALL.search(formula):
                continue
            if "distinct" not in formula:
                out.append(
                    Finding(
                        "distinct_dropped_in_aggregate",
                        Severity.REFUSE,
                        "The DISTINCT inside the aggregate was DROPPED by Epicor's parser: "
                        f"`{f.get('Formula')}`. The number returned would be too large, "
                        "with an EMPTY Errors array. Use a `select distinct` derived table "
                        "and count(*) over it.",
                        evidence='Engine compatibility behavior; validate against the configured Epicor version and local data.',
                        detail={"formula": f.get("Formula"), "alias": f.get("FieldName")},
                    )
                )
                break

    # --- 6. a set operation's ORDER BY, silently discarded -----------------
    # A parsed dataset with no sort rows did not retain the requested ordering.
    if (
        _RE_SETOP.search(masked)
        and _RE_ORDER_BY.search(masked)
        and not _rows(ds, "QuerySortBy")
    ):
        out.append(
            Finding(
                "setop_order_by_discarded",
                Severity.REFUSE,
                "The ORDER BY on this UNION / INTERSECT / EXCEPT is SILENTLY DISCARDED — "
                "the parsed query carries no sort at all, so the rows come back in an "
                "arbitrary order that LOOKS ranked. Qualifying the sort key does not fix "
                "it. Wrap the set operation in a derived table and sort the wrapper.",
                evidence="Epicor ordering behavior: `desc` and `asc` "
                "returned byte-identical rows and the parsed DS carried QuerySortBy == []. "
                "successful execution does not establish that the sort was applied",
                detail={"sort_rows": 0},
            )
        )

    # --- 7. fan-out: an aggregate over the many side of >= 2 joins ---------
    # OFF BY DEFAULT because this broad heuristic can over-report. See `_fanout_findings`.
    if fanout_warning:
        out.extend(_fanout_findings(ds))
    return out


# --------------------------------------------------------------------------- #
# Rule 4: an unresolved TABLE vs a genuine phantom COLUMN
# --------------------------------------------------------------------------- #

#: Epicor's own run-time message for a table it could not resolve. Quoted in the
#: refusal because it is what the caller would otherwise have paid a round trip
#: (and a `table_not_accessible`) to read.
_INACCESSIBLE = "References to inaccessible tables detected"


def _resolution_findings(
    ds: Mapping[str, Any],
    tabtype: Mapping[str, Any],
    fields: Sequence[Mapping[str, Any]],
) -> list[Finding]:
    """Split *"this column does not exist"* from *"this TABLE did not resolve"*.

    **Why.** ``select top 10 JobNum, PartNum, ProdQty from JobHead`` — one
    missing ``Erp.`` — must not be refused as three phantom COLUMNS naming
    ``JobHead.JobNum``, ``JobHead.PartNum`` and ``JobHead.ProdQty`` as things
    that do not exist. All three are real. Epicor blanks ``DataType`` on
    **every** column of a table it could not resolve, exactly as it does for one
    genuine OData-only phantom, so the naive rule — ``TableType == 'DB' and not
    DataType`` — cannot tell the two apart and blames the caller's own correct
    columns.

    The discriminators, in the order they are trusted:

    1. **``DBSchemaName == ''``** on the ``QueryTable`` row. This is *Epicor's
       own resolution*, not a reading of the SQL text, and it is exact: the
       Execute error prints the empty schema as a leading dot — ``table
       '.JobHead' with alias 'J'``. Across the captured fixtures, exactly
       the unresolved-table ones carry it. It is read off the
       ``QueryTable`` row, so a table that resolves nothing and projects no
       column (a join partner) is caught too.
    2. **Every column of the table blank, and the catalogue disagrees with the
       schema Epicor echoed.** ``Erp.NoSuchTableXyz`` and ``Ice.JobHead`` both
       parse with their schema intact and every ``DataType`` blank, and both die
       at Execute with the same ``inaccessible tables`` 400 — the DS alone cannot
       see it, so this tier asks the catalogue whether the table exists under
       that schema.
    3. Otherwise the table resolved and the blanks are genuine phantoms:
       ``unknown_column``, unchanged. ``select [P].[OnHandQty] from
       Erp.Part as [P]`` is all-blank *and* correct to report as a column — which
       is why "all blank ⇒ the table failed" is not sufficient on its own.

    **This rule never enumerates a table's columns.** It judges only the columns
    the caller already wrote, so it cannot become the pre-authorization schema
    schema leak deny filtering prevents — and it runs after the deny-list anyway.
    """
    meta: dict[Any, Mapping[str, Any]] = {}
    for t in db_tables(ds):
        tid = t.get("TableID")
        if tid is not None and tid not in meta:
            meta[tid] = t

    blanks_by_table: dict[Any, list[Mapping[str, Any]]] = {}
    total_by_table: dict[Any, int] = {}
    for f in fields:
        tid = f.get("TableID")
        if tabtype.get(tid) != "DB":
            continue
        total_by_table[tid] = total_by_table.get(tid, 0) + 1
        if not f.get("DataType"):
            blanks_by_table.setdefault(tid, []).append(f)

    index: _SchemaIndex | None = None
    unresolved: dict[Any, Finding] = {}

    for tid, row in meta.items():
        name = str(row.get("DBTableName") or "")
        if not name:
            continue  # the deny-list already refuses an unattributable DB row
        schema = str(row.get("DBSchemaName") or "").strip()
        blanks = blanks_by_table.get(tid, [])
        total = total_by_table.get(tid, 0)
        if total > len(blanks):
            # ONE typed column proves the table resolved, whatever the schema
            # says. This guard is what keeps a rule that REFUSES from ever
            # firing on a statement that would have returned rows (unknown source handling
            # — a detector that false-alarms on a correct answer is turned off,
            # not shipped with a caveat).
            continue
        all_blank = bool(blanks)
        if schema and not all_blank:
            continue  # tier 3: the table resolved
        if index is None:
            index = _schema_index()
        finding = _unresolved_table_finding(index, tid, name, schema, blanks)
        if finding is not None:
            unresolved[tid] = finding

    out = list(unresolved.values())
    for f in fields:
        tid = f.get("TableID")
        if tabtype.get(tid) != "DB" or f.get("DataType") or tid in unresolved:
            continue
        out.append(
            Finding(
                "unknown_column",
                Severity.REFUSE,
                f"`{f.get('DBTableName')}.{f.get('DBFieldName')}` does not exist at the "
                "SQL layer. Epicor's parser accepted it and it would have FAILED at run "
                "time with `Bad SQL statement.` — it would not have returned rows.",
                # The old wording offered "…or returned rows with the column
                # silently empty" as an alternative outcome. An unselectable physical
                # column causes execution failure; a successful parse alone
                # must not be described as evidence that the column exists.
                evidence="Epicor behavior: ParseFromSQL returns DataType == '' for a "
                "column that is not SQL-selectable, such as an OData-only projection; "
                "execution then fails instead of returning an empty value",
                detail={
                    "table": f.get("DBTableName"),
                    "column": f.get("DBFieldName"),
                },
            )
        )
    return out


def _allowed_suggestions(names: "list[str]") -> "list[str]":
    """Drop deny-listed tables from anything we are about to RECOMMEND.

    **The gate belongs on the served side.** :func:`_suggestion_allowed`
    tests only the name the caller WROTE; the names being *served* —
    ``index.full_name()`` and ``index.close_names()`` — are a separate channel.
    Left unfiltered, a one-character typo (``Ice.UserFil``) removes the premise of
    that check and the server hands back the reachable spelling of a hard-denied
    table under its own message calling that table "denied to every user,
    including an Epicor Security Manager."

    Gating the suggestion rather than the input is the invariant: **never name a
    table in a recovery that the caller is not allowed to read**, regardless of
    what they typed to get here.
    """
    from epicor_mcp.sql.denylist import is_denied_table

    try:
        return [n for n in names if n and not is_denied_table(n)]
    except Exception:  # noqa: BLE001 - never let this path change the refusal
        return []


def _suggestion_allowed(*names: str) -> bool:
    """False when the table is deny-listed, so no RECOVERY is served for it.

    The refusal itself still fires — this only withholds *"write `Erp.UserFile`"*
    and the column clean bill. The `deny_ice_userfile` fixture
    (`from Ice.UserFile`) resolves to a blank `DataType` exactly like any other
    unresolved table, and the catalogue's answer for it is `Erp.UserFile`. The
    deny-list refuses that statement one stage EARLIER by the pipeline's gate order, so
    this is unreachable in the pipe — but a correctness control that hands back
    the reachable spelling of a hard-denied table is one reordering away from
    being the schema leak deny filtering prevents. Fail closed.
    """
    from epicor_mcp.sql.denylist import is_denied_table

    try:
        return not any(is_denied_table(n) for n in names if n)
    except Exception:  # noqa: BLE001 - never let this path change the refusal
        return False


def _unresolved_table_finding(
    index: _SchemaIndex,
    table_id: Any,
    name: str,
    schema: str,
    blanks: Sequence[Mapping[str, Any]],
) -> Finding | None:
    """One ``unknown_table`` finding, or ``None`` when the table is fine.

    ``None`` means *"nothing here proves the table failed"* and hands the columns
    back to the phantom rule — the conservative direction, and the one a clean
    checkout with no catalogue takes for every off-card table.
    """
    written = f"{schema}.{name}" if schema else name
    serve = _suggestion_allowed(name, written)
    # Gate the SUGGESTION, not just the input — see _allowed_suggestions.
    correct = ""
    if serve:
        correct = (_allowed_suggestions([index.full_name(name)]) or [""])[0]
    columns = [str(f.get("DBFieldName") or "") for f in blanks]
    detail: dict[str, Any] = {
        "table": name,
        "alias": table_id if table_id != name else None,
        "written_as": written,
        "schema_returned": schema,
        "columns_reported_blank": columns,
        "catalogue": "schema_catalogue" if index.authoritative else "card",
    }
    if index.generated:
        detail["catalogue_generated"] = index.generated
    if not serve:
        detail["recovery_withheld"] = "deny_listed"

    # The columns the caller wrote, judged against the table they MEANT. Only
    # ever a positive clean bill — a name missing from a dated catalogue is not
    # proof of a phantom, and this refusal is about the table either way.
    if correct and columns:
        verdicts = [index.has_column(name, c) for c in columns]
        if verdicts and all(v is True for v in verdicts):
            detail["columns_verified_on"] = correct

    if not schema:
        # TIER 1 — Epicor itself resolved no schema. Certain.
        if correct:
            fix = f"Write `{correct}`."
        elif not serve:
            fix = "Prefix it with its schema."
        elif index.authoritative:
            near = _allowed_suggestions(index.close_names(name))
            detail["did_you_mean"] = near
            fix = (
                f"There is also no table called `{name}` in the catalogue"
                + (f" — did you mean {', '.join(f'`{n}`' for n in near)}?" if near else ".")
            )
        else:
            fix = (
                f"Prefix it with its schema — `Erp.{name}` for a business table, "
                f"`Ice.{name}` for a framework one."
            )
        verified = detail.get("columns_verified_on")
        if not columns:
            cleared = ""
        elif verified:
            cleared = (
                f" The {len(columns)} column(s) you named are NOT the problem — all of "
                f"them exist on `{verified}`."
            )
        else:
            cleared = (
                f" The {len(columns)} column(s) you named are NOT the problem: Epicor "
                "blanks the DataType of every column of a table it could not resolve, "
                "which is what made them look like phantoms."
            )
        return Finding(
            "unknown_table",
            Severity.REFUSE,
            f"`{name}` has no schema prefix, so Epicor resolved it to `.{name}` — an "
            f"empty schema — and Execute would have failed with `{_INACCESSIBLE}: "
            f"subquery 'Main', table '.{name}'`. " + fix + cleared,
            evidence="Epicor behaviour: `from JobHead` parses with "
            "DBSchemaName == '' and DataType == '' on all three of JobNum, PartNum and "
            "ProdQty — all REAL columns — and Execute 400s with `References to "
            "inaccessible tables detected: subquery 'Main', table '.JobHead' with alias "
            "'J'`; the identical statement as `from Erp.JobHead` parses with "
            "DBSchemaName == 'Erp', typed columns, and returns rows",
            detail={**detail, "reason": "missing_schema_prefix", "write": correct or None},
        )

    # TIER 2 — the schema is there but nothing under it resolved. Only the
    # catalogue can see this; the card cannot, because absence from 30 curated
    # tables proves nothing.
    if not index.authoritative:
        return None
    if index.knows(name):
        if not serve:
            # The table exists and is deny-listed: say the statement cannot run
            # as written and stop. The deny-list owns WHY, one stage earlier.
            return Finding(
                "unknown_table",
                Severity.REFUSE,
                f"`{written}` did not resolve — Epicor blanked the DataType of every "
                f"column under it, and Execute would have failed with `{_INACCESSIBLE}: "
                f"subquery 'Main', table '{written}'`.",
                evidence="Epicor behaviour: a table Epicor cannot resolve returns "
                "every column with DataType == '' — identical to a genuine phantom column "
                "— and dies at Execute with `References to inaccessible tables detected`",
                detail={**detail, "reason": "unresolved_table", "write": None},
            )
        if correct and correct.lower() != written.lower():
            return Finding(
                "unknown_table",
                Severity.REFUSE,
                f"`{written}` does not exist: `{index.display_of(name)}` lives in schema "
                f"`{index.schema_of(name)}`, not `{schema}`. Epicor accepted the "
                f"parse and Execute would have failed with `{_INACCESSIBLE}: subquery "
                f"'Main', table '{written}'`. Write `{correct}`."
                + (
                    f" The {len(columns)} column(s) you named all exist on `{correct}`."
                    if detail.get("columns_verified_on") else ""
                ),
                evidence="Epicor behaviour: `from Ice.JobHead` parses with "
                "DBSchemaName == 'Ice' and a blank DataType on the real column JobNum, and "
                "Execute 400s with `References to inaccessible tables detected: subquery "
                "'Main', table 'Ice.JobHead' with alias 'J'`",
                detail={**detail, "reason": "wrong_schema", "write": correct},
            )
        return None  # right schema, real table -> the blanks are real phantoms
    near = _allowed_suggestions(index.close_names(name))
    return Finding(
        "unknown_table",
        Severity.REFUSE,
        f"`{written}` is not a table in this database — it is not one of the "
        f"{len(index.schemas):,} in Epicor's own catalogue. The parse succeeded anyway; "
        f"Execute would have failed with `{_INACCESSIBLE}: subquery 'Main', table "
        f"'{written}'`."
        + (f" Did you mean {', '.join(f'`{n}`' for n in near)}?" if near else ""),
        evidence="Epicor behaviour: `from Erp.NoSuchTableXyz` parses with "
        "DBSchemaName == 'Erp' and a blank DataType on every column — indistinguishable in "
        "the DS from a real table whose only projected column is a phantom — and Execute "
        "400s with `References to inaccessible tables detected: subquery 'Main', table "
        "'Erp.NoSuchTableXyz' with alias 'X'`",
        detail={
            **detail,
            "reason": "unknown_table_name",
            "write": None,
            **({"did_you_mean": near} if near else {}),
        },
    )


#: A table reference in a FROM / JOIN slot: `from JobHead`, `from [JobHead]`,
#: `join Erp.Part`. Anchored on the keyword so a column called `from_date` or a
#: table name appearing in the projection is never touched.
_RE_FROM_SOURCE = re.compile(
    r"(?P<kw>\b(?:from|join)\s+)"
    r"(?P<src>(?:\[[^\]]+\]|[A-Za-z_][\w$#@]*)"
    r"(?:\s*\.\s*(?:\[[^\]]+\]|[A-Za-z_][\w$#@]*))*)",
    re.I,
)


def qualify_tables(sql: str, corrections: Mapping[str, str]) -> str | None:
    """Rewrite unqualified/mis-qualified FROM-JOIN sources; ``None`` if nothing hit.

    ``corrections`` is ``{bare or written name: 'Erp.JobHead'}``. The scan runs on
    the MASKED text — literals and comments blanked, **offsets preserved** — and
    splices by index into the raw string, so a table name that also appears
    inside a string literal or a comment can never be rewritten. That is the same
    reason the `top`/`select *` rules are masked-text rules (masked-SQL lint policy).

    It exists so the refusal can hand back a RUNNABLE statement rather than a
    template. The caller can copy ``retry_with.sql`` without reconstructing a
    schema qualification from prose.
    """
    if not corrections:
        return None
    wanted = {k.rsplit(".", 1)[-1].lower(): v for k, v in corrections.items() if v}
    masked = mask_sql(sql)
    edits: list[tuple[int, int, str]] = []
    for m in _RE_FROM_SOURCE.finditer(masked):
        src = m.group("src")
        bare = re.sub(r"[\[\]]", "", src.split(".")[-1]).strip().lower()
        replacement = wanted.get(bare)
        if not replacement:
            continue
        if src.replace(" ", "").lower() == replacement.lower():
            continue
        edits.append((m.start("src"), m.end("src"), replacement))
    if not edits:
        return None
    out = sql
    for start, end, replacement in reversed(edits):
        out = out[:start] + replacement + out[end:]
    return out if out != sql else None


def _fanout_findings(ds: Mapping[str, Any]) -> list[Finding]:
    'WARN when an aggregate reads a table that sits under >= 2 joins.'
    relations = _rows(ds, "QueryRelation")
    if len(relations) < 2:
        return []
    tables = {t.get("TableID"): t for t in _rows(ds, "QueryTable")}
    # A table on the "many" side of two or more joins: it is the CHILD of one
    # relation while the parent participates in another.
    child_of = [r.get("ChildTableID") for r in relations]
    parents = {r.get("ParentTableID") for r in relations}
    multi_parent = {p for p in parents if child_of.count(p) == 0}
    if not multi_parent:
        return []
    fanned = [
        tid
        for tid in child_of
        if any(r.get("ParentTableID") in multi_parent for r in relations
               if r.get("ChildTableID") == tid)
    ]
    if len(set(fanned)) < 2:
        return []
    agg_tables: set[str] = set()
    for f in _rows(ds, "QueryField"):
        if not f.get("IsCalculated"):
            continue
        formula = f.get("Formula") or ""
        if not _RE_AGG_CALL.search(formula):
            continue
        for tid in set(fanned):
            if re.search(rf"(?<![\w.]){re.escape(str(tid))}\s*\.", formula):
                agg_tables.add(str(tid))
    if not agg_tables:
        return []
    named = sorted(
        {str(tables.get(t, {}).get("DBTableName") or t) for t in agg_tables}
    )
    return [
        Finding(
            "aggregate_fanout",
            Severity.WARN,
            "GRAIN WARNING: this statement aggregates "
            f"{', '.join(named)} while the same parent is joined to more than one child "
            "table. Each extra child MULTIPLIES the rows of the others, so the sum is "
            "several times too large — with no error. Aggregate one child per query, or "
            "aggregate the child in a derived table first and join that.",
            evidence="This heuristic detects only some aggregate fan-out shapes; absence "
            "of this warning does not prove that every join preserves the requested grain",
            detail={"aggregated_tables": named},
        )
    ]


def refusals(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == Severity.REFUSE]


def warnings(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == Severity.WARN]


def findings_to_dicts(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    return [f.to_dict() for f in findings]

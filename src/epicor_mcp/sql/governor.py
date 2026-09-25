'The cost governor (cost-governor policy) — launch-blocking, and it fails closed.'

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from epicor_mcp.sql.envelope import error_envelope

__all__ = [
    "BIG_TABLES",
    "GovernorPolicy",
    "CostGovernor",
    "SessionBudgetExceeded",
    "check_cost",
]

#: Curated **transaction** tables (large-scan policy: *"never the masters"*): the
#: high-volume tables whose unbounded rollups are the slow tail. ``Erp.Part``/``Customer``/``Vendor`` are deliberately absent: their
#: rollups finish in one page and must stay available.
BIG_TABLES: frozenset[str] = frozenset(
    {
        "orderhed", "orderdtl", "orderrel",
        "quotehed", "quotedtl",
        "invchead", "invcdtl",
        "apinvhed", "apinvdtl",
        "poheader", "podetail", "porel",
        "jobhead", "jobasmbl", "joboper", "jobmtl", "jobprod",
        "labordtl", "laborhed",
        "parttran",
        "rcvhead", "rcvdtl",
        "shiphead", "shipdtl",
        "gljrndtl",
    }
)


@dataclass(frozen=True)
class GovernorPolicy:
    """The knobs. Defaults are cost-governor policy as measured."""

    #: Hard client wall-clock timeout on Execute, seconds. Epicor's own
    #: GetExecutionWarningTime is 30; stay under it.
    execute_timeout_s: float = 25.0
    #: Concurrent Executes allowed per process (shared runtime-budget policy).
    max_inflight: int = 2
    #: Rolling per-session wall-clock budget, seconds (shared runtime-budget policy).
    session_budget_s: float = 120.0
    #: Window the budget rolls over, seconds.
    session_budget_window_s: float = 300.0
    #: large-scan policy's literal rule. OFF by default — see the module docstring.
    strict_scan_guard: bool = False


def _rows(ds: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    rows = ds.get(name) or ds.get(f"{name}Designer") or []
    return [r for r in rows if isinstance(r, Mapping)]


def _is_big(table: str) -> bool:
    return (table or "").rsplit(".", 1)[-1].strip().lower() in BIG_TABLES


def _qualified(row: Mapping[str, Any]) -> str:
    schema = (row.get("DBSchemaName") or "").strip()
    table = (row.get("DBTableName") or "").strip()
    return f"{schema}.{table}" if schema else table


def _literal_conjuncts(ds: Mapping[str, Any]) -> dict[str, list[str]]:
    """table_id -> the columns bounded by a CONSTANT in the WHERE clause.

    A column-to-column conjunct (``[A].[Company] = [B].[Company]``, which is how
    Epicor renders a comma join's predicate) is **not** a bound, and neither is
    ``in (<subquery>)`` — both were measured in this build's
    ``gov_unbounded_scan`` / ``clean_in_subquery`` fixtures.
    """
    sub_ids = {str(s.get("SubQueryID") or "") for s in _rows(ds, "QuerySubQuery")} - {""}
    out: dict[str, list[str]] = {}
    for w in _rows(ds, "QueryWhereItem"):
        if w.get("ToTableID") or w.get("ToFieldName"):
            continue
        rvalue = str(w.get("RValue") or "").strip()
        if not rvalue or rvalue in sub_ids:
            continue
        tid = str(w.get("TableID") or "")
        if tid:
            out.setdefault(tid, []).append(str(w.get("FieldName") or ""))
    return out


def _connected_components(ids: list[str], edges: list[tuple[str, str]]) -> list[set[str]]:
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    groups: dict[str, set[str]] = {}
    for i in ids:
        groups.setdefault(find(i), set()).add(i)
    return list(groups.values())


def check_cost(
    ds: Mapping[str, Any], *, policy: GovernorPolicy | None = None
) -> dict[str, Any] | None:
    """Return an INV-1 refusal, or ``None`` when the statement may run.

    Evaluated on Epicor's parsed DS, per subquery, before Execute is called.
    """
    policy = policy or GovernorPolicy()
    try:
        return _check(ds, policy)
    except Exception as exc:  # noqa: BLE001 - a cost guard must fail CLOSED
        return error_envelope(
            "query_too_expensive",
            "The cost governor could not evaluate this statement, so it was refused. A "
            "control that cannot complete must fail closed — this is a server bug, not a "
            "problem with your SQL; please report it.",
            evidence=f"{type(exc).__name__}: {exc}",
            terminal=False,
        )


def _check(ds: Mapping[str, Any], policy: GovernorPolicy) -> dict[str, Any] | None:
    all_tables = _rows(ds, "QueryTable")
    tables = [t for t in all_tables if t.get("TableType") == "DB"]
    by_sub: dict[str, list[Mapping[str, Any]]] = {}
    for t in tables:
        by_sub.setdefault(str(t.get("SubQueryID") or ""), []).append(t)
    # EVERY node of each subquery, whatever its TableType — the join graph needs
    # them. Epicor stores a CTE or derived-table reference as its own QueryTable
    # row (`TableType == 'SQ'`) and its QueryRelation rows point AT that node.
    nodes_by_sub: dict[str, list[str]] = {}
    for t in all_tables:
        nodes_by_sub.setdefault(str(t.get("SubQueryID") or ""), []).append(
            str(t.get("TableID") or "")
        )
    relations = _rows(ds, "QueryRelation")
    rel_fields = _rows(ds, "QueryRelationField")
    conjuncts = _literal_conjuncts(ds)

    # --- 3. join_on_company_only ------------------------------------------
    for rel in relations:
        rid = str(rel.get("RelationID") or "")
        pairs = [
            (str(f.get("ParentFieldName") or ""), str(f.get("ChildFieldName") or ""))
            for f in rel_fields
            if str(f.get("RelationID") or "") == rid
        ]
        if not pairs:
            continue
        if all(p.lower() == "company" and c.lower() == "company" for p, c in pairs):
            names = [
                _qualified(t)
                for t in tables
                if str(t.get("TableID")) in {rel.get("ParentTableID"), rel.get("ChildTableID")}
            ]




            return error_envelope(
                "query_too_expensive",
                "This join's ON predicate contains ONLY Company, which is a cartesian "
                f"product between {' and '.join(names)} — every row of one against "
                "every row of the other. It parses clean, lints clean and returns a "
                "confidently wrong number. Join on the business key as well "
                "(JobNum, PartNum, OrderNum, VendorNum, ...).",
                evidence="join-lint policy: `join_on_company_only` is the single most damaging "
                "shape the model produced and it has no error channel at all; it is not "
                "rewritable because the server cannot invent a business key. Epicor behavior: "
                "restricting this refusal to BIG_TABLES let Erp.Part x Erp.Part on Company "
                "(~261M rows) through to Execute",
                valid={
                    "shape": "on [A].[Company] = [B].[Company] and [A].[<Key>] = "
                    "[B].[<Key>]"
                },
                detail={
                    "tables": names,
                    "join_fields": pairs,
                    "big_tables": [n for n in names if _is_big(n)],
                },
                terminal=False,
            )

    # --- 2. cross join / unrelated tables ---------------------------------
    for sub_id, subs in by_sub.items():
        if len(subs) < 2:
            continue
        db_ids = {str(t.get("TableID") or "") for t in subs}
        edges = [
            (str(r.get("ParentTableID") or ""), str(r.get("ChildTableID") or ""))
            for r in relations
            if str(r.get("SubQueryID") or "") == sub_id
        ]
        # Connectivity runs over ALL of the subquery's nodes, and only the DB
        # tables are then asked whether they landed in one group. Measured: with
        # DB tables as the only nodes, every edge to a
        # CTE / derived table (`TableType == 'SQ'`) was dropped, so
        # `Part ⋈ oh`, `PartCost ⋈ oh`, `PartPlant ⋈ oh` — each properly keyed —
        # read as four unjoined tables and were refused as a cross join. A real
        # cartesian routed through a CTE still splits the DB tables into two
        # groups, and a Company-only edge to one is still rule 3's refusal.
        ids = list(dict.fromkeys(nodes_by_sub.get(sub_id) or [])) or sorted(db_ids)
        comps = [
            c & db_ids for c in _connected_components(ids, edges) if c & db_ids
        ]
        if len(comps) > 1:
            names = [_qualified(t) for t in subs]
            big = [n for n in names if _is_big(n)]
            # Also NOT gated on BIG_TABLES. Two disconnected tables
            # in one subquery multiply, whatever they are: Part x Customer is as
            # unbounded as PartTran x Part, and the 25 s client timeout is [U]
            # about whether it stops the SERVER doing the work.
            return error_envelope(
                "query_too_expensive",
                "Refused: this statement joins "
                f"{', '.join(names)} with no join predicate between "
                f"{len(comps)} of the tables — a cross join. That is an unbounded "
                "cartesian product on a production ERP"
                + (f" (transaction table(s): {', '.join(big)})" if big else "")
                + ". Give every table an ON clause with Company AND the business key, "
                "or query the tables separately.",
                evidence="cartesian-join policy (acceptance shape 1). Measured this build: a "
                "`cross join` and a comma join both parse to two or more DB tables in "
                "one subquery with ZERO QueryRelation rows. Epicor behavior: requiring a "
                "BIG_TABLE here let master-on-master cartesians reach Execute",
                valid={
                    "shape": "from Erp.A as [A] inner join Erp.B as [B] on "
                    "[A].[Company] = [B].[Company] and [A].[Key] = [B].[Key]"
                },
                detail={
                    "tables": names,
                    "unjoined_groups": [sorted(c) for c in comps],
                    "big_tables": big,
                },
                terminal=False,
            )

    # --- 4. an unbounded multi-table scan ---------------------------------
    for sub_id, subs in by_sub.items():
        if len(subs) < 3:
            continue
        names = [_qualified(t) for t in subs]
        big = [n for n in names if _is_big(n)]
        if not big:
            continue
        if any(str(t.get("TableID")) in conjuncts for t in subs):
            continue
        return error_envelope(
            "query_too_expensive",
            f"Refused: {len(subs)} tables ({', '.join(names)}) are joined with NO literal "
            "filter anywhere, and "
            f"{', '.join(big)} are transaction tables. That scans the whole history on a "
            "production ERP. Add a WHERE that bounds at least one table — a date window, a "
            "job number, a part, a plant.",
            evidence="large-scan policy re-expressed over the parsed DS; the cost of an "
            "unfiltered multi-table join is unbounded and the 25 s timeout would be the "
            "only thing to stop it",
            valid={
                "example": "where [OrderHed].[OrderDate] >= dateadd(month, -12, getdate())"
            },
            detail={"tables": names, "big_tables": big},
            terminal=False,
        )

    # --- large-scan policy literal, opt-in ------------------------------------------
    if policy.strict_scan_guard:
        grouped = any(f.get("IsGroupBy") for f in _rows(ds, "QueryField"))
        joined_ids = {str(r.get("ParentTableID") or "") for r in relations} | {
            str(r.get("ChildTableID") or "") for r in relations
        }
        for t in tables:
            name = _qualified(t)
            tid = str(t.get("TableID") or "")
            if not _is_big(name) or tid in conjuncts:
                continue
            if tid in joined_ids or grouped:
                return error_envelope(
                    "query_too_expensive",
                    f"Refused (strict scan guard): {name} is a transaction table with no "
                    "filter of its own and it participates in a join or a group-by. Add a "
                    "WHERE that bounds it.",
                    evidence="large-scan policy, literal wording. Enabled by "
                    "EPICOR_MCP_SQL_STRICT_SCAN_GUARD=true; OFF by default because it "
                    "can reject inexpensive joins that are already bounded by related records",
                    detail={"table": name},
                    terminal=False,
                )
    return None


class SessionBudgetExceeded(RuntimeError):
    """Raised when a session has spent its rolling wall-clock budget."""


@dataclass
class _SessionSpend:
    events: list[tuple[float, float]] = field(default_factory=list)  # (t, seconds)


class CostGovernor:
    """Process-wide concurrency cap + a rolling per-session wall-clock budget.

    shared runtime-budget policy. Both are cheap and both are the difference between one bad
    question and a queue of them.
    """

    def __init__(self, policy: GovernorPolicy | None = None) -> None:
        self.policy = policy or GovernorPolicy()
        self._sem = asyncio.Semaphore(self.policy.max_inflight)
        self._spend: dict[str, _SessionSpend] = {}

    def _prune(self, session: str, now: float) -> _SessionSpend:
        s = self._spend.setdefault(session, _SessionSpend())
        cutoff = now - self.policy.session_budget_window_s
        s.events = [(t, d) for t, d in s.events if t >= cutoff]
        return s

    def spent(self, session: str, *, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        return sum(d for _, d in self._prune(session, now).events)

    def check_budget(self, session: str, *, now: float | None = None) -> dict | None:
        """INV-1 refusal when *session* has no budget left, else ``None``."""
        now = now if now is not None else time.monotonic()
        used = self.spent(session, now=now)
        if used < self.policy.session_budget_s:
            return None
        return error_envelope(
            "query_budget_exhausted",
            f"This session has spent {used:.0f} s of Epicor query time in the last "
            f"{self.policy.session_budget_window_s:.0f} s, which is its budget. Wait, or "
            "narrow the question — this server shares one database with the shop floor.",
            evidence="shared runtime-budget policy: the only existing throttle counts ERRORS (5 in 5 s), so "
            "a loop of successful but expensive queries never trips it",
            detail={"spent_s": round(used, 1), "budget_s": self.policy.session_budget_s},
        )

    def record(self, session: str, seconds: float, *, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._prune(session, now).events.append((now, max(0.0, seconds)))

    def inflight(self):
        """``async with governor.inflight():`` — the concurrency cap."""
        return self._sem


def timeout_envelope(seconds: float, sql: str = "") -> dict[str, Any]:
    """The refusal returned when Execute passes the wall-clock wall."""
    return error_envelope(
        "query_too_expensive",
        f"The query was still running after {seconds:.0f} s and the client stopped "
        "waiting. Narrow it: add a date window, filter to one plant/job/part, or "
        "aggregate fewer tables at once. NOTE: this stops US waiting — whether Epicor "
        "also stops the work server-side is NOT yet established.",
        evidence="The client timeout bounds waiting time; it does not prove that Epicor "
        "cancelled the server-side SQL execution",
        valid={
            "narrowing": [
                "where [T].[Date] >= dateadd(month, -12, getdate())",
                "where [T].[Plant] = '10'",
                "group by fewer columns",
            ]
        },
        retry_with={"sql": sql} if sql else None,
        detail={"timeout_s": seconds},
    )

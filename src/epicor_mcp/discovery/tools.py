"""Table/field discovery from administrator-imported metadata."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Sequence

from epicor_mcp.discovery import rank as _rank
from epicor_mcp.sql.envelope import error_envelope
from epicor_mcp.sql.scope_gate import AUTHZ_UNAVAILABLE_GUIDANCE

logger = logging.getLogger(__name__)

__all__ = ["register_discovery_tools", "TABLES_DESCRIPTION", "FIELDS_DESCRIPTION"]

_KEYS_PATH = Path(__file__).resolve().parents[1] / "sql" / "table_keys.json"






_VALUE_HINT_MIN_DIGITS = 3


def _looks_like_value(query: str) -> str:
    """Return the offending token, or ``""``. Conservative on purpose."""
    for tok in query.replace(",", " ").split():
        core = tok.strip("'\"()[]")
        if len(core) < 4:
            continue
        digits = sum(c.isdigit() for c in core)
        if digits >= _VALUE_HINT_MIN_DIGITS and (
            "-" in core or digits >= len(core) - 2
        ):
            return core
    return ""


def _load_keys() -> dict[str, list[str]]:
    try:
        raw = json.loads(_KEYS_PATH.read_text())["tables"]
    except Exception:  # noqa: BLE001
        logger.warning("table_keys.json unavailable — primary keys will be omitted")
        return {}
    out: dict[str, list[str]] = {}
    for table, keysets in raw.items():
        if keysets and isinstance(keysets, list) and keysets[0]:
            out[table.lower()] = list(keysets[0])
    return out


TABLES_DESCRIPTION = """\
Find which Epicor tables hold jobs, parts, purchase orders, sales orders, invoices, customers, \
quotes, shipments, labor, inventory or quality data. Give it the plain-English SUBJECT — \
"employee labor hours on a job", "purchase order lines", "discrepant material", "packing slips we \
sent customers" — not a data value. "customer Example Customer" is a value: search "customer", then filter \
in SQL. Returns candidate tables with their description, primary key, the columns closest to your \
words, and how many columns they have. Call this first when you do not already know the table, \
then epicor_fields, then epicor_query."""

FIELDS_DESCRIPTION = """\
List administrator-imported columns of a table — the job, part, order, invoice or labor fields you can \
validate before SELECT; Swagger-only metadata may include BO projection fields. `table` is REQUIRED and may be a LIST: pass every table your query will join and \
get all of them in one call. An unscoped column search is meaningless — OnHandQty exists on 30+ \
tables. `query` may list several things comma-separated ("invoice number, due date, balance") — \
each is matched on its own. Returns the column name, SQL type, Epicor's UI label and a short \
description, the table's primary key, and — when the column you asked for lives on a DIFFERENT \
table — which one. \
The OData names you may remember (Part.OnHandQty, JobOper.ScrapQty, OrderDtl.ExtPrice) are NOT \
selectable; this tool returns only columns that exist in SQL."""


def register_discovery_tools(
    mcp: Any,
    index: Any,
    *,
    embed_query: Callable[[str, str], Any],
    authorizer: Any = None,
    denied_column: Callable[[str, str], bool] | None = None,
    denied_table: Callable[[str], bool] | None = None,
    denial_source: Callable[[str], str] | None = None,
    session_email: Callable[[], str] | None = None,
    search_note: Callable[[], str] | None = None,
) -> bool:
    """Register both tools. Returns False (and registers nothing) without an index.

    *embed_query* is ``async (text, prefix) -> vector | None``; injected so the
    deterministic suite can drive the REGISTERED tools with no embedding server.
    A ``None`` vector degrades to lexical + deterministic ranking, announced.
    *search_note*, when given, returns WHY the ranking is substring (switch off,
    model mismatch, provider outage) and rides back under ``notes`` on every
    substring-ranked response to a non-empty query — the caller gets the reason,
    not just the mode.
    """
    if index is None:
        logger.warning(
            "discovery index not loaded — epicor_tables/epicor_fields NOT registered. "
            "Run scripts/build_schema_catalogue.py then scripts/build_discovery_index.py"
        )
        return False

    from epicor_mcp.discovery.store import FIELD_QUERY_PREFIX, TABLE_QUERY_PREFIX

    from epicor_mcp.sql import grain
    keys = {table: [grain._cased(col) for col in sorted(candidates[0])]
            for table, candidates in grain.TABLE_KEYS.items() if candidates}

    def _full(table: str) -> str:
        info = index.table_info(table) or {}
        return info.get("full_name") or f"{info.get('schema', 'Erp')}.{table}"

    def _annotate_mode(resp: dict[str, Any], vec: Any, query: str) -> None:
        resp["search_mode"] = "substring" if vec is None else "semantic"
        if vec is None and query.strip() and search_note is not None:
            note = search_note()
            if note:
                resp.setdefault("notes", []).append(note)

    def _pk(table: str) -> list[str]:
        return (index.table_info(table) or {}).get("primary_key") or keys.get(table.lower(), [])

    async def _scope():
        """Resolve identity from the SESSION ONLY, then fetch the scope.

        ``for_email`` was removed from both tool signatures: in gate mode it
        OUTRANKED the session, so any
        caller could choose whose authorization applied to them — an
        identity-spoofing vector. Identity is the connection session's
        principal (or the server-side ``EPICOR_MCP_DEV_IDENTITY`` override
        inside ``resolve_identity``); there is no per-call override on this
        surface. Inspecting someone ELSE's access is an admin operation:
        ``GET /admin/authz/{email}``.
        """
        if authorizer is None:
            return None
        email = authorizer.resolve_identity(
            session_email=session_email() if session_email else ""
        )
        return await authorizer.scope_for(email)

    def _visible(table: str, column: str) -> bool:
        """Discovery must never name a column ``epicor_query`` will refuse.

        Otherwise the server teaches the model the exact SQL it is about to deny, which is a guaranteed multi-turn thrash. The count
        is reported; the names never are.
        """
        if denied_column is None:
            return True
        try:
            return not denied_column(table, column)
        except Exception:  # noqa: BLE001
            return False

    def _table_denied(table: str) -> bool:
        """A HARD-denied table is invisible to discovery, not merely unlisted.

        `_visible` is per COLUMN, so on a wholly-denied table (`Erp.PREmpMas`,
        `Erp.UserFile`) it hid the compensation columns and served the rest —
        i.e. `epicor_fields("PREmpMas")` enumerated a payroll table's schema to
        an unauthenticated dev-mode caller. Column filtering alone therefore cannot protect a wholly denied table. Suggesting the table is just
        as bad in the other direction: `epicor_query` refuses it as **final**,
        so naming it costs a guaranteed dead round trip.
        """
        if denied_table is None:
            return False
        try:
            return bool(denied_table(table))
        except Exception:  # noqa: BLE001
            return True  # fail CLOSED — an unreadable verdict hides the table

    def _alternatives(scope: Any, blocked: list[str], query: str) -> list[str]:
        """Up to 5 IN-scope, non-denied tables near a refused fields request.

        Served on ``table_not_authorized`` and NEVER on ``table_access_denied``
        — see :func:`_not_authorized_envelope` for why the asymmetry is
        deliberate. Filtered twice: ``allowed=`` hard-filters to the caller's
        scope inside the index, and the deny-list drops what ``epicor_query``
        would refuse — a suggestion either gate refuses is a guaranteed dead
        round trip. ``vec=None`` on purpose: lexical ranking is deterministic
        and this path must not depend on the embedding server being awake.
        """
        probe = " ".join([*blocked, query]).strip()
        try:
            hits = index.search_tables(None, probe, limit=15, allowed=scope.tables)
        except Exception:  # noqa: BLE001
            return []
        # The scope filter already excludes the blocked names (they are out of
        # scope by construction); the explicit drop is a backstop against a
        # duck-typed index that ignores `allowed`.
        out = [
            h.table for h in hits
            if not _table_denied(h.full_name) and scope.allows(h.full_name) and h.table not in blocked
        ]
        return out[:5]

    # ---------------------------------------------------------------- tables #
    @mcp.tool(name="epicor_tables", description=TABLES_DESCRIPTION)
    async def epicor_tables(
        query: str,
        limit: int = 5,
    ) -> dict:
        if not (query or "").strip():
            return error_envelope(
                "missing_query",
                "epicor_tables needs a plain-English subject to search for.",
                valid={"examples": ["purchase order lines", "discrepant material",
                                    "employee labor hours on a job"]},
                retry_with={"query": "purchase order lines"},
            )
        limit = max(1, min(int(limit or 5), 25))
        value_tok = _looks_like_value(query)

        scope = await _scope()
        gate = scope is not None and authorizer.mode == "gate"
        if gate and scope.is_unavailable:
            # Gate mode fails CLOSED: serving unpersonalised
            # results on a snapshot failure would be the exact fail-open the
            # three-state scope exists to remove. Retryable, not terminal —
            # UNAVAILABLE is never cached, so the next call retries the
            # snapshot. Boost mode keeps its old degrade-to-no-boost below.
            return _authz_unavailable(scope, retry_with={"query": query})
        allowed = None
        if gate and scope.active:
            # SCOPED under the gate: a hard filter. `search_tables` fuses
            # limit+400 candidates BEFORE applying `allowed`, so a small scope
            # still fills the page without the caller over-fetching for authz.
            # UNLIMITED (SecurityMgr / mode=off) leaves `allowed` None — no
            # filter and, below, no `reachable_by_you` noise.
            allowed = scope.tables

        vec = await embed_query(query, TABLE_QUERY_PREFIX)
        # Over-fetch, then drop the hard-denied: filtering after a limit-sized
        # fetch would silently return FEWER than the caller asked for.
        hits = index.search_tables(vec, query, limit=limit * 3, allowed=allowed)
        hits = [h for h in hits if not _table_denied(h.full_name) and (not gate or scope.allows(h.full_name))][:limit]

        if scope is not None and scope.active and authorizer.mode == "boost":
            hits = _boost(hits, scope.tables, limit)

        out = []
        for h in hits:
            cols = [
                f["name"]
                for f in index.fields_of(h.table)[:400]
                if _visible(h.table, f["name"])
            ]
            top = [f for f in index.search_fields(h.table, None, query, limit=30) if _visible(h.full_name, f.field)][:6]
            out.append(
                {
                    "table": h.full_name,
                    "name": h.table,
                    "description": h.description,
                    "primary_key": _pk(h.table),
                    "column_count": h.field_count,
                    "closest_columns": [t.field for t in top] or cols[:6],
                    "reachable_by_you": (
                        None if scope is None or not scope.active
                        else scope.allows(h.full_name)
                    ),
                    "score": h.score,
                }
            )

        resp: dict[str, Any] = {
            "success": True,
            "tables": out,
            "query": query,
            "next_step": (
                "Call epicor_fields with the table (or a LIST of tables if you will "
                "join) to get selectable columns, then write SQL for epicor_query."
            ),
        }
        _annotate_mode(resp, vec, query)
        resp["metadata_source"] = index.manifest.get("source", "administrator catalogue")
        if index.manifest.get("source") == "swagger_projection_unverified":
            resp["metadata_note"] = "Swagger describes BO projections; physical SQL tables and fields are unverified. Import an authoritative catalogue for SQL discovery."
        if scope is not None:
            # Boost keeps the zero-arg note BYTE-IDENTICAL to the pre-gate
            # response; only gate mode gets the tailored "only tables served"
            # wording.
            resp["authz"] = {
                "mode": authorizer.mode,
                "note": scope.note("gate") if gate else scope.note(),
            }
        if value_tok:
            resp["not_a_table"] = {
                "reason": f"{value_tok!r} looks like a data VALUE, not a subject",
                "guidance": (
                    "Search for the subject instead, then filter on the value in SQL. "
                    f"e.g. epicor_tables('part'), then where [P].[PartNum] = '{value_tok}'"
                ),
            }
        if not out:
            env = error_envelope(
                "no_tables_found",
                f"Nothing matched {query!r}.",
                valid={"hint": "Use a business subject, e.g. 'purchase order lines'."},
                retry_with={"query": " ".join(query.split()[:2]) or query},
            )
            if allowed is not None:
                # Under the gate a miss has TWO causes the model cannot tell
                # apart unless told: nothing matched, or the matches exist and
                # this caller cannot reach them through the menu chain.
                env["authz"] = {"mode": "gate", "note": scope.note("gate")}
            return env
        return resp

    # ---------------------------------------------------------------- fields #
    @mcp.tool(name="epicor_fields", description=FIELDS_DESCRIPTION)
    async def epicor_fields(
        table: str | list[str],
        query: str = "",
        limit: int = 15,
    ) -> dict:
        names = _as_list(table)
        if not names:
            return error_envelope(
                "missing_table",
                "epicor_fields requires `table`. An unscoped column search is "
                "meaningless — OnHandQty exists on 30+ tables.",
                valid={"hint": "Call epicor_tables first, or pass a known table."},
                retry_with={"table": "JobHead", "query": query or "job number"},
            )
        limit = max(1, min(int(limit or 15), 100))

        blocked: list[str] = []
        blocked_by_file: set[str] = set()
        for n in names:
            c = index.resolve_table(n)
            if c is None:
                continue
            full = (index.table_info(c) or {}).get("full_name", c)
            if not _table_denied(full):
                continue
            blocked.append(n)
            # The injected `denial_source` splits the CLAIM, never the code
            # path: a table on the operator's blacklist (table_blacklist.txt)
            # gets the same error/terminality/leak rules, but must
            # not be described as payroll/security data — that would be a false
            # statement the model then repeats. Absent or crashing lookup keeps
            # the built-in wording, the pre-blacklist behaviour.
            if denial_source is not None:
                try:
                    if denial_source(full) == "blacklist":
                        blocked_by_file.add(n)
                except Exception:  # noqa: BLE001
                    pass
        if blocked:
            # No `closest_tables`, no column list, no count. A recovery that
            # names the reachable spelling of a denied table IS the leak
            # (`detail.recovery_withheld` records why the recovery is suppressed).
            policy = sorted(n for n in blocked if n not in blocked_by_file)
            by_file = sorted(n for n in blocked if n in blocked_by_file)
            lead: list[str] = []
            if policy:
                lead.append(
                    f"{', '.join(policy)} holds payroll, compensation or "
                    "security data"
                )
            if by_file:
                lead.append(
                    f"{', '.join(by_file)} is restricted by the server's "
                    "table blacklist"
                )
            return error_envelope(
                "table_access_denied",
                " and ".join(lead) + ". This surface does not describe those "
                "tables and epicor_query will not run SQL against them. The "
                "refusal is final — do not retry, and answer the user that "
                "this data is not available through this tool.",
                terminal=True,
                detail={"stage": "denylist"},
            )

        # ---- table-level authorization (gate mode only) -------------------- #
        # AFTER the deny-list, on purpose: a denied table keeps its stronger,
        # identity-independent `table_access_denied` — downgrading it to an
        # authz message would imply the table becomes describable with more
        # menu access, which is false. Fetched ONLY under gate: boost and off
        # never read the scope here, so those modes stay byte-identical (and
        # snapshot-call-identical) to the pre-gate tool.
        scope = None
        if authorizer is not None and authorizer.mode == "gate":
            scope = await _scope()
        if scope is not None and scope.is_unavailable:
            # Fail CLOSED, retryable: "we do not know who you are" must not
            # degrade to serving the schema unpersonalised. UNAVAILABLE is
            # never cached, so the same call again retries the snapshot.
            return _authz_unavailable(
                scope, retry_with={"table": names, "query": query}
            )
        allowed = scope.tables if (scope is not None and scope.active) else None

        unknown = [n for n in names if index.resolve_table(n) is None]
        if unknown:
            # `allowed=` keeps the suggestions inside the caller's scope in
            # gate mode — a `closest_tables` entry the gate then refuses is a
            # guaranteed dead round trip (same rule the deny-list filter below
            # already follows).
            near = [
                h for h in index.search_tables(
                    await embed_query(unknown[0], TABLE_QUERY_PREFIX),
                    unknown[0], limit=15, allowed=allowed,
                )
                if not _table_denied(h.full_name) and (scope is None or scope.allows(h.full_name))
            ][:5]
            return error_envelope(
                "unknown_table",
                f"No such table: {', '.join(unknown)}. These are physical SQL table "
                f"names from Epicor's catalogue, not OData entity names.",
                valid={"closest_tables": [h.table for h in near]},
                retry_with={
                    "table": near[0].table if near else "JobHead",
                    "query": query,
                },
            )

        unauthorized: list[str] = []
        alternatives: list[str] = []
        if allowed is not None:
            # Scope is SCOPED here (UNLIMITED left `allowed` None). One
            # membership test for the whole surface: `scope.allows` — never a
            # re-implemented normalisation.
            canon_by_name = {n: index.resolve_table(n) for n in names}
            served = [n for n in names if scope.allows(_full(canon_by_name[n]))]
            unauthorized = sorted(
                {canon_by_name[n] for n in names if not scope.allows(_full(canon_by_name[n]))}
            )
            if unauthorized:
                alternatives = _alternatives(scope, unauthorized, query)
                if not served:
                    return _not_authorized_envelope(unauthorized, alternatives, query)
                # MIXED request (some allowed, some not): serve the allowed
                # tables ALONGSIDE a named refusal of the blocked subset —
                # never wholesale. Refusing everything would cost the model a
                # full extra turn to re-request columns it is entitled to,
                # and serving silently without naming the blocked tables is
                # the silent-drop class this repo bans. One response, both
                # halves explicit; the refusal block is attached below, after
                # the allowed blocks are built.
                names = served

        # A comma/semicolon list is several concepts, ranked one at a time and
        # merged round-robin (rank.split_terms, store.search_fields_terms) — with
        # or without vectors. One term keeps the original single-query path byte
        # for byte. The limit is raised to one slot per term so no concept the
        # caller named is cut off.
        terms = _rank.split_terms(query) if query.strip() else [query]
        by_terms = getattr(index, "search_fields_terms", None)
        if len(terms) > 1 and by_terms is not None:
            limit = min(max(limit, len(terms)), 100)
            vecs = await asyncio.gather(
                *(embed_query(t, FIELD_QUERY_PREFIX) for t in terms)
            )
            term_vecs = list(zip(terms, vecs))
            vec = next((v for v in vecs if v is not None), None)
        else:
            terms = [query]
            term_vecs = None
            vec = await embed_query(query, FIELD_QUERY_PREFIX) if query.strip() else None
        blocks = []
        for name in names:
            canon = index.resolve_table(name)
            info = index.table_info(canon) or {}
            if term_vecs is not None:
                hits = by_terms(canon, term_vecs, limit=limit + 25)
            else:
                hits = index.search_fields(canon, vec, query, limit=limit + 25)
            shown, hidden = [], 0
            for h in hits:
                if not _visible(canon, h.field):
                    hidden += 1
                    continue
                shown.append(_field_entry(h))
                if len(shown) >= limit:
                    break
            total = info.get("field_count", len(index.fields_of(canon)))
            block = {
                "table": info.get("full_name", canon),
                "name": canon,
                "description": info.get("description", ""),
                "primary_key": _pk(canon),
                "fields": shown,
                "shown": len(shown),
                "total_columns": total,
            }
            if hidden:
                block["restricted_columns"] = hidden
            # The mirror gets its own deny check: under the
            # built-in patterns a denied mirror always implied a denied PARENT
            # (inheritance is parent→mirror), so this block was unreachable for
            # one — but the operator blacklist can deny `<X>_UD` with X clean, and
            # without the check the parent's block named the mirror AND served
            # its `_c` column list: the exact schema leak the `table_access_
            # denied` envelope above withholds. `_table_denied` fails closed,
            # so an unreadable verdict hides the block, same as everywhere.
            ud = index.resolve_table(f"{canon}_UD")
            ud_full = _full(ud) if ud else ""
            if ud and not _table_denied(ud_full) and (scope is None or scope.allows(ud_full)):
                block["custom_columns"] = {
                    "table": ud_full,
                    "note": (
                        "Custom `_c` columns live on this mirror table, NOT on "
                        f"{canon}. Selecting [{canon}].[Something_c] parses and then FAILS "
                        "at execute."
                    ),
                    "join": (
                        f"left outer join {ud_full} as [UD] "
                        f"on [{canon}].[SysRowID] = [UD].[ForeignSysRowID]"
                    ),
                    "columns": [
                        f["name"] for f in index.fields_of(ud) if f["name"].endswith("_c") and _visible(ud_full, f["name"])
                    ][:20],
                }
            blocks.append(block)

        resp: dict[str, Any] = {
            "success": True,
            "tables": blocks,
            "query": query,
        }
        _annotate_mode(resp, vec, query)
        resp["metadata_source"] = index.manifest.get("source", "administrator catalogue")
        if index.manifest.get("source") == "swagger_projection_unverified":
            resp["metadata_note"] = "Swagger describes BO projections; physical SQL tables and fields are unverified. Import an authoritative catalogue for SQL discovery."
        if query.strip():
            # Both "elsewhere" channels POINT AT OTHER TABLES, so both are a
            # second way to surface a denied one — and, in gate mode, an
            # out-of-scope one: a suggestion the gate will refuse is a
            # guaranteed dead round trip. Filtered at the seam rather than
            # inside each helper — they are independently tested and have no
            # business knowing about the deny-list or the authz scope.
            def _suggestable(t: str) -> bool:
                return not _table_denied(_full(t)) and (scope is None or scope.allows(_full(t)))

            elsewhere = []
            for entry in _elsewhere(index, names, query, blocks):
                owners = [t for t in entry["lives_on"] if _suggestable(t)]
                if owners:
                    elsewhere.append({**entry, "lives_on": owners})
            # Only for the terms NOTHING served here matches by name. Run over the
            # whole query it was mostly noise beside columns that already
            # answered the question.
            unmatched = [t for t in terms if not _served_by_name(t, blocks)]
            named = [
                (t, c) for t, c in (
                    index.name_matches_elsewhere(
                        " ".join(unmatched), [b["name"] for b in blocks]
                    ) if unmatched else []
                )
                if _suggestable(t) and _visible(t, c)
            ]
            if named:
                elsewhere.append(
                    {
                        "also_named_on_other_tables": [
                            f"{t}.{c}" for t, c in named
                        ],
                        "why": (
                            "These are OTHER tables carrying a column whose NAME "
                            "contains a word you used. Stated as a fact, not a "
                            "recommendation — check the definitions before "
                            "switching tables."
                        ),
                    }
                )
            if elsewhere:
                resp["elsewhere"] = elsewhere
        if unauthorized:
            # The mixed-request refusal: same claim set as the wholesale
            # `table_not_authorized` envelope — final for the named tables,
            # no columns, no counts — attached beside the data the caller IS
            # entitled to, so the allowed half costs zero extra turns.
            resp["not_authorized"] = {
                "error": "table_not_authorized",
                "tables": unauthorized,
                "terminal": True,
                "message": (
                    f"{', '.join(unauthorized)}: outside the tables your Epicor "
                    "menu access reaches. Not described here, and epicor_query "
                    "will refuse SQL against them — do not retry them. The "
                    "served tables above are complete."
                ),
            }
            if alternatives:
                resp["not_authorized"]["reachable_alternatives"] = alternatives
        if len(names) > 1:
            resp["join_hint"] = (
                "Join on Company AS WELL AS the business key — an ON clause with only "
                "Company is a cartesian product and is refused. The primary_key list of "
                "each table above is the column set to join on."
            )
        return resp

    logger.info(
        "discovery surface: epicor_tables + epicor_fields registered "
        "(%s tables, %s fields, authz=%s)",
        index.manifest.get("table_count"),
        index.manifest.get("field_count"),
        getattr(authorizer, "mode", "none"),
    )
    return True


# --------------------------------------------------------------------------- #
#: A description longer than this is cut. Enough to tell ``RelQty`` ("in
#: vendors unit of measure") from ``XRelQty`` ("in our unit of measure"); the
#: full data-dictionary prose cost ~350 chars a column.
_DESC_MAX = 100


def _field_entry(h: Any) -> dict[str, Any]:
    """One column, compact. No per-field ``primary_key``/``required``: the
    table-level ``primary_key`` list already says the first, and the second is a
    write-side fact on a read-only surface."""
    entry: dict[str, Any] = {"name": h.field, "type": h.sql_type, "label": h.label}
    desc = (h.description or "").strip()
    label = (h.label or "").strip().lower()
    if desc and not _rank.thin_description(h.field, desc) and desc.lower().rstrip(".") != label:
        entry["description"] = desc if len(desc) <= _DESC_MAX else desc[: _DESC_MAX - 1].rstrip() + "…"
    return entry


def _served_by_name(term: str, blocks: list[dict]) -> bool:
    """True when a column already served shares most of *term*'s name."""
    for b in blocks:
        for f in b["fields"]:
            if _rank.term_name_match(term, b["name"], f["name"]) >= 0.5:
                return True
    return False


def _authz_unavailable(scope: Any, *, retry_with: dict[str, Any]) -> dict[str, Any]:
    """The gate-mode fail-CLOSED envelope for an UNAVAILABLE scope.

    ``terminal=False`` is load-bearing: the authorizer never caches an
    UNAVAILABLE scope, so retrying the same call retries the snapshot — a
    transient Epicor hiccup must read as "try again", never as "this data is
    off limits" (that claim belongs to ``table_not_authorized`` /
    ``table_access_denied`` alone).

    The guidance sentence is imported from ``sql/scope_gate.py`` — its single
    source. It deliberately avoids "usually transient — retry the same
    call", which is false for minutes on a cold SCOPED user, whose first
    authorization crawls Epicor menu security.
    """
    return error_envelope(
        "authorization_unavailable",
        "Table authorization for this identity could not be established "
        f"({scope.reason}). The gate fails closed: no tables or columns are "
        "served until the authorization snapshot succeeds. "
        + AUTHZ_UNAVAILABLE_GUIDANCE,
        detail={"stage": "authz", "reason": scope.reason, "email": scope.email},
        retry_with=retry_with,
        terminal=False,
    )


def _not_authorized_envelope(
    blocked: list[str], alternatives: list[str], query: str
) -> dict[str, Any]:
    """Every requested table is outside the caller's scope.

    THE DELIBERATE ASYMMETRY vs ``table_access_denied``: the deny-list refusal
    withholds every recovery, because naming the reachable spelling of a
    payroll table IS the leak it exists to prevent. An authz miss is a
    different animal — the table is not secret, this caller just cannot reach
    it through the menu chain — so a dead-end envelope would only cost the
    model a wasted turn. We MAY therefore name reachable alternatives (already
    scope-filtered AND deny-filtered by :func:`_alternatives`). What stays
    identical to the deny case: NO column list, NO column count, ``terminal``
    True — for the named tables the refusal is final.
    """
    plural = len(blocked) > 1
    them = "them" if plural else "it"
    return error_envelope(
        "table_not_authorized",
        f"{', '.join(blocked)}: outside the set of tables your Epicor menu "
        f"access reaches. This surface will not describe {them}, and "
        f"epicor_query will refuse SQL against {them}. The refusal is final "
        f"for {'these tables' if plural else 'this table'} — pivot to a table "
        "you can reach, or ask an Epicor admin for the menu access.",
        valid={"reachable_tables": alternatives} if alternatives else None,
        retry_with=(
            {"table": alternatives[0], "query": query} if alternatives else None
        ),
        detail={"stage": "authz"},
        terminal=True,
    )


def _as_list(table: str | list[str]) -> list[str]:
    if isinstance(table, str):
        return [t.strip() for t in table.replace(",", " ").split() if t.strip()]
    if isinstance(table, (list, tuple)):
        return [str(t).strip() for t in table if str(t).strip()]
    return []


def _boost(hits: Sequence[Any], allowed: frozenset[str], limit: int) -> list[Any]:
    """Stable re-rank: reachable tables first, original order preserved inside
    each group. A boost must never invent an ordering the ranker did not produce.
    """
    reachable = [h for h in hits if h.table.lower() in allowed]
    rest = [h for h in hits if h.table.lower() not in allowed]
    return (reachable + rest)[:limit]


#: An exact-name ``elsewhere`` is only useful for a DISTINCTIVE column. Above
#: this many owner tables the name is generic (``PartNum`` is on 477 tables,
#: ``Quantity`` on dozens) and listing arbitrary owners is noise.
_ELSEWHERE_MAX_OWNERS = 8


def _elsewhere(index: Any, asked: list[str], query: str, blocks: list[dict]) -> list[dict]:
    """"You named column X, and X lives on table Y, not here."

    For example, a distinctive field requested from the wrong table can be
    redirected to its actual owner without guessing a different field.

    Fires only on a token the caller plainly typed as a COLUMN NAME — internal
    capitalisation or an underscore — and only when that name is distinctive.
    An earlier version keyed on any 4+ character token, which made *"scrap and
    completed quantity"* report that ``quantity`` "lives on" five unrelated
    tables. The semantic counterpart in
    :meth:`~epicor_mcp.discovery.store.DiscoveryIndex.better_elsewhere` is what
    handles plain English.
    """
    here = {b["name"].lower() for b in blocks}
    shown = {f["name"].lower() for b in blocks for f in b["fields"]}
    out = []
    for tok in query.replace(",", " ").split():
        core = tok.strip("'\"()[].")
        looks_like_identifier = "_" in core or any(c.isupper() for c in core[1:])
        if len(core) < 4 or not looks_like_identifier or core.lower() in shown:
            continue
        owners, total = index.find_column_elsewhere(core)
        owners = [t for t in owners if t.lower() not in here]
        if owners and total <= _ELSEWHERE_MAX_OWNERS:
            out.append({"asked": core, "lives_on": owners[:5]})
    return out[:3]

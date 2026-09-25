"""The read-only ad-hoc SQL pipeline.

``ParseFromSQL`` compiles SQL to an in-memory designer tableset. Removing the
``Designer`` array suffix produces the runtime tableset accepted by ``Execute``.
This path never calls ``Update`` or ``DeleteByID``; it persists no definition.
The endpoint boundary is checked by ``tests/test_query_no_write_methods.py``.

GATE ORDER
----------
1. Transpile the supported SQL subset and check page reachability locally.
2. Validate columns against a deny-filtered physical catalogue when available.
3. Parse through Epicor, then enforce the denylist on its resolved tableset.
4. Enforce the injected caller/table scope.
5. Lint the parsed dataset, check cost, and execute under the shared timeout,
   concurrency limit and session budget.
6. Add grain and value advisories; diagnose only successful zero-row page 1.

The denylist runs before lint because lint may return a table's column list.
A denied table must not leak its schema through a helpful error. Parsed lint
must follow Epicor's parse; the separate local column check avoids that round
trip only when absence can be proved from imported physical metadata.

RESPONSE CHANNELS
-----------------
``assumptions`` records server rewrites and row bounds. ``notes`` contains
advisories for returned rows, tagged by source. ``diagnosis`` alone explains
zero-row results; ``grain_checks`` offers runnable checks without executing them.
A refusal returns an error envelope immediately. ``next_step`` combines the
channels into actionable guidance without inventing a result.

Fresh bounded domain measurements outrank dated snapshots. A snapshot that
contradicts returned rows is omitted; a disagreement with a zero-row diagnosis
is reported as ``diagnosis.static_catalogue_stale``. A static finding cannot
change ``terminal``. A measured likely mistake can change it from true to false,
but diagnosis never changes success, rows, row_count or completeness.

PAGING AND DIAGNOSIS
-------------------
No signed paging cursor is issued, and no parsed-dataset cache bypasses a fresh
authorization decision. ``TOP`` bounds the entire result; ``PageSize`` and
``PageNum`` select a window within it. An injected bound equals page size, so
page 2 cannot expose more rows and is refused. Keyset paging is the supported
way to continue beyond that bound. Full pages do not claim completeness.

A successful zero-row first page may run at most ``DEFAULT_PROBE_BUDGET`` extra
bounded queries. Every diagnostic query repeats the denylist and caller-scope
checks. A query returning rows pays none of this extra cost, and an empty result
with no measured mistake remains a valid terminal answer. Static snapshots do
not prefill the measured-domain cache. See ``docs/design.md`` for the complete
surface and authorization boundaries.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from dataclasses import replace
from typing import Any, Mapping

from epicor_mcp.sql import (
    denylist,
    domains as domainsmod,
    grain as grainmod,
    lint as lintmod,
    scope_gate as scopegate,
    validate_columns as colvalid,
)
from epicor_mcp.sql.diagnose_empty import (
    DEFAULT_PROBE_BUDGET,
    DomainCache,
    ProbeResult,
    diagnose_empty,
)
from epicor_mcp.sql.envelope import error_envelope
from epicor_mcp.sql.governor import (
    CostGovernor,
    GovernorPolicy,
    check_cost,
    timeout_envelope,
)
from epicor_mcp.sql.next_step import annotate_next_step
from epicor_mcp.sql.transpile import (
    SORT_KEY_MAX_CHARS,
    WEDGE_POLICY,
    Outcome,
    transpile,
    wrap_sort_in_cte,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "PROBE_PAGE_SIZE",
    "PARSE_PATH",
    "EXECUTE_PATH",
    "ANALYZE_PATH",
    "make_probe_runner",
    "run_sql",
    "rows_to_tsv",
]

PARSE_PATH = "Ice.BO.BAQDesignerSvc/ParseFromSQL"
EXECUTE_PATH = "Ice.BO.DynamicQuerySvc/Execute"
ANALYZE_PATH = "Ice.BO.DynamicQuerySvc/Analyze"

DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 1000

#: Server-side page size for a DIAGNOSTIC probe. Every probe shape the
#: diagnostician builds is a bounded aggregate — ``count(*)``, ``min``/``max``,
#: or a ``group by`` capped at ``DOMAIN_LIMIT + 1`` — so 64 is comfortably above
#: the largest one and is a backstop regardless of the SQL's own row bound.
PROBE_PAGE_SIZE = 64

#: Epicor dataset contract: `RowMod: "A"` and a non-empty `QueryID` are both
#: REQUIRED. With `RowMod` missing the parse 400s; with `QueryID` missing it
#: parses 200 and then Execute **500s**, because the blank id propagates into
#: every child row. The legacy tools' 26-field template and its 20 sibling arrays are not
#: needed.
_PARSE_BODY = {
    "ds": {
        "DynamicQueryDesigner": [
            {"QueryID": "AdHocV3", "DisplayPhrase": "", "RowMod": "A"}
        ]
    }
}


def _designer_to_runtime(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """``QueryFieldDesigner`` -> ``QueryField``. That is the whole translation."""
    out: dict[str, Any] = {}
    for key, value in parsed.items():
        if key.endswith("Designer"):
            out[key[: -len("Designer")]] = value
        else:
            out[key] = value
    return out


def _extract_parsed_ds(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for candidate in (
        (response.get("parameters") or {}).get("ds"),
        response.get("ds"),
        response.get("returnObj"),
    ):
        if isinstance(candidate, Mapping) and "DynamicQueryDesigner" in candidate:
            return candidate
    return None


def rows_to_tsv(rows: list[Mapping[str, Any]], columns: list[str]) -> str:
    """Tab-separated, first line the header, empty field = null (TSV result contract).

    TSV, **not** CSV: Epicor description and address fields contain commas
    constantly, so CSV pays quoting overhead on exactly our widest columns.
    pgEdge measured 30-40 % token savings against JSON in production.
    """
    lines = ["\t".join(columns)]
    for row in rows:
        cells = []
        for col in columns:
            value = row.get(col)
            if value is None:
                cells.append("")
            else:
                cells.append(str(value).replace("\t", " ").replace("\n", " ").replace("\r", ""))
        lines.append("\t".join(cells))
    return "\n".join(lines)


def _columns_of(rows: list[Mapping[str, Any]]) -> list[str]:
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    return seen


def _epicor_detail(exc: Exception) -> dict[str, Any]:
    return {
        "status": getattr(exc, "status_code", None),
        "message": str(getattr(exc, "message", exc))[:1200],
    }


def _connection_envelope(detail: dict[str, Any], stage: str) -> dict[str, Any] | None:
    """Envelope for a failure to reach or authenticate to Epicor, else ``None``.

    Transport errors and timeouts (status 0 / 408) and rejected credentials or
    API-key scope (401 / 403) fail the first call of every query. Reported as a
    SQL error they send a model rewriting valid SQL and an operator debugging
    the wrong thing, so they get their own terminal envelopes.
    """
    status = detail.get("status")
    message = detail.get("message") or ""
    if status in (0, 408):
        return error_envelope(
            "epicor_unreachable",
            f"Could not reach Epicor ({message}). This is a connection problem, not a "
            "problem with your SQL: check the configured Epicor URL "
            "(EPICOR_MCP_EPICOR_LIVE_URL, or EPICOR_MCP_EPICOR_PILOT_URL in pilot) and "
            "network access from the server.",
            evidence="HTTP status 0 is a transport failure and 408 a timeout; "
            "neither is Epicor judging the SQL",
            detail={**detail, "stage": stage},
            terminal=True,
        )
    if status in (401, 403):
        return error_envelope(
            "epicor_auth_error",
            f"Epicor rejected the server's credentials (HTTP {status}: {message}). This "
            "is a configuration problem, not a problem with your SQL: check the "
            "service-account username and password, the API key, and that the key's "
            "access scope allows Ice.BO.BAQDesignerSvc and Ice.BO.DynamicQuerySvc.",
            evidence="HTTP 401/403 is Epicor refusing the caller or the API key's access scope",
            detail={**detail, "stage": stage},
            terminal=True,
        )
    return None


# --------------------------------------------------------------------------- #
# E14 — the column catalogue, with the deny-list applied to it
# --------------------------------------------------------------------------- #

#: ``(base catalogue, deny-filtered copy)``. The base object is kept so its
#: ``id()`` cannot be recycled under the identity check, and
#: ``load_catalogue`` is already cached on ``(path, mtime)`` — so a rebuilt
#: catalogue produces a new object and this rebuilds with it.
_SAFE_CATALOGUE: tuple[Any, Any] | None = None

#: Catalogue tables the deny-list is EXPECTED to remove, each for a stated
#: reason. Anything else disappearing is a coverage loss and is logged as one.
EXPECTED_CATALOGUE_DENIALS: frozenset[str] = frozenset(
    {
        # Ice.UserFile* is the security master. Unfiltered metadata would expose
        # its SecurityMgr / DspPayrollMgr / GroupList / PwdExpires column names
        # before the parsed-dataset denylist runs.
        "UserFile",
    }
)


def _catalogue_for_scope(scope: Any) -> Any:
    catalogue = _safe_catalogue()
    if scope is None or getattr(scope, "is_unlimited", False):
        return catalogue
    return catalogue.excluding(lambda table: not scope.allows(table))


def _safe_catalogue() -> Any:
    """Column-validation metadata with denied tables removed.
    
    Column validation runs before Epicor parses the SQL. Without filtering, its
    unknown-column recovery could expose the schema of a denied table such as
    UserFile. Removing that table makes local validation abstain, allowing the
    resolved-dataset denylist to refuse it after parse. This is metadata filtering,
    not a replacement for authorization; the parsed-dataset gate always runs.
    
    The cache follows the loaded catalogue object's identity, so a rebuilt source
    invalidates it. Unexpected removals are logged because they reduce local
    validation coverage even though the security policy remains fail-closed.
    """
    global _SAFE_CATALOGUE
    base = colvalid.load_catalogue()
    cached = _SAFE_CATALOGUE
    if cached is not None and cached[0] is base:
        return cached[1]
    # The filter runs on BARE names, where `is_denied_table` deliberately
    # over-matches ("fail closed, never open"). A rebuilt catalogue can add a
    # table whose bare name collides with a deny pattern, reducing column
    # validation coverage. Preserve the denial and report unexpected removals.
    dropped = sorted(n for n in base.tables if denylist.is_denied_table(n))
    filtered = base.excluding(denylist.is_denied_table)
    unexpected = [n for n in dropped if n not in EXPECTED_CATALOGUE_DENIALS]
    if unexpected:
        logger.warning(
            "E14 catalogue: %d table(s) removed by the deny-list that are NOT in the "
            "expected set, so column validation now ABSTAINS on them: %s. Either add "
            "them to EXPECTED_CATALOGUE_DENIALS with a reason, or narrow the deny "
            "pattern.",
            len(unexpected),
            unexpected,
        )
    else:
        logger.info("E14 catalogue: deny-filtered %s (expected)", dropped or "nothing")
    _SAFE_CATALOGUE = (base, filtered)
    return filtered


# --------------------------------------------------------------------------- #
# UD mirrors — `_c` columns live on `<Table>_UD`
# --------------------------------------------------------------------------- #

#: The cheap gate in front of the mirror map. `\b` keeps `_class`/`_curve`
#: intact while `[P].[Flagged_c]`, `Flagged_c` and even `FLAGGED_C` all hit; a false
#: positive (a `_c` inside a string literal) costs one cached file read and
#: nothing else. A statement this regex misses carries no `_c` reference, so
#: for it the whole feature — including the catalogue read — does not
#: exist and the pipe is byte-identical to its pre-UD behaviour.
_UD_COLUMN_HINT = re.compile(r"_c\b", re.IGNORECASE)


def _ud_mirrors_for(sql: str) -> Any:
    """The deny-filtered UD mirror map, or ``None`` when this statement cannot
    need it. ``load_ud_mirrors`` is Erp-only and applies ``is_denied_table`` to
    BOTH the mirror and its parent at load (defence in depth on top of the
    deny-list's own `_UD` inheritance), so nothing downstream — the transpiler's
    rewrite, E14's envelope, `column_lives_on` — can ever name a denied table's
    mirror. The mirror map must apply the same deny filtering, and
    `tests/test_ud_column_rewrite.py` pins it at the loader.
    """
    if not sql or not _UD_COLUMN_HINT.search(sql):
        return None
    return colvalid.load_ud_mirrors() or None


# --------------------------------------------------------------------------- #
# The note channel — one shape, one `source` per finding
# --------------------------------------------------------------------------- #

def _tag(notes: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """Stamp ``source`` on each note.

    ``lint.Finding.to_dict()`` and ``grain.GrainFinding.to_dict()`` are
    byte-identical in shape (``rule``/``severity``/``message``/``evidence``/
    ``detail``), so before this the model could not tell a claim derived from
    Epicor's own parsed tableset from one derived from our AST — and those have
    very different standing. Purely additive: the existing keys are untouched.
    """
    out = []
    for note in notes:
        tagged = dict(note)
        tagged["source"] = source
        out.append(tagged)
    return out


def _domain_notes(findings: Any) -> list[dict[str, Any]]:
    """E15a ``Finding`` -> the uniform note record.

    ``severity`` is ``WARN`` only when the predicate sits in a plain top-level
    AND chain (``causes_empty``) — under an ``OR``, a ``NOT`` or a join
    condition the fact is still true but it does not govern the result, and
    calling that a warning would over-claim.
    """
    notes: list[dict[str, Any]] = []
    for finding in findings:
        detail = finding.to_dict()
        message = detail.pop("message", "")
        rule = detail.pop("finding", "domain")
        notes.append(
            {
                "rule": rule,
                "severity": "WARN" if finding.causes_empty else "INFO",
                "message": message,
                "evidence": (
                    f"E15a static value catalogue ({finding.as_of}). It is a "
                    "dated snapshot: where it "
                    "disagrees with a live probe, the probe is right."
                ),
                "source": "domains",
                "detail": detail,
            }
        )
    return notes


def _fold_static_into_diagnosis(
    diagnosis: dict[str, Any], findings: Any
) -> None:
    """Merge E15a's dated findings into E15b's measured diagnosis, in place.

    The zero-row channel has exactly one owner. Emitting both a `notes` entry
    and a `diagnosis` about the same empty result is the two-contradictory-
    signals failure this integration exists to prevent, so every static finding
    lands in one of three places:

    * **corroboration** — the live probe already named this column as a killer.
      Dropped: the diagnosis says it better, with a measurement behind it.
    * **``static_catalogue_stale``** — the live probe measured this column as
      SATISFIED while the snapshot says it matches nothing. The probe wins
      and the disagreement is published, because that
      disagreement is the only signal that the snapshot needs re-sweeping.
    * **``static_evidence``** — the diagnosis never reached this predicate
      (budget spent, or it was skipped). Genuinely additive, and free.

    **It never touches ``likely_mistake``.** That flag is deliberately
    conservative, and letting a snapshot raise it would import E15a's one
    unmeasurable failure class — staleness — into the one place where a false
    alarm makes a weak model hunt.
    """
    if not findings:
        return
    killed = " ".join(
        str(f.get("predicate") or "") + " " + str(f.get("column") or "")
        for f in diagnosis.get("killing_predicates") or []
    ).lower()
    satisfied = " ".join(str(p) for p in diagnosis.get("satisfied_predicates") or []).lower()

    stale: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for finding in findings:
        column = (finding.column or "").lower()
        if not column:
            continue
        if column in killed:
            continue  # the measurement already said it, with a probe behind it
        if column in satisfied:
            stale.append(
                {
                    **finding.to_dict(),
                    "superseded_by": "a live probe measured this predicate as matching "
                    "rows; the live measurement is authoritative and this catalogue "
                    "entry is stale",
                }
            )
            continue
        evidence.append(finding.to_dict())
    if stale:
        diagnosis["static_catalogue_stale"] = stale
        logger.warning(
            "E15a/E15b disagreement — catalogue may need re-sweeping: %s",
            [s.get("column") for s in stale],
        )
    if evidence:
        diagnosis["static_evidence"] = evidence
        # ONE message, not two. Without this the diagnosis says "the cause is not
        # established" in `message` while `static_evidence` sitting beside it
        # names the cause — the model then has to adjudicate between two fields
        # of the same object, which is the contradiction this wiring exists to
        # remove. The sentence is appended only where the probes reached no
        # conclusion, and it is explicitly labelled as dated rather than
        # measured, so it can never be mistaken for the probe result.
        if not diagnosis.get("killing_predicates"):
            head = evidence[0]
            more = (
                f" ({len(evidence) - 1} further static finding(s) in "
                "`static_evidence`.)"
                if len(evidence) > 1
                else ""
            )
            diagnosis["message"] = (
                diagnosis.get("message", "").rstrip()
                + " A static catalogue measured on "
                + str(head.get("as_of", domainsmod.AS_OF))
                + " does have something to say about a predicate the probes did not "
                "reach: "
                + str(head.get("message", "")).rstrip()
                + " That is a DATED SNAPSHOT, not a measurement of this query, so it "
                "is offered as a lead rather than a verdict." + more
            )





_KEYSET_RECIPE = (
    "order by a key, read a page, then re-run with "
    "`where [T].[Key] > '<the last value on the page you just read>'`"
)


def _page_reachability_refusal(
    bound: Any, page_size: int, page_num: int
) -> dict[str, Any] | None:
    """Refuse a page that CANNOT contain rows, instead of returning an empty one.

    `top N` and `PageSize`/`PageNum` **compose**: the `top`
    bounds the whole result and the page window is taken *inside* it. Since row-bounding policy injects the row bound **at `page_size`**, an unbounded
    statement gets exactly one page by construction — so page 1 said
    *"INCOMPLETE … there are almost certainly more"* and page 2 answered
    **0 rows, `complete: true`, `terminal: true`, "the complete result for this
    query"**. A model that reads page 1 correctly and asks for page 2 was told
    the data was exhausted.

    An empty page is not evidence about the data, so it is never reported as
    one. Refusing costs **zero Epicor calls** (this runs before ParseFromSQL) and
    hands back the recovery that works.

    Returns ``None`` when the page is reachable — including when the only bound
    is ``PageSize`` itself (``value is None``: a `select distinct`, or a
    set-operation branch bound), because Epicor's own paging does window those.
    """
    if page_num <= 1 or bound is None:
        return None
    value = getattr(bound, "value", None)
    if not value:
        return None
    first_row = page_size * (page_num - 1) + 1
    if value >= first_row:
        return None

    source = getattr(bound, "source", "") or ""
    note = getattr(bound, "note", "") or ""
    last_page = max(1, -(-int(value) // page_size))  # ceil
    common = (
        f" This server issues no paging cursor (paging-cursor policy: a cursor may only be issued "
        f"for a deterministically ordered query, and it must be signed). To read a set "
        f"larger than one page, use keyset pagination — {_KEYSET_RECIPE}."
    )
    if "grand-total" in note:
        return error_envelope(
            "page_beyond_row_bound",
            f"There is no page {page_num}: this statement is a grand-total aggregate, so it "
            "returns exactly one row. Asking for a later page would return an empty page, "
            "which says nothing about your data.",
            evidence="result-completeness policy: a full page never claims completeness, and an empty later "
            "page must not claim it either",
            retry_with={"page": 1},
            detail={"row_bound": bound.to_dict(), "page_size": page_size},
            terminal=False,
        )
    if source in {"injected", "clamped"}:
        return error_envelope(
            "page_unreachable",
            f"Page {page_num} cannot contain rows, so it is refused rather than returned "
            f"empty. Your statement carried no row bound of its own, so the server bounded "
            f"it at `top {value}` — and in Epicor a `top` bounds the WHOLE result while the "
            f"page window is taken INSIDE it, which means this statement has exactly one "
            f"page." + common + " Or narrow the query, or aggregate instead of listing.",
            evidence="TOP bounds the full result before PageSize/PageNum select a window; "
            "an empty later page cannot establish that all source rows were read",
            valid={"keyset_example": "where [OrderDtl].[PartNum] > 'LAST-VALUE-SEEN'"},
            retry_with={"page": 1, "how": _KEYSET_RECIPE},
            detail={"row_bound": bound.to_dict(), "page_size": page_size},
            terminal=False,
        )
    return error_envelope(
        "page_beyond_row_bound",
        f"Page {page_num} (rows {first_row}-{first_row + page_size - 1}) is past the end of "
        f"this statement: its `top {value}` bounds the WHOLE result to {value} rows, so "
        f"page{'s 1-' + str(last_page) if last_page > 1 else ' 1'} hold everything it can "
        "return. An empty page is not evidence that the data ended, so it is refused rather "
        "than returned." + common,
        evidence="Epicor behavior: `top N` bounds the result set and the PageNum window is "
        "taken inside it",
        retry_with={"page": last_page, "how": _KEYSET_RECIPE},
        detail={"row_bound": bound.to_dict(), "page_size": page_size, "last_page": last_page},
        terminal=False,
    )


def make_probe_runner(
    *,
    client: Any,
    api_key: str,
    base_url: str,
    timeout_s: float,
    session_id: str = "anonymous",
    table_scope: Any = None,
) -> Any:
    """Build the ``async (sql) -> ProbeResult`` the diagnostician runs probes on.

    It is a **narrowed** copy of the main pipe, not a recursive ``run_sql`` call,
    and the narrowing is deliberate in both directions:

    * **The deny-list still runs**, on Epicor's own resolved tableset (resolved-dataset authorization rule). A
      diagnostic that lists the distinct values of a pay-rate column is an
      authorization bypass wearing a helpful hat, so this gate is not optional
      even though ``diagnose_empty`` also refuses to *construct* such a probe.
    * The lint and the governor do **not** run. Every probe is generated by this
      server from columns the caller's own statement already resolved: it is one
      table, no join, no ``select *``, and a bounded aggregate. The lint's
      recovery text (a column list) would be meaningless here and the governor's
      cartesian rules cannot fire on a single-table select.
    * ``PageSize`` is still sent (including on diagnostic calls) and the wall-clock timeout is
      still enforced, because a probe is a real query against production.
    """

    async def probe(probe_sql: str) -> ProbeResult:
        started = time.monotonic()
        body = copy.deepcopy(_PARSE_BODY)
        body["ds"]["DynamicQueryDesigner"][0]["DisplayPhrase"] = probe_sql
        try:
            parse_response = await asyncio.wait_for(
                client.post(f"{base_url}/{PARSE_PATH}", api_key, json_body=body),
                timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(False, error=f"parse: {_epicor_detail(exc)['message']}",
                               ms=(time.monotonic() - started) * 1000)
        parsed = _extract_parsed_ds(parse_response)
        if parsed is None:
            return ProbeResult(False, error="parse returned no tableset",
                               ms=(time.monotonic() - started) * 1000)
        queryds = _designer_to_runtime(parsed)

        denial = denylist.check_parsed_ds(queryds)
        if denial:
            logger.warning(
                "diagnostic probe DENIED session=%s tables=%s columns=%s",
                session_id, denial.denied_tables, denial.denied_columns,
            )
            return ProbeResult(False, error="denied by the deny-list",
                               ms=(time.monotonic() - started) * 1000)

        if scopegate.check_table_scope(table_scope, queryds) is not None:
            return ProbeResult(False, error="denied by the table authorization policy",
                               ms=(time.monotonic() - started) * 1000)

        exec_body = {
            "queryDS": queryds,
            "executionParams": {
                "ExecutionFilter": [],
                "ExecutionParameter": [],
                "ExecutionSetting": [
                    {"Name": "PageSize", "Value": str(PROBE_PAGE_SIZE)},
                    {"Name": "PageNum", "Value": "1"},
                ],
                "ExecutionValueSetItems": [],
                "ExtensionTables": [],
            },
        }
        try:
            exec_response = await asyncio.wait_for(
                client.post(f"{base_url}/{EXECUTE_PATH}", api_key, json_body=exec_body),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return ProbeResult(False, error=f"probe exceeded {timeout_s:g}s",
                               ms=(time.monotonic() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(False, error=f"execute: {_epicor_detail(exc)['message']}",
                               ms=(time.monotonic() - started) * 1000)
        ret = exec_response.get("returnObj")
        ms = (time.monotonic() - started) * 1000
        if not isinstance(ret, Mapping) or (ret.get("Errors") or []):
            return ProbeResult(False, error="the probe returned an error", ms=ms)
        rows = [r for r in (ret.get("Results") or []) if isinstance(r, Mapping)]
        return ProbeResult(True, rows=[dict(r) for r in rows], ms=ms)

    return probe


async def run_sql(
    sql: str,
    *,
    client: Any,
    api_key: str,
    base_url: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_num: int = 1,
    governor: CostGovernor | None = None,
    governor_policy: GovernorPolicy | None = None,
    session_id: str = "anonymous",
    max_bytes: int = 700_000,
    diagnose: bool = True,
    probe_budget: int = DEFAULT_PROBE_BUDGET,
    domain_cache: DomainCache | None = None,
    company_id: str | None = None,
    validate_columns: bool = True,
    ground_domains: bool = True,
    lint_fanout_warning: bool = False,
    table_scope: Any = None,
    _sort_wrapped: bool = False,
) -> dict[str, Any]:
    """Run one SELECT and return rows, or an INV-1 envelope. Never raises.

    *table_scope* is the caller's menu-derived table authorization (an
    ``AuthzScope``-shaped object) or ``None`` — and ``None`` means UNGATED,
    byte-identically the pre-gate behaviour, which is what keeps the standalone
    wedge server and every existing test untouched. ``server.py`` injects a
    real scope through ``WedgeRuntime`` when ``EPICOR_MCP_TABLE_AUTHZ_MODE`` is
    ``gate``.

    *_sort_wrapped* is private: it marks the ONE re-entry made after step 3c
    re-wrote an over-long ORDER BY into a CTE, so a second long key refuses
    instead of wrapping again.
    """
    started = time.monotonic()
    stage_ms: dict[str, float] = {}

    def _took(name: str, since: float) -> None:
        """Record a LOCAL stage's cost, but only when it is worth a byte.

        Sub-millisecond stages are dropped, so the happy path carries an empty
        dict and the key never appears. That keeps the latency budget auditable
        in production without spending prompt tokens on five zeros.
        """
        elapsed = (time.monotonic() - since) * 1000
        if elapsed >= 1.0:
            stage_ms[name] = round(elapsed, 1)
    governor = governor or CostGovernor(governor_policy or GovernorPolicy())
    policy = governor.policy
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    if page_size <= 0:
        page_size = DEFAULT_PAGE_SIZE
    page_size = min(page_size, MAX_PAGE_SIZE)
    try:
        page_num = max(1, int(page_num))
    except (TypeError, ValueError):
        page_num = 1

    budget = governor.check_budget(session_id)
    if budget is not None:
        return budget

    # --- 1. transpile: the PROVEN SUBSET only (rewrite-safety policy) ----------------
    # No schema is supplied, which is a second, independent reason S1 and S3
    # cannot fire (unresolved sources must remain unresolved).
    #
    # The injected row bound tracks the page the caller ASKED for. A fixed
    # `top 100` under `page_size=1000` would silently trim the answer to a
    # tenth of the requested page and then report it as complete — the same
    # class of defect as the legacy tools binding `limit` to the GetRows pageSize.
    policy_for_sql = replace(WEDGE_POLICY, default_limit=page_size, max_rows=MAX_PAGE_SIZE)
    # The UD mirror map powers the `ud_mirror_join` rewrite here
    # and the layer-1 recovery in E14 below. Loaded ONLY when the statement
    # mentions a `_c` name, deny-filtered at load (see `_ud_mirrors_for`), and
    # `None` keeps both consumers byte-identical to their pre-UD behaviour.
    ud_started = time.monotonic()
    ud_mirrors = _ud_mirrors_for(sql)
    _took("ud_mirrors", ud_started)
    transpile_started = time.monotonic()
    result = transpile(
        sql,
        schema=None,
        policy=policy_for_sql,
        ud_mirrors=ud_mirrors,
        ud_catalogue=_catalogue_for_scope(table_scope) if ud_mirrors else None,
    )
    _took("transpile", transpile_started)
    if result.outcome is Outcome.REFUSED:
        env = dict(result.error or {})
        env.setdefault("success", False)
        env.setdefault("terminal", False)
        env["detail"] = {**env.get("detail", {}), "stage": "transpile"}
        return env
    sql_to_run = result.sql or sql
    assumptions: dict[str, Any] = {}
    if result.transformations:
        assumptions["rewrites"] = [t.to_dict() for t in result.transformations]
    if result.advisories:
        assumptions["advisories"] = [a.to_dict() for a in result.advisories]
    if result.row_bound:
        assumptions["row_bound"] = result.row_bound.to_dict()

    # --- 1b. a page that cannot contain rows is refused, never returned ---
    # ...and refused HERE, before ParseFromSQL, so it costs no Epicor call.
    unreachable = _page_reachability_refusal(result.row_bound, page_size, page_num)
    if unreachable is not None:
        detail = unreachable.setdefault("detail", {})
        detail["stage"] = "paging"
        detail["sql_sent"] = sql_to_run
        return unreachable

    # --- 1c. E14: does every column actually exist? -----------------------
    # Local, ~0.5 ms, ZERO Epicor calls, and it runs BEFORE ParseFromSQL —
    # which is the whole feature. Observed behavior: a phantom column is not a
    # silent zero. It parses 200 and then FAILS at Execute with
    # `Bad SQL statement. Review the server event logs for details.`, and only a
    # THIRD call (Analyze) says `Invalid column name 'X'.` — a phantom never
    # returns rows. So this replaces an extra round trip and a
    # message with no recovery in it. It cannot fix a wrong answer, because a
    # phantom column never produced one.
    #
    # The catalogue is DENY-FILTERED (`_safe_catalogue`). Read its docstring
    # before moving this line: E14 serves a table's real column names, so an
    # unfiltered catalogue makes this gate a pre-authorization schema leak.
    if validate_columns:
        validate_started = time.monotonic()
        checked = colvalid.validate_columns(
            sql_to_run, catalogue=_catalogue_for_scope(table_scope), ud_mirrors=ud_mirrors
        )
        _took("validate_columns", validate_started)
        if not checked.ok and checked.envelope:
            env = dict(checked.envelope)
            env["detail"] = {
                **(env.get("detail") or {}),
                "stage": "validate_columns",
                "sql_sent": sql_to_run,
                # Same claim the lint's own refusal makes, and here it is
                # stronger: nothing was sent to Epicor at all.
                "checked_before_running": True,
                "epicor_calls": 0,
            }
            return env

    # --- 2. ParseFromSQL --------------------------------------------------
    body = copy.deepcopy(_PARSE_BODY)
    body["ds"]["DynamicQueryDesigner"][0]["DisplayPhrase"] = sql_to_run
    parse_started = time.monotonic()
    try:
        parse_response = await client.post(f"{base_url}/{PARSE_PATH}", api_key, json_body=body)
    except Exception as exc:  # noqa: BLE001 - EpicorError and transport errors alike
        detail = _epicor_detail(exc)
        connection = _connection_envelope(detail, "parse")
        if connection is not None:
            return connection
        message = detail.get("message") or ""
        inaccessible = "inaccessible table" in message.lower()
        return error_envelope(
            "table_not_accessible" if inaccessible else "sql_parse_error",
            (
                "Epicor refused this statement at parse time. Its message, verbatim: "
                f"{message}"
            ),
            evidence="Parse-error handling: a parse failure is HTTP 400 and Epicor's text "
            "is human-readable, so it is passed through rather than paraphrased",
            valid={
                "shape": "select top 100 [A].[Col] as [Name] from Erp.Table as [A] "
                "where ... order by [A].[Col] desc"
            },
            detail={**detail, "stage": "parse", "sql_sent": sql_to_run},
            terminal=inaccessible,
        )
    parse_ms = (time.monotonic() - parse_started) * 1000

    parsed = _extract_parsed_ds(parse_response)
    if parsed is None:
        return error_envelope(
            "server_error",
            "Epicor accepted the parse but returned a tableset this server does not "
            "recognise. This is a server bug, not a problem with your SQL.",
            evidence="Malformed-response handling: a malformed queryDS is OUR bug, never the "
            "caller's",
            detail={"stage": "parse", "keys": sorted(parse_response.keys())[:20]},
        )
    queryds = _designer_to_runtime(parsed)

    # --- 3. ENFORCE the deny-list — before anything is served back --------
    denial = denylist.check_parsed_ds(queryds)
    if denial:
        logger.warning(
            "epicor_query DENIED session=%s tables=%s columns=%s anomalies=%s",
            session_id, denial.denied_tables, denial.denied_columns, denial.anomalies,
        )
        denied_env = denylist.denial_envelope(denial, sql=sql_to_run)
        # Every other gate names itself in `detail.stage`; this one did not, so
        # a caller could not tell an authorization refusal from a cost refusal
        # without reading the prose. The stage names are the only machine-
        # readable "which gate stopped me" the envelope has.
        denied_env["detail"] = {**(denied_env.get("detail") or {}), "stage": "denylist"}
        return denied_env
    tables_read = denial.allowed_tables

    # --- 3b. the menu-derived table gate (scope_gate.py) ------------------
    # AFTER the deny-list — deny beats everything, so a payroll table inside
    # the caller's own scope still reads `table_access_denied`, never a
    # request-wider-access invitation — and BEFORE the lint, whose select_star
    # refusal serves a table's real column list: the same serve-nothing-first
    # ordering that put the deny-list at step 3. Reads the SAME extraction the
    # deny-list ran (`db_tables_read`): Epicor's own
    # resolved QueryTable rows, never the SQL text.
    if table_scope is not None:
        authz_refusal = scopegate.check_table_scope(
            table_scope, queryds, sql=sql_to_run
        )
        if authz_refusal is not None:
            logger.warning(
                "epicor_query NOT AUTHORIZED session=%s error=%s tables=%s",
                session_id,
                authz_refusal.get("error"),
                (authz_refusal.get("detail") or {}).get("unauthorized_tables"),
            )
            return authz_refusal

    # --- 3c. sort keys Epicor cannot store ------------------------------
    # After the deny-list and the scope gate (deny beats everything), before
    # the lint. Measured: Epicor stores each ORDER BY term as its OWN rendering
    # in QuerySortBy.FieldName, and a key over 125 characters fails at Execute
    # with "An object or column name is missing or empty" — CASE or no CASE.
    # Only the parsed DS knows that length, so this is the earliest point it can
    # be seen. The outer ORDER BY is re-written ONCE into a CTE that sorts on a
    # named column, and the rewritten statement re-enters the pipe from the top
    # so every gate judges what will actually run.
    long_keys = _long_sort_keys(queryds)
    if long_keys:
        wrap = None if _sort_wrapped else wrap_sort_in_cte(sql_to_run)
        if wrap is not None and wrap.sql:
            inner = await run_sql(
                wrap.sql,
                client=client,
                api_key=api_key,
                base_url=base_url,
                page_size=page_size,
                page_num=page_num,
                governor=governor,
                session_id=session_id,
                max_bytes=max_bytes,
                diagnose=diagnose,
                probe_budget=probe_budget,
                domain_cache=domain_cache,
                company_id=company_id,
                validate_columns=validate_columns,
                ground_domains=ground_domains,
                lint_fanout_warning=lint_fanout_warning,
                table_scope=table_scope,
                _sort_wrapped=True,
            )
            return _merge_sort_wrap(inner, assumptions, wrap.transformation, started)
        return _sort_key_refusal(
            long_keys,
            sql_to_run,
            why_not=(
                wrap.why_not
                if wrap is not None
                else "the over-long key is inside a CTE, derived table or subquery "
                "rather than the outer ORDER BY"
            ),
        )

    # --- 4. lint ----------------------------------------------------------
    lint_started = time.monotonic()
    findings = lintmod.lint_parsed(
        sql_to_run, queryds, fanout_warning=lint_fanout_warning
    )
    _took("lint", lint_started)
    refusals = [f for f in findings if f.severity == lintmod.Severity.REFUSE]
    warns = [f for f in findings if f.severity == lintmod.Severity.WARN]
    if refusals:
        # An unresolved TABLE leads, always. It is the CAUSE of every blank
        # column Epicor reported under it, so a phantom-column headline over a
        # table that does not exist would send the caller after the wrong thing.
        first = next(
            (f for f in refusals if f.rule == "unknown_table"), refusals[0]
        )
        valid: dict[str, Any] = {}
        rewrites: dict[str, str] = {}
        for finding in refusals:
            if finding.rule == "select_star":
                valid["columns"] = finding.detail.get("columns", [])
            if finding.rule == "unknown_column":
                valid.setdefault("phantom_columns", []).append(
                    f"{finding.detail.get('table')}.{finding.detail.get('column')}"
                )
            if finding.rule == "unknown_table":
                written = finding.detail.get("written_as") or finding.detail.get("table")
                write = finding.detail.get("write")
                if write:
                    valid.setdefault("tables", {})[str(written)] = write
                    rewrites[str(finding.detail.get("table"))] = write
                else:
                    valid.setdefault("unknown_tables", []).append(str(written))
                near = finding.detail.get("did_you_mean")
                if near:
                    valid.setdefault("did_you_mean", []).extend(near)
        error = "sql_silently_wrong"
        if first.rule == "unknown_table":
            error = "sql_unknown_table"
        elif first.rule == "unknown_column":
            error = "sql_unknown_column"
        # A runnable recovery, not a template: the schema prefix spliced back
        # into the caller's own statement (masked offsets, so a literal or a
        # comment can never be rewritten).
        retry_with: dict[str, Any] | None = None
        if rewrites:
            fixed = lintmod.qualify_tables(sql_to_run, rewrites)
            if fixed:
                retry_with = {"sql": fixed}
        return error_envelope(
            error,
            first.message
            + (
                f" ({len(refusals)} problems found; the others are in detail.findings.)"
                if len(refusals) > 1
                else ""
            ),
            evidence=first.evidence,
            valid=valid or None,
            retry_with=retry_with,
            detail={
                "stage": "lint",
                "findings": lintmod.findings_to_dicts(refusals),
                "sql_sent": sql_to_run,
                "checked_before_running": True,
            },
        )

    # --- 5. cost governor -------------------------------------------------
    governor_started = time.monotonic()
    refusal = check_cost(queryds, policy=policy)
    _took("governor", governor_started)
    if refusal is not None:
        refusal.setdefault("detail", {})["stage"] = "governor"
        return refusal

    # --- 6/7. Execute -----------------------------------------------------
    exec_body = {
        "queryDS": queryds,
        "executionParams": {
            "ExecutionFilter": [],
            "ExecutionParameter": [],
            # Observed behavior: the setting names are exactly PageSize / PageNum;
            # PageNumber / Page / CurrentPage / SkipRows are SILENTLY IGNORED and
            # re-serve page 1 forever. PageSize=0 is UNBOUNDED. Values are strings.
            "ExecutionSetting": [
                {"Name": "PageSize", "Value": str(page_size)},
                {"Name": "PageNum", "Value": str(page_num)},
            ],
            "ExecutionValueSetItems": [],
            "ExtensionTables": [],
        },
    }
    exec_started = time.monotonic()
    try:
        async with governor.inflight():
            exec_response = await asyncio.wait_for(
                client.post(f"{base_url}/{EXECUTE_PATH}", api_key, json_body=exec_body),
                timeout=policy.execute_timeout_s,
            )
    except asyncio.TimeoutError:
        governor.record(session_id, time.monotonic() - started)
        return timeout_envelope(policy.execute_timeout_s, sql_to_run)
    except Exception as exc:  # noqa: BLE001
        governor.record(session_id, time.monotonic() - started)
        detail = _epicor_detail(exc)
        connection = _connection_envelope(detail, "execute")
        if connection is not None:
            return connection
        status = detail.get("status")
        message = detail.get("message") or ""
        if status == 400 and "inaccessible table" in message.lower():
            return error_envelope(
                "table_not_accessible",
                "Epicor refuses this table to every caller, including a Security Manager: "
                f"{message}",
                evidence="Epicor table-access behavior: Ice.UserFile, Ice.UserComp, Ice.SessionState, "
                "Erp.PayrollExp and IM.* return 400 `References to inaccessible tables "
                "detected` even for a hand-built DS — a structural ceiling, not a server rule",
                detail={**detail, "stage": "execute"},
                terminal=True,
            )
        if status and int(status) >= 500:
            return error_envelope(
                "server_error",
                "Epicor returned a server error. This is a server or Epicor fault, not a "
                "problem with your SQL — it has been logged.",
                evidence="An upstream HTTP 5xx is a server-side failure, not proof of invalid SQL",
                detail={**detail, "stage": "execute"},
            )
        return error_envelope(
            "sql_run_error",
            f"The query failed at execution: {message}",
            evidence="Execution failures preserve the upstream explanation and the SQL that was sent",
            detail={**detail, "stage": "execute", "sql_sent": sql_to_run},
        )
    exec_ms = (time.monotonic() - exec_started) * 1000
    governor.record(session_id, time.monotonic() - started)

    ret = exec_response.get("returnObj")
    if not isinstance(ret, Mapping):
        return error_envelope(
            "server_error",
            "Epicor returned 200 with no result object. This is a server bug.",
            detail={"stage": "execute", "keys": sorted(exec_response.keys())[:20]},
        )

    # --- 8. HTTP 200 is NEVER success on its own (inspect execution errors) ----
    errors = ret.get("Errors") or []
    if errors:
        analyzed = await _analyze(client, api_key, base_url, queryds)
        texts = [
            str(e.get("ErrorText") or e) if isinstance(e, Mapping) else str(e)
            for e in errors
        ]
        return error_envelope(
            "sql_run_error",
            "The query ran and failed. Epicor's own text is always the useless "
            f"`{texts[0]}`, so the server re-ran it through Analyze, which says: "
            + ("; ".join(analyzed) if analyzed else "(Analyze returned nothing further)"),
            evidence="Execution-error handling: HTTP 200 with a populated returnObj.Errors is "
            "how every run-time defect surfaces; Analyze on the DS already held returns "
            "the real `Invalid column name 'X'.`",
            detail={
                "stage": "execute",
                "epicor_errors": texts[:10],
                "analyze": analyzed[:10],
                "sql_sent": sql_to_run,
            },
        )

    rows = ret.get("Results")
    if not isinstance(rows, list):
        rows = []
    rows = [r for r in rows if isinstance(r, Mapping)]
    columns = _columns_of(rows)
    tsv = rows_to_tsv(rows, columns)

    dropped = 0
    if len(tsv.encode("utf-8")) > max_bytes:
        keep = len(rows)
        while keep > 0 and len(rows_to_tsv(rows[:keep], columns).encode("utf-8")) > max_bytes:
            keep = int(keep * 0.8) if keep > 10 else keep - 1
        dropped = len(rows) - keep
        rows = rows[:keep]
        tsv = rows_to_tsv(rows, columns)

    full_page = len(rows) + dropped >= page_size
    # An EMPTY later page is not an answer. `_page_reachability_
    # refusal` catches the shapes we can predict from the row bound; this is the
    # belt-and-braces for any shape we cannot — a `select distinct` bounded only
    # by PageSize, a set-operation branch bound, anything a future change adds.
    # "0 rows on page 4" must never read as "this is the complete result".
    empty_later_page = page_num > 1 and not rows and dropped == 0
    complete = not full_page and dropped == 0 and not empty_later_page
    sql_ms = None
    for info in ret.get("ExecutionInfo") or []:
        if isinstance(info, Mapping) and info.get("Name") == "ExecutionTime":
            try:
                sql_ms = float(info.get("Value"))
            except (TypeError, ValueError):
                sql_ms = None

    if dropped:
        summary = (
            f"INCOMPLETE: {len(rows)} of {len(rows) + dropped} rows returned — the rest "
            "were dropped to stay under the response size cap. Select fewer columns or "
            "narrow the query."
        )
    elif empty_later_page:
        summary = (
            f"EMPTY PAGE: page {page_num} returned 0 rows. That is NOT evidence that the "
            "result set ended — a later page can be empty because the statement's row bound "
            "was reached, not because the data was. Re-read page 1, or walk the set with "
            f"keyset pagination ({_KEYSET_RECIPE})."
        )
    elif full_page:
        summary = (
            f"INCOMPLETE: {len(rows)} rows, which is a FULL page (page_size="
            f"{page_size}). There are almost certainly more. Narrow the query, raise "
            "page_size (max 1000), or aggregate instead of listing. This server does not "
            "issue a paging cursor: to read past one page, "
            f"{_KEYSET_RECIPE}."
        )
    else:
        summary = (
            f"{len(rows)} row(s) — a partial page, so this is the complete result for "
            "this query."
        )
    # --- 8b. grain (grain analysis) ---------------------------------
    # Never a refusal, and deliberately appended AFTER the lint's own warnings:
    # `lint.aggregate_fanout` is the DS-side rule and stays the first note, so
    # this is strictly additive. `rows=` runs the free post-execution
    # duplicate-collapse check on the page the caller is about to be shown.
    grain_started = time.monotonic()
    grain_report = grainmod.analyse_grain(sql_to_run, rows=rows)
    _took("grain", grain_started)
    grain_notes = _tag(grain_report.to_dicts(), "grain")

    # --- 8c. E15a value grounding ------------------------------
    # `ground()` is a pure function over the SQL text and a dated snapshot: no
    # Epicor call, no rows read, ~0.3 ms. `row_count` is what makes it honest —
    # at `> 0` it returns only the `certain` rules (a whole-table measurement a
    # right query cannot contradict) and withholds every rule that can go stale.
    #
    # TWO COHERENCE RULES, both of them the point of this integration:
    #  * On a ZERO-row page 1 the findings do NOT become notes. They are folded
    #    into `diagnosis` instead (`_fold_static_into_diagnosis`), because the
    #    diagnosis is the single owner of "why is this empty" and two answers to
    #    one question is the failure mode this pipe is being wired to avoid.
    #  * A `causes_empty` finding next to rows that DID come back is refuted by
    #    the caller's own result. It is dropped and logged, never served: telling
    #    a model "your Company filter matches nothing" above 200 matching rows
    #    teaches it to distrust the notes channel entirely.
    zero_rows_page1 = not rows and not dropped and page_num == 1
    domain_findings: list[Any] = []
    domain_notes: list[dict[str, Any]] = []
    if ground_domains:
        ground_started = time.monotonic()
        domain_findings = domainsmod.ground(sql_to_run, row_count=len(rows) + dropped)
        _took("ground", ground_started)
        if rows or dropped:
            refuted = [f for f in domain_findings if f.causes_empty]
            if refuted:
                logger.warning(
                    "E15a says these predicates match nothing, but the query returned "
                    "%d row(s) — the catalogue (as of %s) is stale: %s",
                    len(rows) + dropped,
                    domainsmod.AS_OF,
                    [f"{f.table}.{f.column}" for f in refuted],
                )
            domain_notes = _domain_notes(f for f in domain_findings if not f.causes_empty)
        elif zero_rows_page1 and not diagnose:
            # Nothing came back on page 1 and the measured diagnostician is
            # switched off. The dated snapshot is then the best explanation
            # available, so it is served as notes — never as a claim that the
            # answer is wrong.
            #
            # An empty LATER page gets NOTHING: `empty_later_page` already owns
            # that explanation, and it is the right one — a later page can be
            # empty because the row bound was reached, not because the data was.
            # A value note there would be a second, competing answer about a
            # page that structurally cannot hold rows.
            domain_notes = _domain_notes(domain_findings)

    # ONE summary line, one owner, in a fixed precedence. A zero-row page 1
    # overrides all of this below — the diagnosis owns that response outright.
    lint_notes = _tag(lintmod.findings_to_dicts(warns), "lint")
    if warns:
        summary = warns[0].message + " || " + summary
    elif grain_report.findings:
        summary = grain_report.findings[0].message + " || " + summary
    else:
        # An E15a note may lead ONLY when it is a WARN — i.e. the predicate sits
        # in a plain top-level AND chain and the snapshot says it matches
        # nothing. An INFO advisory must not displace paging information in
        # the summary of an otherwise successful aggregate.
        # Completeness is the one thing the model must read first; the advisory
        # still rides in `notes`.
        leading = next(
            (n for n in domain_notes if n.get("severity") == "WARN"), None
        )
        if leading:
            summary = leading["message"] + " || " + summary

    result: dict[str, Any] = {
        "success": True,
        "columns": columns,
        "rows": tsv,
        "format": "tab-separated; the first line is the header; an empty field is null",
        "row_count": len(rows),
        "rows_dropped_for_size": dropped,
        "complete": complete,
        "terminal": complete,
        "summary": summary,
        # Declared HERE, next to `summary`, so it lands early in the JSON the
        # model reads — and filled (or dropped) at the very end of the function,
        # once `diagnosis` exists. `annotate_next_step` is the single owner.
        "next_step": "",
        "tables_read": tables_read,
        "sql_executed": sql_to_run,
        "assumptions": assumptions,
        "notes": lint_notes + grain_notes + domain_notes,
        "sql_ms": sql_ms,
        "parse_ms": round(parse_ms, 1),
        "execute_ms": round(exec_ms, 1),
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    if grain_report.verifications:
        # Offered, never auto-run: each one is a real Execute against production
        # and the caller decides whether the number is worth a second call. The
        # statement is runnable as-is and answers the same question at the right
        # grain (the check measures the relevant join multiplicity).
        result["grain_checks"] = list(grain_report.verifications)

    # --- 9. ZERO rows on page 1: diagnose it (Feature E15b) ----------------
    # The gate is `not rows and page_num == 1`, so a query that returned data
    # never enters `diagnose_empty` at all — no parse, no probe, no cost.
    # An empty LATER page is a paging artefact, already answered by
    # `empty_later_page` above, and diagnosing it would be a false alarm.
    if diagnose and zero_rows_page1:
        diag_started = time.monotonic()
        diagnosis = await diagnose_empty(
            sql_to_run,
            probe=make_probe_runner(
                client=client,
                api_key=api_key,
                base_url=base_url,
                timeout_s=policy.execute_timeout_s,
                session_id=session_id,
                table_scope=table_scope,
            ),
            budget=probe_budget,
            cache=domain_cache,
            company_id=company_id,
        )
        # E15a's dated findings join the MEASURED diagnosis rather than
        # competing with it from the notes channel. Corroboration is dropped,
        # a disagreement is published as `static_catalogue_stale` with the live
        # probe winning, and anything the budget never reached rides as
        # `static_evidence`. It cannot raise `likely_mistake`.
        _fold_static_into_diagnosis(diagnosis, domain_findings)
        result["diagnosis"] = diagnosis
        result["summary"] = "ZERO ROWS — " + diagnosis["message"]
        # `terminal` is only ever taken AWAY, and only when there is something
        # concrete to do about it. An empty result the diagnostician cannot
        # fault stays the clean terminal answer it was (Epicor behavior: an
        # empty result is often the CORRECT answer).
        if diagnosis.get("likely_mistake"):
            result["terminal"] = False
        # The probes are real work against production, so they are charged to
        # the session budget like any other Execute.
        diag_s = time.monotonic() - diag_started
        governor.record(session_id, diag_s)
        result["diagnose_ms"] = round(diag_s * 1000, 1)
        result["elapsed_s"] = round(time.monotonic() - started, 3)

    if stage_ms:
        result["stage_ms"] = stage_ms
    # --- 10. what to DO with all of this, in band -----------
    # Last, because it is DERIVED: it reads `diagnosis`, `notes` and
    # `grain_checks` and names one action. It adds no claim of its own, and on a
    # clean result it removes its own key so the happy path costs nothing.
    return annotate_next_step(result)


def _long_sort_keys(queryds: Mapping[str, Any]) -> list[dict[str, Any]]:
    """QuerySortBy rows whose rendered key is longer than Epicor can store."""
    rows = queryds.get("QuerySortBy") or queryds.get("QuerySortByDesigner") or []
    out = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("FieldName") or "")
        if len(key) > SORT_KEY_MAX_CHARS:
            out.append({"length": len(key), "sort_key": key[:200]})
    return out


def _merge_sort_wrap(
    inner: dict[str, Any],
    outer_assumptions: Mapping[str, Any],
    wrap_tx: Any,
    started: float,
) -> dict[str, Any]:
    """Put the FIRST pass's assumptions back in front of the re-entered run's.

    The re-entry transpiles the wrapped statement afresh, so on its own it would
    forget every rewrite the caller's statement already received (an
    `order_by_alias`, an injected `top`) and misreport the row bound as the
    caller's. The row bound belongs to the caller's statement, so the first
    pass's wins.
    """
    wrap_dict = wrap_tx.to_dict() if wrap_tx is not None else None
    if not inner.get("success"):
        detail = inner.setdefault("detail", {})
        if isinstance(detail, dict) and wrap_dict:
            detail["sort_key_wrapped"] = wrap_dict
        return inner
    got = inner.get("assumptions") or {}
    merged: dict[str, Any] = {}
    rewrites = list(outer_assumptions.get("rewrites") or [])
    if wrap_dict:
        rewrites.append(wrap_dict)
    seen = {(r.get("rule"), r.get("before")) for r in rewrites}
    rewrites += [r for r in got.get("rewrites") or [] if (r.get("rule"), r.get("before")) not in seen]
    if rewrites:
        merged["rewrites"] = rewrites
    advisories = list(outer_assumptions.get("advisories") or [])
    seen_adv = {a.get("rule") for a in advisories}
    advisories += [a for a in got.get("advisories") or [] if a.get("rule") not in seen_adv]
    if advisories:
        merged["advisories"] = advisories
    row_bound = outer_assumptions.get("row_bound") or got.get("row_bound")
    if row_bound:
        merged["row_bound"] = row_bound
    inner["assumptions"] = merged
    inner["elapsed_s"] = round(time.monotonic() - started, 3)
    return inner


def _sort_key_refusal(
    long_keys: list[dict[str, Any]], sql: str, *, why_not: str
) -> dict[str, Any]:
    worst = max(k["length"] for k in long_keys)
    return error_envelope(
        "sql_sort_key_too_long",
        f"An ORDER BY expression is {worst} characters as Epicor renders it. Epicor stores at "
        f"most {SORT_KEY_MAX_CHARS} characters per sort key, and a longer one fails at "
        "execution with `An object or column name is missing or empty` — this has nothing to "
        "do with CASE or any other construct in it. The server could not re-write it "
        f"automatically because {why_not}. Fix: compute the expression as a named column in "
        "a CTE and sort on that column.",
        evidence="measured against Epicor Kinetic: a rendered sort key of 125 characters runs "
        "and 126 fails, stepped over 122..134 on one expression; the CTE form returned the "
        "independently computed top N",
        valid={
            "shape": "with [q] as (select [A].[Col] as [Col], <long expression> as [SortKey] "
            "from Erp.A as [A] where …) select top 100 [q].[Col] as [Col] from [q] order by "
            "[q].[SortKey] desc",
            "max_sort_key_chars": SORT_KEY_MAX_CHARS,
        },
        detail={
            "stage": "sort_key",
            "long_sort_keys": long_keys,
            "sql_sent": sql,
            "checked_before_running": True,
        },
    )


async def _analyze(
    client: Any, api_key: str, base_url: str, queryds: Mapping[str, Any]
) -> list[str]:
    """Ask Epicor what actually broke: one extra call, and only on the failure path.

    error-envelope contract: *recover* with Analyze, do not pre-flight with it — pre-flighting
    costs an extra round trip on every success to save one on failures. When Analyze
    returns *"An object or column name is missing or empty…"* the message is a
    mask produced by a sort key Epicor cannot store (step 3c refuses the
    measured over-length case before Execute; anything else that trips it lands
    here), so the sort is stripped and it is re-analysed.
    """
    async def _call(ds: Mapping[str, Any]) -> list[str]:
        try:
            res = await client.post(
                f"{base_url}/{ANALYZE_PATH}", api_key,
                json_body={"queryDS": ds, "executionParams": {}},
            )
        except Exception as exc:  # noqa: BLE001
            return [f"(Analyze itself failed: {getattr(exc, 'message', exc)})"[:300]]
        params = res.get("parameters") or {}
        msgs = [str(m) for m in (params.get("errorMessages") or [])]
        analyze_result = (params.get("analyzeResult") or {}).get("QueryAnalyzeResult") or []
        for row in analyze_result:
            if isinstance(row, Mapping) and row.get("MessageText"):
                msgs.append(str(row["MessageText"]))
        return list(dict.fromkeys(msgs))

    msgs = await _call(queryds)
    if any("missing or empty" in m.lower() for m in msgs) and queryds.get("QuerySortBy"):
        stripped = dict(queryds)
        stripped["QuerySortBy"] = []
        retry = await _call(stripped)
        if retry:
            return retry + [
                "(the ORDER BY was stripped and the query re-analysed — "
                "'missing or empty' is the mask Epicor gives a sort key it cannot store)"
            ]
    return msgs

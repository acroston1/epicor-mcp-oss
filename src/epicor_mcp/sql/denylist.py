'The hard denylist. Deny beats everything, including SecurityMgr.'

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from epicor_mcp.sql.envelope import error_envelope

logger = logging.getLogger(__name__)

__all__ = [
    "DENIED_TABLE_PATTERNS",
    "DENIED_COLUMNS_ANY_TABLE",
    "DENIED_COLUMNS_PERSON_TABLES",
    "PERSON_KEYED_TABLES",
    "Denial",
    "check_parsed_ds",
    "db_tables_read",
    "denial_envelope",
    "denial_source",
    "install_table_blacklist",
    "install_table_blacklist_from_file",
    "is_denied_table",
    "is_denied_column",
    "parse_table_blacklist",
]

#: Wholly-sensitive tables. Patterns are matched case-insensitively against the
#: ``Schema.Table`` Epicor resolved, and ``*`` is a suffix wildcard.
DENIED_TABLE_PATTERNS: tuple[str, ...] = (
    # ---- the payroll family, ENUMERATED ---------------------------------
    # Observed behavior: this was a single `Erp.PR*`, and `_matches` is a
    # case-insensitive prefix test, so `erp.prodgrup` and `erp.project` both
    # start with `erp.pr`. LIVE, `select top 3 [G].[ProdCode] from Erp.ProdGrup`
    # was refused with *"hold payroll / security data and are denied to every
    # user"*. The product-group master and every project-costing table in the
    # install were denied to the whole company, under a message telling an
    # ordinary business question it had touched payroll data. `is_denied_table`
    # returned True for Project, ProjPhase, ProdGrup, PriceLst, PrcChg and
    # Prospect.
    #
    # Enumerate payroll families so unrelated names such as PriceLst,
    # ProdGrup and Project do not match a case-insensitive `Erp.PR*` prefix.
    # Keep families even when absent from an operator's imported catalogue:
    # an unused deny entry costs nothing, a missing one can expose data.
    # THREE layers still stand behind this list, which is why enumerating is
    # safe: (a) `_PAYROLL_CANONICAL` below catches any FUTURE `Erp.PR<Upper>`
    # table on Epicor's own canonical resolution; (b) `DENIED_COLUMNS_ANY_TABLE`
    # denies SocSecNum / PayRate* / Salary* / LaborRate / BirthDate on EVERY
    # table, so even an unlisted payroll table cannot yield the sensitive
    # columns; (c) Epicor's own row-level security. **This list still needs
    # human approval under Feature S1 — it is generated evidence, not a sign-off.**
    "Erp.PRCheck*",
    "Erp.PRChk*",
    "Erp.PRClass*",
    "Erp.PRCls*",
    "Erp.PRDeduct*",
    "Erp.PREmp*",
    "Erp.PRHold*",
    "Erp.PRJob*",
    "Erp.PRPay*",
    "Erp.PRRate*",
    "Erp.PRSyst*",
    "Erp.PRTax*",
    "Erp.PRVoid*",
    "Erp.PRW2*",
    "Erp.PRWrkCmp*",
    "Erp.Payroll*",
    "Erp.ExtPREmp",      # holds SocSecNum and matches NEITHER Erp.PR* NOR Erp.Payroll*
    "Erp.EmpBasicAttch",
    "Erp.PayrollExp",
    "Ice.Security*",
    # **The tables are `Erp.`, so `Ice.`-only entries would make the deny-list
    # bypassable by writing the CORRECT name.**
    # Epicor's own catalogue (`Ice.BO.BAQDesignerSvc/GetTableList`) reports
    # `UserFile` and `UserComp` with `DBSchemaName = "Erp"` and `FullTableName =
    # "Erp.UserFile"` / `"Erp.UserComp"`. `is_denied_table` matches the
    # `Schema.Table` Epicor RESOLVED, so with `Ice.` entries alone:
    #     is_denied_table("Ice.UserFile") -> True
    #     is_denied_table("Erp.UserFile") -> False   <- the canonical spelling
    # `select ... from Erp.UserFile` would execute and return rows, while the
    # same query written `Ice.UserFile` is refused. The bare-name fallback in
    # `is_denied_table` catches an unqualified `UserFile`, so a test that writes
    # only `Ice.` or the bare name cannot see the gap.
    #
    # BOTH spellings are carried rather than just the correct one: a wrong
    # deny-list entry costs nothing, a missing one leaks, and `Ice.` remains the
    # name a caller may reasonably type for an Ice-flavoured system table.
    "Erp.UserFile*",
    "Ice.UserFile*",
    "Erp.UserComp*",
    "Ice.UserComp*",
    "Ice.SysUserFile",
    "Erp.SysUserFile",
    "Ice.ExtSecurity",
    "Erp.ExtSecurity",
)

#: Compensation / PII column names, denied on EVERY table. A generated list
#: (Feature S1) will widen this; it must never narrow it silently.
DENIED_COLUMNS_ANY_TABLE: tuple[str, ...] = (
    "LaborRate",
    "OverRidePayRate",
    "PayRate*",
    "Salary*",
    "SSN*",
    "SocSecNum",
    "SocSecNumber",











    "DspSocSecNum",
    "BirthDate",
    "DateOfBirth",
    "HourlyRate",
    "WageRate",
)

#: Rates that are compensation ONLY when the table is keyed by a person.
DENIED_COLUMNS_PERSON_TABLES: tuple[str, ...] = (
    "ChargeRate",
    "BurdenRate",
    "BillServiceRate",
)

#: Tables where a "rate" column is somebody's pay rather than a costing factor.
#: Names are matched on the bare table name, schema-insensitively.
PERSON_KEYED_TABLES: frozenset[str] = frozenset(
    {
        "labordtl", "labordtlimport", "laborhed", "empbasic", "labexpcd",
        "fscallsv", "fsservcd", "joboper", "employee", "premmpmas", "premmas",
        "premmpdtl", "empexpense",
    }
)

_VALID_TABLE_TYPES = frozenset({"DB", "SQ", "TT"})

#: `LD.LaborRate` inside a computed Formula. Epicor writes the TableID, not the
#: caller's bracket syntax, so this reads Epicor's OWN resolution — it is not a
#: hand-rolled parse of the model's SQL.
_FORMULA_REF = re.compile(r"(?<![\w.])([A-Za-z_][\w]*)\s*\.\s*([A-Za-z_][\w]*)")
_FORMULA_BARE = re.compile(r"(?<![\w.])([A-Za-z_][\w]*)(?![\w.(])")

#: A single-quoted literal inside an expression Epicor rendered, `''` escape
#: included. It is DATA, never a column reference, and it is blanked before the
#: reference scan for the same reason resolved-dataset authorization rule refuses to read the caller's SQL:
#: `isnull(P.ClassID, 'LaborRate')` names no pay column, and a literal that
#: merely LOOKS like `alias.column` (`'GHOST.Col'`) must not manufacture an
#: unresolved-table anomaly against a perfectly clean query.
_SQL_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _mask_literals(text: str) -> str:
    """Blank every quoted literal, preserving length so nothing else shifts."""
    return _SQL_LITERAL.sub(lambda m: " " * len(m.group(0)), text)


@dataclass(frozen=True)
class Denial:
    """Everything the caller needs to fix the query, or to know it cannot be."""

    denied_tables: list[str] = field(default_factory=list)
    denied_columns: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    allowed_tables: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.denied_tables or self.denied_columns or self.anomalies)


def _matches(name: str, patterns: Sequence[str]) -> bool:
    low = (name or "").strip().lower()
    if not low:
        return False
    for p in patterns:
        pl = p.lower()
        if pl.endswith("*"):
            if low.startswith(pl[:-1]):
                return True
        elif low == pl:
            return True
    return False


#: The structural backstop behind the enumerated payroll roots. Epicor names
#: every payroll table ``PR`` + an upper-case word (``PREmpMas``, ``PRChkDtl``,
#: ``PRW2Dtl``) while every non-payroll ``Pr`` table is ``Pr`` + lower case
#: (``Prod``, ``Proj``, ``Price``, ``Prj``, ``Pref``). It is deliberately CASE-SENSITIVE, so
#: it is a backstop and never the primary gate: the authority it runs against is
#: Epicor's own resolved ``DBTableName``, which is canonically cased
#: (``check_parsed_ds``), and the enumerated roots — matched case-insensitively —
#: cover everything a caller can type. Its whole job is a payroll table added in
#: a future Epicor version that no root names.
_PAYROLL_CANONICAL = re.compile(r"^PR[A-Z]")


#: The OPERATOR-EDITABLE blacklist (table_blacklist.txt), installed at
#: startup by :func:`install_table_blacklist`. Patterns are the BARE table half
#: only (schema stripped at install), matched against the bare half of the name
#: under test — so one entry covers every spelling ("PayTranHed",
#: "Erp.PayTranHed", "paytranhed"), schema-INSENSITIVELY. That is the fail-safe
#: direction, and it mirrors the built-in list's own practice of carrying both
#: the ``Erp.`` and ``Ice.`` spellings ("a wrong deny-list entry costs nothing,
#: a missing one leaks"). A tuple replaced WHOLESALE on install: the built-ins
#: above live in their own immutable tuple, so the file can only ever ADD
#: denials, never subtract one, and a re-install is idempotent by assignment.
_FILE_DENIED_PATTERNS: tuple[str, ...] = ()

#: Where the current file patterns came from — log/debug attribution only.
#: NEVER served in a user-facing envelope: the path is a server-internal detail
#: (the envelope says "the server's table blacklist", which is source enough).
_FILE_BLACKLIST_SOURCE: str = ""


def _builtin_denied(name: str) -> bool:
    """One spelling through the three SHIPPED layers — patterns, payroll
    canonical, bare-name fallback — with NO file input. Split from
    :func:`_denied_base` so
    :func:`denial_source` can answer *which* layer denied a name without
    re-implementing any of them: the envelope must not claim "payroll /
    security data" about a table the operator blacklisted for some other reason."""
    if _matches(name, DENIED_TABLE_PATTERNS):
        return True
    if _PAYROLL_CANONICAL.match(name.rsplit(".", 1)[-1]):
        return True
    if "." not in name:
        # No schema: test against every pattern's own table half, so a bare
        # `PREmpMas` is still caught. Fail closed, never open.
        bare = name.lower()
        for p in DENIED_TABLE_PATTERNS:
            tail = p.split(".", 1)[-1].lower()
            if tail.endswith("*"):
                if bare.startswith(tail[:-1]):
                    return True
            elif bare == tail:
                return True
    return False


def _file_denied(name: str) -> bool:
    """The operator-editable blacklist layer. Bare-half vs bare-half comparison —
    ``_matches`` supplies the case folding and the trailing-``*`` prefix
    wildcard, so the file rides the SAME machinery as the built-in patterns
    rather than a parallel implementation that could drift from it."""
    if not _FILE_DENIED_PATTERNS:
        return False
    return _matches(name.rsplit(".", 1)[-1], _FILE_DENIED_PATTERNS)


def _denied_base(name: str) -> bool:
    """One spelling through ALL the layers — the three built-in ones plus the
    file blacklist. Split out of :func:`is_denied_table` so the
    UD-mirror inheritance below re-tests the stripped parent through the SAME
    layers instead of duplicating any of them — which is also what makes a
    blacklisted table's ``<X>_UD`` mirror denied with zero extra code, and what
    lets every consumer (``check_parsed_ds``, discovery's hiding, E14's
    ``_safe_catalogue`` exclusion, the UD-mirror loader, both ``elsewhere``
    channels) see a file entry with zero new call sites."""
    return _builtin_denied(name) or _file_denied(name)


def is_denied_table(qualified: str) -> bool:
    """``Erp.PREmpMas`` -> True. Accepts a bare table name too.

    UD-MIRROR INHERITANCE: ``<X>_UD`` is denied whenever ``X``
    is — a payroll table's custom-field mirror must be exactly as invisible as
    the payroll table, and deny beats everything including the authz
    inheritance in ``discovery/authz.py``. The prefix wildcards already cover
    their mirrors (``Erp.PREmp*`` matches ``Erp.PREmpMas_UD``); the EXACT
    patterns do not. Without inheritance, ``Erp.UserFile_UD`` refuses at
    stage ``denylist`` while ``Ice.SysUserFile_UD`` — a security-master mirror
    that can carry ``_c`` columns — parses AND executes. The suffix is stripped
    only when the remainder is non-empty, and the stripped name runs the SAME
    three layers as the original. Every consumer inherits this from the one
    function: ``check_parsed_ds``, the discovery seams, and E14's
    ``adhoc._safe_catalogue`` exclusion.
    """
    name = (qualified or "").strip()
    if _denied_base(name):
        return True
    bare = name.rsplit(".", 1)[-1]
    if bare.lower().endswith("_ud") and len(bare) > len("_ud"):
        return _denied_base(name[: -len("_ud")])
    return False


def denial_source(qualified: str) -> str:
    """``"builtin"`` | ``"blacklist"`` — WHICH layer denies *qualified*.

    Message composers use it so a file-sourced denial is never described with
    the built-in claim ("payroll / security data") — a claim that would be
    false, and would teach the model a wrong fact about the data, for a table
    the operator blacklisted for some other reason. When BOTH layers deny the
    name, "builtin" wins: the payroll/security claim is then true and the file
    entry is merely redundant. Only meaningful for a name
    :func:`is_denied_table` already refuses; a name denied by neither returns
    "builtin", the safe default for callers who by contract hold a denied name.
    """
    name = (qualified or "").strip()
    if _builtin_denied(name):
        return "builtin"
    bare = name.rsplit(".", 1)[-1]
    if bare.lower().endswith("_ud") and len(bare) > len("_ud"):
        if _builtin_denied(name[: -len("_ud")]):
            return "builtin"
    return "blacklist" if is_denied_table(name) else "builtin"


# --------------------------------------------------------------------------- #
# The operator-editable table blacklist (table_blacklist.txt)
# --------------------------------------------------------------------------- #
#
# ONE registration seam, everything inherits: the file registers INTO
# `_FILE_DENIED_PATTERNS`, which `_denied_base` consults beside the built-in
# patterns — so `is_denied_table`, `check_parsed_ds` (ad-hoc SQL AND saved-BAQ
# definitions), discovery's `_table_denied`, E14's `_safe_catalogue`, the
# UD-mirror loader, every `elsewhere`/`closest_tables` channel and the `_UD`
# one-strip inheritance all see a file entry with ZERO extra call sites. It is
# deliberately NOT a second, parallel check anywhere else.
#
# Deny beats scope at every DECISION point: the deny-list is re-consulted live
# on every query and every discovery call.

#: One blacklist entry: a bare or schema-qualified SQL identifier, optional
#: trailing '*' prefix wildcard. Anything else — spaces, punctuation, a lone
#: '*' (which would deny EVERY table), a trailing dot — is a malformed line,
#: skipped LOUDLY: a silently-mangled entry protects nothing.
_BLACKLIST_ENTRY_RE = re.compile(r"^[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?\*?$")


def _parse_blacklist_lines(text: str) -> tuple[list[tuple[int, str]], list[str]]:
    """``[(lineno, bare_pattern), ...]`` plus human-readable warnings.

    The line numbers exist so a startup WARNING can point at the exact line —
    a typo'd blacklist entry protects nothing, and "somewhere in the file" is
    how a typo survives review.
    """
    entries: list[tuple[int, str]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        # '#' starts a comment — full-line or trailing. Stripped BEFORE the
        # shape check so `Erp.PayTranHed  # why` parses as the name alone.
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not _BLACKLIST_ENTRY_RE.match(line):
            warnings.append(
                f"line {lineno}: {raw.strip()!r} is not a table name (one bare or "
                "schema-qualified name per line, optional trailing '*') — line "
                "SKIPPED; it denies nothing"
            )
            continue
        # Normalise to the BARE table half at parse time: matching is
        # schema-insensitive by design (see _FILE_DENIED_PATTERNS), so
        # "Erp.PayTranHed" and "PayTranHed" are the same entry and duplicates
        # collapse silently — the policy is identical either way.
        pattern = line.rsplit(".", 1)[-1]
        key = pattern.lower()
        if key in seen:
            continue
        seen.add(key)
        entries.append((lineno, pattern))
    return entries, warnings


def parse_table_blacklist(text: str) -> tuple[tuple[str, ...], list[str]]:
    """Pure parse of a blacklist file's text: ``(names, warnings)``.

    Comments (full-line and trailing ``#``), blank lines, bare vs qualified vs
    case variants, duplicates and the trailing-``*`` wildcard are all handled;
    a malformed line becomes a warning naming the line, never an exception.
    """
    entries, warnings = _parse_blacklist_lines(text)
    return tuple(p for _, p in entries), warnings


def install_table_blacklist(names: Iterable[str], *, source: str) -> None:
    """REPLACE the file-sourced deny set with *names* (bare or qualified).

    * **Replace, not merge** — that is what makes a restart the only reload
      and the test suite hermetic (``install_table_blacklist((), ...)`` is a
      full reset of the file layer).
    * **Additive only** — the built-in patterns live in their own immutable
      tuple which this function never touches, so the file can never un-deny a
      built-in denial. There is no negation syntax on purpose.
    * **Cache hygiene** — two lazy caches snapshot the deny verdict:
      ``adhoc._SAFE_CATALOGUE`` (E14's deny-filtered catalogue) and
      ``validate_columns._load_ud_cached`` (the deny-filtered UD-mirror map).
      Both are reset here so a pre-install fill cannot serve a blacklisted
      table's schema for the rest of the process. Startup installs before any
      tool serves, so in production these resets are no-ops; they are the
      belt-and-braces that keeps the invariant true under ANY call order.
    """
    global _FILE_DENIED_PATTERNS, _FILE_BLACKLIST_SOURCE
    cleaned: list[str] = []
    seen: set[str] = set()
    for n in names or ():
        tail = str(n or "").strip().rsplit(".", 1)[-1]
        if not tail or tail == "*":
            # A lone '*' would deny EVERY table — a self-inflicted outage, not
            # a policy. The parser already refuses it; this guard covers direct
            # callers, loudly.
            logger.warning("table blacklist: ignoring unusable entry %r", n)
            continue
        if tail.lower() in seen:
            continue
        seen.add(tail.lower())
        cleaned.append(tail)
    _FILE_DENIED_PATTERNS = tuple(cleaned)
    _FILE_BLACKLIST_SOURCE = source
    try:  # E14's deny-filtered catalogue keys on the BASE catalogue's identity,
        # which never changes when the deny set does — without this reset a
        # pre-install fill would serve the un-blacklisted copy forever.
        from epicor_mcp.sql import adhoc as _adhoc

        _adhoc._SAFE_CATALOGUE = None
    except Exception:  # noqa: BLE001 - cache hygiene must never break install
        pass
    try:
        from epicor_mcp.sql import validate_columns as _vc

        _vc._load_ud_cached.cache_clear()
    except Exception:  # noqa: BLE001
        pass


def _catalogue_table_tails() -> frozenset[str]:
    'Bare lower-cased table names from the FULL schema catalogue, or empty.'
    try:
        from epicor_mcp.sql import validate_columns as _vc

        path = os.environ.get(_vc._SCHEMA_CATALOGUE_ENV) or _vc.SCHEMA_CATALOGUE_JSON
        raw = json.loads(Path(path).read_text())
        return frozenset(
            str(k).rsplit(".", 1)[-1].lower() for k in (raw.get("tables") or {})
        )
    except Exception:  # noqa: BLE001 - a typo CHECK must never block startup
        return frozenset()


def install_table_blacklist_from_file(
    path: str | Path, *, known_tables: Iterable[str] | None = None
) -> int:
    """Read *path*, parse it, install it. NEVER raises; returns entries installed.

    * Missing file => the file layer is installed EMPTY (so a deleted file plus
      a restart really clears it), one INFO line, no error.
    * Malformed lines are skipped with a WARNING naming the line.
    * An entry matching nothing in the schema catalogue still DENIES
      (fail-safe — deny is the direction that cannot leak) but is logged as a
      WARNING naming the line, because a typo'd blacklist entry protects
      nothing and must be loud. *known_tables* injects the corpus for tests;
      ``None`` reads the real schema catalogue lazily, and no corpus at all
      skips the check rather than accusing every entry.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        install_table_blacklist((), source=str(p))
        logger.info("table blacklist: no file at %s — empty blacklist (0 tables)", p)
        return 0
    except Exception:  # noqa: BLE001 - startup must never crash on this file
        install_table_blacklist((), source=str(p))
        logger.warning(
            "table blacklist: could not read %s — installed EMPTY", p, exc_info=True
        )
        return 0
    # Decode OURSELVES, forgivingly, for two failure modes. (1) ``utf-8-sig``
    # strips the BOM Windows editors prepend — under plain ``utf-8`` the BOM
    # rides into line 1's entry, which is then skipped as malformed: an entry
    # that fails to INSTALL is the fail-UNSAFE direction for a blacklist,
    # warning or no warning. (2) ``errors="replace"`` confines a stray
    # non-UTF-8 byte to its own line — a strict ``read_text`` raises
    # ``UnicodeDecodeError``, which the blanket handler above would turn into
    # "installed EMPTY": ONE bad byte would void every GOOD entry in the file. The
    # replacement char fails ``_BLACKLIST_ENTRY_RE``, so the damaged line still
    # warns per-line ("skip bad lines with warnings"), and every other line
    # installs.
    text = raw.decode("utf-8-sig", errors="replace")

    entries, warnings = _parse_blacklist_lines(text)
    for w in warnings:
        logger.warning("table blacklist %s: %s", p, w)

    tails = (
        frozenset(str(t).rsplit(".", 1)[-1].lower() for t in known_tables)
        if known_tables is not None
        else _catalogue_table_tails()
    )
    if tails:
        for lineno, pattern in entries:
            pl = pattern.lower()
            hit = (
                any(t.startswith(pl[:-1]) for t in tails)
                if pl.endswith("*")
                else pl in tails
            )
            if not hit:
                logger.warning(
                    "table blacklist %s line %d: %r matches no table in the schema "
                    "catalogue. It still DENIES (fail-safe), but a typo'd entry "
                    "protects nothing — check the spelling.",
                    p, lineno, pattern,
                )

    install_table_blacklist([pat for _, pat in entries], source=str(p))
    logger.info("table blacklist: %d table(s) from %s", len(entries), p)
    return len(entries)


def is_denied_column(table: str, column: str) -> bool:
    """True when *column* is compensation/PII on *table* (or on any table)."""
    if _matches(column, DENIED_COLUMNS_ANY_TABLE):
        return True
    bare = (table or "").rsplit(".", 1)[-1].strip().lower()
    if bare in PERSON_KEYED_TABLES and _matches(column, DENIED_COLUMNS_PERSON_TABLES):
        return True
    return False


def _rows(ds: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    rows = ds.get(name) or ds.get(f"{name}Designer") or []
    return [r for r in rows if isinstance(r, Mapping)]


def _qualified(row: Mapping[str, Any]) -> str:
    schema = (row.get("DBSchemaName") or "").strip()
    table = (row.get("DBTableName") or "").strip()
    return f"{schema}.{table}" if schema else table


def _db_table_map(ds: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """THE one extraction of the parsed DS's DB tables (design rule 2).

    ``TableID -> qualified Schema.Table`` for every ``TableType == 'DB'`` row
    Epicor actually resolved, plus the anomalies for the rows it did not — a
    type outside ``{DB, SQ, TT}``, or a ``DB`` row with a blank ``DBTableName``.
    ``SQ``/``TT`` rows (CTEs, derived tables, ``Calculated``) are skipped: their
    base tables appear as their own ``DB`` rows, so nothing is lost.

    Both authorization gates read *which tables does this statement touch*
    through this function — :func:`check_parsed_ds` (the denylist policy deny-list) and
    ``sql/scope_gate.py`` (the menu-derived table gate, via
    :func:`db_tables_read`). One implementation on purpose: two extractions
    would eventually disagree, and then a statement could be judged over two
    different table lists by two gates that both claim to read Epicor's own
    resolution.
    """
    table_by_id: dict[str, str] = {}
    anomalies: list[str] = []
    raw_tables = ds.get("QueryTable") or ds.get("QueryTableDesigner") or []
    if not isinstance(raw_tables, list) or any(not isinstance(row, Mapping) for row in raw_tables):
        return {}, ["QueryTable must be a list of table objects"]
    for t in _rows(ds, "QueryTable"):
        ttype = (t.get("TableType") or "").strip()
        tid = str(t.get("TableID") or "")
        if ttype not in _VALID_TABLE_TYPES:
            anomalies.append(
                f"QueryTable {tid or '<no id>'} has TableType={ttype!r}, which is "
                "outside {DB, SQ, TT}"
            )
            continue
        if ttype != "DB":
            continue
        if not (t.get("DBTableName") or "").strip():
            anomalies.append(
                f"QueryTable {tid or '<no id>'} is TableType='DB' with an empty "
                "DBTableName — Epicor did not resolve it to a real table"
            )
            continue
        qualified = _qualified(t)
        if tid in table_by_id and table_by_id[tid].casefold() != qualified.casefold():
            # Aliases can repeat in nested queries. The column evaluator is
            # keyed by alias; an ambiguous alias must never erase a table.
            anomalies.append(f"QueryTable alias {tid!r} refers to multiple DB tables; use unique aliases")
        table_by_id[tid] = qualified
    return table_by_id, anomalies


def db_tables_read(ds: Mapping[str, Any]) -> list[str]:
    """Qualified ``Schema.Table`` of every resolved ``TableType=='DB'`` row.

    Sorted and de-duplicated. Unlike :func:`check_parsed_ds` this CAN raise on a
    malformed mapping — the scope gate wraps it and fails closed, and the
    deny-list runs the same extraction inside its own fail-closed handler, so
    neither caller can be failed open by an exception here.
    """
    _, anomalies = _db_table_map(ds)
    if anomalies:
        raise ValueError("; ".join(anomalies))
    return sorted({_qualified(row) for row in _rows(ds, "QueryTable")
                   if (row.get("TableType") or "").strip() == "DB"})


def check_parsed_ds(
    ds: Mapping[str, Any], *, unattributed_denies: bool = True
) -> Denial:
    """Evaluate the deny-list against Epicor's OWN parse output.

    Returns an empty :class:`Denial` when the statement is clean. **Never
    raises** — an exception here would fail *open*, so every unexpected shape is
    recorded as an anomaly instead, and an anomaly denies the whole query.

    *unattributed_denies* is the ONE thing a caller may relax, and only on the
    **saved-BAQ** path. The fail-closed rule for an unattributable reference
    (unresolved-column policy) exists because in AD-HOC SQL the caller writes the text, so a
    reference this function cannot place could be a denied column smuggled
    behind an alias it cannot resolve. **A saved BAQ has no such vector**: the
    caller supplies an *id*, the definition is fixed in Epicor and authored in
    the BAQ Designer, and nothing the caller sends can change what it selects.
    There, an unplaceable reference is not an evasion — it is the ordinary
    vocabulary of a hand-authored BAQ: a Query Parameter, a BAQ runtime token
    like ``CurrentUserID``, or a cross-subquery reference Epicor renders with no
    TableID.

    A saved definition can have permitted tables and resolved columns while
    still containing unattributed runtime tokens or cross-subquery aliases.
    Those references alone must not be treated as proof of column denial.

    What this does NOT relax: the table deny-list and the resolved-column
    deny-list both still run unchanged, so a saved BAQ over `Erp.PREmpMas`, or
    one selecting a resolved `LaborRate`, is refused exactly as before. Those
    are the actual controls; this rule was only ever a backstop for text the
    caller wrote. The anomalies are still collected and logged — never silently
    dropped — they simply stop denying.
    """
    denied_tables: list[str] = []
    denied_columns: list[str] = []
    anomalies: list[str] = []
    #: Anomalies that deny REGARDLESS of *unattributed_denies*: an evaluator
    #: that crashed (the handler at the bottom of this function) and the
    #: table-extraction anomalies from `_db_table_map` (a row this gate cannot
    #: map to a physical table is not a column-reference attribution problem).
    hard_anomalies: list[str] = []
    allowed: list[str] = []
    table_by_id: dict[str, str] = {}

    #: Every TableID Epicor put in the tableset, and the SQ/TT (CTE / derived /
    #: `Calculated`) subset. Computed up front because BOTH the column path and
    #: the expression path have to answer *"is this prefix a table Epicor
    #: resolved at all?"*, and that question is what separates a legal
    #: expression from a genuinely unattributable reference.
    calc_table_ids: set[str] = set()

    try:
        for t in _rows(ds, "QueryTable"):
            if (t.get("TableType") or "").strip() in {"SQ", "TT"}:
                calc_table_ids.add(str(t.get("TableID") or ""))

        # The SHARED extraction (`_db_table_map`) — the same rows the scope gate
        # reads, so the deny-list and the menu gate can never disagree about
        # which tables a statement touches. Only the judgement differs here.
        #
        # Extraction anomalies are HARD: a QueryTable row with a TableType
        # outside {DB, SQ, TT}, or a 'DB' row with no DBTableName, is a TABLE
        # this gate cannot see — not the unattributable column-reference
        # vocabulary `unattributed_denies=False` exists to relax (Query
        # Parameters, `CurrentUserID`, cross-subquery references). Routed into
        # `anomalies` instead, the saved-BAQ path would clear them and run a
        # MIXED definition (one readable table + one unreadable row) with the
        # unreadable part ungated — the sibling of the shape guard's fail-open,
        # which only covers a definition with ZERO readable rows. The motivating
        # saved-BAQ case carried no extraction anomaly, so hardening these cannot
        # re-refuse it.
        extracted, extraction_anomalies = _db_table_map(ds)
        hard_anomalies.extend(extraction_anomalies)
        table_by_id.update(extracted)
        for qualified in table_by_id.values():
            if is_denied_table(qualified):
                denied_tables.append(qualified)
            else:
                allowed.append(qualified)

        def _note_expression(text: str, where: str) -> None:
            """Check an EXPRESSION Epicor resolved (a WHERE/HAVING predicate, a
            computed Formula, or an aggregate sort key).

            The text is Epicor's own rendering (``sum(OrderDtl.ExtPriceDtl)``,
            with the TableID as the prefix), not the caller's SQL — so reading
            it is reading the parse, not text-matching the input.

            An expression is **not** an escape hatch: every ``alias.column``
            reference in it is checked exactly as a projected column would be,
            and a prefix naming no table Epicor resolved is the genuine
            unattributable case that still denies the whole query.
            """
            masked = _mask_literals(text)
            for tid, col in _FORMULA_REF.findall(masked):
                if tid in table_by_id:
                    if is_denied_column(table_by_id[tid], col):
                        denied_columns.append(f"{table_by_id[tid]}.{col}")
                    continue
                # Fail closed on the sensitive vocabulary either way, so an
                # unresolved prefix can never be the thing that lets a pay
                # column through.
                if _matches(col, DENIED_COLUMNS_ANY_TABLE):
                    denied_columns.append(f"{tid}.{col}")
                if tid in calc_table_ids:
                    # A CTE / derived / `Calculated` table: its own DB base
                    # tables are enforced on their own QueryTable rows.
                    continue
                anomalies.append(
                    f"{where} references {tid}.{col}, and {tid!r} is not a table Epicor "
                    "resolved in this statement"
                )
            stripped = _FORMULA_REF.sub(" ", masked)
            for bare in _FORMULA_BARE.findall(stripped):
                if _matches(bare, DENIED_COLUMNS_ANY_TABLE) or _matches(
                    bare, DENIED_COLUMNS_PERSON_TABLES
                ):
                    denied_columns.append(f"<computed>.{bare}")

        def _note_column(tid: Any, col: Any, where: str) -> None:
            name = str(col or "").strip()
            if not name:
                return
            tid_s = str(tid or "")
            table = table_by_id.get(tid_s)
            if table is None:
                # Not attributable to a DB table. An ordinal sort (`order by 1`)
                # is the one known shape with an empty TableID and it is a
                # digit; the lint refuses it separately. Anything else that
                # names a denied column, or that we cannot place at all, denies
                # the whole query (unresolved-column policy: fail closed).
                if name.isdigit():
                    return
                if not tid_s and ("(" in name or "." in name):
                    # Epicor renders EVERY
                    # expression predicate as `TableID='' ` with the whole
                    # expression in `FieldName`:
                    #   having sum([OD].[OrderQty]) > 100
                    #     -> {TableID: '', FieldName: 'sum(OD.OrderQty)'}
                    #   where year([OH].[OrderDate]) = 2025
                    #     -> {TableID: '', FieldName: 'year(OH.OrderDate)'}
                    # and likewise for `count(*)`, `a * b`, `case…end` and
                    # `isnull(col,'')`. Reading that as an unattributable column
                    # would deny all six shapes as `column_access_denied`, terminal,
                    # under a message about payroll and SSN policy — while
                    # `sql/tool.py`'s own dialect block INSTRUCTS the model to
                    # write `having sum(x) > 1000`. `QuerySortBy` below gets the
                    # same treatment: read the column references out of the
                    # expression and check each one.
                    _note_expression(name, where)
                    return
                if tid_s and tid_s in calc_table_ids:
                    # A calculated / subquery table id: its own DB base tables
                    # are enforced on their own rows.
                    if _matches(name, DENIED_COLUMNS_ANY_TABLE):
                        denied_columns.append(f"{tid_s}.{name}")
                    return
                anomalies.append(
                    f"{where} references {name!r} but it could not be attributed to any "
                    "resolved DB table"
                )
                return
            if is_denied_column(table, name):
                denied_columns.append(f"{table}.{name}")

        for f in _rows(ds, "QueryField"):
            if f.get("IsCalculated"):
                continue
            _note_column(f.get("TableID"), f.get("DBFieldName") or f.get("FieldName"),
                         "the SELECT list")

        subquery_ids = {
            str(s.get("SubQueryID") or "") for s in _rows(ds, "QuerySubQuery")
        } - {""}
        for w in _rows(ds, "QueryWhereItem"):
            _note_column(w.get("TableID"), w.get("FieldName"), "the WHERE clause")

            # --- the RValue channel ------------------------------------------
            # When the LEFT side of a predicate is an EXPRESSION, Epicor puts the
            # whole RIGHT side in ``RValue``, which must be read explicitly:
            #
            #   where ([LD].[LaborHrs] * 0 + 60) < ([LD].[LaborRate] * 1)
            #     TableID   = ''
            #     FieldName = '(((LD.LaborHrs * 0) + 60))'   <- scanned
            #     RValue    = '((LD.LaborRate * 1))'         <- must ALSO be scanned
            #
            # Without the expression handling above, the empty ``TableID`` would
            # make the whole row an unattributable-column anomaly, so this would be
            # denied by accident — the fail-closed rule incidentally covering an
            # unread channel. Teaching ``_note_column`` to understand expressions
            # removes that incidental protection. A forbidden right-hand
            # column must be checked explicitly: repeated threshold predicates
            # could otherwise reveal sensitive values without selecting them.
            #
            # ``RValue`` legitimately also holds a literal, a BETWEEN pair
            # (``10 AND 20``), a subquery id (``{Subquery2}``) and an IN marker
            # (``where--<hash>``), so it goes through the same literal masking and
            # only ever contributes a reference that actually looks like
            # ``alias.column``.
            rvalue = str(w.get("RValue") or "")
            if (
                rvalue
                and rvalue not in subquery_ids
                and _FORMULA_REF.search(_mask_literals(rvalue))
            ):
                _note_expression(rvalue, "the WHERE clause")

            to_tid, to_field = w.get("ToTableID"), w.get("ToFieldName")
            if not (to_tid or to_field):
                continue
            if not to_tid and str(w.get("RValue") or "") in subquery_ids:
                # `where X in (select Y from …)`: Epicor puts the SUBQUERY's id in
                # RValue and its projected output name in ToFieldName, with no
                # ToTableID. That is not an unattributable reference — the
                # subquery's own base tables are enforced on their own DB rows
                # (tests/fixtures/parsed_ds/deny_subquery.json
                # catches LaborDtl.LaborRate through exactly this path). Still
                # fail closed if the projected NAME is in the sensitive
                # vocabulary.
                if _matches(str(to_field or ""), DENIED_COLUMNS_ANY_TABLE):
                    denied_columns.append(f"<subquery>.{to_field}")
                continue
            _note_column(to_tid, to_field, "the WHERE clause")

        for s in _rows(ds, "QuerySortBy"):
            tid, fname = s.get("TableID"), str(s.get("FieldName") or "")
            # An AGGREGATE sort key — `order by sum([OD].[ExtPriceDtl]) desc`, the
            # shape SQL dialect policy's own top-N recipe uses — resolves to TableID='' with the
            # EXPRESSION in FieldName (e.g. 'sum(OrderDtl.ExtPriceDtl)').
            # Treating that as an unattributable column reference would deny the
            # wedge's own exit-gate query. It is an expression: read the column
            # references out of it and check those.
            if not tid and ("(" in fname or "." in fname):
                _note_expression(fname, "the ORDER BY clause")
                continue
            _note_column(tid, fname, "the ORDER BY clause")

        for f in _rows(ds, "QueryField"):
            if not f.get("IsCalculated"):
                continue
            formula = str(f.get("Formula") or "")
            if formula:
                _note_expression(formula, "a computed column")
    except Exception as exc:  # noqa: BLE001 - an authz bug must fail CLOSED
        # NOT an attribution anomaly, and `unattributed_denies=False` must never
        # reach it: a control that could not complete tells us nothing about the
        # statement, on ANY path. Kept separate so relaxing attribution can
        # never widen into "ignore a crashed evaluator".
        hard_anomalies.append(
            f"the deny-list evaluator raised {type(exc).__name__}: {exc}. The query is "
            "denied because an authorization control that cannot complete must fail closed."
        )

    if not unattributed_denies and anomalies:
        logger.info(
            "saved-BAQ path: %d unattributable reference(s) noted and NOT denied: %s",
            len(anomalies), "; ".join(sorted(set(anomalies))[:4]),
        )
        anomalies = []

    return Denial(
        denied_tables=sorted(set(denied_tables)),
        denied_columns=sorted(set(denied_columns)),
        anomalies=sorted(set(anomalies + hard_anomalies)),
        allowed_tables=sorted(set(allowed)),
    )


def denial_envelope(denial: Denial, *, sql: str = "") -> dict[str, Any]:
    """The INV-1 refusal (denylist policy, *"the denial envelope is a product feature"*).

    Names which objects were denied, which were clean, and — where the query
    still makes sense without them — a runnable ``retry_with``. A bare
    "access denied" makes a weak model retry the same SQL.
    """
    parts: list[str] = []
    # A file-sourced denial keeps the SAME error code, the same terminality and
    # the same leak rules — but not the built-in CLAIM. "Holds payroll /
    # security data" would be a false statement about a table the operator
    # blacklisted for other reasons, and a model repeats what an envelope
    # asserts. The file path is deliberately NOT named (server-internal
    # detail); "the server's table blacklist" is source enough.
    file_denied = [t for t in denial.denied_tables if denial_source(t) == "blacklist"]
    policy_denied = [t for t in denial.denied_tables if t not in file_denied]
    if policy_denied:
        parts.append(
            f"table(s) {', '.join(policy_denied)} hold payroll / security data and "
            "are denied to every user, including an Epicor Security Manager"
        )
    if file_denied:
        parts.append(
            f"table(s) {', '.join(file_denied)} are restricted by the server's table "
            "blacklist and are denied to every user, including an Epicor Security "
            "Manager"
        )
    if denial.denied_columns:
        parts.append(
            f"column(s) {', '.join(denial.denied_columns)} are compensation or PII and are "
            "denied wherever they are referenced — projected, filtered, sorted or inside a "
            "computed expression"
        )
    if denial.anomalies:
        parts.append(
            "part of the statement could not be attributed to a resolved table, so the "
            "whole query is denied (fail closed): " + "; ".join(denial.anomalies)
        )
    message = "Access denied: " + "; ".join(parts) + ". The query was NOT executed."

    valid: dict[str, Any] = {}
    if denial.allowed_tables:
        valid["allowed_tables_in_this_query"] = denial.allowed_tables
    valid["policy"] = (
        "Pay rate, salary, SSN and birth date are denied on every table. Erp.EmpBasic "
        "itself is allowed — EmpID, FirstName, LastName and the like answer 'who ran this "
        "job'. Payroll tables (Erp.PR*, Erp.Payroll*, Erp.ExtPREmp) and Ice security "
        "tables are denied outright."
    )
    if file_denied:
        valid["policy"] += (
            " Additionally, tables on the server's table blacklist are denied outright,"
            " for every user."
        )
    retry: dict[str, Any] = {}
    if denial.denied_columns and not denial.denied_tables and not denial.anomalies:
        retry["how"] = (
            "Re-send the same statement with the denied column(s) removed from the SELECT, "
            "WHERE, ORDER BY and any computed expression."
        )
        if sql:
            retry["original_sql"] = sql
    return error_envelope(
        "table_access_denied" if denial.denied_tables else "column_access_denied",
        message,
        evidence="The server denylist applies even when the configured Epicor service "
        "account could read the table or column; upstream account privileges do not override it",
        valid=valid,
        retry_with=retry or None,
        detail={
            "denied_tables": denial.denied_tables,
            "denied_columns": denial.denied_columns,
            "anomalies": denial.anomalies,
            "enforced_on": "Epicor's own ParseFromSQL QueryTable/QueryField/QueryWhereItem/"
            "QuerySortBy rows — never the SQL text",
        },
        terminal=True,
    )

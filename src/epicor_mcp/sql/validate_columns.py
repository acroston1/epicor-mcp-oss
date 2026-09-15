'E14 — the column-existence validator. Pre-flight, zero extra round trips.'

from __future__ import annotations


import json
import logging
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from epicor_mcp.sql.envelope import error_envelope

logger = logging.getLogger(__name__)

__all__ = [
    "ColumnCatalogue",
    "ColumnRef",
    "UdMirror",
    "UdMirrorMap",
    "UdSplice",
    "ValidationResult",
    "load_catalogue",
    "load_ud_mirrors",
    "splice_ud_joins",
    "ud_retry_sql",
    "validate_columns",
    "DIALECT",
    "CORRECTION_CUTOFF",
]

DIALECT = "tsql"

#: The legacy tools' threshold, carried over unchanged. ``_correct_column`` silently repairs a
#: typo at difflib >= 0.82 (``Description`` -> ``PartDescription``) and errors
#: below it. Raising it costs recoveries; lowering it starts guessing.
CORRECTION_CUTOFF = 0.82

#: Anything at or above this is worth NAMING as a candidate, even when it is too
#: weak to apply.
SUGGEST_CUTOFF = 0.60

#: Column names served back per table before truncation. transpile.py's
#: ``sql_unknown_column`` uses the same figure.
MAX_COLUMNS_SERVED = 60

#: Bound cross-table suggestions so shared names do not produce an unhelpful list.
MAX_OWNERS_SERVED = 8

#: Boolean type spellings accepted from imported field metadata.
_BOOLEAN_TYPES = frozenset({"bit", "bool", "boolean"})

#: A candidate that is the phantom with one of these glued on the front is the
#: legacy ``OnHandQty`` -> ``HasOnHandQty`` trap: ``onhandqty`` IS a substring of
#: ``hasonhandqty``, difflib rates the pair **0.857** (above the cutoff), and the
#: rewrite builds an invalid numeric comparison against a boolean field.
#: Vetoed unless the use really is boolean.
_BOOLEAN_PREFIXES = ("has", "is", "can", "allow", "enable", "disable", "use", "chk")

#: Service metadata describes BO projections, not physical SQL columns.
_FORBIDDEN_SOURCE = "service_index.db"

#: Operator imports and the server resolve data paths from the working directory.
SCHEMA_CATALOGUE_JSON = Path("data/schema_catalogue.json")
_SCHEMA_CATALOGUE_ENV = "EPICOR_MCP_SCHEMA_CATALOGUE"

#: Only these schemas are matched against the catalogue by bare name. An
#: ``Ice.``/``IM.`` table sharing a bare name with an ``Erp.`` one must not
#: inherit its column list.
_JUDGED_SCHEMAS = frozenset({"", "erp"})

_POSITION_BY_ARG = {
    "expressions": "select",
    "where": "where",
    "order": "order_by",
    "group": "group_by",
    "having": "having",
    "joins": "join_on",
    "from": "from",
    "limit": "limit",
    "qualify": "qualify",
}


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Column:
    name: str
    type: str = ""


class ColumnCatalogue:
    """``table -> columns``, case-insensitive, plus the reverse index.

    Built from physical SQL metadata. Each table maps to a list of field names
    or ``{"name": ..., "type": ...}`` objects. :func:`load_catalogue` adapts
    the richer table objects written by the documented import scripts.

    A table this object has never heard of is **unjudgeable**. There is no
    ``__contains__`` that answers True by default and no fuzzy table matching:
    the whole value of the check is that a flagged name is provably absent.
    """

    def __init__(
        self,
        tables: Mapping[str, Iterable[Any]] | None = None,
        *,
        generated: str = "",
    ) -> None:
        #: When the GetFieldList snapshot was taken. Served in the envelope's
        #: ``detail`` so a STALE-CACHE false positive is diagnosable: a column
        #: added to Epicor after this timestamp is absent here and present there,
        #: and that is the one false-positive class offline checks cannot catch.
        self.generated = generated
        self._cols: dict[str, dict[str, _Column]] = {}
        self._display: dict[str, str] = {}
        self._owners: dict[str, list[str]] = {}
        for raw_table, entries in (tables or {}).items():
            table = str(raw_table).rsplit(".", 1)[-1]
            key = table.lower()
            bucket = self._cols.setdefault(key, {})
            self._display.setdefault(key, table)
            for entry in entries or []:
                if isinstance(entry, Mapping):
                    name = entry.get("name") or entry.get("FieldName") or entry.get("Name")
                    ctype = str(entry.get("type") or entry.get("DataType") or "")
                else:
                    name, ctype = entry, ""
                if not name:
                    continue
                name = str(name)
                bucket[name.lower()] = _Column(name, ctype.lower())
                owners = self._owners.setdefault(name.lower(), [])
                if table not in owners:
                    owners.append(table)

    # -- introspection ----------------------------------------------------- #
    def __bool__(self) -> bool:
        return bool(self._cols)

    def __len__(self) -> int:
        return len(self._cols)

    @property
    def tables(self) -> list[str]:
        return [self._display[k] for k in sorted(self._cols)]

    def knows_table(self, table: str) -> bool:
        return table.rsplit(".", 1)[-1].lower() in self._cols

    def display(self, table: str) -> str:
        key = table.rsplit(".", 1)[-1].lower()
        return self._display.get(key, table)

    def has(self, table: str, column: str) -> bool:
        key = table.rsplit(".", 1)[-1].lower()
        return column.lower() in self._cols.get(key, {})

    def column_names(self, table: str) -> list[str]:
        key = table.rsplit(".", 1)[-1].lower()
        return [c.name for c in self._cols.get(key, {}).values()]

    def column_type(self, table: str, column: str) -> str:
        key = table.rsplit(".", 1)[-1].lower()
        col = self._cols.get(key, {}).get(column.lower())
        return col.type if col else ""

    def excluding(self, predicate: Any) -> "ColumnCatalogue":
        """A copy without the tables *predicate(table)* selects. **Types survive.**

        This keeps local column-validation refusals from exposing metadata for
        tables denied by the query policy. Validation runs before Epicor parses
        the query, so both column lists and cross-table suggestions must already
        exclude inaccessible tables.

        Returns ``self`` unchanged when nothing is excluded, so the common case
        costs one predicate call per table and allocates nothing.
        """
        drop = {key for key, name in self._display.items() if predicate(name)}
        if not drop:
            return self
        clone = ColumnCatalogue(generated=self.generated)
        clone._cols = {k: dict(v) for k, v in self._cols.items() if k not in drop}
        clone._display = {k: v for k, v in self._display.items() if k not in drop}
        clone._owners = {}
        for key, bucket in clone._cols.items():
            for col in bucket.values():
                clone._owners.setdefault(col.name.lower(), []).append(clone._display[key])
        return clone

    def lives_on(self, column: str, *, exclude: str = "") -> list[str]:
        """Catalogued tables that DO carry *column* — the legacy ``column_lives_on``.

        The list is bounded by the catalogue's own coverage, so an empty result
        means *"not in the tables I know"*, never *"nowhere in Epicor"*. Callers
        must not print it as the latter.
        """
        skip = exclude.rsplit(".", 1)[-1].lower()
        return [t for t in self._owners.get(column.lower(), []) if t.lower() != skip]


def _read(path: Path) -> dict[str, Any]:
    if _FORBIDDEN_SOURCE in str(path):
        raise ValueError(
            f"{path} is the OData projection, not physical SQL metadata. "
            "Import a physical catalogue with scripts/build_schema_catalogue.py."
        )
    return json.loads(path.read_text())


def _physical_tables(raw: Any) -> dict[str, Any]:
    """Exclude projections and malformed fields before claiming SQL authority."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("tables"), Mapping):
        raise ValueError("Physical catalogue must contain a tables object")
    if "swagger" in str(raw.get("source", "")).casefold():
        return {}
    tables: dict[str, Any] = {}
    for table, metadata in raw["tables"].items():
        if isinstance(metadata, Mapping):
            if "projection" in str(metadata.get("table_type", "")).casefold():
                continue
            fields = metadata.get("fields")
        else:
            fields = metadata
        if not isinstance(fields, (list, tuple)) or not fields:
            continue
        for entry in fields:
            name = (entry.get("name") or entry.get("FieldName") or entry.get("Name")) \
                if isinstance(entry, Mapping) else entry
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                break
        else:
            tables[table] = metadata
    return tables


def _column_tables(raw: Any) -> dict[str, list[Any]]:
    """Adapt rich imported tables and explicit legacy field lists to Erp columns."""
    tables: dict[str, list[Any]] = {}
    for name, metadata in _physical_tables(raw).items():
        parts = str(name).split(".")
        if len(parts) > 2 or (len(parts) == 2 and parts[0].casefold() != "erp"):
            continue
        bare = parts[-1]
        fields = metadata
        if isinstance(metadata, Mapping):
            schema = str(metadata.get("schema") or "Erp").casefold()
            full_name = str(metadata.get("full_name") or f"{schema}.{bare}").casefold()
            if schema != "erp" or full_name != f"erp.{bare.casefold()}":
                continue
            fields = metadata.get("fields")
        tables[bare] = list(fields)
    return tables


@lru_cache(maxsize=8)
def _load_cached(signature: tuple[tuple[str, int, int], ...]) -> ColumnCatalogue:
    merged: dict[str, list[Any]] = {}
    generated = ""
    for name, _mtime, _size in signature:
        try:
            raw = _read(Path(name))
            # Later explicit sources replace earlier entries, retaining types.
            merged.update(_column_tables(raw))
            generated = str(raw.get("generated") or "") or generated
        except Exception:  # noqa: BLE001 — a bad cache must never break import
            logger.warning("column catalogue: could not read %s", name, exc_info=True)
    return ColumnCatalogue(merged, generated=generated)


def load_catalogue(paths: Sequence[Path | str] | None = None) -> ColumnCatalogue:
    """Load CWD ``data/schema_catalogue.json``, cached on path, mtime and size.

    Rich imported table objects preserve field names and types. Explicit legacy
    name/typed-field lists remain supported. Missing, malformed or Swagger-only
    metadata cannot establish SQL column absence and therefore judges nothing.

    Process environment ``EPICOR_MCP_COLUMN_CATALOGUE`` overrides the paths
    (``os.pathsep`` separated); otherwise ``EPICOR_MCP_SCHEMA_CATALOGUE`` selects
    one shared physical catalogue. Relative paths resolve from the current CWD.
    """
    if paths is None:
        override = os.environ.get("EPICOR_MCP_COLUMN_CATALOGUE")
        if override:
            paths = [p for p in override.split(os.pathsep) if p.strip()]
        else:
            paths = [os.environ.get(_SCHEMA_CATALOGUE_ENV) or SCHEMA_CATALOGUE_JSON]
    signature: list[tuple[str, int, int]] = []
    for p in paths:
        path = Path(p).resolve()
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return _load_cached(tuple(signature))


# --------------------------------------------------------------------------- #
# UD mirror tables — where the site's custom `_c` columns actually live
# --------------------------------------------------------------------------- #
#
# A custom `_c` field may physically reside on the parent's `_UD` mirror.
# Imported physical metadata must confirm the mirror field and both join keys
# before recovery can prescribe SysRowID = ForeignSysRowID. Swagger projections
# cannot establish that this SQL join is valid.


@dataclass(frozen=True)
class UdMirror:
    """One ``<Table>_UD`` mirror: the join target for the parent's ``_c`` columns."""

    parent: str  # bare parent display name, e.g. "Customer"
    table: str  # qualified mirror display name, e.g. "Erp.Customer_UD"
    columns: frozenset[str]  # ALL physical mirror columns, lower-cased
    custom: tuple[str, ...]  # the ``*_c`` columns, display casing

    def has_custom(self, column: str) -> bool:
        low = column.lower()
        return low.endswith("_c") and low in self.columns


class UdMirrorMap:
    """``bare parent name -> UdMirror``, case-insensitive.

    Built ONLY by :func:`load_ud_mirrors`, which deny-filters at load — the same
    discipline as ``adhoc._safe_catalogue`` (deny-filtered metadata): a denied parent's
    mirror, a denied mirror, and every non-``Erp`` mirror are all absent from
    this map, so nothing that consults it can name one in any envelope or
    rewrite. Absence from the map is *"not usable"*, never *"does not exist"*.
    """

    def __init__(self, mirrors: Iterable[UdMirror] | None = None) -> None:
        self._by_parent: dict[str, UdMirror] = {
            m.parent.rsplit(".", 1)[-1].lower(): m for m in (mirrors or [])
        }

    def __bool__(self) -> bool:
        return bool(self._by_parent)

    def __len__(self) -> int:
        return len(self._by_parent)

    def mirror_for(self, parent: str) -> UdMirror | None:
        return self._by_parent.get(str(parent).rsplit(".", 1)[-1].lower())


@lru_cache(maxsize=4)
def _load_ud_cached(signature: tuple[str, float] | None) -> UdMirrorMap:
    if signature is None:
        return UdMirrorMap()
    try:
        raw = json.loads(Path(signature[0]).read_text())
        tables = _physical_tables(raw)
    except Exception:  # noqa: BLE001 — a bad catalogue must never break the pipe
        logger.warning("ud mirrors: could not read %s", signature[0], exc_info=True)
        return UdMirrorMap()
    # Lazy import, same pattern as `card_columns` below: the deny layer is only
    # paid for by processes that actually load the mirror map.
    from epicor_mcp.sql.denylist import is_denied_table

    by_bare: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for key, meta in tables.items():
        if isinstance(meta, Mapping) and str(meta.get("schema", "")).casefold() == "erp":
            by_bare.setdefault(str(key).rsplit(".", 1)[-1].lower(), (str(key), meta))

    mirrors: list[UdMirror] = []
    for key, meta in tables.items():
        if not isinstance(meta, Mapping):
            continue
        bare = str(key).rsplit(".", 1)[-1]
        if not bare.lower().endswith("_ud") or len(bare) <= len("_UD"):
            continue
        schema = str(meta.get("schema") or "").strip()
        # Erp mirrors only; a same-named Ice table cannot supply this join.
        if schema.lower() != "erp":
            continue
        parent_bare = bare[: -len("_UD")]
        # Deny beats everything even for privileged users. The mirror inheritance
        # in `denylist.is_denied_table` makes two of these four calls redundant
        # — they stay as defence in depth, so this map keeps the invariant even
        # if that inheritance is ever regressed.
        if (
            is_denied_table(parent_bare)
            or is_denied_table(f"{schema}.{parent_bare}")
            or is_denied_table(bare)
            or is_denied_table(f"{schema}.{bare}")
        ):
            continue
        fields = meta.get("fields") or []
        names = [
            str(f.get("name"))
            for f in fields
            if isinstance(f, Mapping) and f.get("name")
        ]
        low_names = frozenset(n.lower() for n in names)
        custom = tuple(n for n in names if n.lower().endswith("_c"))
        # Both halves of the join contract must be PRESENT in the catalogue —
        # the mirror's ForeignSysRowID and the parent's SysRowID — or the map
        # would prescribe a join it cannot prove well-formed.
        if not custom or "foreignsysrowid" not in low_names:
            continue
        parent_entry = by_bare.get(parent_bare.lower())
        if parent_entry is None:
            continue
        parent_key, parent_meta = parent_entry
        parent_cols = {
            str(f.get("name")).lower()
            for f in (parent_meta.get("fields") or [])
            if isinstance(f, Mapping) and f.get("name")
        }
        if "sysrowid" not in parent_cols:
            continue
        mirrors.append(
            UdMirror(
                parent=str(parent_key).rsplit(".", 1)[-1],
                table=f"{schema}.{bare}",
                columns=low_names,
                custom=custom,
            )
        )
    return UdMirrorMap(mirrors)


def load_ud_mirrors(path: Path | str | None = None) -> UdMirrorMap:
    """The deny-filtered UD mirror map, cached on (path, mtime).

    Missing file, unreadable file, and a file with no usable mirrors all return
    an EMPTY map — and an empty map disables both UD layers, which is the safe
    direction. Never raises, never calls Epicor.
    """
    if path is None:
        path = os.environ.get(_SCHEMA_CATALOGUE_ENV) or SCHEMA_CATALOGUE_JSON
    p = Path(path).resolve()
    try:
        signature: tuple[str, float] | None = (str(p), p.stat().st_mtime)
    except OSError:
        signature = None
    return _load_ud_cached(signature)


# --------------------------------------------------------------------------- #
# The UD join splice — ONE engine behind layer 1's retry and layer 2's rewrite
# --------------------------------------------------------------------------- #

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class UdSplice:
    """One applied mirror join, itemised for the assumptions channel."""

    parent: str  # parent table display name ("Customer")
    mirror: str  # "Erp.Customer_UD"
    alias: str  # the join alias chosen
    qualifier: str  # the parent-side qualifier used in the ON clause
    columns: tuple[str, ...]  # the `_c` references re-qualified, as written
    before: str  # first reference, as the caller wrote it
    after: str  # the same reference, re-qualified


def splice_ud_joins(
    root: exp.Expression,
    *,
    mirrors: UdMirrorMap,
    catalogue: ColumnCatalogue | None = None,
) -> list[UdSplice]:
    """Splice ``left outer join Erp.<T>_UD`` into *root* for its ``_c`` refs.

    MUTATES *root* and returns what it did — or returns ``[]`` and touches
    NOTHING. All-or-nothing: either every ``_c`` reference that needs the
    mirror is resolvable and gets its join, or the statement is left for the
    layer-1 envelope. Only the following grammar supports an unambiguous rewrite:

    * plain single SELECT only — no set operations, no CTEs (a CTE name can
      shadow a base table, the S1/S3 trap class), no subqueries or derived
      tables, every source a real ``Erp``/bare-schema table;
    * a reference qualified by a resolvable alias, or bare with exactly ONE
      source; one join per parent SOURCE (a self-join gets one join per alias);
    * declined outright when the mirror is already joined, when an alias
      cannot be bracket-quoted safely, when a bare ``_c`` name collides with an
      output alias, when the reference sits inside an existing ON clause
      (T-SQL scoping: an earlier ON cannot see a later join), or when a bare
      non-``_c`` column would become ambiguous against the new mirror's
      columns;
    * a reference the parent PROVABLY owns (per *catalogue*) is left alone,
      and a catalogued parent without ``SysRowID`` declines — the rewrite must
      clear E14 one stage later, and a retry another gate refuses is turn two
      of the same loop without treating an unknown source as proof of absence.
    """
    try:
        return _splice_ud_joins(root, mirrors, catalogue)
    except Exception:  # noqa: BLE001 — declining is always available and always safe
        logger.warning("ud splice: declined on an unexpected error", exc_info=True)
        return []


def _splice_ud_joins(
    root: exp.Expression,
    mirrors: UdMirrorMap,
    catalogue: ColumnCatalogue | None,
) -> list[UdSplice]:
    if not mirrors or not isinstance(root, exp.Select):
        return []
    c_refs = [
        c
        for c in root.find_all(exp.Column)
        if not isinstance(c.this, exp.Star) and c.name.lower().endswith("_c")
    ]
    if not c_refs:
        return []

    # --- conservative shape gates (decline = fall through to layer 1) -------
    if root.args.get("with") or root.args.get("with_"):
        return []
    if next(iter(root.find_all(exp.SetOperation)), None) is not None:
        return []
    if [s for s in root.find_all(exp.Select)] != [root]:
        return []
    for cls in (exp.Subquery, exp.Lateral, exp.Values):
        if next(iter(root.find_all(cls)), None) is not None:
            return []
    # A starred projection ITEM (`*` / `[T].*` — outer item only, so `count(*)`
    # stays eligible): the lint owns the `select *` refusal, and splicing a join
    # first would silently WIDEN what the star covers.
    for item in root.expressions:
        node = item.this if isinstance(item, exp.Alias) else item
        if isinstance(node, exp.Star) or (
            isinstance(node, exp.Column) and isinstance(node.this, exp.Star)
        ):
            return []

    frm = root.args.get("from") or root.args.get("from_")
    if frm is None:
        return []
    source_nodes = [frm.this] + [j.this for j in root.args.get("joins") or []]
    sources: dict[str, tuple[str, str, str]] = {}  # key -> (table, schema, qualifier)
    table_names: set[str] = set()
    for node in source_nodes:
        if not isinstance(node, exp.Table) or node.args.get("catalog"):
            return []
        qualifier = node.alias or node.name
        if not qualifier or not _SAFE_IDENT_RE.match(qualifier):
            return []
        key = qualifier.lower()
        if key in sources:  # duplicate qualifier: resolution would be a guess
            return []
        sources[key] = (node.name, node.db or "", qualifier)
        table_names.add(node.name.lower())

    out_aliases = _output_aliases(root)

    def _inside_join(col: exp.Column) -> bool:
        cursor = col.parent
        while cursor is not None and cursor is not root:
            if isinstance(cursor, exp.Join):
                return True
            cursor = cursor.parent
        return False

    # --- classify every `_c` reference --------------------------------------
    targets: dict[str, list[exp.Column]] = {}
    for col in c_refs:
        if col.args.get("db"):
            return []
        alias = (col.table or "").lower()
        if alias:
            if alias not in sources:
                return []
            key = alias
        else:
            if col.name.lower() in out_aliases:
                # `order by [MyAlias_c]` names the OUTPUT, not a base column.
                # Re-qualifying it would change what the statement means; in a
                # WHERE it IS the base column and skipping under-fixes. Decline.
                return []
            if len(sources) != 1:
                return []
            key = next(iter(sources))
        table, schema, _qualifier = sources[key]
        if table.lower().endswith("_ud"):
            continue  # already reading a mirror — nothing to fix
        if schema.lower() not in _JUDGED_SCHEMAS:
            return []
        if catalogue is not None and catalogue.knows_table(table) and catalogue.has(
            table, col.name
        ):
            continue  # provably a REAL parent column — not ours to touch
        mirror = mirrors.mirror_for(table)
        if mirror is None or not mirror.has_custom(col.name):
            return []  # unverifiable: refusing to guess IS the feature
        if _inside_join(col):
            return []  # an earlier ON clause cannot reference the join we add last
        targets.setdefault(key, []).append(col)

    if not targets:
        return []

    # --- per-parent guards ---------------------------------------------------
    for key in targets:
        table, _schema, _qualifier = sources[key]
        mirror = mirrors.mirror_for(table)
        if mirror.table.rsplit(".", 1)[-1].lower() in table_names:
            return []  # the caller already joined the mirror: do nothing
        if (
            catalogue is not None
            and catalogue.knows_table(table)
            and not catalogue.has(table, "SysRowID")
        ):
            return []  # E14 would refuse our own ON clause one stage later

    # A bare column we are NOT fixing that also exists on a joined mirror would
    # become ambiguous the moment the join lands (e.g. a bare `ForeignSysRowID`).
    fixed_ids = {id(c) for cols in targets.values() for c in cols}
    mirror_columns: set[str] = set()
    for key in targets:
        mirror_columns |= mirrors.mirror_for(sources[key][0]).columns
    for col in root.find_all(exp.Column):
        if isinstance(col.this, exp.Star) or col.table or id(col) in fixed_ids:
            continue
        if col.name.lower() in mirror_columns:
            return []

    # --- plan the joins (everything that can fail happens BEFORE mutation) ---
    taken = set(sources)
    planned: list[tuple[str, list[exp.Column], exp.Join, UdMirror, str, str]] = []
    for key, cols in targets.items():
        table, _schema, qualifier = sources[key]
        mirror = mirrors.mirror_for(table)
        base = mirror.table.rsplit(".", 1)[-1]
        alias, n = base, 1
        while alias.lower() in taken:
            n += 1
            if n > 9:
                return []
            alias = f"{base}{n}"
        taken.add(alias.lower())
        join = sqlglot.parse_one(
            f"left outer join {mirror.table} as [{alias}] "
            f"on [{qualifier}].[SysRowID] = [{alias}].[ForeignSysRowID]",
            read=DIALECT,
            into=exp.Join,
        )
        if not isinstance(join, exp.Join):
            return []
        planned.append((key, cols, join, mirror, alias, qualifier))

    # --- apply ---------------------------------------------------------------
    splices: list[UdSplice] = []
    for key, cols, join, mirror, alias, qualifier in planned:
        root.append("joins", join)
        first = cols[0]
        before = f"[{first.table}].[{first.name}]" if first.table else first.name
        for col in cols:
            col.set("table", exp.to_identifier(alias, quoted=True))
        seen: list[str] = []
        for col in cols:
            if col.name not in seen:
                seen.append(col.name)
        splices.append(
            UdSplice(
                parent=catalogue.display(sources[key][0]) if catalogue else sources[key][0],
                mirror=mirror.table,
                alias=alias,
                qualifier=qualifier,
                columns=tuple(seen),
                before=before,
                after=f"[{alias}].[{first.name}]",
            )
        )
    return splices


def ud_retry_sql(
    sql: str,
    *,
    mirrors: UdMirrorMap,
    catalogue: ColumnCatalogue | None = None,
    must_fix: Sequence[str] = (),
) -> str | None:
    """The caller's own statement with the mirror join(s) spliced in, or None.

    Layer 1's recovery builder. ``None`` rather than a half-fixed statement
    whenever the splice declines or any name in *must_fix* was not
    re-qualified — a ``retry_with`` that does not run is a second failed hop.
    """
    try:
        root = _parse(sql)
        if root is None:
            return None
        splices = splice_ud_joins(root, mirrors=mirrors, catalogue=catalogue)
        if not splices:
            return None
        fixed = {c.lower() for s in splices for c in s.columns}
        if any(str(m).lower() not in fixed for m in must_fix):
            return None
        return root.sql(dialect=DIALECT, pretty=False) or None
    except Exception:  # noqa: BLE001 — a cosmetic failure must never break the envelope
        logger.warning("ud retry: could not build a spliced statement", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Scope resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Source:
    """One FROM/JOIN source. ``kind`` is ``base`` | ``derived`` | ``cte`` | ``other``."""

    kind: str
    table: str = ""  # physical name, only when kind == "base"
    schema: str = ""


@dataclass(frozen=True)
class ColumnRef:
    """One column reference, resolved or explicitly not."""

    written: str  # as the caller wrote it, e.g. "P.OnHandQty"
    column: str
    alias: str = ""
    owner: str = ""  # physical table, "" when unresolved
    position: str = "other"
    skipped: str = ""  # "" when judged; otherwise the reason

    @property
    def judged(self) -> bool:
        return not self.skipped


@dataclass
class ValidationResult:
    """The outcome. ``ok`` is False **only** when a column is provably absent."""

    ok: bool
    unknown: list[ColumnRef] = field(default_factory=list)
    judged: list[ColumnRef] = field(default_factory=list)
    skipped: list[ColumnRef] = field(default_factory=list)
    corrections: dict[str, str] = field(default_factory=dict)
    corrected_sql: str | None = None
    envelope: dict[str, Any] | None = None
    unparsed: str = ""

    @property
    def coverage(self) -> tuple[int, int]:
        """``(judged, total)`` — how much of the statement was actually checked."""
        return len(self.judged), len(self.judged) + len(self.skipped)

    def to_dict(self) -> dict[str, Any]:
        judged, total = self.coverage
        return {
            "ok": self.ok,
            "columns_checked": judged,
            "columns_seen": total,
            "unknown": [r.written for r in self.unknown],
            "not_checked": _reason_counts(self.skipped),
            "corrections": dict(self.corrections),
        }


def _reason_counts(refs: Sequence[ColumnRef]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ref in refs:
        out[ref.skipped] = out.get(ref.skipped, 0) + 1
    return dict(sorted(out.items()))


def _cte_names(root: exp.Expression) -> set[str]:
    """Every CTE name in the statement, lower-cased.

    Collected GLOBALLY rather than per-scope on purpose: treating a name as a CTE
    where it is not in scope only makes this module SKIP more, and skipping is the
    safe direction. Treating a CTE as a base table is the S1 defect.
    """
    return {c.alias_or_name.lower() for c in root.find_all(exp.CTE) if c.alias_or_name}


def _own_sources(select: exp.Select, ctes: set[str]) -> dict[str, _Source]:
    """``alias -> _Source`` for this SELECT's own FROM and JOINs."""
    out: dict[str, _Source] = {}

    def add(node: Any) -> None:
        if isinstance(node, exp.Table):
            alias = (node.alias or node.name or "").lower()
            if not alias:
                return
            if node.name.lower() in ctes and not node.db:
                out[alias] = _Source("cte")
            else:
                out[alias] = _Source("base", node.name, (node.db or ""))
        elif isinstance(node, (exp.Subquery, exp.Lateral, exp.Unnest, exp.Values)):
            alias = (node.alias or "").lower()
            if alias:
                out[alias] = _Source("derived")
        elif node is not None:
            alias = (getattr(node, "alias", "") or "").lower()
            if alias:
                out[alias] = _Source("other")

    # sqlglot renamed the FROM arg key from "from" to "from_" in v26+ (see
    # transpile._sources) — read both, or every join silently becomes one source.
    frm = select.args.get("from") or select.args.get("from_")
    if frm is not None:
        add(frm.this)
    for join in select.args.get("joins") or []:
        add(join.this)
    return out


def _enclosing_select(node: exp.Expression) -> exp.Select | None:
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent if isinstance(parent, exp.Select) else None


def _position_of(node: exp.Expression, select: exp.Select) -> str:
    """Which clause of *select* the node sits in."""
    cursor: exp.Expression = node
    while cursor.parent is not None and cursor.parent is not select:
        cursor = cursor.parent
    if cursor.parent is not select:
        return "other"
    return _POSITION_BY_ARG.get(cursor.arg_key or "", "other")


def _visible_sources(
    select: exp.Select, ctes: set[str], cache: dict[int, dict[str, _Source]]
) -> dict[str, _Source]:
    """This SELECT's sources plus every enclosing SELECT's, innermost first.

    The outer entries are what make a **correlated** subquery resolvable
    (``where [OD].[PartNum] = [P].[PartNum]`` inside ``exists``-substitute
    subqueries). Adding them can only ever make a bare column MORE ambiguous, so
    it never turns a skip into a flag.
    """
    key = id(select)
    if key in cache:
        return cache[key]
    merged: dict[str, _Source] = {}
    chain: list[exp.Select] = []
    cursor: exp.Select | None = select
    while cursor is not None:
        chain.append(cursor)
        cursor = _enclosing_select(cursor)
    for scope in reversed(chain):  # outermost first, so inner shadows outer
        merged.update(_own_sources(scope, ctes))
    cache[key] = merged
    return merged


def _output_aliases(select: exp.Select) -> set[str]:
    return {
        item.alias.lower()
        for item in select.expressions
        if isinstance(item, exp.Alias) and item.alias
    }


# --------------------------------------------------------------------------- #
# Correction
# --------------------------------------------------------------------------- #
def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _boolean_context(col: exp.Column) -> bool:
    """True when the reference is used the way a boolean flag is used.

    ``[T].[Flag] = true`` / ``= false`` and a bare predicate (``where [T].[Flag]``,
    ``and not [T].[Flag]``) qualify. ``> 0`` and ``= 5`` do not — and that is the
    whole point: the legacy tools rewrote ``OnHandQty > 0`` into ``HasOnHandQty > 0``.
    """
    parent = col.parent
    if parent is None:
        return False
    if isinstance(parent, (exp.Where, exp.And, exp.Or, exp.Not, exp.Paren)):
        return True
    if isinstance(parent, (exp.EQ, exp.NEQ, exp.Is)):
        other = parent.right if parent.left is col else parent.left
        if isinstance(other, exp.Boolean) or isinstance(other, exp.Null):
            return True
        if isinstance(other, exp.Column):  # column-to-column: no literal to judge
            return False
        text = other.sql(dialect=DIALECT).strip().strip("[]").lower()
        return text in {"true", "false"}
    return False


def _correction_ok(phantom: str, candidate: str, ctype: str, col: exp.Column) -> bool:
    """Keep suggested repairs compatible with the column's use.

    Two independent vetoes support both typed metadata and legacy name-only lists:

    1. **type** — a boolean candidate is refused unless the use is boolean.
    2. **name** — a candidate that is the phantom with a boolean prefix glued on
       is refused the same way, which covers a catalogue with no type data.

    A veto does not discard the candidate; it demotes it from *applied* to
    *named*, which is still a one-hop recovery and never a wrong answer.
    """
    if ctype in _BOOLEAN_TYPES and not _boolean_context(col):
        return False
    low_p, low_c = phantom.lower(), candidate.lower()
    if low_c != low_p and low_c.endswith(low_p):
        prefix = low_c[: -len(low_p)]
        if prefix in _BOOLEAN_PREFIXES and not _boolean_context(col):
            return False
    return True


def _suggest(
    catalogue: ColumnCatalogue, table: str, phantom: str, col: exp.Column
) -> tuple[str | None, list[str]]:
    """``(unambiguous_correction, named_candidates)``.

    A correction is applied only when **exactly one** column clears
    :data:`CORRECTION_CUTOFF` and survives :func:`_correction_ok`. Two strong
    candidates are ambiguous and are NAMED, never picked; a fail-soft pick
    between them would be a silent wrong answer.
    """
    names = catalogue.column_names(table)
    scored = sorted(
        ((n, _ratio(phantom, n)) for n in names), key=lambda kv: (-kv[1], kv[0].lower())
    )
    strong = [n for n, r in scored if r >= CORRECTION_CUTOFF]
    # Nothing close enough is NAMED AS NOTHING. A weak "did you mean" on an
    # invented name is the model's next wrong hop; `columns_by_table` and
    # `column_lives_on` are the honest recovery.
    named = [n for n, r in scored if r >= SUGGEST_CUTOFF][:5]
    if len(strong) == 1 and _correction_ok(phantom, strong[0], catalogue.column_type(table, strong[0]), col):
        return strong[0], named
    return None, named


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #
def _parse(sql: str) -> exp.Expression | None:
    try:
        parsed = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except Exception:  # noqa: BLE001 — a model can emit anything; never raise
        return None
    return parsed[0] if len(parsed) == 1 else None


def validate_columns(
    sql: str,
    *,
    catalogue: ColumnCatalogue | Mapping[str, Iterable[Any]] | None = None,
    build_retry_sql: bool = True,
    ud_mirrors: UdMirrorMap | None = None,
) -> ValidationResult:
    """Check every column reference in *sql* against the GetFieldList catalogue.

    **Never raises**, never calls Epicor, never looks at rows. A statement it
    cannot parse, a table it does not know and a column it cannot attribute all
    come back ``ok=True`` with the reason recorded in ``skipped`` — a clean bill
    from this function means *"nothing provably absent"*, not *"verified"*.

    *ud_mirrors* (optional, default ``None`` = no mirror metadata, prose-only
    ``_c`` help) upgrades the ``_c``-column half of the envelope from prose to a
    VERIFIED recovery: a phantom ``_c`` column the deny-filtered mirror map
    confirms on ``<Table>_UD`` names that mirror in ``column_lives_on`` and —
    when the whole statement is spliceable — hands back the caller's own
    statement with the mirror join in ``retry_with.sql``.
    """
    if catalogue is None:
        catalogue = load_catalogue()
    elif not isinstance(catalogue, ColumnCatalogue):
        catalogue = ColumnCatalogue(catalogue)

    result = ValidationResult(ok=True)
    if not sql or not sql.strip():
        result.unparsed = "empty"
        return result
    root = _parse(sql)
    if root is None:
        # The transpiler and Epicor both get their own say on an unparseable
        # statement. Refusing here would duplicate that with a worse message.
        result.unparsed = "not a single parseable statement"
        return result
    if not catalogue:
        result.unparsed = "no column catalogue is loaded"
        return result

    ctes = _cte_names(root)
    visible_cache: dict[int, dict[str, _Source]] = {}

    for col in root.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            result.skipped.append(ColumnRef(col.sql(dialect=DIALECT), "*", skipped="star"))
            continue
        name = col.name
        alias = (col.table or "").lower()
        written = f"{col.table}.{name}" if col.table else name
        select = _enclosing_select(col)
        position = _position_of(col, select) if select is not None else "other"

        if select is None:
            result.skipped.append(
                ColumnRef(written, name, alias, position=position, skipped="no_enclosing_select")
            )
            continue

        sources = _visible_sources(select, ctes, visible_cache)

        if alias:
            source = sources.get(alias)
            if source is None:
                # An alias that names no source is an ALIAS defect. Reporting it
                # as an unknown COLUMN would blame the wrong half of the
                # reference (the legacy `unknown_columns` lesson, exactly).
                result.skipped.append(
                    ColumnRef(written, name, alias, position=position, skipped="unresolved_alias")
                )
                continue
            if source.kind != "base":
                # Unresolved sources are not proof that a column is absent.
                result.skipped.append(
                    ColumnRef(
                        written, name, alias, position=position,
                        skipped="derived_or_cte_output",
                    )
                )
                continue
            owner, schema = source.table, source.schema
        else:
            if name.lower() in _output_aliases(select):
                result.skipped.append(
                    ColumnRef(written, name, position=position, skipped="output_alias")
                )
                continue
            bases = [s for s in sources.values() if s.kind == "base"]
            if len(sources) != 1 or len(bases) != 1:
                result.skipped.append(
                    ColumnRef(written, name, position=position, skipped="unqualified_ambiguous")
                )
                continue
            owner, schema = bases[0].table, bases[0].schema

        if schema.lower() not in _JUDGED_SCHEMAS or not catalogue.knows_table(owner):
            result.skipped.append(
                ColumnRef(written, name, alias, owner, position, "table_not_in_catalogue")
            )
            continue

        ref = ColumnRef(written, name, alias, catalogue.display(owner), position)
        if catalogue.has(owner, name):
            result.judged.append(ref)
        else:
            result.judged.append(ref)
            result.unknown.append(ref)

    if not result.unknown:
        return result

    result.ok = False
    _build_envelope(result, root, sql, catalogue, build_retry_sql, ud_mirrors)
    return result


def _build_envelope(
    result: ValidationResult,
    root: exp.Expression,
    sql: str,
    catalogue: ColumnCatalogue,
    build_retry_sql: bool,
    ud_mirrors: UdMirrorMap | None = None,
) -> None:
    """The INV-1 envelope: name the column, the table, the side that broke, the fix."""
    unknown_nodes: dict[str, exp.Column] = {}
    for col in root.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        key = f"{col.table}.{col.name}" if col.table else col.name
        unknown_nodes.setdefault(key, col)

    by_position: dict[str, list[str]] = {}
    did_you_mean: dict[str, list[str]] = {}
    lives_on: dict[str, list[str]] = {}
    columns_by_table: dict[str, list[str]] = {}
    truncated: dict[str, int] = {}
    corrections: dict[str, str] = {}

    ud_verified: dict[ColumnRef, UdMirror] = {}
    for ref in result.unknown:
        by_position.setdefault(ref.position, []).append(f"{ref.owner}.{ref.column}")
        label = f"{ref.owner}.{ref.column}"
        mirror: UdMirror | None = None
        if (
            ud_mirrors is not None
            and ref.column.lower().endswith("_c")
            and not ref.owner.endswith("_UD")
        ):
            candidate_mirror = ud_mirrors.mirror_for(ref.owner)
            if candidate_mirror is not None and candidate_mirror.has_custom(ref.column):
                mirror = candidate_mirror
        if mirror is not None:
            # The mirror IS the answer (verified against the deny-filtered
            # schema catalogue), so a difflib guess against the parent's own
            # columns would serve a WRONG fix beside the right one.
            ud_verified[ref] = mirror
            fix, candidates = None, []
        else:
            node = unknown_nodes.get(ref.written)
            fix, candidates = (
                _suggest(catalogue, ref.owner, ref.column, node)
                if node is not None
                else (None, [])
            )
        if candidates:
            did_you_mean[label] = candidates
        elsewhere = _rank_owners(catalogue.lives_on(ref.column, exclude=ref.owner))
        if mirror is not None and mirror.table not in elsewhere:
            elsewhere = [mirror.table] + elsewhere[: MAX_OWNERS_SERVED - 1]
        if elsewhere:
            lives_on[label] = elsewhere
        if fix:
            corrections[ref.written] = fix
        if ref.owner not in columns_by_table:
            names, total = _columns_to_serve(catalogue, ref.owner)
            columns_by_table[ref.owner] = names
            if total > len(names):
                truncated[ref.owner] = total

    result.corrections = corrections

    # --- "name the SIDE that broke" (legacy unknown_by_argument, in SQL terms) --
    judged_positions = {r.position for r in result.judged}
    clean = sorted(judged_positions - set(by_position))

    # --- retry_with is runnable, or it is absent -----------------------------
    retry_with: dict[str, Any] | None = None
    if build_retry_sql and len(corrections) == len(result.unknown) and corrections:
        fixed = _apply_corrections(sql, corrections)
        if fixed is not None:
            result.corrected_sql = fixed
            retry_with = {"sql": fixed}
    if (
        retry_with is None
        and build_retry_sql
        and ud_verified
        and all(r in ud_verified for r in result.unknown)
    ):
        # Every unknown is a VERIFIED `_c`-on-base reference, so the fix is the
        # metadata-verified mirror join, spliced into the caller's own statement by
        # the same all-or-nothing engine the transpiler uses. Withheld the
        # moment the splice declines (set operation, CTE, ambiguity, mirror
        # already joined …): a half-fixed retry is a second failed hop.
        fixed = ud_retry_sql(
            sql,
            mirrors=ud_mirrors,
            catalogue=catalogue,
            must_fix=[r.column for r in result.unknown],
        )
        if fixed is not None:
            result.corrected_sql = fixed
            retry_with = {"sql": fixed}

    unknown_labels = sorted({f"{r.owner}.{r.column}" for r in result.unknown})
    n_bad, n_seen = len(unknown_labels), len(result.judged)
    where_clause = ""
    if clean:
        where_clause = (
            " Your " + ", ".join(f"`{p}`" for p in clean) + " column reference"
            + ("s are" if len(clean) > 1 else " is")
            + " VALID — only "
            + ", ".join(f"`{p}`" for p in sorted(by_position))
            + " broke."
        )
    message = (
        f"{n_bad} of {n_seen} checked column reference"
        f"{'s' if n_seen != 1 else ''} do{'es' if n_bad == 1 else ''} not exist at the SQL "
        f"layer: {', '.join(unknown_labels)}."
        + where_clause
        + (
            f" A corrected statement is in `retry_with.sql`."
            if retry_with
            else " The real column names are in `valid`; pick one rather than retrying "
            "the same name."
        )
    )

    valid: dict[str, Any] = {"unknown_by_position": by_position}
    ud = _ud_extension_help(result.unknown, ud_mirrors)
    if ud:
        valid["user_defined_columns"] = ud
    if did_you_mean:
        valid["did_you_mean"] = did_you_mean
    if lives_on:
        valid["column_lives_on"] = lives_on
    if columns_by_table:
        valid["columns_by_table"] = columns_by_table
    if truncated:
        valid["columns_by_table_truncated"] = {
            t: f"showing {MAX_COLUMNS_SERVED} of {n}" for t, n in truncated.items()
        }
    if clean:
        valid["validated_clean"] = clean

    result.envelope = error_envelope(
        "sql_unknown_column",
        message,
        # NB: no endpoint PATH is spelled here. `tests/test_query_no_write_methods
        # ::test_only_the_read_endpoints_are_reachable` scans executable code for
        # `<Ns>.BO.<X>Svc/<Method>` and pins the reachable set to exactly three;
        # this module calls NONE of them and must not look as if it does.
        evidence=(
            "The administrator-imported physical column catalogue does not contain "
            "this column. BO/OData projections are not used to prove SQL column "
            "existence. Refresh the physical catalogue if the schema recently changed."
        ),
        valid=valid,
        retry_with=retry_with,
        detail={
            "stage": "validate_columns",
            "checked_before_running": True,
            "columns_checked": len(result.judged),
            "not_checked": _reason_counts(result.skipped),
            "catalogue_tables": len(catalogue),
            # The one false-positive class offline checks cannot catch is a STALE
            # snapshot: a column added to Epicor after this timestamp is absent
            # here and present there. Serving the timestamp is what makes that
            # diagnosable instead of baffling.
            "catalogue_generated": catalogue.generated,
        },
    )


def _columns_to_serve(catalogue: ColumnCatalogue, table: str) -> tuple[list[str], int]:
    """The table's real columns, **curated first**.

    Serving 60 columns alphabetically is the wrong recovery. For a miss such as
    ``LaborDtl.PartNum`` it lists ``ABTUID, ActID, ActiveTaskID, …`` and does
    **not** reach ``JobNum`` — the column the caller wanted — while the static
    table card already carries the columns that answer most questions about
    that table. The same holds on the prompt side: a curated card lets the model
    answer questions that a full card makes it DECLINE, often because of the
    card's SIZE alone. An error's recovery list is a card in miniature and the
    same rule applies.
    """
    from epicor_mcp.sql.card import card_columns  # local: card has no deps on us

    real = catalogue.column_names(table)
    lower = {n.lower(): n for n in real}
    curated = [lower[c.lower()] for c in card_columns(table) if c.lower() in lower]
    rest = sorted((n for n in real if n not in set(curated)), key=str.lower)
    return (curated + rest)[:MAX_COLUMNS_SERVED], len(real)


def _rank_owners(tables: Sequence[str]) -> list[str]:
    """``column_lives_on``, card tables first and capped.

    Widely shared field names need a short, consistently ranked suggestion list.
    """
    from epicor_mcp.sql.card import CARD_TABLES

    order = {t.lower(): i for i, t in enumerate(CARD_TABLES)}
    ranked = sorted(tables, key=lambda t: (order.get(t.lower(), len(order)), t.lower()))
    return ranked[:MAX_OWNERS_SERVED]


def _ud_extension_help(
    unknown: Sequence[ColumnRef], ud_mirrors: UdMirrorMap | None = None
) -> dict[str, str]:
    """A ``_c`` column on a BASE table is real, and lives on ``<Table>_UD``.

    Through this pipe,
    ``select [OR].[Note_c] from Erp.OrderRel as [OR]`` parses 200 and then
    fails at Execute — Analyze: ``Invalid column name 'Note_c'.`` — while the
    ``left outer join Erp.OrderRel_UD ... on SysRowID = ForeignSysRowID`` form
    returns rows. A SAVED BAQ splits the same way, but there the failure
    surfaces as a blank ``400`` instead. The OData surface and the
    Swagger-derived index both mirror ``_c``
    fields onto the base table, so a model that has seen either will write the
    base-table form. Flagging it is correct; flagging it WITHOUT the join recipe
    dead-ends the model on a column that does exist.

    With *ud_mirrors* the claim is graded by evidence: a mirror
    hit says VERIFIED and names the catalogued mirror; a mirror that exists but
    lacks the column says so and lists the ``_c`` columns that DO exist there;
    a parent absent from the (deny-filtered, Erp-only) map keeps the original
    unverified recipe — absence from the map is not absence from Epicor.
    """
    out: dict[str, str] = {}
    for ref in unknown:
        if not ref.column.endswith("_c") or ref.owner.endswith("_UD"):
            continue
        label = f"{ref.owner}.{ref.column}"
        mirror = ud_mirrors.mirror_for(ref.owner) if ud_mirrors else None
        if mirror is not None and mirror.has_custom(ref.column):
            base = mirror.table.rsplit(".", 1)[-1]
            out[label] = (
                f"`{ref.column}` is a user-defined column: it lives on the mirror table "
                f"{mirror.table} (VERIFIED in the schema catalogue), not on "
                f"Erp.{ref.owner}. Add `left outer join {mirror.table} as [{base}] on "
                f"[{ref.alias or ref.owner}].[SysRowID] = [{base}].[ForeignSysRowID]` "
                f"and select `[{base}].[{ref.column}]`."
            )
        elif mirror is not None:
            shown = ", ".join(mirror.custom[:20])
            out[label] = (
                f"`{ref.column}` looks like a user-defined column, but the mirror table "
                f"{mirror.table} does not carry it. The user-defined columns that DO "
                f"exist there: {shown}."
            )
        else:
            out[label] = (
                f"`{ref.column}` is a user-defined column: it lives on the extension table "
                f"Erp.{ref.owner}_UD, not on Erp.{ref.owner}. Add "
                f"`left outer join Erp.{ref.owner}_UD as [{ref.owner}_UD] on "
                f"[{ref.alias or ref.owner}].[SysRowID] = [{ref.owner}_UD].[ForeignSysRowID]` "
                f"and select `[{ref.owner}_UD].[{ref.column}]`."
            )
    return out


def _apply_corrections(sql: str, corrections: Mapping[str, str]) -> str | None:
    """Rewrite the phantom identifiers on the AST and re-serialise.

    Returns ``None`` rather than a half-fixed statement if anything about the
    round trip looks wrong — a ``retry_with`` that does not run is a second
    failed hop, which is the thing the envelope exists to prevent.
    """
    try:
        root = _parse(sql)
        if root is None:
            return None
        changed = 0
        for col in root.find_all(exp.Column):
            if isinstance(col.this, exp.Star):
                continue
            key = f"{col.table}.{col.name}" if col.table else col.name
            fix = corrections.get(key)
            if fix:
                col.set("this", exp.to_identifier(fix, quoted=True))
                changed += 1
        if changed == 0:
            return None
        out = root.sql(dialect=DIALECT, pretty=False)
        if not out or any(bad.lower() in out.lower() for bad in _phantom_names(corrections)):
            return None
        return out
    except Exception:  # noqa: BLE001 — a cosmetic failure must never break the envelope
        logger.warning("validate_columns: could not build a corrected statement", exc_info=True)
        return None


def _phantom_names(corrections: Mapping[str, str]) -> list[str]:
    """The bad column names only — a name still present means the rewrite missed."""
    out = []
    for written, fix in corrections.items():
        bad = written.rsplit(".", 1)[-1]
        if bad.lower() != fix.lower() and bad.lower() not in fix.lower():
            out.append(bad)
    return out

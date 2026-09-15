"""Shared BAQ create/run primitives.

``epicor_baq_create`` (with ``run=True``) and the standalone ``epicor_run_baq``
both call into ``run_baq``; ``create_baq`` is the create-or-replace
primitive used only by ``epicor_baq_create``. Keeping the network/RBAC logic
in one place stops the two code paths from drifting.
"""

from __future__ import annotations

import copy
import difflib
import html
import logging
import re
from typing import TYPE_CHECKING, Any

from epicor_mcp.epicor_client.error_handler import EpicorError

if TYPE_CHECKING:
    from epicor_mcp.auth.session import MCPSession
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_BAQ_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Matches a trailing version suffix like ``_v2``, ``-v3``, ``_V12``.
# Used to nudge callers off the v2/v3/v4 sprawl now that replace=True
# iterates in place.
_VERSION_SUFFIX_RE = re.compile(r"[_-]v\d+$", re.IGNORECASE)

_PARSE_DS_TEMPLATE: dict[str, Any] = {
    "DynamicQueryDesigner": [
        {
            "Company": "DEMO",
            "QueryID": "New Query",
            "AuthorID": "",
            "Description": "",
            "DisplayPhrase": "",
            "IsShared": False,
            "Version": "",
            "CGCCode": "",
            "XCompany": False,
            "GlbCompany": "",
            "Updatable": False,
            "ExtQuery": False,
            "ExtDatasourceName": "",
            "SystemFlag": False,
            "Extension": None,
            "SysRevID": 0,
            "SysRowID": "00000000-0000-0000-0000-000000000000",
            "SecCode": "BAQDEFAULT",
            "UseLiveDB": False,
            "Comment": None,
            "LastUpdated": None,
            "LastUpdatedBy": None,
            "IsProtected": False,
            "BPMUpdateOnly": False,
            "AllCompanies": False,
            "BitFlag": 0,
            "RowMod": "A",
        }
    ],
    "QueryCtrlDesigner": [],
    "QueryCtrlValuesDesigner": [],
    "QueryCustomActionDesigner": [],
    "QueryExecuteSettingDesigner": [],
    "QueryParameterDesigner": [],
    "QueryReferenceDesigner": [],
    "QueryParameterBindingDesigner": [],
    "QuerySubQueryDesigner": [],
    "QueryRelationDesigner": [],
    "QueryRelationFieldDesigner": [],
    "QuerySortByDesigner": [],
    "QueryWhereItemDesigner": [],
    "QueryGroupByDesigner": [],
    "QueryTableDesigner": [],
    "QueryFieldDesigner": [],
    "QueryFieldAttributeDesigner": [],
    "QueryFunctionCallDesigner": [],
    "QueryUpdateFieldDesigner": [],
    "QueryUpdateSettingsDesigner": [],
    "QueryValueSetItemsDesigner": [],
    "ExtensionTables": [],
}


def _normalize_sql(sql: str) -> str:
    """Apply HTML-entity un-escape and line-ending normalisation."""
    if "&" in sql:
        sql = html.unescape(sql)
    sql = sql.replace("\\r\\n", "\r\n")
    sql = sql.replace("\\n", "\n")
    sql = sql.replace("\\t", "\t")
    sql = sql.replace("\r\n", "\n").replace("\n", "\r\n")
    return sql


def strip_outer_where(sql: str) -> tuple[str, bool]:
    """Remove the outermost top-level WHERE clause from a SQL string.

    Walks left-to-right tracking paren depth and quote state, finds the
    last ``WHERE`` at depth 0, then deletes from there until the next
    top-level ``GROUP BY`` / ``ORDER BY`` / ``HAVING`` keyword (or end of
    string).

    Returns ``(new_sql, stripped)``. ``stripped`` is False when no
    top-level WHERE was found. Subquery WHEREs are preserved.
    """
    keywords_after = ("group by", "order by", "having")
    lowered = sql.lower()
    n = len(sql)

    # Find the position of the last top-level WHERE.
    depth = 0
    in_str = False
    str_ch = ""
    where_pos = -1
    i = 0
    while i < n:
        ch = sql[i]
        if in_str:
            if ch == str_ch:
                in_str = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = True
            str_ch = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue
        if depth == 0 and lowered.startswith("where", i):
            before = sql[i - 1] if i > 0 else " "
            after = sql[i + 5] if i + 5 < n else " "
            if (not before.isalnum() and before != "_"
                    and not after.isalnum() and after != "_"):
                where_pos = i
        i += 1

    if where_pos < 0:
        return sql, False

    # Find the next top-level keyword after where_pos (depth/quote-aware).
    depth = 0
    in_str = False
    str_ch = ""
    end_pos = n
    j = where_pos + 5
    while j < n:
        ch = sql[j]
        if in_str:
            if ch == str_ch:
                in_str = False
            j += 1
            continue
        if ch in ("'", '"'):
            in_str = True
            str_ch = ch
            j += 1
            continue
        if ch == "(":
            depth += 1
            j += 1
            continue
        if ch == ")":
            depth -= 1
            j += 1
            continue
        if depth == 0:
            for kw in keywords_after:
                if lowered.startswith(kw, j):
                    before = sql[j - 1] if j > 0 else " "
                    after = sql[j + len(kw)] if j + len(kw) < n else " "
                    if (not before.isalnum() and before != "_"
                            and not after.isalnum() and after != "_"):
                        end_pos = j
                        break
            if end_pos != n:
                break
        j += 1

    new_sql = (sql[:where_pos].rstrip() + "\r\n" + sql[end_pos:]).rstrip()
    return new_sql, True


# ----------------------------------------------------------------------------
# Pre-flight SQL column validation
# ----------------------------------------------------------------------------
#
# Epicor's ParseFromSQL is lenient — it accepts column names that don't exist
# on the target table, then the BAQ runtime fails at execution time with the
# infamous empty ``400 BAQ execution failed with error:``. We catch the entire
# class of bug before that round-trip by parsing the SQL ourselves, mapping
# every alias to a real Schema.Table, and validating each field reference
# against the in-process schema index.

# ``from`` or ``join`` <Schema.Table> [as [<alias>]]
_FROM_JOIN_RE = re.compile(
    r"""
    \b(?:from|join)\s+
    (?P<schema>[A-Za-z_][\w]*)
    \.
    (?P<table>[A-Za-z_][\w]*)
    (?:\s+as\s+\[?(?P<alias>[A-Za-z_][\w]*)\]?)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ``) as [<alias>]`` — closing paren of a subquery followed by its alias.
# These aliases project synthetic columns we can't validate; skip them.
_SUBQUERY_ALIAS_RE = re.compile(
    r"\)\s*as\s+\[?(?P<alias>[A-Za-z_][\w]*)\]?",
    re.IGNORECASE,
)

# ``[Alias].[Field]`` — fully bracketed reference. Always a field ref.
_BRACKETED_FIELD_RE = re.compile(
    r"\[(?P<alias>[A-Za-z_][\w]*)\]\s*\.\s*\[(?P<field>[A-Za-z_][\w]*)\]"
)

# ``alias.field`` — bare dotted reference. Could also be a Schema.Table
# inside ``from``/``join`` (we filter those out by checking the alias map).
_BARE_FIELD_RE = re.compile(
    r"\b(?P<alias>[A-Za-z_][\w]*)\.(?P<field>[A-Za-z_][\w]*)\b"
)


def _build_alias_map(sql: str) -> tuple[dict[str, str], set[str]]:
    """Parse SQL FROM/JOIN clauses into ``{alias: "Schema.Table"}``.

    Returns ``(alias_map, subquery_aliases)``. Aliases used for subqueries
    (``from ( ... ) as [Sub]``) appear in ``subquery_aliases`` instead of
    the map — we have no schema for them, so field refs against them are
    skipped during validation rather than flagged.
    """
    alias_map: dict[str, str] = {}
    for m in _FROM_JOIN_RE.finditer(sql):
        schema = m.group("schema")
        table = m.group("table")
        alias = m.group("alias") or table
        alias_map[alias.lower()] = f"{schema}.{table}"

    subquery_aliases: set[str] = set()
    for m in _SUBQUERY_ALIAS_RE.finditer(sql):
        subquery_aliases.add(m.group("alias").lower())

    return alias_map, subquery_aliases


def _extract_field_refs(sql: str) -> list[tuple[str, str]]:
    """Return every ``(alias, field)`` pair found in *sql*.

    Includes both bracketed (``[A].[F]``) and bare (``A.F``) forms.
    Duplicates allowed — the caller dedupes after alias resolution so the
    error message mentions each problem ref once.
    """
    refs: list[tuple[str, str]] = []
    for m in _BRACKETED_FIELD_RE.finditer(sql):
        refs.append((m.group("alias"), m.group("field")))
    for m in _BARE_FIELD_RE.finditer(sql):
        refs.append((m.group("alias"), m.group("field")))
    return refs


# Common SQL keywords / function names that look like an alias but aren't —
# never report a field reference against these.
_SQL_KEYWORD_ALIASES = frozenset({
    "select", "from", "where", "and", "or", "as", "on", "join",
    "inner", "outer", "left", "right", "group", "order", "by",
    "having", "case", "when", "then", "else", "end", "in", "not",
    "is", "null", "distinct", "union", "all", "sum", "count", "avg",
    "min", "max", "year", "month", "day", "cast", "convert",
})


def validate_sql_columns(
    sql: str,
    baq_index: "BAQSchemaIndex",
) -> list[dict[str, Any]]:
    """Return a list of unknown-column diagnostics for *sql*.

    Empty list means every resolvable field reference matches a real
    column in the schema index. Each diagnostic dict has shape::

        {"ref": "[OrderHed].[DocTotalOrder]",
         "alias": "OrderHed",
         "table": "Erp.OrderHed",
         "field": "DocTotalOrder",
         "did_you_mean": ["DocOrderAmt", "DocTotalCharges", ...]}

    Field references against subquery aliases, schema names (``Erp.X``,
    ``Ice.X``), and unbound aliases are silently skipped — we'd rather
    let those reach Epicor than block a legal query with a false positive.
    """
    alias_map, subquery_aliases = _build_alias_map(sql)

    # Build per-table field caches lazily so we hit the DB at most once
    # per table referenced.
    field_cache: dict[str, set[str]] = {}
    field_name_list: dict[str, list[str]] = {}

    def get_fields(table_full_name: str) -> tuple[set[str], list[str]]:
        if table_full_name not in field_cache:
            try:
                rows = baq_index.get_fields(table_full_name)
            except Exception:
                rows = []
            names = [r.get("field_name", "") for r in rows if r.get("field_name")]
            field_cache[table_full_name] = {n.lower() for n in names}
            field_name_list[table_full_name] = names
        return field_cache[table_full_name], field_name_list[table_full_name]

    refs = _extract_field_refs(sql)
    seen: set[tuple[str, str]] = set()
    diagnostics: list[dict[str, Any]] = []

    # Common SQL-keywords / function names that look like an alias but
    # aren't — never report against these.
    skip_aliases = _SQL_KEYWORD_ALIASES

    for alias, field in refs:
        key = (alias.lower(), field.lower())
        if key in seen:
            continue
        seen.add(key)

        a_lower = alias.lower()
        if a_lower in skip_aliases or a_lower in subquery_aliases:
            continue

        full_table = alias_map.get(a_lower)
        if full_table is None:
            # Could be a schema name (``Erp.OrderHed`` in a FROM clause
            # picked up by the bare regex) or an unknown alias. Cheapest
            # check: if it matches a known schema name, skip; otherwise
            # skip too (we don't want false positives blocking valid SQL).
            continue

        known, name_list = get_fields(full_table)
        if not known:
            # We don't have schema for this table — skip rather than
            # accuse Epicor of a missing column we can't prove is missing.
            continue
        if field.lower() in known:
            continue

        suggestions = difflib.get_close_matches(
            field, name_list, n=5, cutoff=0.5
        )
        if not suggestions:
            f_lower = field.lower()
            suggestions = [
                n for n in name_list
                if f_lower in n.lower() or n.lower() in f_lower
            ][:5]

        diagnostics.append({
            "ref": f"[{alias}].[{field}]",
            "alias": alias,
            "table": full_table,
            "field": field,
            "did_you_mean": suggestions,
        })

    return diagnostics


# ----------------------------------------------------------------------------
# User-defined (``_c``) column checks
# ----------------------------------------------------------------------------
#
# UD columns physically live in an extension table (``Erp.OrderRel_UD``,
# joined to its base table on ``SysRowID = ForeignSysRowID``). The BO/OData
# surface hides that — ``epicor_query`` happily returns ``OrderRel.Note_c``
# — and so does the Swagger-derived schema index, which mirrors every ``_c``
# field onto the base table. BAQ SQL does not: ``ParseFromSQL`` accepts
# ``[OrderRel].[Note_c]``, resolves it to a field with an empty DataType,
# saves without complaint, and the BAQ then dies at *execution* time with the
# infamous blank ``400 BAQ execution failed with error:``.
#
# The two forms behave differently:
#   [OrderRel].[Note_c]        -> DataType ''        -> 400 on run
#   [OrderRel_UD].[Note_c]     -> DataType 'nvarchar' -> 200 + data


def find_ud_field_refs(
    sql: str,
    baq_index: "BAQSchemaIndex",
) -> list[dict[str, Any]]:
    """Return diagnostics for ``_c`` fields read off a base table.

    Only flags a reference when the matching ``<Table>_UD`` extension table
    is known to carry that field — i.e. when we can hand back the exact
    rewrite. Anything we can't prove is left for
    :func:`find_unresolved_parsed_fields` to catch post-parse.
    """
    alias_map, subquery_aliases = _build_alias_map(sql)
    refs = _extract_field_refs(sql)

    # ``alias_map`` is keyed lower-case; keep the caller's original spelling
    # so the suggested SQL is copy-pasteable rather than case-mangled.
    alias_case: dict[str, str] = {}
    for m in _FROM_JOIN_RE.finditer(sql):
        a = m.group("alias") or m.group("table")
        alias_case.setdefault(a.lower(), a)

    seen: set[tuple[str, str]] = set()
    diagnostics: list[dict[str, Any]] = []

    for alias, field in refs:
        if not field.endswith("_c"):
            continue

        key = (alias.lower(), field.lower())
        if key in seen:
            continue
        seen.add(key)

        a_lower = alias.lower()
        if a_lower in _SQL_KEYWORD_ALIASES or a_lower in subquery_aliases:
            continue

        full_table = alias_map.get(a_lower)
        if full_table is None or full_table.endswith("_UD"):
            continue

        ud_table = f"{full_table}_UD"
        try:
            ud_rows = baq_index.get_fields(ud_table)
        except Exception:
            ud_rows = []
        if field.lower() not in {
            r.get("field_name", "").lower() for r in ud_rows
        }:
            continue

        # Prefer an alias the caller already bound to the extension table;
        # only invent one (and ask for a join) when it isn't in the query.
        existing = [
            a for a, t in alias_map.items() if t == ud_table
        ]
        if existing:
            ud_alias = alias_case.get(existing[0], existing[0])
            add_join = ""
        else:
            ud_alias = f"{alias}_UD"
            add_join = (
                f"LEFT OUTER JOIN {ud_table} as [{ud_alias}] "
                f"ON {alias}.SysRowID = {ud_alias}.ForeignSysRowID"
            )

        diagnostics.append({
            "ref": f"[{alias}].[{field}]",
            "field": field,
            "base_table": full_table,
            "ud_table": ud_table,
            "use_instead": f"[{ud_alias}].[{field}] as [{ud_alias}_{field}]",
            "add_join": add_join,
        })

    return diagnostics


def find_unresolved_parsed_fields(
    parsed_ds: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return fields ``ParseFromSQL`` failed to bind to a real column.

    A resolvable column comes back with a populated ``DataType``; one Epicor
    couldn't bind comes back with ``DataType: ''`` and the raw name as its
    label. Saving such a definition always yields a blank 400 at run time.

    Calculated and subquery-projected fields legitimately carry an empty
    ``DataType`` too, so the check keys off ``QueryTableDesigner.TableType``:
    ``DB`` is a real database table (empty DataType == defect), while ``SQ``
    (subquery alias) and ``TT`` (the synthetic ``Calculated`` table) are
    expected to be blank and are skipped.
    """
    db_tables = {
        row.get("TableID")
        for row in parsed_ds.get("QueryTableDesigner") or []
        if isinstance(row, dict) and row.get("TableType") == "DB"
    }

    unresolved: list[dict[str, Any]] = []
    for row in parsed_ds.get("QueryFieldDesigner") or []:
        if not isinstance(row, dict):
            continue
        if row.get("TableID") not in db_tables:
            continue
        if str(row.get("DataType") or "").strip():
            continue
        if str(row.get("Formula") or "").strip():
            continue
        unresolved.append({
            "ref": f"[{row.get('TableID')}].[{row.get('FieldName')}]",
            "table": row.get("TableID"),
            "field": row.get("FieldName"),
        })

    return unresolved


async def create_baq(
    *,
    session: "MCPSession",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    baq_name: str,
    description: str,
    sql: str,
    replace: bool = True,
    baq_index: "BAQSchemaIndex | None" = None,
) -> dict[str, Any]:
    """Create (or replace) an AUTO-prefixed BAQ. Returns a dict result.

    Validation errors and Epicor failures are returned as ``{"error": ...}``
    so callers can serialise the dict directly to JSON.
    """
    user_profile = rbac._user_map.get_user(session.user_id)
    has_baq_permission = (
        session.access_level == "read_write"
        or (user_profile and user_profile.can_write_baqs)
    )
    if not has_baq_permission:
        return {
            "error": (
                "BAQ creation requires either write access or BAQ Designer "
                "permissions. Your Epicor account does not have "
                "ExtBAQDesigner, BAQ, BAMP, or BAMS security groups. "
                "Contact your administrator."
            )
        }

    baq_name = baq_name.strip()
    if not baq_name:
        return {"error": "baq_name cannot be empty."}

    # Strip ``_v2`` / ``-v3`` / ``_V12`` suffixes. Callers reflexively bump
    # the version on retries; with replace=True the same name iterates in
    # place and we'd rather not pile up orphaned AUTO-*_v2..vN definitions.
    original_name = baq_name
    name_suffix_stripped = False
    if replace:
        stripped = _VERSION_SUFFIX_RE.sub("", baq_name)
        if stripped and stripped != baq_name:
            baq_name = stripped
            name_suffix_stripped = True

    if len(baq_name) > 25:
        return {
            "error": (
                f"baq_name is too long ({len(baq_name)} chars). "
                "Maximum is 25 characters."
            )
        }
    if not _BAQ_NAME_RE.match(baq_name):
        return {
            "error": (
                f"Invalid baq_name '{baq_name}'. Only alphanumeric "
                "characters, hyphens, and underscores are allowed."
            )
        }

    query_id = f"AUTO-{baq_name}"

    if not sql.strip():
        return {"error": "SQL cannot be empty."}
    if not description.strip():
        return {"error": "Description cannot be empty."}

    sql = _normalize_sql(sql)

    # ---------------------------------------------------------------
    # Pre-flight: validate every [Table].[Field] reference against the
    # schema index. Epicor's ParseFromSQL accepts unknown columns and
    # the BAQ runtime then dies with an unhelpful empty 400 — better to
    # catch hallucinated field names before we even hit the wire.
    # ---------------------------------------------------------------
    if baq_index is not None:
        try:
            unknown = validate_sql_columns(sql, baq_index)
        except Exception:
            logger.exception("pre-flight column validation crashed; skipping")
            unknown = []
        if unknown:
            return {
                "error": "unknown_columns",
                "message": (
                    "The SQL references columns that don't exist on the "
                    "named tables. Fix the field names (see did_you_mean) "
                    "and re-call. This was caught before sending to Epicor."
                ),
                "unknown_columns": unknown,
            }

        # ``_c`` columns exist on the base table for OData but not for BAQ
        # SQL — they have to be read off the ``_UD`` extension table.
        try:
            ud_refs = find_ud_field_refs(sql, baq_index)
        except Exception:
            logger.exception("pre-flight UD-field check crashed; skipping")
            ud_refs = []
        if ud_refs:
            return {
                "error": "ud_fields_need_extension_table",
                "message": (
                    "User-defined (_c) columns can't be selected off the "
                    "base table in a BAQ — they live in the table's _UD "
                    "extension table. Epicor would accept this SQL and "
                    "then fail at run time with a blank "
                    "'400 BAQ execution failed with error:'. For each ref "
                    "below, add the join in add_join (skip it when empty — "
                    "the query already has one) and select use_instead. "
                    "Use LEFT OUTER so rows without a _UD record survive. "
                    "This was caught before sending to Epicor."
                ),
                "ud_fields": ud_refs,
            }

    if session.access_level == "read_write":
        api_key = rbac._user_map.get_write_key()
    else:
        api_key = rbac._user_map.get_baq_key()
    if not api_key:
        return {
            "error": (
                "No BAQ write API key configured. Contact your "
                "administrator to configure a BAQ API key (baq_api_key)."
            )
        }

    author_id = (
        user_profile.epicor_username
        if user_profile
        else session.user_id.split("@")[0]
    )

    ds = copy.deepcopy(_PARSE_DS_TEMPLATE)
    ds["DynamicQueryDesigner"][0]["DisplayPhrase"] = sql
    ds["DynamicQueryDesigner"][0]["AuthorID"] = author_id

    logger.info("create_baq: ParseFromSQL '%s' user=%s", query_id, session.user_id)
    parse_response = await client.post(
        "Ice.BO.BAQDesignerSvc/ParseFromSQL",
        api_key,
        json_body={"ds": ds},
    )

    parsed_ds = (
        parse_response.get("parameters", {}).get("ds")
        or parse_response.get("ds")
        or parse_response.get("returnObj")
        or parse_response
    )

    if "DynamicQueryDesigner" not in parsed_ds:
        return {
            "error": (
                "ParseFromSQL returned an unexpected response. "
                "The SQL may have syntax errors. Check that table "
                "names are prefixed with schema (Erp.), all fields "
                "have aliases, and brackets are properly formatted."
            ),
            "raw_response_keys": list(parse_response.keys()),
        }

    # Epicor binds every real column to a DataType. A blank one on a DB
    # table means ParseFromSQL couldn't resolve the field — saving that
    # definition guarantees a blank 400 the moment anyone runs it. Bail
    # while the existing BAQ (if any) is still intact.
    unresolved = find_unresolved_parsed_fields(parsed_ds)
    if unresolved:
        return {
            "error": "unresolved_fields",
            "message": (
                "Epicor parsed the SQL but could not bind these fields to "
                "real columns, so the BAQ would save and then fail at run "
                "time with a blank '400 BAQ execution failed with error:'. "
                "Nothing was saved and any existing BAQ of this name is "
                "untouched. If a field ends in _c it must be selected from "
                "the table's _UD extension table (LEFT OUTER JOIN "
                "<Table>_UD ON <Table>.SysRowID = <Table>_UD."
                "ForeignSysRowID); otherwise check the field exists on "
                "that table via epicor_baq_schema."
            ),
            "unresolved_fields": unresolved,
        }

    replaced_existing = False
    if replace:
        try:
            await client.post(
                "Ice.BO.BAQDesignerSvc/DeleteByID",
                api_key,
                json_body={"queryID": query_id},
            )
            replaced_existing = True
            logger.info("create_baq: replaced existing '%s'", query_id)
        except Exception as del_exc:
            logger.debug("create_baq: pre-delete skipped: %s", del_exc)

    parsed_ds["DynamicQueryDesigner"][0]["QueryID"] = query_id
    parsed_ds["DynamicQueryDesigner"][0]["Description"] = description
    parsed_ds["DynamicQueryDesigner"][0]["AuthorID"] = author_id

    for arr in parsed_ds.values():
        if isinstance(arr, list):
            for row in arr:
                if isinstance(row, dict) and "QueryID" in row:
                    row["QueryID"] = query_id

    logger.info("create_baq: Update '%s'", query_id)
    update_response = await client.post(
        "Ice.BO.BAQDesignerSvc/Update",
        api_key,
        json_body={"ds": parsed_ds},
    )

    saved_ds = (
        update_response.get("parameters", {}).get("ds")
        or update_response.get("ds")
        or update_response.get("returnObj")
        or update_response
    )

    result: dict[str, Any] = {
        "success": True,
        "baq_id": query_id,
        "replaced_existing": replaced_existing,
        "description": description,
        "author": author_id,
        "tables_used": len(saved_ds.get("QueryTableDesigner", [])),
        "fields_selected": len(saved_ds.get("QueryFieldDesigner", [])),
        "usage": f"epicor_baq(action='run', baq='{query_id}')",
    }
    if name_suffix_stripped:
        result["name_suffix_stripped"] = {
            "submitted": original_name,
            "saved_as": baq_name,
            "note": (
                "Trailing _vN / -vN was stripped — replace=True iterates in "
                "place on the same name. Keep using the base name on retries "
                "and the previous version gets overwritten."
            ),
        }
    return result


async def run_baq(
    *,
    session: "MCPSession",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    baq_id: str,
    filter: str = "",
    top: int = 10,
    orderby: str = "",
    baq_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a BAQ by id. Returns ``{records, record_count, baq_id, ...}``.

    *baq_params* are the BAQ's own execution parameters (Query Parameters in
    the BAQ Designer, e.g. ``{"FromDate": "2025-01-01"}``) — a different
    channel from ``$filter``: parameters feed the query itself, the filter
    trims its result. BaqSvc takes them as plain query-string entries by
    ParameterID; a BAQ with a mandatory parameter 400s without them.
    """
    baq_result = rbac.check_baq_access(session.user_id)
    if not baq_result.allowed:
        return {"error": baq_result.message}

    baq_api_key = baq_result.api_key or ""

    top = max(1, min(top, 1000))
    params: dict[str, str | int] = {"$top": top}
    if filter:
        params["$filter"] = filter
    if orderby:
        params["$orderby"] = orderby
    for key, value in (baq_params or {}).items():
        key = str(key).strip()
        # $-names are OData system options, not BAQ parameters — the real
        # ones never start with '$', so drop rather than double-set $top etc.
        if key and not key.startswith("$"):
            params[key] = "" if value is None else str(value)

    url = f"BaqSvc/{baq_id}/Data"
    actual_baq_id = baq_id
    try:
        response = await client.get(url, baq_api_key, params=params)
    except EpicorError as exc:
        if exc.status_code in (404, 400) and not baq_id.startswith("AUTO-"):
            auto_id = f"AUTO-{baq_id}"
            logger.info("run_baq: '%s' missing, retrying as '%s'", baq_id, auto_id)
            response = await client.get(
                f"BaqSvc/{auto_id}/Data", baq_api_key, params=params
            )
            actual_baq_id = auto_id
        else:
            raise

    records = response.get("value", response)
    record_count = len(records) if isinstance(records, list) else None

    result: dict[str, Any] = {
        "baq_id": actual_baq_id,
        "records": records,
    }
    if actual_baq_id != baq_id:
        result["note_baq_id"] = (
            f"BAQ '{baq_id}' was not found. Used '{actual_baq_id}' "
            "instead (AUTO- prefix added automatically)."
        )
    if record_count is not None:
        result["record_count"] = record_count
    if record_count == top:
        result["note"] = (
            f"Result limited to {top} records. "
            "Increase 'top' or add a filter to narrow results."
        )
    return result

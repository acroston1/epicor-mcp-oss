"""Response formatting and size control for Epicor MCP tool responses.

Every tool routes its final return value through :func:`format_response` so
that four concerns are applied consistently:

1. Known bloat keys (``RowMod``, ``SysRowID``, ``SysRevID``, ``BitFlag``,
   ``RowIdent``) and OData metadata (``@odata.*``) are stripped, along with
   empty/null values that just inflate the response.
2. Optional CSV/TSV encoding of the rows array cuts wire size ~2-3x when
   callers request it via the ``format`` param.
3. If the encoded response exceeds ``response_max_bytes`` (default 100 KB),
   the payload is offloaded to a file under ``response_offload_dir`` and a
   small pointer (path + row counts + primary-list preview + stats) is
   returned instead.  The client uses filesystem tools (grep/head/tail) to
   inspect the file without loading the full thing into context.
4. Progressive truncation is the last-resort fallback when offloading is
   disabled or fails.

Stdlib-only. Functions are pure — no shared state, safe under asyncio.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import statistics
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from epicor_mcp.config import Settings

logger = logging.getLogger(__name__)

BLOAT_KEYS: frozenset[str] = frozenset(
    {"RowMod", "SysRowID", "SysRevID", "BitFlag", "RowIdent"}
)
BLOAT_PREFIXES: tuple[str, ...] = ("@odata.",)

# --- Aggressive bloat keys (opt-in, default ON for read paths) --------------
# Fields that almost no analysis question depends on. They consume tokens on
# every row of every query response. Mirrors the lists in
# ``_inline_schema._AUDIT_FIELDS`` / ``_AUDIT_PREFIXES`` so the schema
# description and the response stripping agree on "what's noise."
AGGRESSIVE_BLOAT_KEYS: frozenset[str] = frozenset({
    "Company",
    "CreatedBy", "CreatedOn", "CreatedDate", "CreatedTime",
    "ChangedBy", "ChangedOn", "ChangeDate", "ChangeTime",
    "EntryDate", "EntryTime",
    "GlobalLock", "GlobalRowMod",
})
AGGRESSIVE_BLOAT_PREFIXES: tuple[str, ...] = (
    "Glb", "TaxConnect", "ETC", "MX", "PE", "TH", "AG",
    "AttributeSet", "ABT", "DspWithhold", "Enable",
)

# --- Long-string truncation -------------------------------------------------
# Wall-of-text columns (descriptions, comments, memos) frequently carry
# paragraphs that exceed any practical token budget. Truncate string values
# longer than this when stripping. Configurable via response_max_string_chars
# in Settings; pass ``max_string_chars=None`` to disable.
_DEFAULT_MAX_STRING_CHARS = 300
_TRUNCATION_SUFFIX = "…[truncated]"

_DEFAULT_MAX_BYTES = 900_000
_DEFAULT_TRUNCATE_KEEP = 20
_DEFAULT_STATS_TOP_K = 10

_OVERSIZE_GUIDANCE = (
    "Response exceeded the ~{budget_kb} KB tool-result limit "
    "({actual_kb} KB). Showing first {kept} of {total} rows plus aggregate "
    "stats. To narrow: add a $filter, reduce the date range, or pass "
    'fields="col1,col2,..." to request only the columns you need.'
)


def _get_settings():
    """Late-import the settings singleton to avoid a circular import."""
    from epicor_mcp.config import get_settings

    return get_settings()


def _is_empty(value: Any) -> bool:
    """Return True for None / empty string / empty list. Keeps 0 and False."""
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    if isinstance(value, list) and not value:
        return True
    return False


def _truncate_string(value: str, max_chars: int | None) -> str:
    """Truncate *value* to *max_chars* with a marker suffix; pass-through if
    ``max_chars`` is None or the string fits."""
    if max_chars is None or len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + _TRUNCATION_SUFFIX


def strip_bloat(
    obj: Any,
    *,
    preserve_row_meta: bool = False,
    drop_empty: bool = True,
    aggressive: bool = False,
    max_string_chars: int | None = None,
) -> Any:
    """Recursively remove bloat keys, truncate long strings, drop empties.

    Always removes keys whose name starts with any prefix in
    :data:`BLOAT_PREFIXES` (OData metadata like ``@odata.context``).

    Unless ``preserve_row_meta`` is True, also removes keys in
    :data:`BLOAT_KEYS` (``RowMod``, ``SysRowID``, ``SysRevID``, ``BitFlag``,
    ``RowIdent``).  The write path sets ``preserve_row_meta=True`` so
    ``workflow`` responses keep the operation markers callers may need.

    When ``aggressive`` is True, also drops audit/locale fields
    (``CreatedBy``, ``ChangeDate``, ``Company``, …) and prefix groups
    (``Glb*``, ``TaxConnect*``, ``MX*``, …). Default for read paths.

    When ``max_string_chars`` is set, any string value longer than that
    is truncated with a marker suffix. Keeps responses sane when a row
    happens to carry a paragraph in a Description / Comments field.

    If ``drop_empty`` is True, also removes keys whose value is ``None``,
    the empty string, or an empty list.  ``0`` and ``False`` are kept.

    Idempotent — safe to call multiple times.  The input is not mutated.
    """
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for key, value in obj.items():
            if any(key.startswith(p) for p in BLOAT_PREFIXES):
                continue
            if not preserve_row_meta and key in BLOAT_KEYS:
                continue
            if aggressive:
                if key in AGGRESSIVE_BLOAT_KEYS:
                    continue
                if any(key.startswith(p) for p in AGGRESSIVE_BLOAT_PREFIXES):
                    continue
            cleaned_value = strip_bloat(
                value,
                preserve_row_meta=preserve_row_meta,
                drop_empty=drop_empty,
                aggressive=aggressive,
                max_string_chars=max_string_chars,
            )
            if drop_empty and _is_empty(cleaned_value):
                continue
            cleaned[key] = cleaned_value
        return cleaned
    if isinstance(obj, list):
        return [
            strip_bloat(
                item,
                preserve_row_meta=preserve_row_meta,
                drop_empty=drop_empty,
                aggressive=aggressive,
                max_string_chars=max_string_chars,
            )
            for item in obj
        ]
    if isinstance(obj, str) and max_string_chars is not None:
        return _truncate_string(obj, max_string_chars)
    return obj


def _cell_to_csv(value: Any) -> str:
    """Render a single cell value for CSV/TSV output.

    Nested dicts/lists are ``json.dumps``-encoded into a single cell with
    compact separators; the caller can re-parse those cells if needed.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


def rows_to_csv(
    rows: list[dict], *, delimiter: str = ","
) -> tuple[str, list[str]]:
    """Serialise a list of row dicts as CSV (or TSV with ``delimiter='\\t'``).

    Columns are the union of keys seen across rows, in first-seen order —
    preserves the caller's ``$select`` intent rather than sorting.

    Returns a tuple of ``(csv_text, columns)``.  Empty input returns
    ``("", [])``.
    """
    if not rows:
        return "", []

    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                columns.append(k)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell_to_csv(row.get(c)) for c in columns])
    return buf.getvalue(), columns


def compute_stats(
    rows: list[dict], *, top_k: int = _DEFAULT_STATS_TOP_K
) -> dict[str, Any]:
    """Compute cheap aggregate stats over a list of row dicts.

    Per column:
    - All-numeric (int/float/None, excluding bool): ``count``, ``min``,
      ``max``, ``sum``, ``avg``.
    - Low-cardinality string (≤ ``top_k`` distinct, ≤60% unique):
      ``top`` list of ``[value, count]`` pairs plus ``distinct_count``.
    - Otherwise: ``non_null_count`` + ``distinct_count``.
    - Columns containing nested dicts/lists: skipped.

    Returns an empty dict for empty input.
    """
    if not rows:
        return {}

    columns: dict[str, list[Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            columns.setdefault(k, []).append(v)

    stats: dict[str, Any] = {}
    total = len(rows)
    for col, values in columns.items():
        non_null = [v for v in values if v is not None]
        if not non_null:
            continue
        if any(isinstance(v, (dict, list)) for v in non_null):
            continue
        if all(
            isinstance(v, (int, float)) and not isinstance(v, bool)
            for v in non_null
        ):
            try:
                stats[col] = {
                    "count": len(non_null),
                    "min": min(non_null),
                    "max": max(non_null),
                    "sum": sum(non_null),
                    "avg": round(statistics.mean(non_null), 4),
                }
            except statistics.StatisticsError:
                stats[col] = {"non_null_count": len(non_null)}
            continue

        str_values = [str(v) for v in non_null]
        distinct = len(set(str_values))
        if distinct <= top_k and distinct / total <= 0.6:
            counter = Counter(str_values)
            stats[col] = {
                "top": [list(pair) for pair in counter.most_common(top_k)],
                "distinct_count": distinct,
            }
        else:
            stats[col] = {
                "non_null_count": len(non_null),
                "distinct_count": distinct,
            }
    return stats


def _find_largest_rows(
    obj: Any,
) -> tuple[Any, Any, list | None]:
    """Return ``(container, key, rows)`` for the largest ``list[dict]`` in obj.

    Walks dicts and lists.  If nothing matches, returns ``(None, None, None)``.
    """
    all_lists = _collect_rows_lists(obj)
    if not all_lists:
        return None, None, None
    container, key, rows = max(all_lists, key=lambda t: len(t[2]))
    return container, key, rows


def _collect_rows_lists(obj: Any) -> list[tuple[Any, Any, list]]:
    """Return every ``(container, key, list_of_dicts)`` found anywhere in obj.

    Used by the oversize path so *all* rows arrays get capped, not just the
    single largest — important for Epicor multi-table datasets where a
    Quote/Job/PO header can have many sizeable child tables.
    """
    found: list[tuple[Any, Any, list]] = []

    def visit(node: Any, container: Any, key: Any) -> None:
        if isinstance(node, list):
            if node and all(isinstance(r, dict) for r in node):
                found.append((container, key, node))
            for i, item in enumerate(node):
                visit(item, node, i)
        elif isinstance(node, dict):
            for k, v in node.items():
                visit(v, node, k)

    visit(obj, None, None)
    return found


def _wire_size_bytes(payload: str) -> int:
    """Approximate the MCP-transport size of a tool-result ``payload`` string.

    Claude Desktop's ~1 MB limit is enforced on the full JSON-RPC message,
    not on our raw Python string.  Our string ends up embedded as the
    ``text`` field of an MCP content item, which JSON-escapes every ``"``
    and ``\\n`` in our output — easily inflating a pretty-printed JSON
    payload by 25-35 %.

    ``json.dumps(payload)`` produces exactly that escaped form (with
    surrounding quotes), and we add a small constant for the RPC envelope.
    """
    return len(json.dumps(payload).encode("utf-8")) + 80


def truncate_and_summarize(
    result: dict,
    *,
    records_key: str | None,
    actual_bytes: int,
    settings: "Settings",
) -> dict:
    """Cap every rows array in ``result`` and append aggregate stats.

    Mutates and returns ``result``.  Stats are computed from the "primary"
    rows array (``result[records_key]`` when set, otherwise the largest
    ``list[dict]`` found anywhere).  *Every* ``list[dict]`` in the tree is
    then truncated to ``settings.response_truncate_keep`` rows — critical
    for Epicor multi-child datasets (Quote/Job/PO) where one pass over
    the largest list leaves other child tables untouched.
    """
    keep = max(1, settings.response_truncate_keep)
    budget_kb = settings.response_max_bytes // 1024
    actual_kb = actual_bytes // 1024
    top_k = settings.response_stats_top_k

    all_lists = _collect_rows_lists(result)

    primary_rows: list[dict] | None = None
    if records_key and isinstance(result.get(records_key), list):
        candidate = result[records_key]
        if candidate and all(isinstance(r, dict) for r in candidate):
            primary_rows = candidate
    if primary_rows is None and all_lists:
        _, _, primary_rows = max(all_lists, key=lambda t: len(t[2]))

    if not primary_rows:
        result["truncated"] = True
        result["note"] = _OVERSIZE_GUIDANCE.format(
            budget_kb=budget_kb, actual_kb=actual_kb, kept=0, total=0
        )
        result["error"] = (
            "Response exceeded byte budget but no rows array was found to "
            'truncate. Pass fields="col1,col2,..." to narrow the columns.'
        )
        return result

    total = len(primary_rows)
    stats = compute_stats(primary_rows, top_k=top_k)
    kept = min(keep, total)

    # Cap every rows array — not just the primary.  Reduces worst-case
    # responses from multi-table datasets (Quote/Job/PO with many children).
    for container, key, rows in all_lists:
        if len(rows) > keep:
            container[key] = rows[:keep]

    result["truncated"] = True
    result["truncation_reason"] = "response_exceeded_byte_budget"
    result["original_record_count"] = total
    result["returned_record_count"] = kept
    # `record_count` was set from the PRE-truncation row list and used to be
    # left stale here, so every downstream consumer read a count it no longer
    # had the rows for — epicor_read then computed has_more from the TRUNCATED
    # list and suppressed next_cursor, mislabeling partial rows as the complete
    # result. Keep the payload self-consistent.
    result["record_count"] = kept
    result["rows_dropped_for_size"] = total - kept
    result["stats"] = stats
    result["note"] = _OVERSIZE_GUIDANCE.format(
        budget_kb=budget_kb,
        actual_kb=actual_kb,
        kept=kept,
        total=total,
    )
    return result


def format_response(
    result: Any,
    *,
    records_key: str | None = "records",
    format: str = "json",
    strip_only: bool = False,
    settings: "Settings | None" = None,
) -> str:
    """Format a tool result as a JSON string, with shrink + size guard.

    Parameters
    ----------
    result:
        Tool result — typically a dict.  Non-dicts are pass-through encoded
        without shrinking.
    records_key:
        Top-level key holding the rows array.  ``None`` means the result
        has no primary rows array at the top level (``get_record`` headers,
        ``workflow`` write responses, metadata tools); CSV encoding is then
        skipped and the size guard falls back to finding the largest
        nested ``list[dict]``.
    format:
        Wire format for the rows array.  One of ``"json"``, ``"csv"``, or
        ``"tsv"``.  Ignored unless ``records_key`` points at a real rows
        array.
    strip_only:
        When True, apply a minimal cleanup only: strip ``@odata.*`` keys
        but preserve row metadata (``RowMod``, ``SysRowID`` …) and empty
        fields, and skip both the CSV encoding and the size-guard
        truncation.  Used by ``workflow`` so the write response keeps
        whatever data Epicor returned about what was written.
    settings:
        Settings instance.  Defaults to the module singleton.

    Returns
    -------
    str
        JSON-encoded response ready to return from a tool.
    """
    if settings is None:
        settings = _get_settings()

    if not isinstance(result, dict):
        return json.dumps(result, indent=2, default=str)

    if strip_only:
        result = strip_bloat(
            result, preserve_row_meta=True, drop_empty=False
        )
        return json.dumps(result, indent=2, default=str)

    drop_empty = getattr(settings, "response_drop_empty", True)
    aggressive = getattr(settings, "response_aggressive_strip", True)
    max_string_chars = getattr(
        settings, "response_max_string_chars", _DEFAULT_MAX_STRING_CHARS
    )
    result = strip_bloat(
        result,
        preserve_row_meta=False,
        drop_empty=drop_empty,
        aggressive=aggressive,
        max_string_chars=max_string_chars,
    )

    if (
        format in ("csv", "tsv")
        and records_key
        and isinstance(result.get(records_key), list)
        and result[records_key]
        and all(isinstance(r, dict) for r in result[records_key])
    ):
        delim = "\t" if format == "tsv" else ","
        csv_text, columns = rows_to_csv(
            result[records_key], delimiter=delim
        )
        result = {
            **{k: v for k, v in result.items() if k != records_key},
            "format": format,
            "columns": columns,
            "rows_csv": csv_text,
        }

    payload = json.dumps(result, indent=2, default=str)
    max_bytes = getattr(settings, "response_max_bytes", _DEFAULT_MAX_BYTES)
    wire_size = _wire_size_bytes(payload)
    if wire_size <= max_bytes:
        return payload

    # Primary path: offload to disk and return a pointer so the client
    # can grep the file instead of ingesting the whole thing.
    offload_dir = _offload_dir(settings)
    if offload_dir is not None:
        try:
            return offload_response(
                result,
                payload,
                offload_dir=offload_dir,
                settings=settings,
            )
        except OSError as exc:
            logger.warning(
                "Offload to %s failed (%s); falling back to truncation.",
                offload_dir,
                exc,
            )

    logger.info(
        "Response wire size %d bytes exceeded budget %d; truncating.",
        wire_size,
        max_bytes,
    )
    # Fallback: truncate + compute stats using the configured keep value.
    result = truncate_and_summarize(
        result,
        records_key=records_key,
        actual_bytes=wire_size,
        settings=settings,
    )
    payload = json.dumps(result, indent=2, default=str)
    wire_size = _wire_size_bytes(payload)

    # Progressive retry: halve the keep cap until we fit or reach 1.
    # We'd rather hand back fewer rows of real data than a bare stats
    # block, so we only fall through to the drop-all fallback if even
    # one row per table still overflows the budget.
    keep = max(1, settings.response_truncate_keep)
    while wire_size > max_bytes and keep > 1:
        keep = max(1, keep // 2)
        _cap_all_rows_lists(result, keep)
        result["returned_record_count"] = min(
            keep, result.get("original_record_count", keep)
        )
        payload = json.dumps(result, indent=2, default=str)
        wire_size = _wire_size_bytes(payload)
        logger.info(
            "Re-truncated to keep=%d; wire size now %d bytes.",
            keep,
            wire_size,
        )

    if wire_size > max_bytes:
        logger.warning(
            "Response still %d bytes after truncation to keep=1; "
            "dropping rows entirely.",
            wire_size,
        )
        fallback: dict = {
            "truncated": True,
            "truncation_reason": "single_row_exceeded_budget",
            "stats": result.get("stats", {}),
            "note": result.get("note", ""),
            "error": (
                "Single-row response exceeded the byte budget. Pass "
                'fields="col1,col2,..." to narrow which columns are returned,'
                " or call epicor_read with a where filter to fetch only the"
                " rows you need."
            ),
        }
        payload = json.dumps(fallback, indent=2, default=str)

    return payload


def _cap_all_rows_lists(obj: Any, keep: int) -> None:
    """Mutate *obj* in-place so every ``list[dict]`` is capped to *keep* rows."""
    for container, key, rows in _collect_rows_lists(obj):
        if len(rows) > keep:
            container[key] = rows[:keep]


def _offload_dir(settings: "Settings | None") -> Path | None:
    """Resolve the offload directory from settings.  Return ``None`` when disabled."""
    if settings is None:
        return None
    if not getattr(settings, "response_offload_enabled", True):
        return None
    raw = getattr(settings, "response_offload_dir", None)
    if raw is None:
        return None
    path = Path(raw) if not isinstance(raw, Path) else raw
    if str(path).strip() == "":
        return None
    return path


def offload_response(
    result: Any,
    payload: str,
    *,
    offload_dir: Path,
    settings: "Settings",
) -> str:
    """Write *payload* to a file under *offload_dir* and return a pointer JSON.

    The pointer is a small JSON object summarising the offloaded payload —
    top-level keys, per-table row counts, the first N rows of the primary
    list, and aggregate stats — plus a ``url`` the client can curl and a
    concrete ``usage`` block with shell examples.  Filenames carry 128
    bits of entropy, so knowledge of the URL is the access capability;
    files are purged per the retention setting.
    """
    offload_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # 32 hex chars = 128 bits.  URL-as-capability; no other auth needed
    # on the /response-files/{name} endpoint.
    filename = f"{stamp}-{uuid.uuid4().hex}.json"
    file_path = offload_dir / filename
    file_path.write_text(payload, encoding="utf-8")

    preview_rows = int(getattr(settings, "response_offload_preview_rows", 5))
    top_k = int(getattr(settings, "response_stats_top_k", _DEFAULT_STATS_TOP_K))

    # Per-table row counts from every list[dict] we find in the payload.
    row_counts: dict[str, int] = {}
    all_lists = _collect_rows_lists(result) if isinstance(result, (dict, list)) else []
    for _, key, rows in all_lists:
        row_counts[str(key)] = len(rows)

    # Primary list + stats + inline preview so Claude can reason without
    # immediately hitting the file for simple questions.
    primary_name: str | None = None
    primary_total = 0
    primary_preview: list[dict] = []
    primary_stats: dict[str, Any] = {}
    if all_lists:
        _, primary_key, primary_rows = max(all_lists, key=lambda t: len(t[2]))
        primary_name = str(primary_key)
        primary_total = len(primary_rows)
        primary_preview = primary_rows[:preview_rows]
        primary_stats = compute_stats(primary_rows, top_k=top_k)

    top_level_keys: list[str] = []
    if isinstance(result, dict):
        top_level_keys = list(result.keys())

    size_bytes = len(payload.encode("utf-8"))
    size_kb = size_bytes // 1024
    base_url = str(getattr(settings, "response_public_base_url", "") or "").rstrip("/")
    url = f"{base_url}/response-files/{filename}" if base_url else ""

    # Pick a primary-list name to drop into the python example so the
    # shell snippet is immediately runnable against this specific payload.
    example_list = primary_name or "<list>"

    if url:
        usage = (
            f"Full result is {size_kb} KB on the MCP server (NOT on your "
            f"sandbox — do not try to open it as a local path).\n"
            f"\n"
            f"Pick the right follow-up based on what you're looking for:\n"
            f"\n"
            f"1) BEST for structured field lookups (you know the field "
            f"and value) — skip grepping and run a targeted query"
            f" instead of consulting this file at all:\n"
            f"     epicor_query(service=<svc>, entity_set=<child-table>,"
            f" filter=\"<Key> eq <Value> and <Field> eq '<value>'\")\n"
            f"   Example: epicor_query(service=\"Erp.BO.QuoteSvc\","
            f" entity_set=\"QuoteDtl\","
            f" filter=\"QuoteNum eq <num> and PartNum eq '<part>'\")\n"
            f"\n"
            f"2) GOOD for field lookups when the record is already"
            f" offloaded — field-aware row filter on this file:\n"
            f"     epicor_filter_offloaded(file=\"{url}\","
            f" table=\"<TableName>\", where='{{\"<Field>\": <value>}}')\n"
            f"\n"
            f"3) FALLBACK for free-text / regex search — line-oriented"
            f" grep on this file (can be noisy when a field name repeats"
            f" across rows):\n"
            f"     epicor_grep_offloaded(file=\"{url}\", pattern=\"<regex>\")\n"
            f"\n"
            f"All three go through the MCP bridge, so sandbox host"
            f" allowlists don't block them.  Prefer option 1 when you"
            f" know the field; fall to 2 only if the record is already"
            f" offloaded; reserve 3 for unstructured text search."
        )
    else:
        usage = (
            f"Full result is {size_kb} KB but EPICOR_MCP_RESPONSE_PUBLIC_BASE_URL"
            f" is not configured, so the client has no way to reach the "
            f"file. Ask the server admin to set that env var, or fall "
            f"back to narrower epicor_query calls with a $filter on the "
            f"specific entity_set."
        )

    pointer: dict[str, Any] = {
        "offloaded": True,
        "reason": "response_too_large_for_inline_context",
        "size_bytes": size_bytes,
        "top_level_keys": top_level_keys,
        "row_counts": row_counts,
    }
    if url:
        pointer["url"] = url
    if primary_name is not None:
        pointer["primary_list"] = {
            "name": primary_name,
            "total_rows": primary_total,
            "preview": primary_preview,
        }
    if primary_stats:
        pointer["stats"] = primary_stats
    pointer["usage"] = usage

    # Return only the URL: the client does not share the server's filesystem.
    # A local path could point to an unrelated file in the client's sandbox.
    logger.info(
        "Offloaded %d-byte response to %s (url: %s)",
        size_bytes,
        file_path,
        url or "<no public URL configured>",
    )
    return json.dumps(pointer, indent=2, default=str)


def purge_stale_offloads(offload_dir: Path, retention_hours: int) -> int:
    """Delete offloaded files older than ``retention_hours``.  Return count.

    No-op when ``offload_dir`` doesn't exist or ``retention_hours`` is 0
    or negative.  Failures on individual files are logged but don't raise.
    """
    if retention_hours <= 0 or not offload_dir.exists():
        return 0
    cutoff = time.time() - retention_hours * 3600
    deleted = 0
    for entry in offload_dir.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                deleted += 1
        except OSError as exc:
            logger.warning("Could not purge %s: %s", entry, exc)
    return deleted

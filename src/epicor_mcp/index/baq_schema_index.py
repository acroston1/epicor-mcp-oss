"""Query interface for the BAQ schema SQLite index.

Provides FTS5-powered search over the Epicor data dictionary tables and fields,
enabling MCP tools to help users find the right tables/fields for BAQ SQL.

All methods are synchronous -- SQLite is fast enough for this use case.

Usage:
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    idx = BAQSchemaIndex("data/baq_schema.db")
    results = idx.search_tables("purchase order")
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


# Regex for stripping FTS5 special characters from user input.
_FTS_SANITIZE_RE = re.compile(r"[^\w\s.]", re.UNICODE)

# Words to drop from natural-language queries before building the FTS
# expression. They burn AND-slots in the old build and rarely appear in
# table names or terse data-dictionary descriptions.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "to", "in", "on", "at", "by", "for", "from",
    "with", "and", "or", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "as", "it", "its", "their",
    # Domain noise — these never disambiguate Epicor tables
    "data", "info", "information", "list", "lookup", "find", "search",
    "show", "get", "fields", "field", "table", "tables", "schema",
    "epicor", "kinetic", "erp",
})


def _split_terms(query: str) -> list[str]:
    """Sanitize a free-text query into ranked search terms.

    Lower-cases, strips FTS punctuation, splits on whitespace, drops
    stopwords. Preserves dots so callers can pass ``Erp.OrderHed`` and
    get the schema-qualified form treated as a single token.
    """
    cleaned = _FTS_SANITIZE_RE.sub(" ", query).strip()
    if not cleaned:
        return []
    return [t for t in cleaned.split() if t.lower() not in _STOPWORDS]


def _sanitize_fts(query: str, *, mode: str = "or") -> str:
    """Build a prefix-match FTS5 expression from a free-text query.

    ``mode="or"`` (the new default) joins terms with ``OR`` so a
    multi-word natural-language query doesn't require every term to
    match. ``mode="and"`` keeps the legacy behaviour.

    Stopwords are dropped first.
    """
    terms = _split_terms(query)
    if not terms:
        return ""
    quoted = [f'"{t}"*' for t in terms]
    joiner = " OR " if mode == "or" else " "
    return joiner.join(quoted)


class BAQSchemaIndex:
    """Read-only query interface over the BAQ schema SQLite index.

    Parameters
    ----------
    db_path:
        Path to the ``baq_schema.db`` SQLite database built by
        ``scripts/build_baq_index.py``.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def __enter__(self) -> "BAQSchemaIndex":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dict_rows(self, cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
        """Convert cursor results to a list of plain dicts."""
        cols = [d[0] for d in cursor.description] if cursor.description else []
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Table queries
    # ------------------------------------------------------------------

    def search_tables(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search the data dictionary for matching tables.

        Strategy (in order, deduped):
          1. Exact case-insensitive match on ``table_name`` or ``full_name``
             for each query term. Pins ``Erp.OrderHed`` to the top when
             the user typed ``OrderHed``.
          2. Prefix match on ``table_name`` for each term — catches
             ``Erp.OrderHedXxx`` after the exact hit.
          3. FTS5 with terms OR-joined so multi-word natural-language
             queries still return something.

        Stopwords (``the``, ``of``, ``order``, ``customer``, etc.) are
        dropped from the FTS pass but kept for #1 and #2.
        """
        if not query or not query.strip():
            return []

        raw_terms = [
            t for t in _FTS_SANITIZE_RE.sub(" ", query).split()
            if t
        ]
        if not raw_terms:
            return []

        seen: set[str] = set()
        results: list[dict[str, Any]] = []

        def add_rows(rows: list[dict[str, Any]]) -> None:
            for r in rows:
                key = r.get("full_name") or ""
                if key and key not in seen and len(results) < limit:
                    seen.add(key)
                    results.append(r)

        # --- 1. Exact (case-insensitive) table_name / full_name match ---
        for term in raw_terms:
            placeholders = ", ".join(["?"] * 2)
            cur = self._conn.execute(
                f"""SELECT full_name, schema_name, table_name, description
                    FROM tables
                    WHERE LOWER(table_name) = ?
                       OR LOWER(full_name) = ?
                    ORDER BY
                      CASE WHEN schema_name = 'Erp' THEN 0
                           WHEN schema_name = 'Ice' THEN 1
                           ELSE 2 END,
                      LENGTH(table_name)""",
                (term.lower(), term.lower()),
            )
            add_rows(self._dict_rows(cur))
            if len(results) >= limit:
                return results

        # --- 2. Prefix match on table_name for each term ---
        for term in raw_terms:
            if len(term) < 3:
                continue  # don't prefix-match on tiny tokens
            cur = self._conn.execute(
                """SELECT full_name, schema_name, table_name, description
                   FROM tables
                   WHERE LOWER(table_name) LIKE ? || '%'
                   ORDER BY
                     CASE WHEN schema_name = 'Erp' THEN 0
                          WHEN schema_name = 'Ice' THEN 1
                          ELSE 2 END,
                     LENGTH(table_name)
                   LIMIT ?""",
                (term.lower(), limit),
            )
            add_rows(self._dict_rows(cur))
            if len(results) >= limit:
                return results

        # --- 3. FTS5 OR-search across name + description ---
        fts_expr = _sanitize_fts(query, mode="or")
        if fts_expr:
            cur = self._conn.execute(
                """SELECT t.full_name, t.schema_name, t.table_name, t.description
                   FROM table_search AS ts
                   JOIN tables AS t ON ts.rowid = t.rowid
                   WHERE table_search MATCH ?
                   ORDER BY
                     CASE WHEN t.schema_name = 'Erp' THEN 0
                          WHEN t.schema_name = 'Ice' THEN 1
                          ELSE 2 END,
                     rank
                   LIMIT ?""",
                (fts_expr, limit),
            )
            add_rows(self._dict_rows(cur))

        return results

    def get_table(self, full_name: str) -> dict[str, Any] | None:
        """Get table info by full name (e.g. ``'Erp.POHeader'``).

        Returns ``None`` if the table is not found.
        """
        cur = self._conn.execute(
            "SELECT full_name, schema_name, table_name, description "
            "FROM tables WHERE full_name = ?",
            (full_name,),
        )
        rows = self._dict_rows(cur)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Field queries
    # ------------------------------------------------------------------

    def get_fields(self, full_table_name: str) -> list[dict[str, Any]]:
        """Get all fields for a table.

        Parameters
        ----------
        full_table_name:
            Full table name (e.g. ``"Erp.POHeader"``).

        Returns
        -------
        list of dicts with keys: full_table_name, field_name, data_type,
        field_label, mandatory, field_format, description.
        """
        cur = self._conn.execute(
            "SELECT full_table_name, field_name, data_type, field_label, "
            "mandatory, field_format, description "
            "FROM fields WHERE full_table_name = ? "
            "ORDER BY field_name",
            (full_table_name,),
        )
        return self._dict_rows(cur)

    def search_fields(
        self,
        query: str,
        table_filter: str = "",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """FTS5 search for fields by name, label, or description.

        Parameters
        ----------
        query:
            Search keywords (e.g. ``"vendor number"``).
        table_filter:
            Optional full table name to restrict results
            (e.g. ``"Erp.POHeader"``).
        limit:
            Maximum number of results to return.

        Returns
        -------
        list of dicts with keys: full_table_name, field_name, field_label,
        description, data_type, mandatory, field_format.
        """
        fts_expr = _sanitize_fts(query)
        if not fts_expr:
            return []

        if table_filter:
            sql = """
                SELECT f.full_table_name, f.field_name, f.data_type,
                       f.field_label, f.mandatory, f.field_format,
                       f.description
                FROM field_search AS fs
                JOIN fields AS f ON fs.rowid = f.rowid
                WHERE field_search MATCH ?
                  AND f.full_table_name = ?
                ORDER BY rank
                LIMIT ?
            """
            cur = self._conn.execute(sql, (fts_expr, table_filter, limit))
        else:
            sql = """
                SELECT f.full_table_name, f.field_name, f.data_type,
                       f.field_label, f.mandatory, f.field_format,
                       f.description
                FROM field_search AS fs
                JOIN fields AS f ON fs.rowid = f.rowid
                WHERE field_search MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            cur = self._conn.execute(sql, (fts_expr, limit))

        return self._dict_rows(cur)

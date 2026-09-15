"""Query interface for the Epicor service SQLite index.

All methods are synchronous — SQLite is fast enough that async is unnecessary.

Usage:
    from epicor_mcp.index import ServiceIndex
    idx = ServiceIndex("data/service_index.db")
    results = idx.search_services("vendor")
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class ServiceIndex:
    """Read-only query interface over the Epicor service SQLite index."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        # Enable WAL for concurrent reads
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def __enter__(self) -> "ServiceIndex":
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
    # Service queries
    # ------------------------------------------------------------------

    def search_services(
        self,
        query: str,
        department: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search across services.

        Optionally filter to services accessible by *department*.
        Returns a list of service dicts ordered by relevance.
        """
        # Build the FTS query — append * for prefix matching
        fts_query = query.strip()
        if not fts_query:
            return []

        # Sanitize input: remove FTS5 special characters to prevent
        # query injection.  We keep only alphanumeric chars, spaces, and
        # dots (which appear in service IDs like "Erp.BO.PartSvc").
        sanitized = "".join(
            ch for ch in fts_query if ch.isalnum() or ch in " ."
        ).strip()
        if not sanitized:
            return []

        # Add prefix wildcard for better usability.  FTS5 combines
        # space-separated terms with implicit AND, so every term must
        # prefix-match a token.  Over the sparse per-service search text
        # this makes multi-word queries brittle: a single qualifier that
        # isn't indexed (e.g. "customer invoice") zeroes the entire result
        # set.  We therefore run an AND pass first (most precise) and, if it
        # returns nothing, fall back to an OR pass so partial matches still
        # surface, ranked by relevance.
        terms = sanitized.split()
        and_expr = " ".join(f'"{t}"*' for t in terms)

        results = self._run_fts(and_expr, department, limit)
        if not results and len(terms) > 1:
            or_expr = " OR ".join(f'"{t}"*' for t in terms)
            results = self._run_fts(or_expr, department, limit)
        return results

    def _run_fts(
        self,
        fts_expr: str,
        department: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Execute one FTS ``MATCH`` pass, optionally department-scoped."""
        if department:
            sql = """
                SELECT s.service_id, s.prefix, s.short_name, s.description,
                       s.category, s.path_count, s.method_count
                FROM service_search AS ss
                JOIN services AS s ON ss.service_id = s.service_id
                JOIN department_services AS ds ON ds.service_id = s.service_id
                WHERE service_search MATCH ?
                  AND ds.department = ?
                ORDER BY rank
                LIMIT ?
            """
            cur = self._conn.execute(sql, (fts_expr, department, limit))
        else:
            sql = """
                SELECT s.service_id, s.prefix, s.short_name, s.description,
                       s.category, s.path_count, s.method_count
                FROM service_search AS ss
                JOIN services AS s ON ss.service_id = s.service_id
                WHERE service_search MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            cur = self._conn.execute(sql, (fts_expr, limit))

        return self._dict_rows(cur)

    def service_exists(self, service_id: str) -> bool:
        """Return True if *service_id* exists in the index."""
        cur = self._conn.execute(
            "SELECT 1 FROM services WHERE service_id = ? LIMIT 1",
            (service_id,),
        )
        return cur.fetchone() is not None

    def get_service(self, service_id: str) -> dict[str, Any] | None:
        """Get full service info including entity sets and method list.

        Returns None if the service is not found.
        """
        cur = self._conn.execute(
            "SELECT * FROM services WHERE service_id = ?", (service_id,)
        )
        rows = self._dict_rows(cur)
        if not rows:
            return None

        svc = rows[0]
        svc["entity_sets"] = self.get_entity_sets(service_id)
        svc["methods"] = self.get_methods(service_id)
        return svc

    def get_methods(self, service_id: str) -> list[dict[str, Any]]:
        """Get all methods for a service."""
        cur = self._conn.execute(
            "SELECT * FROM methods WHERE service_id = ? ORDER BY method_name",
            (service_id,),
        )
        return self._dict_rows(cur)

    def get_entity_sets(self, service_id: str) -> list[str]:
        """Get entity set names for a service."""
        cur = self._conn.execute(
            "SELECT entity_set_name FROM entity_sets WHERE service_id = ? ORDER BY entity_set_name",
            (service_id,),
        )
        return [row[0] for row in cur.fetchall()]

    def services_for_entity(self, entity_set: str) -> list[dict[str, Any]]:
        """Every service exposing an entity set named *entity_set* (case-insensitive).

        The reverse of ``get_entity_sets``. Without it the resolver could only
        ever match a service NAME, so an exact, real table name that isn't in
        the ~40-entry curated map fell through to a description FTS and came
        back as bogus "ambiguity" — yet most entity names in the index
        have exactly ONE host and are not ambiguous at all.

        Returns ``[{"service_id": ..., "entity_set_name": <real casing>}, ...]``.
        """
        cur = self._conn.execute(
            "SELECT DISTINCT service_id, entity_set_name FROM entity_sets "
            "WHERE lower(entity_set_name) = ? ORDER BY service_id",
            ((entity_set or "").strip().lower(),),
        )
        return self._dict_rows(cur)

    def find_field_owners(
        self, field_name: str, limit: int = 6
    ) -> list[dict[str, Any]]:
        """Entities that actually carry a column named *field_name*.

        Lets an ``unknown_columns`` envelope say "OnHandQty is not on Part —
        it lives on PartWhse" instead of only "not here". SearchSvc entries
        rank first: they serve the same columns over plain filterable OData.
        """
        cur = self._conn.execute(
            "SELECT DISTINCT service_id, entity_set_name, field_type FROM fields "
            "WHERE field_name = ? COLLATE NOCASE",
            ((field_name or "").strip(),),
        )
        rows = self._dict_rows(cur)
        rows.sort(key=lambda r: (
            not r["service_id"].lower().endswith("searchsvc"),
            len(r["service_id"]),
            r["service_id"],
        ))
        return rows[:limit]

    def get_field_types(self, service_id: str, entity_set: str) -> dict[str, str]:
        """``{field_name: field_type}`` for one entity set (empty when unknown)."""
        return {
            r["field_name"]: r.get("field_type") or ""
            for r in self.get_fields(service_id, entity_set)
            if r.get("field_name")
        }

    def get_fields(self, service_id: str, entity_set: str) -> list[dict[str, Any]]:
        """Get fields for a specific entity set."""
        cur = self._conn.execute(
            "SELECT field_name, field_type, nullable, description "
            "FROM fields WHERE service_id = ? AND entity_set_name = ? "
            "ORDER BY field_name",
            (service_id, entity_set),
        )
        return self._dict_rows(cur)

    def get_department_services(self, department: str) -> set[str]:
        """Get all service_ids accessible by a department."""
        cur = self._conn.execute(
            "SELECT service_id FROM department_services WHERE department = ?",
            (department,),
        )
        return {row[0] for row in cur.fetchall()}

    def method_exists(self, service_id: str, method_name: str) -> bool:
        """Check if a specific method exists on a service."""
        cur = self._conn.execute(
            "SELECT 1 FROM methods WHERE service_id = ? AND method_name = ?",
            (service_id, method_name),
        )
        return cur.fetchone() is not None

    def is_read_only_method(self, method_name: str) -> bool:
        """Check if a method is read-only (Get* excluding GetNew*).

        This is a static check based on naming convention — does not require
        the method to exist in the index.
        """
        return method_name.startswith("Get") and not method_name.startswith("GetNew")

    def get_method_info(self, service_id: str, method_name: str) -> dict[str, Any] | None:
        """Get full info about a specific method.

        Returns None if the method is not found.
        """
        cur = self._conn.execute(
            "SELECT * FROM methods WHERE service_id = ? AND method_name = ?",
            (service_id, method_name),
        )
        rows = self._dict_rows(cur)
        return rows[0] if rows else None

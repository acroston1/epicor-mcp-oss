"""Read-only document retrieval with substring search and optional embeddings."""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from epicor_mcp.index.substring import substring_score

logger = logging.getLogger(__name__)


class DocsIndex:
    """Read documents without loading an embedding library in the default mode."""

    def __init__(self, db_path: str | Path, *, semantic_dir: str | Path | None = None,
                 semantic_model: str | None = None) -> None:
        self._db_path = Path(db_path)
        if not self._db_path.is_file():
            raise FileNotFoundError(f"Documentation index missing: {self._db_path}; run scripts/build_docs_index.py")
        self._conn = sqlite3.connect(self._db_path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._semantic = None
        self._lock = threading.RLock()
        self.last_search_mode = "substring"
        self.semantic_error = ""
        if semantic_dir is not None:
            try:
                from epicor_mcp.index.document_vectors import DocumentVectors
                self._semantic = DocumentVectors(Path(semantic_dir), self._conn, model=semantic_model)
            except Exception as exc:
                self.semantic_error = str(exc)
                logger.warning("Document vectors unavailable; using substring search: %s", exc)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
            if self._semantic is not None:
                self._semantic.close()
            self._semantic = None

    def __enter__(self) -> "DocsIndex":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def search(self, query: str, source_type: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Return casefold substring matches; semantic ranking is explicit opt-in."""
        with self._lock:
            return self._search(query, source_type, limit)

    def search_with_metadata(self, query: str, source_type: str = "", limit: int = 10):
        """Keep each request's mode/error paired with its results under concurrency."""
        with self._lock:
            rows = self._search(query, source_type, limit)
            return rows, self.last_search_mode, self.semantic_error

    def _search(self, query: str, source_type: str, limit: int) -> list[dict[str, Any]]:
        self.last_search_mode = "substring"
        if not query.strip() or limit <= 0:
            return []
        rows = [dict(row) for row in self._conn.execute(
            "SELECT id, source_type, source_file, title, section, content, page_start, url "
            "FROM doc_chunks WHERE (? = '' OR source_type = ?) ORDER BY id", (source_type, source_type))]
        if self._semantic is not None:
            try:
                results = self._semantic.search(query, rows, limit)
                self.last_search_mode = "semantic"
                self.semantic_error = ""
                return results
            except Exception as exc:
                self.semantic_error = str(exc)
                logger.warning("Document embedding query failed; using substring search: %s", exc)
        scored = []
        for row in rows:
            score = substring_score(query, row["title"], row["section"], row["content"])
            if score:
                scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [dict(row, score=score) for score, row in scored[:limit]]

    def stats(self) -> dict[str, int]:
        return dict(self._conn.execute("SELECT source_type, COUNT(*) FROM doc_chunks GROUP BY source_type"))

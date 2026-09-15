"""Read-only compatibility interface for an operator-provided hybrid help store.

The store lives in a directory (default ``data/help_store/``) containing
``help_index.db`` (SQLite: a ``chunks`` table plus an external-content
FTS5 mirror ``chunks_fts``) and ``help_index.faiss`` (a
``faiss.IndexFlatIP`` whose vector at position ``id - 1`` is the
embedding of ``chunks.id``). A ``meta(key, value)`` table may record the
``faiss_sha256`` digest that binds the database to its matching vector file.
Chunk IDs must be contiguous from 1 and match the number of vectors.

Provides two retrieval methods for compatibility callers
(keyword FTS5 and dense FAISS), plus Reciprocal Rank Fusion and
near-duplicate collapsing as static helpers.

This module never creates or changes a store. For new installations, use
``scripts/build_docs_index.py`` and optional ``scripts/build_document_vectors.py``
as documented in README.md; they produce the separate ``DocsIndex`` format.

Usage:
    from epicor_mcp.index.help_store import HelpStore, HelpStoreError
    store = HelpStore(Path("data/help_store"))
    rows = store.fts_search("purchase order receipt")
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

import faiss
import numpy as np

logger = logging.getLogger(__name__)

DB_NAME = "help_index.db"
FAISS_NAME = "help_index.faiss"

# Characters kept in FTS5 query terms — everything else becomes a space.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")

# Common English filler dropped from keyword queries.  "epicor"/"kinetic"
# are included because they appear in nearly every chunk's breadcrumb and
# only dilute the OR-query.
_STOPWORDS = frozenset(
    """
    a about after all also am an and any are as at be been being but by can
    cannot could did do does doing done epicor for from get gets getting had
    has have having he her here his how i if in into is it its just kinetic
    know like make makes me my need needs no not of on one or other our out
    over please she should so some someone than that the their them then
    there these they this those to under up us use used using want wants was
    way we were what when where which while who why will with without would
    you your
    """.split()
)

_SHINGLE_N = 5  # word n-gram size for near-duplicate detection


class HelpStoreError(RuntimeError):
    """Raised when the help store is missing, unreadable, or misaligned.

    The server treats this as "no store" and falls back to the legacy
    ``DocsIndex`` path.
    """


def _sanitize_fts_query(query: str) -> str:
    """Reduce *query* to a safe FTS5 OR-query.

    Strips everything outside ``[A-Za-z0-9 ]``, drops stopwords and
    duplicates, quotes each surviving term, and joins with ``OR``.
    Returns ``""`` when nothing survives.
    """
    words = _NON_ALNUM_RE.sub(" ", query or "").split()
    seen: set[str] = set()
    terms: list[str] = []
    for word in words:
        low = word.lower()
        if low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        terms.append(f'"{word}"')
    return " OR ".join(terms)


def _sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's bytes (streamed)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _shingles(content: str) -> frozenset[str]:
    """Word ``_SHINGLE_N``-gram shingle set of *content* (lowercased).

    Falls back to the plain word set for texts shorter than one shingle.
    """
    words = _NON_ALNUM_RE.sub(" ", (content or "").lower()).split()
    if len(words) < _SHINGLE_N:
        return frozenset(words)
    return frozenset(
        " ".join(words[i : i + _SHINGLE_N])
        for i in range(len(words) - _SHINGLE_N + 1)
    )


class HelpStore:
    """Read-only hybrid (FTS5 + FAISS) index over the Epicor help corpus."""

    def __init__(self, store_dir: Path) -> None:
        """Open the store at *store_dir* and validate alignment.

        Raises ``HelpStoreError`` if either file is missing, cannot be
        opened, or the FAISS vector count does not match the SQLite row
        count / max id (positional alignment is the store's core
        invariant — a misaligned store must never serve results).
        """
        store_dir = Path(store_dir)
        db_path = store_dir / DB_NAME
        faiss_path = store_dir / FAISS_NAME
        if not db_path.exists() or not faiss_path.exists():
            raise HelpStoreError(
                f"Help store files not found in {store_dir} "
                f"(need {DB_NAME} and {FAISS_NAME}); "
                "the legacy help store is not built by this distribution; use scripts/build_docs_index.py instead"
            )

        try:
            # Read-only URI open shared across worker threads.  SQLite's
            # serialized mode is NOT enough: Python cursors on one shared
            # connection interleave under true concurrency (InterfaceError
            # / silently empty fetches), so every SQLite query below is
            # additionally guarded by ``self._db_lock``.
            self._db_lock = threading.Lock()
            self._conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            rowcount = self._conn.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0]
            max_id = self._conn.execute(
                "SELECT MAX(id) FROM chunks"
            ).fetchone()[0]
            meta_row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'faiss_sha256'"
            ).fetchone()
            expected_faiss_sha = meta_row[0] if meta_row else ""
        except sqlite3.Error as exc:
            raise HelpStoreError(f"Cannot open help store DB {db_path}: {exc}") from exc

        # The optional digest binds this database to its exact vector file.
        # Replacing the files separately can leave an inconsistent pair with
        # equal counts; the hash detects that case. Older stores may omit it.
        if expected_faiss_sha:
            try:
                actual_faiss_sha = _sha256_file(faiss_path)
            except OSError as exc:
                self._conn.close()
                raise HelpStoreError(
                    f"Cannot read FAISS index {faiss_path}: {exc}"
                ) from exc
            if actual_faiss_sha != expected_faiss_sha:
                self._conn.close()
                raise HelpStoreError(
                    f"Help store torn: {FAISS_NAME} hash "
                    f"{actual_faiss_sha[:12]}… does not match the hash "
                    f"recorded at build time ({expected_faiss_sha[:12]}…) — "
                    "the DB and FAISS files are from different builds; "
                    "rebuild required"
                )

        try:
            self._faiss = faiss.read_index(str(faiss_path))
        except Exception as exc:
            self._conn.close()
            raise HelpStoreError(
                f"Cannot open FAISS index {faiss_path}: {exc}"
            ) from exc

        if not (self._faiss.ntotal == rowcount == (max_id or 0)) or rowcount == 0:
            self._conn.close()
            raise HelpStoreError(
                f"Help store misaligned: faiss ntotal={self._faiss.ntotal}, "
                f"rows={rowcount}, max id={max_id} — rebuild required"
            )

        self._size = rowcount

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._db_lock:
            self._conn.close()

    @property
    def size(self) -> int:
        """Number of chunks in the store."""
        return self._size

    # ------------------------------------------------------------------
    # Row helpers
    # ------------------------------------------------------------------

    def _rows_by_id(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch full chunk rows for *ids*, keyed by id."""
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._db_lock:
            rows = self._conn.execute(
                f"SELECT * FROM chunks WHERE id IN ({placeholders})", ids
            ).fetchall()
        return {row["id"]: dict(row) for row in rows}

    # ------------------------------------------------------------------
    # Retrieval legs
    # ------------------------------------------------------------------

    def fts_search(self, query: str, k: int = 50) -> list[dict[str, Any]]:
        """Keyword leg: BM25-ranked FTS5 search.

        The query is sanitized internally (alphanumeric words only,
        stopwords dropped, OR-joined) so raw natural-language questions
        are safe.  Returns up to *k* full chunk rows, each with an added
        1-based ``fts_rank``.  Returns ``[]`` for queries that sanitize
        to nothing.
        """
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []
        try:
            with self._db_lock:
                rows = self._conn.execute(
                    """
                    SELECT c.*, bm25(chunks_fts) AS _bm25
                    FROM chunks_fts
                    JOIN chunks c ON c.id = chunks_fts.rowid
                    WHERE chunks_fts MATCH ?
                    ORDER BY bm25(chunks_fts)
                    LIMIT ?
                    """,
                    (fts_query, k),
                ).fetchall()
        except sqlite3.OperationalError:
            logger.warning("FTS5 query failed for %r", fts_query, exc_info=True)
            return []

        results: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, 1):
            item = dict(row)
            item.pop("_bm25", None)
            item["fts_rank"] = rank
            results.append(item)
        return results

    def dense_search(self, qvec: np.ndarray, k: int = 50) -> list[dict[str, Any]]:
        """Dense leg: FAISS inner-product search with a normalized query vector.

        *qvec* is a 1-D (or ``(1, dim)``) L2-normalized fp32 vector from
        ``EmbedClient``.  Returns up to *k* full chunk rows in similarity
        order, each with an added 1-based ``dense_rank`` and the raw
        inner-product ``score``.
        """
        q = np.ascontiguousarray(
            np.asarray(qvec, dtype=np.float32).reshape(1, -1)
        )
        scores, positions = self._faiss.search(q, min(k, self._size))

        hits = [
            (int(pos) + 1, float(score))  # FAISS position -> chunks.id
            for pos, score in zip(positions[0], scores[0])
            if pos >= 0
        ]
        rows = self._rows_by_id([chunk_id for chunk_id, _ in hits])

        results: list[dict[str, Any]] = []
        for rank, (chunk_id, score) in enumerate(hits, 1):
            row = rows.get(chunk_id)
            if row is None:  # cannot happen on a validated store
                logger.warning("FAISS hit id %d missing from chunks table", chunk_id)
                continue
            item = dict(row)
            item["dense_rank"] = rank
            item["score"] = score
            results.append(item)
        return results

    # ------------------------------------------------------------------
    # Fusion helpers (pure, static)
    # ------------------------------------------------------------------

    @staticmethod
    def rrf_fuse(
        *ranklists: list[dict[str, Any]],
        k: int = 60,
        id_key: str = "id",
    ) -> list[dict[str, Any]]:
        """Fuse rank lists with standard Reciprocal Rank Fusion.

        Each item contributes ``1 / (k + rank)`` per list it appears in
        (rank is its 1-based position in that list).  Items are deduped
        by *id_key*; fields from later duplicates are merged in without
        overwriting.  Returns new dicts in fused order, each with an
        added ``rrf`` score.
        """
        fused: dict[Any, dict[str, Any]] = {}
        scores: dict[Any, float] = {}
        order: list[Any] = []
        for ranklist in ranklists:
            for rank, row in enumerate(ranklist, 1):
                key = row.get(id_key)
                if key not in fused:
                    fused[key] = dict(row)
                    scores[key] = 0.0
                    order.append(key)
                else:
                    for field, value in row.items():
                        fused[key].setdefault(field, value)
                scores[key] += 1.0 / (k + rank)

        # Stable sort: ties keep first-seen order.
        order.sort(key=lambda key: -scores[key])
        results = []
        for key in order:
            row = fused[key]
            row["rrf"] = scores[key]
            results.append(row)
        return results

    @staticmethod
    def collapse_near_dupes(
        rows: list[dict[str, Any]],
        threshold: float = 0.92,
    ) -> list[dict[str, Any]]:
        """Drop rows whose content near-duplicates an earlier row.

        Similarity is Jaccard over word 5-gram shingles of ``content``;
        a row is dropped when it matches any kept row at or above
        *threshold*.  First occurrence wins (input order preserved).
        """
        kept: list[dict[str, Any]] = []
        kept_shingles: list[frozenset[str]] = []
        for row in rows:
            shingles = _shingles(row.get("content", ""))
            duplicate = False
            for other in kept_shingles:
                if not shingles and not other:
                    duplicate = True
                    break
                union = len(shingles | other)
                if union and len(shingles & other) / union >= threshold:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(row)
                kept_shingles.append(shingles)
        return kept

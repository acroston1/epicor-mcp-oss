"""Compatibility reader for an operator-provided FAISS document store.

The directory contains ``index.faiss`` and ``data.json``. The JSON object's
``chunks`` and ``metadata`` arrays correspond positionally to the FAISS vectors.
Select a ``model_name`` matching the stored embeddings; ``reranker_model`` is
optional. Search returns document rows with scores and source metadata.

New installations should use ``scripts/build_docs_index.py`` and, optionally,
``scripts/build_document_vectors.py`` as documented in README.md. Those scripts
produce the separate ``DocsIndex``/``DocumentVectors`` format.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Set by an ``atexit`` hook so a pre-warm thread that has not yet entered torch
#: bails instead of starting a multi-second model load into a dying interpreter.
#: See :meth:`VectorIndex.prewarm` for why that combination aborts the process.
_SHUTTING_DOWN = threading.Event()
atexit.register(_SHUTTING_DOWN.set)

# Stored metadata type -> MCP source type mapping
_TYPE_MAP: dict[str, str] = {
    "official_docs": "pdf",
    "forum_cache": "forum",
    "video": "video",
    "data_dictionary": "data_dictionary",
    "rest_api_docs": "rest_api",
}

_REVERSE_TYPE_MAP: dict[str, str] = {v: k for k, v in _TYPE_MAP.items()}

# Chunks shorter than this (after stripping) are too thin to be useful — most
# are forum "Topic: ..." stubs that contain no useful answer text.
_MIN_USEFUL_CHUNK_CHARS = 80
_TOPIC_STUB_RE = re.compile(r"^\s*Topic:\s*.{0,200}\s*$", re.IGNORECASE | re.DOTALL)


def _is_low_value_chunk(content: str) -> bool:
    """Return True for chunks with insufficient answer text."""
    if not content:
        return True
    stripped = content.strip()
    if len(stripped) < _MIN_USEFUL_CHUNK_CHARS:
        return True
    if _TOPIC_STUB_RE.match(stripped):
        return True
    return False


def _bias_query_toward_kinetic(query: str) -> str:
    """Append ' Epicor Kinetic' unless the query already mentions Kinetic/Classic.

    This compatibility path biases retrieval toward Kinetic documentation.
    """
    lowered = query.lower()
    if "kinetic" in lowered or "classic" in lowered:
        return query
    return f"{query} Epicor Kinetic"


def _calculate_version_boost(version_score: float, current_version: float = 2025.2) -> float:
    """Recency boost for official PDF docs based on Epicor version."""
    if version_score is None or version_score == 0:
        return 1.0
    version_diff = current_version - version_score
    if version_diff <= 0:
        return 2.5
    elif version_diff <= 0.1:
        return 2.0
    elif version_diff <= 0.2:
        return 1.5
    elif version_diff <= 1.0:
        return 1.2
    return 1.0


def _calculate_video_boost(upload_date: str) -> float:
    """Recency boost for video transcripts based on upload year."""
    if not upload_date or len(upload_date) < 4:
        return 1.0
    try:
        upload_year = int(upload_date[:4])
    except ValueError:
        return 1.0
    age_years = 2025 - upload_year
    if age_years <= 1:
        return 2.0
    elif age_years <= 2:
        return 1.5
    elif age_years <= 3:
        return 1.2
    elif age_years <= 5:
        return 0.8
    elif age_years <= 7:
        return 0.3
    return 0.1


class VectorIndex:
    """Read-only semantic search over a FAISS vector store.

    Parameters
    ----------
    embeddings_dir:
        Directory containing ``index.faiss`` and ``data.json``.
    model_name:
        Sentence-transformers model used to encode queries.
    """

    def __init__(
        self,
        embeddings_dir: Path,
        model_name: str = "BAAI/bge-large-en-v1.5",
        reranker_model: str | None = "BAAI/bge-reranker-base",
    ) -> None:
        self._embeddings_dir = Path(embeddings_dir)
        self._model_name = model_name
        self._reranker_model_name = reranker_model
        self._index: Any = None  # faiss.Index
        self._chunks: list[str] = []
        self._metadata: list[dict] = []
        self._model: Any = None  # SentenceTransformer
        self._reranker: Any = None  # CrossEncoder
        self._model_lock = threading.Lock()
        self._reranker_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Read the FAISS index and chunk data from disk."""
        import faiss

        index_path = self._embeddings_dir / "index.faiss"
        data_path = self._embeddings_dir / "data.json"

        self._index = faiss.read_index(str(index_path))

        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self._chunks = data["chunks"]
        self._metadata = data["metadata"]

        logger.info(
            "VectorIndex loaded: %d vectors, %d chunks, %d metadata entries",
            self._index.ntotal,
            len(self._chunks),
            len(self._metadata),
        )

    def _ensure_model(self) -> Any:
        """Lazy-load the embedding model on first use."""
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            from sentence_transformers import SentenceTransformer

            logger.info("Loading embedding model %s on CPU...", self._model_name)
            self._model = SentenceTransformer(self._model_name, device="cpu")
            logger.info("Embedding model loaded")
            return self._model

    def _ensure_reranker(self) -> Any | None:
        """Lazy-load the cross-encoder reranker. Returns None if disabled or unavailable."""
        if self._reranker_model_name is None:
            return None
        if self._reranker is not None:
            return self._reranker
        with self._reranker_lock:
            if self._reranker is not None:
                return self._reranker
            try:
                from sentence_transformers import CrossEncoder

                logger.info("Loading reranker %s on CPU...", self._reranker_model_name)
                self._reranker = CrossEncoder(self._reranker_model_name, device="cpu")
                logger.info("Reranker loaded")
                return self._reranker
            except Exception:
                logger.exception("Failed to load reranker; continuing without rerank")
                self._reranker_model_name = None  # don't retry
                return None

    def prewarm(self) -> None:
        """Eagerly load the embedding model (and reranker) and run a dummy
        encode so the first user query doesn't pay the ~1 second cold-start.

        Safe to call from a background thread.

        Starting a load the process will not live long enough to finish is worse
        than skipping it: this runs in a DAEMON thread, so a short-lived process
        exits mid-load, torch's own pool is already finalized, and the
        `cannot schedule new futures after interpreter shutdown` that follows is
        raised in a daemon thread during finalization — which the C++ runtime
        turns into `terminate called without an active exception`, i.e. SIGABRT
        and a core dump, on an otherwise successful run. `_SHUTTING_DOWN` closes
        the common window (the thread scheduled just before exit). It cannot
        interrupt a load already inside torch — the real defence is not starting
        one in a process that is about to exit, which is why the e2e gate turns
        vector search off outright.
        """
        if _SHUTTING_DOWN.is_set():
            logger.debug("interpreter is shutting down; skipping pre-warm")
            return
        try:
            model = self._ensure_model()
            # A trivial encode forces the model graph / tokeniser caches.
            model.encode(["warmup"], show_progress_bar=False)
            logger.info("Embedding model pre-warmed")
        except Exception:
            logger.exception("Embedding model pre-warm failed")

        # Reranker is optional and ~5x larger than the encoder on first load.
        # Pre-warming it makes the first re-rank call snappy too.
        try:
            reranker = self._ensure_reranker()
            if reranker is not None:
                reranker.predict([("warmup query", "warmup chunk")], show_progress_bar=False)
                logger.info("Reranker pre-warmed")
        except Exception:
            logger.exception("Reranker pre-warm failed")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        source_type: str = "",
        limit: int = 10,
        *,
        overfetch: int = 80,
        max_per_source: dict[str, int] | None = None,
        reserved_slots: dict[str, int] | None = None,
        use_rerank: bool = True,
    ) -> list[dict]:
        """Semantic search with Kinetic bias, low-value filtering, recency
        boosting, optional cross-encoder reranking, and source diversification.

        Parameters
        ----------
        query:
            Natural-language search query. Will be biased toward Kinetic
            content automatically unless it already mentions kinetic/classic.
        source_type:
            MCP source type filter (``"pdf"``, ``"forum"``, ``"video"``,
            ``"data_dictionary"``, ``"rest_api"``, or ``""`` for all).
        limit:
            Maximum results to return.
        overfetch:
            How many FAISS candidates to pull before filtering/boosting/
            reranking. Larger = better quality, slower.
        max_per_source:
            Per-source caps in the final result set (e.g.
            ``{"forum": 2}`` keeps forum noise from drowning out PDFs).
            Defaults to ``{"forum": 2}`` when ``source_type`` is unfiltered.
        reserved_slots:
            Minimum slots to reserve for a source if any candidates exist
            (e.g. ``{"pdf": 2}``). Defaults to ``{"pdf": 2}`` when
            ``source_type`` is unfiltered.
        use_rerank:
            Apply cross-encoder reranking to the candidate pool. Falls back
            to boosted-score ordering if the reranker is unavailable.

        Returns
        -------
        list[dict]
            Results matching the ``DocsIndex.search()`` schema plus a
            ``score`` field (the final relevance score Claude can read).
        """
        import faiss

        if self._index is None:
            raise RuntimeError("VectorIndex not loaded — call load() first")

        # Bias the embedding query toward Kinetic content. This dramatically
        # improves retrieval for short natural-language how-to questions.
        biased_query = _bias_query_toward_kinetic(query)

        model = self._ensure_model()
        query_embedding = model.encode([biased_query]).astype("float32")
        faiss.normalize_L2(query_embedding)

        # Over-fetch so filtering, diversification, and reranking have real
        # material to work with — the old 3x over-fetch was too narrow.
        search_k = min(max(overfetch, limit * 3), self._index.ntotal)
        distances, indices = self._index.search(query_embedding, search_k)

        source_filter = _REVERSE_TYPE_MAP.get(source_type, "") if source_type else ""

        # Default diversification when no explicit source filter is set
        if not source_type:
            if max_per_source is None:
                max_per_source = {"forum": 2}
            if reserved_slots is None:
                reserved_slots = {"pdf": 2}
        else:
            max_per_source = max_per_source or {}
            reserved_slots = reserved_slots or {}

        candidates: list[tuple[dict, float]] = []
        for idx, dist in zip(indices[0], distances[0]):
            if idx < 0 or idx >= len(self._chunks):
                continue

            content = self._chunks[idx]
            if _is_low_value_chunk(content):
                continue  # drop "Topic: ..." stubs and tiny chunks

            meta = self._metadata[idx] if idx < len(self._metadata) else {}
            doc_type = meta.get("type", "")

            if source_filter and doc_type != source_filter:
                continue

            base_score = float(dist)
            boosted_score = base_score

            if doc_type == "official_docs":
                if meta.get("source_folder") == "EUP":
                    boosted_score = base_score * 3.5
                else:
                    version_score = meta.get("version_score", 0)
                    boosted_score = base_score * _calculate_version_boost(version_score)
            elif doc_type == "video":
                upload_date = meta.get("upload_date", "")
                boosted_score = base_score * _calculate_video_boost(upload_date)
            elif doc_type == "rest_api_docs":
                chunk_type = meta.get("chunk_type", "")
                if chunk_type == "method_details":
                    boosted_score = base_score * 2.5
                else:
                    boosted_score = base_score * 1.8

            mapped_type = _TYPE_MAP.get(doc_type, doc_type)
            result = {
                "source_type": mapped_type,
                "source_file": meta.get("source", ""),
                "title": meta.get("title") or meta.get("doc_name", ""),
                "section": "",
                "content": content,
                "page_start": None,
                "url": meta.get("url", ""),
            }
            candidates.append((result, boosted_score))

        if not candidates:
            return []

        # Cross-encoder rerank: scores (query, chunk) pairs directly.
        # Far more accurate than cosine sim for distinguishing useful chunks.
        if use_rerank:
            reranker = self._ensure_reranker()
            if reranker is not None:
                pairs = [(query, c[0]["content"]) for c in candidates]
                try:
                    rerank_scores = reranker.predict(
                        pairs, show_progress_bar=False, batch_size=32
                    )
                    # Combine reranker score (primary) with the per-source
                    # boost (secondary) so authoritative sources still get a
                    # nudge among similarly-ranked content.
                    candidates = [
                        (r, float(rs) + 0.05 * b)
                        for (r, b), rs in zip(candidates, rerank_scores)
                    ]
                except Exception:
                    logger.exception("Reranker failed; falling back to boosted score")

        candidates.sort(key=lambda x: x[1], reverse=True)

        # Diversify: enforce per-source caps + reserved slots so no single
        # source type can monopolise the result set.
        return self._diversify(
            candidates,
            limit=limit,
            max_per_source=max_per_source,
            reserved_slots=reserved_slots,
        )

    @staticmethod
    def _diversify(
        candidates: list[tuple[dict, float]],
        *,
        limit: int,
        max_per_source: dict[str, int],
        reserved_slots: dict[str, int],
    ) -> list[dict]:
        """Greedy pick with per-source caps and reserved slots.

        Pass 1: fill reserved slots for each source (best-scoring chunks of
        that source go in first, regardless of how they'd compete globally).
        Pass 2: fill remaining slots greedily by score, honouring caps.
        """
        chosen: list[dict] = []
        counts: dict[str, int] = {}

        # Pass 1: reserved slots
        for src, n_reserved in reserved_slots.items():
            taken = 0
            for r, _score in candidates:
                if taken >= n_reserved or len(chosen) >= limit:
                    break
                if r["source_type"] != src:
                    continue
                if r in chosen:
                    continue
                chosen.append(r)
                counts[src] = counts.get(src, 0) + 1
                taken += 1

        # Pass 2: fill remaining slots greedily by score
        for r, _score in candidates:
            if len(chosen) >= limit:
                break
            if r in chosen:
                continue
            src = r["source_type"]
            cap = max_per_source.get(src)
            if cap is not None and counts.get(src, 0) >= cap:
                continue
            chosen.append(r)
            counts[src] = counts.get(src, 0) + 1

        # Attach score back onto results (use latest sorted score per result)
        score_lookup = {id(r): s for r, s in candidates}
        for r in chosen:
            r["score"] = round(score_lookup.get(id(r), 0.0), 4)

        return chosen

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def total_vectors(self) -> int:
        """Number of vectors in the FAISS index."""
        return self._index.ntotal if self._index is not None else 0

    def stats(self) -> dict[str, int]:
        """Return chunk counts by mapped MCP source type."""
        counts = Counter(
            _TYPE_MAP.get(m.get("type", ""), m.get("type", "unknown"))
            for m in self._metadata
        )
        return dict(counts)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Re-read the FAISS index and data from disk (atomic swap)."""
        import faiss

        index_path = self._embeddings_dir / "index.faiss"
        data_path = self._embeddings_dir / "data.json"

        new_index = faiss.read_index(str(index_path))
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Atomic swap
        self._index = new_index
        self._chunks = data["chunks"]
        self._metadata = data["metadata"]

        logger.info(
            "VectorIndex reloaded: %d vectors, %d chunks",
            self._index.ntotal,
            len(self._chunks),
        )

    def close(self) -> None:
        """Release resources."""
        self._index = None
        self._chunks = []
        self._metadata = []
        self._model = None


async def watch_for_reload(
    vector_index: VectorIndex,
    signal_path: Path,
    interval: float = 30.0,
) -> None:
    """Poll ``reload.signal`` and trigger a VectorIndex reload when it changes.

    Runs as an asyncio background task.  The heavy I/O (FAISS read + JSON
    parse) is dispatched to a thread so the event loop stays responsive.
    """
    last_mtime: float | None = None
    while True:
        try:
            await asyncio.sleep(interval)
            if signal_path.exists():
                mtime = signal_path.stat().st_mtime
                if last_mtime is None:
                    last_mtime = mtime
                elif mtime != last_mtime:
                    logger.info("reload.signal changed — reloading vector index")
                    await asyncio.to_thread(vector_index.reload)
                    last_mtime = mtime
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in vector index reload watcher")

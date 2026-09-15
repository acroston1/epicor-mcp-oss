"""Tool: epicor_help

Hybrid retrieval over the Epicor help corpus: official Epicor Kinetic PDF
guides, Administrator-supplied procedures, the epiusers.help forum archive, and
training video transcripts.  Dense (FAISS via a local embedding server) and
keyword (FTS5) legs are fused with Reciprocal Rank Fusion, reranked with a
cross-encoder, and augmented with a *live* epiusers.help search.

Falls back to the legacy ``DocsIndex`` FTS5 path when the help store is
missing or invalid, and to keyword-only results when the embedding server
is down.  An empty result set is an answer, never an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from epicor_mcp.config import get_settings
from epicor_mcp.context import get_current_session

if TYPE_CHECKING:
    import numpy as np
    from mcp.server.fastmcp import FastMCP as Server

    from epicor_mcp.index.docs_index import DocsIndex
    from epicor_mcp.index.embed_client import EmbedClient
    from epicor_mcp.index.forum_live import LiveForumSearch
    from epicor_mcp.index.help_store import HelpStore

logger = logging.getLogger(__name__)

# Valid source filters for the help store.
_VALID_SOURCES = {"", "pdf", "eup", "forum", "video"}
# Old filter names accepted for backward compatibility.
_SOURCE_ALIASES = {"documentation": "pdf"}
# Source types the legacy DocsIndex understands.
_LEGACY_SOURCES = {"", "pdf", "forum", "video"}

# Retrieval tuning knobs.
_LEG_K = 50          # per-leg candidates on the first pass
_BACKFILL_K = 200    # per-leg candidates on the single source-filter refetch
_RERANK_POOL = 50    # fused rows handed to the cross-encoder
_RERANK_DOC_CHARS = 1500
_RERANK_TIMEOUT_S = 30.0  # covers first-call model load; inference is ~ms
_EUP_BONUS = 0.25            # Administrator-supplied procedures outrank generic docs
_RECENT_VERSION_BONUS = 0.15  # doc_version >= '2025'

# Standard notes (exact wording is part of the tool contract).
_NOTE_NO_SEMANTIC = "semantic search unavailable — keyword results only"
_NOTE_NO_FORUM = "live forum unavailable"
_NOTE_NO_MATCHES = "no matches — try different terms"

# ---------------------------------------------------------------------------
# Lazy cross-encoder reranker singleton
# ---------------------------------------------------------------------------

_reranker: Any | None = None
_reranker_failed = False
_reranker_lock = threading.Lock()
_reranker_last_used: float = 0.0
_RERANKER_UNLOAD_CHECK_S = 60.0


def _get_reranker(model_name: str) -> Any | None:
    """Return the shared ``CrossEncoder`` instance, constructing it lazily.

    Returns ``None`` (permanently, with a one-time warning) if construction
    fails — e.g. sentence-transformers not installed or the model missing.
    The instance is dropped again by :func:`_unload_reranker_loop` after
    idling, so the GPU holds no reranker weights between bursts of use;
    reloading costs ~2s (weights come from the OS page cache).
    """
    global _reranker, _reranker_failed, _reranker_last_used
    _reranker_last_used = time.monotonic()
    if _reranker is not None:
        return _reranker
    if _reranker_failed:
        return None
    with _reranker_lock:
        if _reranker is not None or _reranker_failed:
            return _reranker
        try:
            from sentence_transformers import CrossEncoder

            device: str | None = None
            try:
                import torch

                if torch.cuda.is_available():
                    device = "cuda:1" if torch.cuda.device_count() > 1 else "cuda:0"
            except Exception:
                device = None

            _reranker = CrossEncoder(model_name, device=device)
            logger.info("Loaded reranker %s on %s", model_name, device or "cpu")
        except Exception:
            _reranker_failed = True
            logger.warning(
                "Reranker %s unavailable — epicor_help keeps RRF order",
                model_name,
                exc_info=True,
            )
    return _reranker


def _unload_reranker_if_idle(unload_after: float) -> None:
    """Drop the reranker and free its GPU memory after *unload_after* idle seconds.

    Safe against in-flight use: ``_rerank_sync`` holds its own strong
    reference for the duration of ``predict``, so dropping the module
    global never yanks weights mid-inference — at worst the VRAM is
    actually released on the next idle check after that call finishes.
    """
    global _reranker
    with _reranker_lock:
        if _reranker is None:
            return
        if time.monotonic() - _reranker_last_used < unload_after:
            return
        _reranker = None
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        logger.info(
            "Reranker unloaded after %.0fs idle (GPU VRAM released)", unload_after
        )


_unload_task: Any | None = None


def _ensure_unload_task(unload_after: float) -> None:
    """Start the idle-unload timer lazily (first tool call has a loop)."""
    global _unload_task
    if unload_after <= 0:
        return
    if _unload_task is not None and not _unload_task.done():
        return

    async def _loop() -> None:
        while True:
            await asyncio.sleep(_RERANKER_UNLOAD_CHECK_S)
            _unload_reranker_if_idle(unload_after)

    try:
        _unload_task = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:  # no running loop (sync/test context)
        pass


def _rerank_sync(query: str, rows: list[dict], model_name: str) -> list[float] | None:
    """Score *rows* against *query* with the cross-encoder.

    Runs in a worker thread (model inference is blocking).  Returns ``None``
    when the reranker is unavailable.
    """
    reranker = _get_reranker(model_name)
    if reranker is None:
        return None
    pairs = [
        (
            query,
            f"{row.get('breadcrumb', '')}\n{row.get('content', '')}"[:_RERANK_DOC_CHARS],
        )
        for row in rows
    ]
    scores = reranker.predict(pairs)
    return [float(s) for s in scores]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_pages(page_start: Any, page_end: Any) -> str | None:
    """Format a page range as ``"12-14"``, ``"12"``, or ``None``."""
    if not page_start:
        return None
    if page_end and page_end != page_start:
        return f"{page_start}-{page_end}"
    return str(page_start)


def _format_result(rank: int, row: dict) -> dict:
    """Map a help-store chunk row to the response schema."""
    return {
        "rank": rank,
        "source_type": row.get("source_type", ""),
        "title": row.get("title", ""),
        "section": row.get("section_path", ""),
        "pages": _format_pages(row.get("page_start"), row.get("page_end")),
        "version": row.get("doc_version", ""),
        "url": row.get("url", ""),
        "text": row.get("content", ""),
    }


def _format_legacy_result(rank: int, row: dict) -> dict:
    """Map a legacy ``DocsIndex`` row to the response schema."""
    return {
        "rank": rank,
        "source_type": row.get("source_type", ""),
        "title": row.get("title", ""),
        "section": row.get("section", ""),
        "pages": _format_pages(row.get("page_start"), None),
        "version": "",
        "url": row.get("url", ""),
        "text": row.get("content", ""),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(
    server: "Server",
    docs_index: "DocsIndex | None" = None,
    vector_index: Any | None = None,
    *,
    help_store: "HelpStore | None" = None,
    embed_client: "EmbedClient | None" = None,
    forum_search: "LiveForumSearch | None" = None,
) -> None:
    """Bind the ``epicor_help`` tool to *server*.

    Parameters
    ----------
    server:
        The MCP server instance.
    docs_index:
        Legacy FTS5 documentation index — used only as a fallback when
        *help_store* is unavailable.
    vector_index:
        Deprecated legacy semantic index.  Accepted for backward
        compatibility with older call sites; ignored.
    help_store:
        Hybrid FTS5 + FAISS help store (primary backend).
    embed_client:
        Client for the local embedding server (dense leg).  Optional —
        without it the store runs keyword-only.
    forum_search:
        Live epiusers.help search client.  Optional.
    """
    if vector_index is not None:
        logger.info("epicor_help: legacy vector_index passed — ignored (superseded by help_store)")

    settings = get_settings()

    def _fused_local(qvec: "np.ndarray | None", query: str, k: int) -> list[dict]:
        """Run the dense + keyword legs against the help store and RRF-fuse."""
        legs: list[list[dict]] = []
        if qvec is not None:
            try:
                legs.append(help_store.dense_search(qvec, k))
            except Exception:
                logger.exception("Dense search failed — continuing keyword-only")
        legs.append(help_store.fts_search(query, k))
        return help_store.rrf_fuse(*legs)

    async def _local_leg(query: str, source: str, limit: int) -> tuple[bool, list[dict]]:
        """Hybrid store leg.  Returns ``(semantic_ok, fused_rows)``.

        The FAISS scan + SQLite FTS query are synchronous CPU/IO work
        (~10-25 ms today, growing with the store), so they run in a worker
        thread to keep the event loop free.  ``HelpStore`` is thread-safe
        for reads (``check_same_thread=False`` read-only connection; FAISS
        searches are re-entrant).
        """
        qvec = None
        if embed_client is not None:
            qvec = await embed_client.embed_query(query)
        rows = await asyncio.to_thread(_fused_local, qvec, query, _LEG_K)
        if source:
            # Filter AFTER fusion; one backfill refetch if too few survive.
            filtered = [r for r in rows if r.get("source_type") == source]
            if len(filtered) < limit:
                rows = await asyncio.to_thread(_fused_local, qvec, query, _BACKFILL_K)
                filtered = [r for r in rows if r.get("source_type") == source]
            rows = filtered
        return qvec is not None, rows

    async def _legacy_leg(query: str, source: str, limit: int) -> tuple[bool, list[dict]]:
        """Legacy ``DocsIndex`` FTS5 leg for when the help store is missing."""
        fts_source = source if source in _LEGACY_SOURCES else ""
        rows = docs_index.search(query=query, source_type=fts_source, limit=limit)
        return False, rows

    @server.tool(structured_output=False)
    async def epicor_help(
        query: str,
        source: str = "",
        limit: int = 5,
    ) -> str:
        """Authoritative Epicor knowledge — CALL THIS BEFORE answering ANY
        Epicor how-to / what-is / where-is / why question.

        Your own training data about Epicor Kinetic is NOT reliable: it is
        version-stale, generic, frequently describes an OLDER UI or a DIFFERENT
        ERP entirely, and knows nothing about your installation's specific configuration,
        menus, or site procedures. Do NOT answer an Epicor factual or procedural
        question from memory — that is the single most common way to give a
        confidently wrong answer here. Instead:

        1. Call this tool FIRST with the user's question.
        2. Ground your answer ONLY in the returned excerpts, and cite them
           (title, section, pages, version, URL).
        3. If it returns nothing useful, say you don't know and suggest
           rephrasing — never backfill the gap from training data.

        Covers four sources, every result carrying a citation:

        - Official Epicor Kinetic user guides (``source="pdf"``)
        - Administrator-supplied procedures — site-specific step-by-step
          instructions that take precedence over generic docs (``source="eup"``)
        - epiusers.help community forum, archive PLUS a live search of
          current posts returned in ``live_forum`` (``source="forum"``)
        - Training video transcripts (``source="video"``)

        Pass the user's question verbatim; do not pre-keyword it.  Retrieval
        is semantic and works best on natural, full questions.  Synthesize the
        returned excerpts — do not paste raw results.

        Parameters
        ----------
        query : str
            The user's question, verbatim.
        source : str, optional
            Restrict to one source: ``"pdf"``, ``"eup"``, ``"forum"``,
            ``"video"``.  Empty (default) searches everything.
        limit : int, optional
            Results to return, 1-10 (default 5).
        """
        try:
            # Verify user is authenticated (no RBAC check beyond this).
            get_current_session()

            # Keep the GPUs clear between bursts of help usage.
            _ensure_unload_task(
                float(getattr(settings, "reranker_unload_after_s", 0) or 0)
            )

            source = _SOURCE_ALIASES.get(source.strip().lower(), source.strip().lower())
            if source not in _VALID_SOURCES:
                return json.dumps({
                    "error": (
                        f"Invalid source filter '{source}'. "
                        "Must be one of: pdf, eup, forum, video, or empty for all."
                    )
                })
            limit = max(1, min(int(limit), 10))

            notes: list[str] = []

            # ----------------------------------------------------------
            # Fan out: local retrieval + live forum, concurrently.
            # ----------------------------------------------------------
            want_forum = forum_search is not None and source in ("", "forum")

            if help_store is not None:
                local_coro = _local_leg(query, source, limit)
            elif docs_index is not None:
                notes.append("help store unavailable — legacy keyword index results")
                local_coro = _legacy_leg(query, source, limit)
            else:

                async def _no_local() -> tuple[bool, list[dict]]:
                    return False, []

                notes.append("help store unavailable")
                local_coro = _no_local()

            coros: list[Any] = [local_coro]
            if want_forum:
                coros.append(forum_search.search(query, max_topics=5))
            outcomes = await asyncio.gather(*coros, return_exceptions=True)

            local_out = outcomes[0]
            forum_out = outcomes[1] if want_forum else None

            if isinstance(local_out, BaseException):
                # Empty results are an answer, never an error.
                logger.error("Local help search failed", exc_info=local_out)
                semantic_ok, rows = False, []
            else:
                semantic_ok, rows = local_out

            # ----------------------------------------------------------
            # Live forum results
            # ----------------------------------------------------------
            live_forum: list[dict] = []
            if want_forum:
                if isinstance(forum_out, BaseException):
                    logger.warning("Live forum search raised", exc_info=forum_out)
                    notes.append(_NOTE_NO_FORUM)
                else:
                    live_forum = [
                        {
                            "title": topic.get("title", ""),
                            "url": topic.get("url", ""),
                            "created": topic.get("created_at", ""),
                            "solved": bool(topic.get("solved", False)),
                            "excerpt": topic.get("excerpt", ""),
                        }
                        for topic in forum_out
                    ]
                    if not live_forum:
                        # LiveForumSearch.last_status distinguishes a genuine
                        # empty result from breaker/rate-limit/HTTP failures.
                        status = str(getattr(forum_search, "last_status", "") or "")
                        if status and status not in ("ok", "no_results", "empty_query"):
                            notes.append(_NOTE_NO_FORUM)

            # ----------------------------------------------------------
            # Rerank → collapse near-dupes → bonuses → final order
            # (help-store path only; legacy rows pass through as-is)
            # ----------------------------------------------------------
            if help_store is not None:
                pool = rows[:_RERANK_POOL]
                rerank_scores: list[float] | None = None
                if pool:
                    try:
                        rerank_scores = await asyncio.wait_for(
                            asyncio.to_thread(
                                _rerank_sync, query, pool, settings.reranker_model
                            ),
                            timeout=_RERANK_TIMEOUT_S,
                        )
                    except (TimeoutError, asyncio.TimeoutError):
                        # The worker thread keeps loading/scoring in the
                        # background; its result is discarded but the
                        # singleton stays usable for the next call.
                        logger.warning(
                            "Reranker timed out after %.0fs — keeping RRF order",
                            _RERANK_TIMEOUT_S,
                        )
                    except Exception:
                        logger.warning(
                            "Reranker inference failed — keeping RRF order",
                            exc_info=True,
                        )

                if rerank_scores is not None:
                    for row, score in zip(pool, rerank_scores):
                        row["_score"] = score
                    pool.sort(key=lambda r: r["_score"], reverse=True)

                pool = help_store.collapse_near_dupes(pool)

                if rerank_scores is not None:
                    # Additive bonuses apply on top of rerank scores only —
                    # when the reranker is unavailable, RRF order stands.
                    for row in pool:
                        if row.get("source_type") == "eup":
                            row["_score"] += _EUP_BONUS
                        if str(row.get("doc_version") or "") >= "2025":
                            row["_score"] += _RECENT_VERSION_BONUS
                    pool.sort(key=lambda r: r["_score"], reverse=True)

                results = [_format_result(i, row) for i, row in enumerate(pool[:limit], 1)]
                if not semantic_ok:
                    notes.append(_NOTE_NO_SEMANTIC)
            else:
                results = [_format_legacy_result(i, row) for i, row in enumerate(rows[:limit], 1)]

            if not results:
                notes.append(_NOTE_NO_MATCHES)

            return json.dumps({
                "query": query,
                "results": results,
                "live_forum": live_forum,
                "notes": notes,
            }, indent=2)

        except RuntimeError as exc:
            # No authenticated session.
            return json.dumps({"error": str(exc)})
        except Exception:
            logger.exception("epicor_help failed")
            return json.dumps({
                "error": "Documentation search failed. Please try again with different keywords.",
            })

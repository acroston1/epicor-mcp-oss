"""Wire portable local metadata/document tools into either authentication mode."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from epicor_mcp.discovery.embeddings import embedder_for_index
from epicor_mcp.discovery.store import DiscoveryIndex
from epicor_mcp.discovery.tools import register_discovery_tools
from epicor_mcp.index.docs_index import DocsIndex

logger = logging.getLogger(__name__)


def discovery_embedding(settings: Any, index: DiscoveryIndex) -> tuple[Any, Any, Any]:
    """``(embed_query, search_note, embedder)`` for a loaded discovery index.

    ``embed_query`` is the ``async (text, prefix) -> vector | None`` the tools
    take. It returns ``None`` — substring ranking, explained through
    ``search_note`` — whenever the index carries no usable vectors, the switch
    is off, the configured provider does not match the build, or the provider
    fails at query time. A vector whose dimension differs from the index is
    ``None`` too: the store would ignore it, and the tool must not then report
    ``search_mode: "semantic"`` for a substring answer.
    """
    if index.semantic_error:
        embedder, reason = None, index.semantic_error
    else:
        embedder, reason = embedder_for_index(settings, index.manifest)
    if reason:
        logger.warning("semantic discovery disabled: %s", reason)
    state = {"note": reason}

    async def embed_query(text: str, prefix: str):
        if embedder is None or not (text or "").strip():
            return None
        vec = await embedder.encode_query(text, prefix)
        if vec is None:
            state["note"] = embedder.last_error or (
                "Semantic discovery provider unavailable; using substring search."
            )
            return None
        shape = getattr(vec, "shape", None)
        if shape != (index.dim,):
            state["note"] = (
                f"Discovery query embedding has dimension {shape[0] if shape else '?'} but the "
                f"index was built with {index.dim}; rebuild the index or reconfigure. "
                "Using substring search."
            )
            return None
        state["note"] = ""
        return vec

    return embed_query, (lambda: state["note"]), embedder


def register_local_retrieval(mcp: Any, settings: Any, table_authorizer: Any, *,
                             denied_table=None, denied_column=None, session_email=None) -> list[Any]:
    """Register all retrieval tools even when optional local corpora are absent."""
    from epicor_mcp.sql.denylist import is_denied_table, is_denied_column
    denied_table = denied_table or is_denied_table
    denied_column = denied_column or is_denied_column
    resources = []
    index = DiscoveryIndex.load(settings.discovery_index_path) or DiscoveryIndex.empty()
    resources.append(index)
    embed_query, search_note, embedder = discovery_embedding(settings, index)
    if embedder is not None:
        resources.append(embedder)

    register_discovery_tools(mcp, index, embed_query=embed_query, authorizer=table_authorizer,
                             denied_table=denied_table, denied_column=denied_column,
                             session_email=session_email, search_note=search_note)
    docs = None
    if Path(settings.docs_db_path).is_file():
        docs = DocsIndex(settings.docs_db_path,
                         semantic_dir=settings.document_vectors_path if settings.vector_search_enabled else None,
                         semantic_model=settings.embedding_model or None)
        resources.append(docs)

    @mcp.tool(structured_output=False)
    async def epicor_help(query: str, source: str = "", limit: int = 5) -> str:
        """Search the documents imported by your server administrator.

        Default search is case-insensitive substring matching: use distinctive
        words or phrases. Optional semantic indexing supports natural questions.
        Cite returned titles, excerpts and page numbers. If nothing relevant is
        found, say so. No vendor documentation is bundled with this server.
        `source` optionally filters the imported source_type; empty searches all.
        """
        limit = max(1, min(int(limit), 10))
        if docs is None:
            return json.dumps({"success": True, "query": query, "results": [],
                               "search_mode": "substring", "notes": [
                "No documents imported. Optional setup: python scripts/build_docs_index.py --input-dir YOUR_DOCS"]})
        rows, mode, semantic_error = await asyncio.to_thread(docs.search_with_metadata, query, source, limit)
        return json.dumps({"success": True, "query": query, "results": rows,
                           "search_mode": mode,
                           "notes": ["Semantic search unavailable; using substring search"] if semantic_error else []})

    return resources

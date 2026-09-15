"""Optional embedding providers for semantic table/field discovery.

Discovery needs none of this: the default index is metadata-only and ranks by
substring. An operator who wants semantic ranking builds the discovery index
WITH vectors — from a local sentence-transformers model or from an
OpenAI-compatible ``/v1/embeddings`` endpoint — and the index manifest records
which provider, model and dimension produced them. At query time
:func:`embedder_for_index` builds the matching provider from settings; any
disagreement (switch off, different model, different dimension, unknown
provider) means substring ranking, announced to the caller, never silent.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "EndpointEmbedder",
    "LocalEmbedder",
    "PROVIDER_ENDPOINT",
    "PROVIDER_LOCAL",
    "VECTOR_SWITCH",
    "embedder_for_index",
]

PROVIDER_LOCAL = "sentence-transformers"
PROVIDER_ENDPOINT = "openai-embeddings-endpoint"
VECTOR_SWITCH = "EPICOR_MCP_VECTOR_SEARCH_ENABLED"


def _check_matrix(matrix: Any, count: int) -> Any:
    import numpy as np

    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != count or not np.isfinite(matrix).all():
        raise ValueError("embedding provider returned an invalid matrix")
    return matrix


class _Failures:
    """Log a provider outage once, and keep the reason for the tool's note."""

    last_error: str = ""

    def _note_failure(self, exc: Exception) -> None:
        message = (
            f"Semantic discovery provider unavailable ({type(exc).__name__}: {exc}); "
            "using substring search."
        )
        if message != self.last_error:
            logger.warning("%s", message)
        self.last_error = message


class LocalEmbedder(_Failures):
    """A sentence-transformers model. Runtime loads are local/cache only."""

    provider = PROVIDER_LOCAL

    def __init__(self, model: str, *, device: str = "cpu", local_files_only: bool = True,
                 progress: bool = False) -> None:
        self.model = model
        self.dim: int | None = None
        self._device = device
        self._local_files_only = local_files_only
        self._progress = progress
        self._encoder: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._encoder is None:
                from sentence_transformers import SentenceTransformer

                self._encoder = SentenceTransformer(
                    self.model, device=self._device, local_files_only=self._local_files_only
                )
            return self._encoder

    def encode(self, texts: list[str], *, batch_size: int = 32) -> Any:
        """Normalised fp32 rows in input order; raises on a bad model output."""
        import numpy as np

        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        encoder = self._load()
        with self._lock:
            raw = encoder.encode(
                texts, batch_size=batch_size, normalize_embeddings=True,
                show_progress_bar=self._progress,
            )
        matrix = _check_matrix(raw, len(texts))
        self.dim = int(matrix.shape[1])
        return matrix

    async def encode_query(self, text: str, prefix: str) -> Any | None:
        """One prefixed query vector, or ``None`` (announced) on any failure."""
        try:
            matrix = await asyncio.to_thread(self.encode, [prefix + text], batch_size=1)
        except Exception as exc:  # noqa: BLE001 - the tool degrades, never fails
            self._note_failure(exc)
            return None
        self.last_error = ""
        return matrix[0]

    def close(self) -> None:
        with self._lock:
            self._encoder = None


class EndpointEmbedder(_Failures):
    """An OpenAI-compatible ``/v1/embeddings`` server, through ``EmbedClient``."""

    provider = PROVIDER_ENDPOINT

    def __init__(self, endpoint: str, model: str, *, dim: int = 2048, timeout: float = 8.0,
                 client: Any | None = None) -> None:
        self.endpoint = endpoint
        self.model = model
        self.dim: int | None = int(dim)
        if client is None:
            from epicor_mcp.index.embed_client import EmbedClient

            client = EmbedClient(endpoint, model, dim=int(dim), timeout=timeout)
        self._client = client

    def encode(self, texts: list[str], *, batch_size: int = 64) -> Any:
        import numpy as np

        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        return _check_matrix(
            asyncio.run(self._client.embed_docs(texts, batch_size=batch_size)), len(texts)
        )

    async def encode_query(self, text: str, prefix: str) -> Any | None:
        # Deliberately ``embed_docs`` with the DISCOVERY prefix, not
        # ``EmbedClient.embed_query``: that method prepends the document-search
        # instruction unconditionally, and the two retrieval tasks differ.
        try:
            vecs = await self._client.embed_docs([prefix + text], batch_size=1)
            matrix = _check_matrix(vecs, 1)
        except Exception as exc:  # noqa: BLE001 - the tool degrades, never fails
            self._note_failure(exc)
            return None
        self.last_error = ""
        return matrix[0]

    async def aclose(self) -> None:
        """Close the HTTP client; the server's shutdown awaits this."""
        aclose = getattr(self._client, "aclose", None)
        if aclose is not None:
            await aclose()

    def close(self) -> None:
        """Sync fallback for callers without a loop (the build script, tests)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(self.aclose())
        else:
            asyncio.run(self.aclose())


def embedder_for_index(settings: Any, manifest: dict[str, Any]) -> tuple[Any | None, str]:
    """The runtime provider matching a built index, or ``(None, why)``.

    ``(None, "")`` means the index is metadata-only and there is nothing to
    announce. A non-empty reason is shown to the caller as a ``notes`` entry on
    every substring-ranked response, so an operator learns WHY from the tool
    output rather than from a server log they may not be watching.
    """
    if not manifest.get("semantic"):
        return None, ""
    provider = str(manifest.get("provider") or "")
    model = str(manifest.get("model") or "")
    dim = manifest.get("dim")
    if not getattr(settings, "vector_search_enabled", False):
        return None, (
            f"Semantic discovery vectors are built ({provider}, {model!r}) but "
            f"{VECTOR_SWITCH} is false; using substring search."
        )
    if provider == PROVIDER_LOCAL:
        configured = str(getattr(settings, "embedding_model", "") or "")
        if not configured:
            return None, (
                "The discovery index was built with a local sentence-transformers model; "
                f"set EPICOR_MCP_EMBEDDING_MODEL to {model!r} to use it. Using substring search."
            )
        if configured != model:
            return None, (
                f"EPICOR_MCP_EMBEDDING_MODEL ({configured!r}) differs from the model that built "
                f"the discovery index ({model!r}); rebuild the index or reconfigure. "
                "Using substring search."
            )
        return LocalEmbedder(configured), ""
    if provider == PROVIDER_ENDPOINT:
        endpoint = str(getattr(settings, "embed_endpoint", "") or "")
        name = str(getattr(settings, "embed_model_name", "") or "")
        configured_dim = int(getattr(settings, "embed_dim", dim or 0) or 0)
        if not endpoint or not name:
            return None, (
                "The discovery index was built through an embeddings endpoint; set "
                "EPICOR_MCP_EMBED_ENDPOINT and EPICOR_MCP_EMBED_MODEL_NAME "
                f"({model!r}, dimension {dim}) to use it. Using substring search."
            )
        if name != model or configured_dim != dim:
            return None, (
                f"EPICOR_MCP_EMBED_MODEL_NAME/EMBED_DIM ({name!r}, {configured_dim}) differ from "
                f"the discovery index build ({model!r}, {dim}); rebuild the index or "
                "reconfigure. Using substring search."
            )
        return EndpointEmbedder(endpoint, name, dim=int(dim)), ""
    return None, (
        f"Unknown discovery vector provider {provider!r}; rebuild the discovery index. "
        "Using substring search."
    )

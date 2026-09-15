"""Compatibility client for an OpenAI-compatible ``/v1/embeddings`` endpoint.

Talks to any server exposing that route (vLLM, for example) and applies
MRL (Matryoshka) truncation client-side: embeddings are cut to the first
``dim`` dimensions and then L2-normalized, so they can be used directly
with a ``faiss.IndexFlatIP`` (inner product == cosine similarity).

Two call paths:

* ``embed_query`` applies the retrieval instruction prefix, returns ``None``
  on failure so callers can use keyword search, and logs at most one warning
  per outage.
* ``embed_docs`` embeds caller-supplied text batches, retries with exponential
  backoff, splits a batch in half on context-length 400s, and raises on
  unrecoverable failure.

Both paths must use the same configured model and truncation dimension. This
adapter is separate from the supported local Sentence Transformers setup in
``scripts/build_document_vectors.py`` and README.md.

Usage:
    from epicor_mcp.index.embed_client import EmbedClient
    client = EmbedClient("http://localhost:8000/v1/embeddings",
                         "your-configured-embedding-model")
    vec = await client.embed_query("how do I close a purchase order")
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# Exact instruction prefix for query embeddings (documents get NO prefix).
_QUERY_INSTRUCTION = (
    "Instruct: Given a question about Epicor Kinetic ERP, retrieve relevant "
    "documentation, procedures, or forum discussions\nQuery: "
)

# How long a health() result stays cached, in seconds.
_HEALTH_CACHE_TTL = 30.0

# embed_docs retry policy.
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds; doubles per attempt

# Sleep management (vLLM --enable-sleep-mode + VLLM_SERVER_DEV_MODE=1).
_SLEEP_CHECK_INTERVAL = 60.0  # watchdog wakeup cadence, seconds
_SLEEP_OP_TIMEOUT = 30.0      # /sleep takes ~10s (weights -> host RAM)


class EmbedClient:
    """Async client for an OpenAI-compatible ``/v1/embeddings`` endpoint."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        dim: int = 2048,
        timeout: float = 4.0,
        sleep_after: float = 0.0,
    ) -> None:
        """Create the client.

        Parameters
        ----------
        endpoint:
            Full embeddings URL, e.g. ``http://localhost:8000/v1/embeddings``.
        model:
            Model name passed in the request body.
        dim:
            Client-side MRL truncation dimension (vectors are cut to the
            first *dim* dims, then L2-normalized).
        timeout:
            Per-request timeout in seconds. The default suits query-time
            use; batch embedding callers should pass something much larger.
        sleep_after:
            Idle seconds after which the vLLM server is put to sleep
            (``POST /sleep?level=1`` — weights offload to host RAM).
            Resource savings and wake latency depend on the model and server.
            A value of 0 disables sleep management. Requires the server to run with
            ``--enable-sleep-mode`` and ``VLLM_SERVER_DEV_MODE=1``;
            when it doesn't, the first 404 disables the watchdog.
        """
        self._endpoint = endpoint
        self._model = model
        self._dim = dim
        self._timeout = timeout
        self._sleep_after = sleep_after

        # Derive the server base URL for the /health and sleep endpoints.
        base = endpoint.split("/v1/")[0] if "/v1/" in endpoint else endpoint
        base = base.rstrip("/")
        self._health_url = base + "/health"
        self._sleep_url = base + "/sleep?level=1"
        self._wake_url = base + "/wake_up"
        self._is_sleeping_url = base + "/is_sleeping"

        self._client: httpx.AsyncClient | None = None
        self._health_cached_at: float = 0.0
        self._health_cached: bool = False
        # Log the "endpoint down" warning once per outage, not per call.
        self._outage_logged = False

        # Sleep management state. `_sleep_supported` starts optimistic and
        # flips False on the first 404 from a sleep endpoint (server not in
        # dev mode / sleep mode). `_asleep_hint` is this process's view; the
        # server is shared, so the hint may be stale either way — the wake
        # path always confirms via /is_sleeping.
        self._sleep_supported = True
        self._asleep_hint = False
        self._last_used: float = time.monotonic()
        self._watchdog: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared AsyncClient, creating it lazily (connection reuse)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._watchdog is not None and not self._watchdog.done():
            self._watchdog.cancel()
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Sleep management (frees GPU VRAM while the help tool is idle)
    # ------------------------------------------------------------------

    def _ensure_watchdog(self) -> None:
        """Start the idle-sleep watchdog lazily (needs a running loop)."""
        if self._sleep_after <= 0 or not self._sleep_supported:
            return
        if self._watchdog is not None and not self._watchdog.done():
            return
        try:
            self._watchdog = asyncio.get_running_loop().create_task(
                self._watchdog_loop()
            )
        except RuntimeError:  # no running loop (sync/test context)
            pass

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(_SLEEP_CHECK_INTERVAL)
            if self._asleep_hint or not self._sleep_supported:
                continue
            if time.monotonic() - self._last_used < self._sleep_after:
                continue
            try:
                resp = await self._get_client().post(
                    self._sleep_url, timeout=_SLEEP_OP_TIMEOUT
                )
                if resp.status_code == 404:
                    self._sleep_supported = False
                    logger.info(
                        "Embedding server has no sleep endpoint — idle sleep disabled"
                    )
                    return
                resp.raise_for_status()
                self._asleep_hint = True
                logger.info(
                    "Embedding server slept after %.0fs idle (GPU VRAM released)",
                    self._sleep_after,
                )
            except Exception as exc:
                logger.debug("Idle-sleep attempt failed: %s", exc)

    async def _wake_if_sleeping(self) -> None:
        """Wake the shared server if it is actually asleep.

        Confirms via ``GET /is_sleeping`` rather than trusting the local
        hint — another MCP server process may have slept or woken it.
        Never raises; on any failure the caller's embed attempt proceeds
        and its own error handling applies.
        """
        if not self._sleep_supported:
            return
        try:
            resp = await self._get_client().get(self._is_sleeping_url, timeout=2.0)
            if resp.status_code == 404:
                self._sleep_supported = False
                return
            if not resp.json().get("is_sleeping", False):
                self._asleep_hint = False
                return
            wake = await self._get_client().post(
                self._wake_url, timeout=_SLEEP_OP_TIMEOUT
            )
            wake.raise_for_status()
            self._asleep_hint = False
            logger.info("Embedding server woken on demand")
        except Exception as exc:
            logger.debug("Wake attempt failed: %s", exc)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """Return True if the embedding server is reachable.

        Tries ``GET <base>/health`` first; if that endpoint is missing,
        falls back to a 1-item embedding probe. The result is cached for
        30 seconds so the hot path never stacks probes.
        """
        now = time.monotonic()
        if now - self._health_cached_at < _HEALTH_CACHE_TTL:
            return self._health_cached

        ok = False
        try:
            resp = await self._get_client().get(self._health_url)
            if resp.status_code == 200:
                ok = True
            elif resp.status_code in (404, 405):
                # No /health route on this server — probe with a tiny embed.
                ok = await self._probe_embed()
        except Exception:
            ok = False

        self._health_cached_at = time.monotonic()
        self._health_cached = ok
        return ok

    async def _probe_embed(self) -> bool:
        """1-item embedding request used as a health fallback."""
        try:
            resp = await self._get_client().post(
                self._endpoint,
                json={"model": self._model, "input": ["ping"]},
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Vector post-processing
    # ------------------------------------------------------------------

    def _truncate_normalize(self, vecs: np.ndarray) -> np.ndarray:
        """MRL-truncate to ``self._dim`` dims and L2-normalize (fp32).

        Zero vectors are left as-is (norm guard) rather than producing NaNs.
        """
        vecs = np.asarray(vecs, dtype=np.float32)[:, : self._dim]
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return np.ascontiguousarray(vecs / norms)

    @staticmethod
    def _parse_response(payload: dict) -> np.ndarray:
        """Extract embeddings from an OpenAI-style response, re-sorted by 'index'.

        The server may return items out of order; positional trust is a
        silent-corruption bug, so we always re-sort.
        """
        items = sorted(payload["data"], key=lambda item: item["index"])
        return np.asarray([item["embedding"] for item in items], dtype=np.float32)

    # ------------------------------------------------------------------
    # Query embedding (hot path — never raises)
    # ------------------------------------------------------------------

    async def embed_query(self, query: str) -> np.ndarray | None:
        """Embed a single search query with the instruction prefix applied.

        Returns a normalized fp32 vector of shape ``(dim,)``, or ``None``
        on any failure (timeout, connection error, bad payload). Failures
        are logged once per outage, not once per call.

        Sleep-aware: wakes the server first when it is known/found to be
        asleep (a sleeping vLLM instance hangs requests rather than
        rejecting them), and retries once after a wake on timeout.
        """
        self._last_used = time.monotonic()
        self._ensure_watchdog()
        if self._asleep_hint:
            await self._wake_if_sleeping()

        async def _post() -> np.ndarray:
            resp = await self._get_client().post(
                self._endpoint,
                json={
                    "model": self._model,
                    "input": [_QUERY_INSTRUCTION + query],
                },
            )
            resp.raise_for_status()
            vecs = self._parse_response(resp.json())
            return self._truncate_normalize(vecs)[0]

        try:
            try:
                result = await _post()
            except httpx.TimeoutException:
                # A sleeping server hangs the request instead of erroring —
                # confirm/wake, then retry exactly once.
                if not self._sleep_supported:
                    raise
                await self._wake_if_sleeping()
                result = await _post()
        except Exception as exc:
            if not self._outage_logged:
                logger.warning(
                    "Embedding endpoint unavailable (%s: %s) — dense search "
                    "disabled until it recovers",
                    type(exc).__name__,
                    exc,
                )
                self._outage_logged = True
            return None

        if self._outage_logged:
            logger.info("Embedding endpoint recovered")
            self._outage_logged = False
        return result

    # ------------------------------------------------------------------
    # Document embedding (build path — raises on failure)
    # ------------------------------------------------------------------

    async def embed_docs(
        self,
        texts: list[str],
        batch_size: int = 64,
    ) -> np.ndarray:
        """Embed *texts* (no instruction prefix) for indexing.

        Returns a normalized fp32 array of shape ``(len(texts), dim)`` in
        input order. Each batch is retried up to 3 times with exponential
        backoff; a 400 (context-length) response splits the batch in half
        and retries the halves. Raises on unrecoverable failure — the
        build must never silently produce a misaligned store.
        """
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)

        # The shared server may be idle-sleeping; wake it before the build.
        self._last_used = time.monotonic()
        await self._wake_if_sleeping()

        parts: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            parts.append(await self._embed_batch(batch))
        return np.vstack(parts)

    async def _embed_batch(self, batch: list[str]) -> np.ndarray:
        """Embed one batch with retry/backoff and 400-split handling."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            split = False
            try:
                resp = await self._get_client().post(
                    self._endpoint,
                    json={"model": self._model, "input": batch},
                )
                if resp.status_code == 400:
                    # Typically a context-length overflow. Splitting halves
                    # the per-request token load; a single item that still
                    # 400s is deterministic — fail fast, no retries.
                    if len(batch) == 1:
                        raise RuntimeError(
                            f"Embedding endpoint rejected a single item "
                            f"(400): {resp.text[:300]}"
                        )
                    logger.warning(
                        "400 from embedding endpoint on batch of %d "
                        "(%.120s) — splitting in half",
                        len(batch),
                        resp.text,
                    )
                    split = True
                else:
                    resp.raise_for_status()
                    vecs = self._parse_response(resp.json())
                    if vecs.shape[0] != len(batch):
                        raise RuntimeError(
                            f"Embedding count mismatch: sent {len(batch)}, "
                            f"got {vecs.shape[0]}"
                        )
                    return self._truncate_normalize(vecs)
            except RuntimeError:
                raise  # hard errors (single-item 400, count mismatch)
            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2**attempt)
                    logger.warning(
                        "Embed batch of %d failed (attempt %d/%d): %s — "
                        "retrying in %.1fs",
                        len(batch),
                        attempt + 1,
                        _MAX_RETRIES,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                continue

            if split:
                # Recurse outside the try block so a failure in one half
                # propagates cleanly instead of re-running the whole batch.
                mid = len(batch) // 2
                left = await self._embed_batch(batch[:mid])
                right = await self._embed_batch(batch[mid:])
                return np.vstack([left, right])

        raise RuntimeError(
            f"Embedding batch of {len(batch)} failed after "
            f"{_MAX_RETRIES} attempts"
        ) from last_exc

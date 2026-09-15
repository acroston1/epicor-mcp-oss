"""Async HTTP client for Epicor Kinetic REST/OData API.

Uses ``httpx.AsyncClient`` with connection pooling, automatic retry on
transient auth failures (401/403), and structured error handling via
``ErrorHandler``.

The client authenticates with Basic auth (service account) and includes a
per-request ``X-API-Key`` header that selects the department-scoped Epicor
Access Scope.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from epicor_mcp.epicor_client.error_handler import EpicorError, ErrorHandler

logger = logging.getLogger(__name__)

# Default HTTP timeout (seconds).
_DEFAULT_TIMEOUT = 30.0

# Maximum number of retries for transient failures.
_MAX_RETRIES = 2

# HTTP status codes that trigger a retry.
_RETRYABLE_STATUSES = {401, 403, 502, 503, 504}


class EpicorClient:
    """Async HTTP client for the Epicor Kinetic OData / REST API.

    Manages a shared ``httpx.AsyncClient`` with connection pooling and
    provides typed convenience methods for common Epicor operations.

    The service account credentials (username/password) are fixed at
    construction time.  The **API key** varies per request based on the
    calling user's department and is supplied as a parameter to every
    public method.

    Parameters
    ----------
    username:
        Epicor service account username for Basic auth.
    password:
        Epicor service account password for Basic auth.
    company_id:
        Epicor company ID supplied by the server configuration.
    timeout:
        HTTP request timeout in seconds (default 30).
    """

    def __init__(
        self,
        username: str,
        password: str,
        company_id: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
        base_url: str = "",
    ) -> None:
        self._basic_token = base64.b64encode(
            f"{username}:{password}".encode()
        ).decode()
        self._company_id = company_id
        self._timeout = timeout
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared ``AsyncClient``, creating it lazily."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self._timeout,
                    read=self._timeout,
                    write=self._timeout,
                    pool=300.0,  # Don't timeout waiting for pool connections
                ),
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=300.0,  # Keep idle connections alive 5 minutes
                ),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP connection pool.  Call on shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("EpicorClient HTTP pool closed.")

    # ------------------------------------------------------------------
    # Public HTTP methods
    # ------------------------------------------------------------------

    async def get(
        self,
        url: str,
        api_key: str,
        params: dict[str, Any] | None = None,
    ) -> dict:
        """Execute an HTTP GET against the Epicor API.

        Parameters
        ----------
        url:
            Full request URL.
        api_key:
            Department-scoped Epicor API key.
        params:
            Optional query parameters (e.g. OData ``$filter``).

        Returns
        -------
        dict
            Parsed JSON response body.

        Raises
        ------
        EpicorError
            If the API returns a non-2xx status after retries.
        """
        return await self._request("GET", url, api_key, params=params)

    async def post(
        self,
        url: str,
        api_key: str,
        json_body: dict | None = None,
    ) -> dict:
        """Execute an HTTP POST against the Epicor API.

        Parameters
        ----------
        url:
            Full request URL.
        api_key:
            Department-scoped Epicor API key.
        json_body:
            Optional JSON request body.

        Returns
        -------
        dict
            Parsed JSON response body.

        Raises
        ------
        EpicorError
            If the API returns a non-2xx status after retries.
        """
        return await self._request("POST", url, api_key, json_body=json_body)

    async def call_method(
        self,
        base_url: str,
        service: str,
        method: str,
        api_key: str,
        params: dict | None = None,
    ) -> dict:
        """Call an Epicor service method (POST).

        Builds the URL as ``{base_url}/{service}/{method}`` and sends a
        POST with *params* as the JSON body.

        Parameters
        ----------
        base_url:
            OData base URL (e.g. ``https://host/api/v2/odata/DEMO/``).
        service:
            Full service name (e.g. ``"Erp.BO.POSvc"``).
        method:
            Method name (e.g. ``"GetByID"``).
        api_key:
            Department-scoped Epicor API key.
        params:
            Method parameters as a dict (sent as JSON body).

        Returns
        -------
        dict
            Parsed JSON response.
        """
        url = f"{base_url.rstrip('/')}/{service}/{method}"
        return await self.post(url, api_key, json_body=params)

    async def odata_query(
        self,
        base_url: str,
        service: str,
        entity_set: str,
        api_key: str,
        odata_params: dict[str, Any] | None = None,
    ) -> dict:
        """Execute an OData GET query against an entity set.

        Parameters
        ----------
        base_url:
            OData base URL.
        service:
            Full service name.
        entity_set:
            Entity set name (e.g. ``"Vendors"``).
        api_key:
            Department-scoped Epicor API key.
        odata_params:
            OData query parameters (``$filter``, ``$select``, etc.).

        Returns
        -------
        dict
            Parsed JSON response (typically contains a ``"value"`` array).
        """
        url = f"{base_url.rstrip('/')}/{service}/{entity_set}"
        return await self.get(url, api_key, params=odata_params)

    # ------------------------------------------------------------------
    # Header construction
    # ------------------------------------------------------------------

    def _build_headers(self, api_key: str) -> dict[str, str]:
        """Build Epicor-compatible HTTP headers.

        Includes:
        - ``Authorization`` -- Basic auth with the service account.
        - ``X-API-Key``     -- Department-scoped Epicor Access Scope key.
        - ``Content-Type``  -- ``application/json``.
        - ``Company``       -- Epicor company ID.
        """
        return {
            "Authorization": f"Basic {self._basic_token}",
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Company": self._company_id,
        }

    # ------------------------------------------------------------------
    # Internal request dispatcher
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        api_key: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """Send an HTTP request with retry logic.

        Retries up to ``_MAX_RETRIES`` times on transient status codes
        (401, 403, 502, 503, 504).

        Parameters
        ----------
        method:
            HTTP method (``"GET"`` or ``"POST"``).
        url:
            Full request URL.
        api_key:
            Department API key.
        params:
            Query parameters (for GET requests).
        json_body:
            JSON body (for POST requests).

        Returns
        -------
        dict
            Parsed JSON response body.

        Raises
        ------
        EpicorError
            On non-2xx responses after all retries are exhausted.
        """
        # If the URL is not absolute (no scheme), prepend the base URL.
        if self._base_url and not url.startswith(("http://", "https://")):
            url = f"{self._base_url}/{url.lstrip('/')}"

        headers = self._build_headers(api_key)
        client = await self._get_client()

        last_error: EpicorError | None = None

        for attempt in range(_MAX_RETRIES + 1):
            log_prefix = f"[attempt {attempt + 1}/{_MAX_RETRIES + 1}]"
            logger.debug(
                "%s %s %s params=%s body_keys=%s",
                log_prefix,
                method,
                url,
                list(params.keys()) if params else None,
                list(json_body.keys()) if json_body else None,
            )

            try:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
            except httpx.TimeoutException as exc:
                logger.warning("%s Timeout calling %s %s: %s", log_prefix, method, url, exc)
                last_error = EpicorError(
                    status_code=408,
                    message=f"Request timed out after {self._timeout}s: {url}",
                )
                continue
            except httpx.HTTPError as exc:
                logger.warning("%s HTTP error calling %s %s: %s", log_prefix, method, url, exc)
                last_error = EpicorError(
                    status_code=0,
                    message=f"HTTP transport error: {exc}",
                )
                continue

            logger.debug(
                "%s Response: %d (%d bytes)",
                log_prefix,
                response.status_code,
                len(response.content),
            )

            # Success
            if response.is_success:
                # Some Epicor endpoints return 204 No Content.
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    # Non-JSON success response (rare).
                    return {"_raw": response.text}

            # Retryable failure
            if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                logger.warning(
                    "%s Retryable status %d from %s %s",
                    log_prefix,
                    response.status_code,
                    method,
                    url,
                )
                # Parse error for potential last_error, then retry.
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                last_error = ErrorHandler.parse_error(response.status_code, body)
                continue

            # Non-retryable failure (or retries exhausted)
            try:
                body = response.json()
            except ValueError:
                body = response.text

            error = ErrorHandler.parse_error(response.status_code, body)
            logger.error(
                "Epicor API error: %d %s (url=%s)",
                error.status_code,
                error.message,
                url,
            )
            raise error

        # All retries exhausted.
        if last_error is not None:
            raise last_error

        # Should never reach here, but satisfy the type checker.
        raise EpicorError(status_code=0, message="Request failed after all retries.")

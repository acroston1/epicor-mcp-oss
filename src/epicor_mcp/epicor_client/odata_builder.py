"""OData URL and query-parameter builder for Epicor Kinetic.

Provides static helpers for constructing OData URLs and ``$filter``,
``$select``, ``$orderby``, ``$top``, ``$expand``, and ``$skip`` query
parameters.  All methods are pure functions with no side effects.
"""

from __future__ import annotations


class ODataBuilder:
    """Build OData query parameters and URLs for Epicor API calls."""

    @staticmethod
    def build_query_params(
        filter: str = "",
        select: str = "",
        orderby: str = "",
        top: int = 100,
        expand: str = "",
        skip: int = 0,
    ) -> dict[str, str | int]:
        """Build an OData query-parameter dict from user inputs.

        Only non-empty / non-default parameters are included in the
        returned dict so that unnecessary query-string clutter is avoided.

        Parameters
        ----------
        filter:
            OData ``$filter`` expression
            (e.g. ``"VendorNum eq 1234"``).
        select:
            Comma-separated list of fields for ``$select``
            (e.g. ``"VendorNum,VendorID,Name1"``).
        orderby:
            OData ``$orderby`` expression
            (e.g. ``"OrderDate desc"``).
        top:
            Maximum number of records (clamped to 1--1000).
        expand:
            OData ``$expand`` for related tables
            (e.g. ``"PORels"``).
        skip:
            Number of records to skip for pagination.

        Returns
        -------
        dict[str, str | int]
            Query parameters ready to pass to ``httpx`` as ``params``.
        """
        params: dict[str, str | int] = {}

        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = select
        if orderby:
            params["$orderby"] = orderby
        if expand:
            params["$expand"] = expand

        top = ODataBuilder.validate_top(top)
        params["$top"] = top

        if skip > 0:
            params["$skip"] = skip

        return params

    @staticmethod
    def build_url(base_url: str, service: str, entity_set: str) -> str:
        """Build the full OData entity-set URL.

        Example::

            >>> ODataBuilder.build_url(
            ...     "https://host/api/v2/odata/DEMO/",
            ...     "Erp.BO.VendorSvc",
            ...     "Vendors",
            ... )
            'https://host/api/v2/odata/DEMO/Erp.BO.VendorSvc/Vendors'

        Parameters
        ----------
        base_url:
            OData base URL (with or without trailing slash).
        service:
            Full Epicor service name (e.g. ``"Erp.BO.VendorSvc"``).
        entity_set:
            Entity set name (e.g. ``"Vendors"``).
        """
        base = base_url.rstrip("/")
        return f"{base}/{service}/{entity_set}"

    @staticmethod
    def build_method_url(base_url: str, service: str, method: str) -> str:
        """Build the full method-invocation URL.

        Example::

            >>> ODataBuilder.build_method_url(
            ...     "https://host/api/v2/odata/DEMO/",
            ...     "Erp.BO.POSvc",
            ...     "GetByID",
            ... )
            'https://host/api/v2/odata/DEMO/Erp.BO.POSvc/GetByID'

        Parameters
        ----------
        base_url:
            OData base URL (with or without trailing slash).
        service:
            Full Epicor service name.
        method:
            Method name (e.g. ``"GetByID"``, ``"GetList"``).
        """
        base = base_url.rstrip("/")
        return f"{base}/{service}/{method}"

    @staticmethod
    def validate_top(top: int) -> int:
        """Clamp *top* to the valid range ``[1, 1000]``.

        Parameters
        ----------
        top:
            Requested maximum record count.

        Returns
        -------
        int
            The clamped value.
        """
        return max(1, min(top, 1000))

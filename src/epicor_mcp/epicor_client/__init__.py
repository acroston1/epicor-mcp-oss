"""Async HTTP client for the Epicor Kinetic REST/OData API.

Provides ``EpicorClient`` (async HTTP transport), ``ODataBuilder`` (URL and
query-parameter construction), ``ErrorHandler`` / ``EpicorError``
(structured error parsing), and ``DatasetHandler`` (multi-step ds workflows).
"""

from epicor_mcp.epicor_client.dataset_handler import DatasetHandler
from epicor_mcp.epicor_client.error_handler import EpicorError, ErrorHandler
from epicor_mcp.epicor_client.http_client import EpicorClient
from epicor_mcp.epicor_client.odata_builder import ODataBuilder

__all__ = [
    "DatasetHandler",
    "EpicorClient",
    "EpicorError",
    "ErrorHandler",
    "ODataBuilder",
]

"""Index modules: SQLite-backed catalogs for Epicor Kinetic services, BAQ schemas, and documentation."""

from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
from epicor_mcp.index.docs_index import DocsIndex
from epicor_mcp.index.service_index import ServiceIndex

try:
    from epicor_mcp.index.vector_index import VectorIndex
except ImportError:
    VectorIndex = None  # type: ignore[assignment,misc]  # faiss-cpu not installed

__all__ = ["BAQSchemaIndex", "DocsIndex", "ServiceIndex", "VectorIndex"]

"""Table and field discovery from administrator-imported schema metadata."""

from __future__ import annotations

from epicor_mcp.discovery.authz import (
    AuthzScope,
    ScopeState,
    TABLE_AUTHZ_MODES,
    TableAuthorizer,
    normalize_table,
)
from epicor_mcp.discovery.embeddings import (
    EndpointEmbedder,
    LocalEmbedder,
    embedder_for_index,
)
from epicor_mcp.discovery.store import (
    EMBED_DIM,
    FIELD_QUERY_PREFIX,
    TABLE_QUERY_PREFIX,
    DiscoveryIndex,
    FieldHit,
    TableHit,
)
from epicor_mcp.discovery.text import (
    ABBREV,
    expand_abbrevs,
    field_document,
    split_camel,
    table_document,
)
from epicor_mcp.discovery.tools import register_discovery_tools

__all__ = [
    "ABBREV",
    "AuthzScope",
    "DiscoveryIndex",
    "EMBED_DIM",
    "EndpointEmbedder",
    "LocalEmbedder",
    "embedder_for_index",
    "FIELD_QUERY_PREFIX",
    "FieldHit",
    "ScopeState",
    "TABLE_AUTHZ_MODES",
    "TABLE_QUERY_PREFIX",
    "TableAuthorizer",
    "TableHit",
    "expand_abbrevs",
    "field_document",
    "normalize_table",
    "register_discovery_tools",
    "split_camel",
    "table_document",
]

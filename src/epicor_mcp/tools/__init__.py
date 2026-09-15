'Tool registration for the Epicor MCP server (legacy tool surface).'

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP as Server

    from epicor_mcp.epicor_client.dataset_handler import DatasetHandler
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    from epicor_mcp.index.docs_index import DocsIndex
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

from epicor_mcp.tools import (
    act,
    baq,
    find_person,
    help,
    mrp_output,
    mrp_status,
    my_access,
    read,
    time_phase,
)

# Kept minimal tools that use the standard 4-parameter register() signature.
_KEPT_TOOL_MODULES = [
    my_access,
    find_person,
    mrp_status,
    mrp_output,
    time_phase,
]


def register_tools(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    dataset_handler: "DatasetHandler",
    baq_index: "BAQSchemaIndex | None" = None,
    docs_index: "DocsIndex | None" = None,
    vector_index: "Any | None" = None,
    help_store: "Any | None" = None,
    embed_client: "Any | None" = None,
    forum_search: "Any | None" = None,
) -> None:
    """Register the legacy tool surface with the MCP server.

    Call once during startup, after the ``ServiceIndex``, ``RBACEnforcer``,
    ``EpicorClient``, and ``DatasetHandler`` are initialised.

    Parameters
    ----------
    server:
        The MCP ``Server`` instance to attach ``@server.tool()`` handlers to.
    index:
        Pre-built service index for metadata lookups.
    rbac:
        RBAC enforcer for department-level access checks.
    client:
        HTTP client configured for the target Epicor environment.
    dataset_handler:
        Dataset workflow handler; required by ``epicor_act``.
    baq_index:
        Pre-built BAQ schema index. If ``None``, ``epicor_baq`` is not
        registered.
    docs_index:
        Legacy FTS5 documentation index — the ``epicor_help`` fallback
        backend when ``help_store`` is unavailable.  If both this and
        ``help_store`` are ``None``, ``epicor_help`` is not registered.
    vector_index:
        Deprecated legacy semantic index.  Accepted for backward
        compatibility; ignored by ``epicor_help``.
    help_store:
        Hybrid FTS5 + FAISS ``HelpStore`` — the primary ``epicor_help``
        backend.
    embed_client:
        ``EmbedClient`` for the local embedding server (dense retrieval
        leg).  Optional; without it ``epicor_help`` runs keyword-only.
    forum_search:
        ``LiveForumSearch`` client for live epiusers.help lookups.
        Optional.
    """
    # Kept minimal tools (standard 4-param register()).
    for module in _KEPT_TOOL_MODULES:
        module.register(server, index, rbac, client)

    # epicor_read — the one read tool.
    read.register(server, index, rbac, client, baq_index, dataset_handler)

    # epicor_act — needs the dataset_handler for multi-step writes.
    act.register(server, index, rbac, client, baq_index, dataset_handler)

    # epicor_baq — only when the BAQ schema index is available.
    if baq_index is not None:
        baq.register(server, index, rbac, client, baq_index, dataset_handler)

    # epicor_help — hybrid help store (primary) with legacy DocsIndex
    # FTS5 fallback.
    if help_store is not None or docs_index is not None:
        help.register(
            server,
            docs_index,
            vector_index,
            help_store=help_store,
            embed_client=embed_client,
            forum_search=forum_search,
        )

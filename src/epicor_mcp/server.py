"""Epicor Kinetic MCP Server entry point.

FastAPI + MCP server using Streamable HTTP transport on port 8015.
Supports both stdio (development) and HTTP (production) transports.

Usage:
    # HTTP mode (default, production)
    epicor-mcp

    # stdio mode (development/testing)
    epicor-mcp --stdio
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jose import JWTError

from mcp.server.fastmcp import FastMCP

from epicor_mcp import __version__
from epicor_mcp.auth.credentials import CredentialManager, create_default_department_keys_file
from epicor_mcp.auth.oauth import AzureADTokenValidator
from epicor_mcp.auth.session import MCPSession, SessionStore
from epicor_mcp.config import Settings, get_settings
from epicor_mcp.context import get_current_session_or_none, set_current_session, clear_current_session
from epicor_mcp.epicor_client.dataset_handler import DatasetHandler
from epicor_mcp.epicor_client.http_client import EpicorClient
from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
from epicor_mcp.index.docs_index import DocsIndex
from epicor_mcp.index.service_index import ServiceIndex
from epicor_mcp.rbac.enforcer import RBACEnforcer
from epicor_mcp.rbac.epicor_user_resolver import (
    EpicorUserResolver,
    validate_group_map,
)
from epicor_mcp.rbac.user_map import UserMap
from epicor_mcp.tools import register_tools

logger = logging.getLogger(__name__)

# Optional menu-authorization dependencies; enforce mode refuses startup
# if they cannot be imported.
try:
    from epicor_mcp.rbac.epicor_authz import EpicorAuthzClient
    from epicor_mcp.rbac.menu_authz import MenuAuthorizer
    from epicor_mcp.rbac.menu_map_store import MenuMapStore

    _MENU_AUTHZ_AVAILABLE = True
except ImportError as _exc:  # pragma: no cover — dependency failure
    EpicorAuthzClient = None  # type: ignore[assignment,misc]
    MenuAuthorizer = None  # type: ignore[assignment,misc]
    MenuMapStore = None  # type: ignore[assignment,misc]
    _MENU_AUTHZ_AVAILABLE = False
    logger.warning("Menu-derived RBAC modules unavailable: %s", _exc)


def _departments_from_groups(
    groups: Any, group_map: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return only the operator's mapped department labels, never permissions."""
    mapping = validate_group_map({} if group_map is None else group_map)
    depts: set[str] = set()
    for g in groups or ():
        depts.update(mapping.get(g, []))
    return sorted(depts)


def _can_write_baqs_from_groups(groups: "Any") -> bool:
    """True when the user holds an Epicor BAQ-authoring security group."""
    return bool({"ExtBAQDesigner", "BAQ", "BAMP", "BAMS"} & set(groups or ()))


def _with_plant_hints(instructions: str, settings: Settings) -> str:
    """Snapshot this app's operator hints without changing query or access policy."""
    if not settings.plants:
        return instructions
    return instructions + (
        "\n\nOperator-provided Plant/site code-to-name hints (not live-verified): "
        + json.dumps(settings.plants, ensure_ascii=False, sort_keys=True)
        + ". Use these hints to identify a requested site code. They do not grant "
        "table access or authorize changes to caller-provided SQL."
    )


def _sso_transport_security(settings: Settings):
    """Keep hosted SSO transport policy identical with or without an index."""
    from urllib.parse import urlparse
    from mcp.server.transport_security import TransportSecuritySettings

    public_host = urlparse(settings.response_public_base_url).hostname or "localhost"
    public_url = settings.response_public_base_url.rstrip("/")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", public_host, public_host + ":*"],
        allowed_origins=[
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
            public_url, public_url + ":*",
            *[origin.strip() for origin in (settings.mcp_client_origins or "").split(",")
              if origin.strip()],
        ],
    )


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------

def _resolve_data_path(path: Path) -> Path:
    """Resolve a possibly-relative path to an absolute path.

    Relative paths are resolved from the current working directory, which is
    why the server must be started from the repository root (see README).
    """
    if path.is_absolute():
        return path
    return Path.cwd() / path


# ---------------------------------------------------------------------------
# Help search stack (epicor_help)
# ---------------------------------------------------------------------------

def _build_help_search(settings: Settings) -> tuple["Any", "Any", "Any"]:
    """Construct the ``epicor_help`` retrieval stack.

    Returns ``(help_store, embed_client, forum_search)`` — any of which may
    be ``None``.  A missing or invalid help store logs a warning and leaves
    ``help_store=None``; ``epicor_help`` then falls back to the legacy
    ``DocsIndex`` path.
    """
    if not settings.help_store_enabled:
        return None, None, None
    help_store = None
    embed_client = None
    forum_search = None

    # Legacy hybrid help store (SQLite FTS5 + FAISS); no build script ships in this distribution.
    try:
        from epicor_mcp.index.help_store import HelpStore, HelpStoreError

        help_store_path = _resolve_data_path(settings.help_store_path)
        try:
            help_store = HelpStore(help_store_path)
            logger.info(
                "Loaded help store: %d chunks from %s", help_store.size, help_store_path
            )
        except HelpStoreError as exc:
            logger.warning(
                "Help store unavailable at %s (%s) — "
                "epicor_help falls back to the legacy docs index.",
                help_store_path,
                exc,
            )
        except Exception:
            logger.exception("Failed to load help store")
    except ImportError as exc:
        logger.warning(
            "Help store dependencies missing (%s) — "
            "epicor_help falls back to the legacy docs index.",
            exc,
        )

    # Embedding client — dense retrieval leg (OpenAI-compatible embeddings endpoint).
    if help_store is not None and settings.embed_endpoint and settings.embed_model_name:
        try:
            from epicor_mcp.index.embed_client import EmbedClient

            embed_client = EmbedClient(
                settings.embed_endpoint,
                settings.embed_model_name,
                dim=settings.embed_dim,
                sleep_after=float(settings.embed_sleep_after_s),
            )
        except ImportError as exc:
            logger.warning(
                "Embed client unavailable (%s) — epicor_help runs keyword-only.", exc
            )

    # Live epiusers.help forum search.
    if settings.forum_live_enabled:
        try:
            from epicor_mcp.index.forum_live import LiveForumSearch

            forum_search = LiveForumSearch(base_url=settings.forum_base_url)
        except ImportError as exc:
            logger.warning("Live forum search unavailable (%s)", exc)

    return help_store, embed_client, forum_search


# ---------------------------------------------------------------------------
# MCP protocol server setup (tools/resources/prompts)
# ---------------------------------------------------------------------------

class _AcceptNormalizingMiddleware:
    """Normalize Accept headers for MCP's Streamable HTTP transport.

    FastMCP requires both application/json and text/event-stream in Accept.
    Adding both lets clients that only request JSON complete the handshake;
    json_response=True still makes each reply a complete JSON response."""

    _REQUIRED = b"application/json, text/event-stream"

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = [
                (k, v) for (k, v) in scope.get("headers", []) if k.lower() != b"accept"
            ]
            headers.append((b"accept", self._REQUIRED))
            scope = dict(scope)
            scope["headers"] = headers
        await self._app(scope, receive, send)


def _init_accepts_kwarg(cls: type, name: str) -> bool:
    """Inspect constructor signatures through wrappers that accept **kwargs.

    Walk the MRO until a concrete signature decides whether a keyword is
    supported. This also handles test adapters wrapping WedgeRuntime."""
    import inspect

    for klass in getattr(cls, "__mro__", (cls,)):
        fn = klass.__dict__.get("__init__")
        if fn is None:
            continue
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):  # pragma: no cover - C-level __init__
            continue
        if name in params:
            return True
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            # The first concrete signature is authoritative: without **kwargs
            # nothing deeper in the MRO is reachable with this keyword.
            return False
    return False


def _create_mcp_server(
    settings: Settings,
    service_index: ServiceIndex,
    rbac: RBACEnforcer,
    epicor_client: EpicorClient,
    dataset_handler: DatasetHandler,
    baq_index: BAQSchemaIndex | None = None,
    docs_index: DocsIndex | None = None,
    vector_index: "Any | None" = None,
    help_store: "Any | None" = None,
    embed_client: "Any | None" = None,
    forum_search: "Any | None" = None,
    audit_logger: "Any | None" = None,
    menu_authorizer: "Any | None" = None,
) -> FastMCP:
    """Create and configure the FastMCP server with all tools registered."""
    from epicor_mcp.sql.grain import configure_keys
    configure_keys(settings.table_keys_path)

    # ------------------------------------------------------------------
    # Install operator denials before registering any tool. Query and discovery
    # paths recheck the denylist even when their metadata was cached earlier.
    from epicor_mcp.sql.denylist import install_table_blacklist_from_file

    install_table_blacklist_from_file(
        getattr(settings, "table_blacklist_path", "table_blacklist.txt")
    )

    # Instructions must match the visible tools, including before a client has
    # fetched their schemas. Keep these aligned with each tool description.
    _PUBLIC_INSTRUCTIONS = "Use epicor_tables and epicor_fields to discover actual tables and fields, then use epicor_query for read-only SELECT data retrieval. epicor_dashboards resolves saved BAQ names; epicor_help searches configured local documents. Built-in denials and user/table grants apply. Never guess tenant-specific site codes or field values."

    _LEGACY_INSTRUCTIONS = "Use epicor_tables and epicor_fields to discover actual tables and fields, then use epicor_query for read-only SELECT data retrieval. epicor_dashboards resolves saved BAQ names; epicor_help searches configured local documents. Built-in denials and user/table grants apply. Never guess tenant-specific site codes or field values."

    mcp = FastMCP(
        name="epicor-mcp-server",
        instructions=_with_plant_hints(
            _PUBLIC_INSTRUCTIONS if getattr(settings, "public_surface", False)
            else _LEGACY_INSTRUCTIONS, settings
        ),
        transport_security=_sso_transport_security(settings),
    )

    # Register all tools from the tools/ package.
    register_tools(
        mcp, service_index, rbac, epicor_client, dataset_handler,
        baq_index, docs_index, vector_index,
        help_store=help_store,
        embed_client=embed_client,
        forum_search=forum_search,
    )

    # ------------------------------------------------------------------
    # Per-session tool filtering: hide BAQ tools from non-BAQ users
    # ------------------------------------------------------------------
    # Compatibility filtering for legacy tools; the supported five-tool
    # surface also blocks calls to those tools at dispatch below.
    _BAQ_ONLY_TOOLS = frozenset({
        "epicor_baq",
    })

    # ------------------------------------------------------------------
    # Supported public surface
    # ------------------------------------------------------------------
    # Host the query runtime together with HTTP authentication and metadata.
    _PUBLIC_TOOLS = frozenset(
        {"epicor_query", "epicor_help", "epicor_tables", "epicor_fields",
         "epicor_dashboards"}
    )
    _query_runtime = None

    # Build authorization independently of optional search indexes. Missing
    # menu/service metadata must fail closed rather than remove the table gate.
    _table_authorizer = None
    try:
        from epicor_mcp.discovery import TableAuthorizer

        _table_authorizer = TableAuthorizer(
            menu_authorizer,
            service_index,
            mode=getattr(settings, "table_authz_mode", "gate"),
            dev_identity=getattr(settings, "dev_identity", ""),
        )
    except Exception:  # noqa: BLE001 - an unimportable discovery package must
        # not take the server down; but the missing gate must not be silent
        # either, because WedgeRuntime treats table_authorizer=None as gate-off.
        raise RuntimeError("Table authorization could not initialize; refusing to start without its gate")

    # SQL registration and legacy-tool visibility are separate decisions.
    from epicor_mcp.baq_ops.gate import make_can_save
    from epicor_mcp.sql.tool import register_query_tool
    from epicor_mcp.wedge_server import WedgeRuntime

    # Resolve save rights per request from the same user map as authentication.
    # Without this callback, the runtime refuses every save request.
    _runtime_kwargs: dict[str, Any] = {"can_save": make_can_save(rbac)}
    # Pass the same authorizer to the runtime and public inspection endpoints.
    # Constructor adapters may wrap the runtime in tests.
    if _init_accepts_kwarg(WedgeRuntime, "table_authorizer"):
        _runtime_kwargs["table_authorizer"] = _table_authorizer
    else:  # pragma: no cover - transitional until wedge_server lands the kwarg
        logger.warning(
            "WedgeRuntime has no table_authorizer parameter — epicor_query "
            "runs with the table gate OFF"
        )
    _query_runtime = WedgeRuntime(settings, **_runtime_kwargs)
    # Surfaced on the FastMCP object so create_app can put the SAME instance —
    # the one whose session-pinned cache actually gates queries — on app.state
    # for /health, the /admin eviction endpoints and the wiring tests. A second
    # construction there would hand admin eviction a cache nobody reads.
    mcp.epicor_table_authorizer = _table_authorizer

    # Local schema discovery is always registered, even before metadata import.
    _discovery_ready = True

    _query_registration = register_query_tool(
        mcp, settings, _query_runtime.run, discovery_available=_discovery_ready
    )
    if not _query_registration:
        logger.warning("epicor_query did NOT register: %s", _query_registration.reason)

    # -- epicor_dashboards ----------------------------------------------------
    # Dashboard lookup returns BAQ ids only. Executing those ids goes through
    # epicor_query, which checks each saved definition before reading its rows.
    from epicor_mcp.baq_ops.dashboards import register_dashboards_tool

    register_dashboards_tool(
        mcp,
        client=_query_runtime.client,
        api_key=_query_runtime.api_key,
        base_url=_query_runtime.base_url,
        query_tool_available=bool(_query_registration),
        max_bytes=int(getattr(settings, "response_max_bytes", 700_000)),
    )

    # -- epicor_tables / epicor_fields ---------------------------------------
    # Metadata lookup is available before a query and enforces its own scope.
    from epicor_mcp.index.local_retrieval import register_local_retrieval
    for name in ("epicor_help", "epicor_tables", "epicor_fields"):
        if name in mcp._tool_manager._tools:
            mcp.remove_tool(name)
    mcp.epicor_retrieval_resources = register_local_retrieval(
        mcp, settings, _table_authorizer,
        session_email=lambda: getattr(get_current_session_or_none(), "user_id", "") or "",
    )
    if getattr(settings, "public_surface", False):
        logger.info(
            "public surface active: exposing %s and hiding legacy tools.",
            sorted(_PUBLIC_TOOLS),
        )
    else:
        logger.info(
            "FULL surface active: epicor_query registered=%s alongside legacy tools.",
            bool(_query_registration),
        )

    _original_list_tools = mcp.list_tools

    async def _filtered_list_tools():
        tools = await _original_list_tools()
        if getattr(settings, "public_surface", False):
            # Applied BEFORE the session check: legacy tools must stay hidden
            # on the no-session paths (dev init, stdio) too, or the bridge's
            # initialize handshake advertises exactly the tools we are hiding.
            return [t for t in tools if t.name in _PUBLIC_TOOLS]
        session = get_current_session_or_none()
        if session is None:
            return tools  # No session (dev init, stdio) — show all
        user = rbac._user_map.get_user(session.user_id)
        if user and not user.can_write_baqs and user.access_level.value != "read_write":
            return [t for t in tools if t.name not in _BAQ_ONLY_TOOLS]
        return tools

    mcp._mcp_server.list_tools()(_filtered_list_tools)

    # ------------------------------------------------------------------
    # Restrict both listing and dispatch to the supported public surface.
    # ------------------------------------------------------------------
    # A hidden tool remains callable by name unless dispatch rejects it. Keep
    # this wrapper inside argument validation and audit logging so refusals are
    # recorded in the same way as other tool responses.
    if getattr(settings, "public_surface", False):
        from epicor_mcp.audit import _build_breaker_response
        from epicor_mcp.sql.envelope import error_envelope

        _surface_manager = mcp._tool_manager
        _surface_original = _surface_manager.call_tool

        async def _surface_gated_call_tool(name: str, arguments: dict, **kwargs):
            if name not in _PUBLIC_TOOLS:
                logger.warning(
                    "public surface: REFUSED call to hidden tool %r (surface=%s)",
                    name, sorted(_PUBLIC_TOOLS),
                )
                env = error_envelope(
                    "tool_not_available",
                    f"There is no tool named {name!r} on this server. It is not "
                    "in your tool list — if you reached for it from memory or "
                    "from a prompt, that guidance is out of date. Use the tools "
                    "you can actually see.",
                    valid={"tools": sorted(_PUBLIC_TOOLS)},
                    retry_with={"tool": "epicor_tables", "query": "<subject>"},
                    terminal=True,
                )
                return _build_breaker_response(
                    json.dumps(env), bool(kwargs.get("convert_result"))
                )
            return await _surface_original(name, arguments, **kwargs)

        _surface_manager.call_tool = _surface_gated_call_tool

    # ------------------------------------------------------------------
    # Argument-validation guard (INV-1) — MUST precede the audit hook so the
    # audit wrapper sits outside it and still sees/logs these failures.
    # ------------------------------------------------------------------
    from epicor_mcp.tools._argguard import install_validation_guard

    install_validation_guard(mcp)

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------
    if audit_logger is not None:
        from epicor_mcp.audit import install_audit_hook

        install_audit_hook(mcp, audit_logger)

    return mcp


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------

async def _close_resources(resources: Any) -> None:
    """Close indexes, embedders and the audit logger; prefer an awaitable close
    (an endpoint embedder owns an HTTP client) over a sync one."""
    for resource in resources or ():
        try:
            aclose = getattr(resource, "aclose", None)
            if aclose is not None:
                await aclose()
            elif hasattr(resource, "close"):
                resource.close()
        except Exception:  # noqa: BLE001 - shutdown must reach every resource
            logger.exception("Failed to close %r", resource)


def _build_readonly_server(settings: Settings):
    """Build the supported SSO-disabled surface without any tenant databases."""
    from epicor_mcp.rbac.table_whitelist import TableWhitelist, NoneTableAuthorizer
    from epicor_mcp.sql.denylist import install_table_blacklist_from_file
    from epicor_mcp.sql.tool import register_query_tool
    from epicor_mcp.wedge_server import WedgeRuntime
    from epicor_mcp.baq_ops.dashboards import register_dashboards_tool
    from epicor_mcp.index.local_retrieval import register_local_retrieval

    settings.validate_runtime()
    from epicor_mcp.sql.grain import configure_keys
    configure_keys(settings.table_keys_path)
    whitelist = TableWhitelist.from_file(settings.table_whitelist_path)
    authorizer = NoneTableAuthorizer(whitelist)
    install_table_blacklist_from_file(settings.table_blacklist_path)
    runtime = WedgeRuntime(settings, table_authorizer=authorizer)
    if not runtime.credentials.service_username or not runtime.credentials.service_password or not runtime.api_key:
        raise ValueError("Epicor credentials are incomplete; configure service account and BAQ API key.")
    from mcp.server.transport_security import TransportSecuritySettings
    from urllib.parse import urlparse
    public_host = urlparse(settings.response_public_base_url).hostname or "localhost"
    mcp = FastMCP(
        name="epicor-mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["localhost:*", "127.0.0.1:*", "[::1]:*", public_host, public_host + ":*"],
            allowed_origins=[o.strip() for o in settings.mcp_client_origins.split(",") if o.strip()]
                + [settings.response_public_base_url.rstrip("/"), "http://localhost:*", "http://127.0.0.1:*"],
        ),
        instructions=_with_plant_hints("Use epicor_tables and epicor_fields for metadata, epicor_query for read-only data, "
                      "epicor_dashboards for saved BAQ IDs, and epicor_help for local documents. Discover real table and field names "
                      "before composing SELECT queries. A configured table whitelist and built-in "
                      "denials apply to every query, including existing saved BAQs. Saving BAQs and "
                      "business record writes are disabled. Missing local schema metadata can be "
                      "imported with scripts/bootstrap_schema.py.", settings),
        json_response=True, stateless_http=True,
    )
    register_query_tool(mcp, settings, runtime.run, discovery_available=True)
    resources = register_local_retrieval(
        mcp, settings, authorizer,
        session_email=lambda: getattr(get_current_session_or_none(), "user_id", "") or "shared-read-only",
    )
    register_dashboards_tool(mcp, client=runtime.client, api_key=runtime.api_key,
                             base_url=runtime.base_url, query_tool_available=True)
    # Argument guard first, audit hook OUTSIDE it — the same order as the SSO
    # server — so every call, including a refused argument shape, reaches the
    # audit log. Without SSO there is no per-person identity: rows carry the
    # shared session principal, and the log still answers WHAT was asked,
    # WHEN, and whether it succeeded. The logger joins `resources` so the
    # lifespan closes it with the indexes.
    from epicor_mcp.tools._argguard import install_validation_guard
    install_validation_guard(mcp)
    audit_logger = None
    if settings.audit_log_enabled:
        from epicor_mcp.audit import AuditLogger, install_audit_hook
        audit_logger = AuditLogger(_resolve_data_path(settings.audit_log_path))
        install_audit_hook(mcp, audit_logger)
        resources.append(audit_logger)
    mcp.epicor_audit_logger = audit_logger
    mcp.epicor_table_authorizer = authorizer
    mcp.epicor_runtime = runtime
    return mcp, runtime, resources


def _create_readonly_app(settings: Settings) -> FastAPI:
    """No Azure, user-map or menu-security dependency; optional shared bearer token."""
    from contextlib import asynccontextmanager
    import secrets
    from starlette.routing import Mount

    mcp, runtime, resources = _build_readonly_server(settings)

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await runtime.client.close()
            await _close_resources(resources)

    app = FastAPI(title="Epicor MCP", lifespan=lifespan)
    app.state.settings = settings
    app.state.mcp_server = mcp
    app.state.runtime = runtime
    app.state.table_authorizer = runtime.table_authorizer
    app.state.credential_manager = runtime.credentials
    app.state.epicor_client = runtime.client
    app.state.token_validator = None
    app.state.user_map = None
    app.add_middleware(CORSMiddleware,
        allow_origins=[s.strip() for s in settings.mcp_client_origins.split(",") if s.strip()],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "MCP-Protocol-Version", "MCP-Session-Id"],
        expose_headers=["MCP-Session-Id"],
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if request.url.path.startswith(("/admin", "/oauth", "/.well-known")):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if request.method == "OPTIONS":
            return await call_next(request)
        if settings.server_token:
            supplied = request.headers.get("authorization", "")
            if not secrets.compare_digest(supplied.encode(), ("Bearer " + settings.server_token).encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        # No request-supplied identity can grant additional rights in this mode.
        set_current_session(MCPSession(session_id="shared-read-only", user_id="shared-read-only",
            department="", access_level="read_only", environment=settings.environment))
        try:
            return await call_next(request)
        finally:
            clear_current_session()

    @app.get("/health")
    async def health():
        return {"status": "ok", "auth_mode": "none", "read_only": True,
                "environment": settings.environment,
                "server_token_required": bool(settings.server_token),
                "table_whitelist_enabled": runtime.table_authorizer.whitelist.active,
                "audit_log": getattr(mcp, "epicor_audit_logger", None) is not None,
                "table_authz_mode": "gate", "public_surface": True}

    app.routes.append(Mount("", app=_AcceptNormalizingMiddleware(mcp.streamable_http_app())))
    return app


async def _run_readonly_stdio(settings: Settings) -> None:
    """Direct stdio is local no-SSO only; the connector handles hosted auth."""
    if settings.auth_mode != "none":
        raise ValueError("Azure SSO requires the hosted HTTP server and connector; direct --stdio uses AUTH_MODE=none.")
    mcp, runtime, resources = _build_readonly_server(settings)
    set_current_session(MCPSession(session_id="stdio-read-only", user_id="shared-read-only",
        department="", access_level="read_only", environment=settings.environment))
    try:
        await mcp.run_stdio_async()
    finally:
        clear_current_session()
        await runtime.client.close()
        await _close_resources(resources)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application with MCP transport and OAuth middleware."""
    if settings is None:
        settings = get_settings()

    settings.validate_runtime()
    if settings.auth_mode == "none":
        return _create_readonly_app(settings)

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # -----------------------------------------------------------------------
    # Initialize auth components
    # -----------------------------------------------------------------------
    token_validator = AzureADTokenValidator(settings)
    session_store = SessionStore()
    credential_manager = CredentialManager(settings)

    # Ensure default department keys file exists
    keys_path = _resolve_data_path(Path(settings.department_keys_path))
    credential_manager.load()
    if not credential_manager.service_username or not credential_manager.service_password or not credential_manager.get_baq_key():
        raise ValueError("Epicor credentials are incomplete; configure service account and BAQ API key.")

    # -----------------------------------------------------------------------
    # Initialize business-logic components
    # -----------------------------------------------------------------------
    users_path = _resolve_data_path(settings.users_config_path)
    dept_keys_path = _resolve_data_path(Path(settings.department_keys_path))

    user_map = UserMap(str(users_path), str(dept_keys_path))

    # Service index -- SQLite database of all Epicor services/methods/fields.
    index_db_path = _resolve_data_path(settings.service_index_path)
    if index_db_path.exists():
        service_index = ServiceIndex(index_db_path)
        logger.info("Loaded service index from %s", index_db_path)
    else:
        logger.warning(
            "Service index not found at %s. "
            "Run 'build-index' to create it.",
            index_db_path,
        )
        service_index = None  # type: ignore[assignment]

    # BAQ schema index -- SQLite database of the Epicor data dictionary.
    baq_db_path = _resolve_data_path(settings.baq_schema_path)
    if baq_db_path.exists():
        baq_index = BAQSchemaIndex(baq_db_path)
        logger.info("Loaded BAQ schema index from %s", baq_db_path)
    else:
        logger.warning(
            "BAQ schema index not found at %s. "
            "Run 'python scripts/build_baq_index.py' to create it.",
            baq_db_path,
        )
        baq_index = None  # type: ignore[assignment]

    # Documentation index -- SQLite database of PDF guides, forum posts, videos.
    docs_db_path = _resolve_data_path(settings.docs_db_path)
    docs_index: DocsIndex | None = None
    if docs_db_path.exists():
        try:
            docs_index = DocsIndex(docs_db_path)
            logger.info("Loaded documentation index from %s", docs_db_path)
        except Exception as exc:
            logger.warning("Failed to load documentation index: %s", exc)
    else:
        logger.warning(
            "Documentation index not found at %s. "
            "Run 'python scripts/build_docs_index.py' to create it.",
            docs_db_path,
        )

    # Vector index — semantic search via an optional legacy FAISS store
    vector_index = None
    if settings.vector_search_enabled:
        try:
            from epicor_mcp.index.vector_index import VectorIndex

            vs_path = settings.vector_store_path
            if (vs_path / "index.faiss").exists() and (vs_path / "data.json").exists():
                vector_index = VectorIndex(vs_path, settings.embedding_model)
                vector_index.load()
                logger.info(
                    "Loaded vector index: %d vectors from %s",
                    vector_index.total_vectors,
                    vs_path,
                )
                # Pre-warm the embedding model + reranker off the request
                # path so the first epicor_help call doesn't pay ~1.2s of
                # cold-start latency.
                import threading
                threading.Thread(
                    target=vector_index.prewarm,
                    name="vector-index-prewarm",
                    daemon=True,
                ).start()
            else:
                logger.warning(
                    "Vector store files not found at %s. Semantic search disabled.",
                    vs_path,
                )
        except ImportError:
            logger.warning(
                "faiss-cpu or sentence-transformers not installed. "
                "Semantic search disabled. Install with: pip install faiss-cpu sentence-transformers"
            )
        except Exception:
            logger.exception("Failed to load vector index")

    # Epicor HTTP client
    epicor_client = EpicorClient(
        username=credential_manager.service_username,
        password=credential_manager.service_password,
        company_id=settings.epicor_company_id,
        base_url=settings.epicor_base_url,
    )

    # Dataset workflow handler
    dataset_handler = DatasetHandler(epicor_client, service_index) if service_index else None  # type: ignore[arg-type]

    # Epicor user resolver — queries Epicor to map user emails to departments
    epicor_resolver = EpicorUserResolver(
        base_url=settings.epicor_base_url,
        username=credential_manager.service_username,
        password=credential_manager.service_password,
        api_key=credential_manager.get_admin_key() or "",
        company_id=settings.epicor_company_id,
        group_map=user_map.epicor_group_to_department,
    )

    # -----------------------------------------------------------------------
    # Audit logger — built before the RBAC enforcer so it can
    # serve as the shadow-divergence sink.
    # -----------------------------------------------------------------------
    audit_logger = None
    try:
        from epicor_mcp.audit import AuditLogger

        audit_db_path = _resolve_data_path(settings.audit_log_path)
        audit_logger = AuditLogger(audit_db_path)
    except Exception:
        logger.exception("Failed to initialize audit logger")

    # -----------------------------------------------------------------------
    # Menu-derived RBAC: authz client -> menu-map store -> authorizer.
    # Authorization reads always go to LIVE (pilot security is stale). The
    # enforcer reads the cached snapshot synchronously; the middleware primes it.
    # -----------------------------------------------------------------------
    menu_authorizer = None
    authz_client = None
    if _MENU_AUTHZ_AVAILABLE and service_index:
        authz_live_url = settings.menu_authz_live_url or settings.epicor_live_url
        authz_client = EpicorAuthzClient(
            base_url=authz_live_url,
            username=credential_manager.service_username,
            password=credential_manager.service_password,
            api_key=credential_manager.get_admin_key() or "",
        )
        menu_map_store = MenuMapStore(_resolve_data_path(Path(settings.menu_map_db_path)))
        if not menu_map_store.is_loaded():
            if settings.menu_authz_mode == "enforce":
                raise RuntimeError(
                    "menu_authz_mode='enforce' but the menu-security map is not "
                    f"loaded at {settings.menu_map_db_path}. Run "
                    "scripts/build_menu_map.py first, or switch to shadow/off."
                )
            logger.error(
                "Menu-security map not loaded at %s — menu authorization will "
                "fail closed for every non-SecurityMgr user. Running in %s mode.",
                settings.menu_map_db_path, settings.menu_authz_mode,
            )
        menu_authorizer = MenuAuthorizer(
            authz_client,
            menu_map_store,
            ttl_seconds=settings.menu_authz_ttl_seconds,
            stale_grace_seconds=settings.menu_authz_stale_grace_seconds,
        )
    elif settings.menu_authz_mode != "off":
        msg = (
            "Menu-derived RBAC modules unavailable (import failed); "
            f"menu_authz_mode='{settings.menu_authz_mode}' cannot run."
        )
        if settings.menu_authz_mode == "enforce":
            raise RuntimeError(msg)
        logger.error("%s Falling back to legacy department enforcement.", msg)

    # Shadow-divergence sink: the enforcer calls this whenever the legacy and
    # menu decisions disagree; it lands in audit.db's authz_shadow_divergence.
    def _record_divergence(div: dict) -> None:
        if audit_logger is None:
            return
        audit_logger.log_shadow_divergence(
            user=div.get("user_id", ""),
            service=div.get("service_id", ""),
            tool="",
            dept_decision=bool(div.get("dept_allowed")),
            menu_decision=bool(div.get("menu_allowed")),
            reason=div.get("reason", ""),
        )

    # RBAC enforcer — menu-derived decision source with the legacy fallback.
    # Degrade to "off" if the authz stack could not be constructed.
    effective_mode = settings.menu_authz_mode if menu_authorizer is not None else "off"
    rbac = (
        RBACEnforcer(
            service_index,
            user_map,
            authorizer=menu_authorizer,
            mode=effective_mode,
            record_divergence=_record_divergence,
        )
        if service_index
        else None
    )  # type: ignore[arg-type]

    # -----------------------------------------------------------------------
    # Purge stale offloaded response files
    # -----------------------------------------------------------------------
    try:
        from epicor_mcp.response import purge_stale_offloads

        offload_dir = settings.response_offload_dir
        if offload_dir and str(offload_dir).strip():
            purged = purge_stale_offloads(
                offload_dir, settings.response_offload_retention_hours
            )
            if purged:
                logger.info(
                    "Purged %d stale offloaded response file(s) from %s",
                    purged,
                    offload_dir,
                )
    except Exception:
        logger.exception("Failed to purge stale offload files")

    # Help search stack — hybrid help store + embed client + live forum
    # search for epicor_help (legacy docs_index remains the fallback).
    help_store, embed_client, forum_search = _build_help_search(settings)

    # -----------------------------------------------------------------------
    # Create the MCP server with tools
    # -----------------------------------------------------------------------
    if service_index and rbac and dataset_handler:
        mcp_server = _create_mcp_server(
            settings, service_index, rbac, epicor_client, dataset_handler,
            baq_index=baq_index if baq_index else None,
            docs_index=docs_index,
            vector_index=vector_index,
            help_store=help_store,
            embed_client=embed_client,
            forum_search=forum_search,
            audit_logger=audit_logger,
            menu_authorizer=menu_authorizer,
        )
    else:
        mcp_server = FastMCP(
            name="epicor-mcp-server",
            instructions=_with_plant_hints(
                "Service index not built yet. Run 'build-index' to enable tools.", settings
            ),
            transport_security=_sso_transport_security(settings),
        )

    # -----------------------------------------------------------------------
    # Plain-JSON, stateless transport
    # -----------------------------------------------------------------------
    # Each JSON-RPC message POSTs and gets a COMPLETE JSON reply: no SSE stream
    # to hold open through Apache, and no mcp-session-id for the client to
    # capture and resend. The SSE default is what a URL-only connector (no stdio
    # bridge) times out on — the symptom is an AbortError on `initialize` with
    # nothing logged server-side, because the request never completes.
    # Set defensively in case an older mcp lacks the attributes.
    for _attr, _value in (("json_response", True), ("stateless_http", True)):
        if hasattr(mcp_server.settings, _attr):
            setattr(mcp_server.settings, _attr, _value)
        else:  # pragma: no cover - version guard
            logger.warning(
                "mcp.settings has no %s; a URL-only client may need SSE/session handling",
                _attr,
            )

    # Get the Streamable HTTP ASGI app from FastMCP
    mcp_asgi_app = _AcceptNormalizingMiddleware(mcp_server.streamable_http_app())

    # -----------------------------------------------------------------------
    # FastAPI app with lifespan to manage MCP session manager
    # -----------------------------------------------------------------------
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            # Start vector index hot-reload watcher if available
            reload_task = None
            if vector_index is not None:
                from epicor_mcp.index.vector_index import watch_for_reload

                signal_path = settings.vector_store_path / "reload.signal"
                reload_task = asyncio.create_task(
                    watch_for_reload(vector_index, signal_path)
                )
            logger.info("MCP session manager started")
            yield
            if reload_task:
                reload_task.cancel()
                try:
                    await reload_task
                except asyncio.CancelledError:
                    pass
            if vector_index is not None:
                vector_index.close()
            if help_store is not None and hasattr(help_store, "close"):
                help_store.close()
            for closable in (embed_client, forum_search, authz_client):
                if closable is not None:
                    try:
                        await closable.aclose()
                    except Exception:
                        logger.exception("Failed to close %r", closable)
            await _close_resources(getattr(mcp_server, "epicor_retrieval_resources", None))
            if audit_logger is not None:
                audit_logger.close()
        logger.info("MCP session manager stopped")

    app = FastAPI(
        title="Epicor Kinetic MCP Server",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS middleware for Claude Desktop and other MCP clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )

    # Store components in app state
    app.state.settings = settings
    app.state.token_validator = token_validator
    app.state.session_store = session_store
    app.state.credential_manager = credential_manager
    app.state.user_map = user_map
    app.state.service_index = service_index
    app.state.rbac = rbac
    app.state.epicor_client = epicor_client
    app.state.menu_authorizer = menu_authorizer
    app.state.authz_client = authz_client
    # The SAME TableAuthorizer the tools were wired with, surfaced off the
    # FastMCP object _create_mcp_server built (None when the no-index fallback
    # server was built instead). Its scope cache is SESSION-PINNED — no TTL —
    # so the /admin eviction endpoints below and a restart are the ONLY two
    # refresh paths; they read it from app.state at request time, which is also
    # what lets the wiring tests substitute a recorder.
    app.state.table_authorizer = getattr(mcp_server, "epicor_table_authorizer", None)

    # -----------------------------------------------------------------------
    # Menu-authz middleware helpers
    # -----------------------------------------------------------------------
    #: How long a request may wait for the menu-authz snapshot before giving up
    #: and letting it finish in the background. The warm path takes single-digit
    #: milliseconds, so this only ever bites on a cold or expired cache.
    _SNAPSHOT_INLINE_BUDGET_S = 1.0

    #: Share a rebuild task per identity so concurrent requests do not multiply
    #: the Epicor paging load when a cached snapshot expires.
    _snapshot_tasks: dict[str, Any] = {}

    async def _ensure_snapshot_safe(email: str) -> None:
        """Prime menu authorization with a bounded wait on the request path.

        Menu and security reads can require several network pages. Share one
        task per identity, wait at most _SNAPSHOT_INLINE_BUDGET_S for a new
        task, and let unfinished work continue in the background. Authorization
        gates still refuse access when the required snapshot is unavailable."""
        if menu_authorizer is None:
            return

        async def _prime() -> None:
            try:
                await menu_authorizer.ensure_snapshot(email)
            except Exception:
                logger.exception("ensure_snapshot failed for %s", email)

        task = _snapshot_tasks.get(email)
        if task is None or task.done():
            task = asyncio.create_task(_prime())
            # Hold a reference: a bare create_task can be garbage-collected
            # mid-flight, which would silently abandon the rebuild.
            _snapshot_tasks[email] = task
            task.add_done_callback(
                lambda t, e=email: _snapshot_tasks.pop(e, None)
            )
            budget = _SNAPSHOT_INLINE_BUDGET_S
        else:
            # A rebuild is already running for this identity. Do not start a
            # second one and do not sit behind it — the whole point is that the
            # request does not depend on it.
            budget = 0.0

        if budget:
            done, _pending = await asyncio.wait({task}, timeout=budget)
            if not done:
                logger.info(
                    "menu-authz snapshot for %s is cold; continuing without it "
                    "and rebuilding in the background (request NOT blocked)",
                    email,
                )

    #: Strong task references keep background table-scope lookups alive.
    #: No per-email dedup here on purpose: the authorizer's own single-flight
    #: makes a duplicate prime cheap and its session-pinned cache makes a
    #: repeat free.
    _scope_prime_tasks: set[Any] = set()

    def _prime_table_scope(email: str) -> None:
        """Start a table-scope lookup as soon as authentication identifies a user.

        The lookup runs in the background; scheduling and lookup errors are
        logged without delaying the request. Actual tool gates fail closed
        until a usable scope exists. Only gate mode needs this early lookup;
        the authorizer handles concurrent requests for the same identity."""
        try:
            _ta = getattr(app.state, "table_authorizer", None)
            if _ta is None or getattr(_ta, "mode", "") != "gate":
                return
            # Resolve through the authorizer so the prime warms the SAME key
            # the gate will read (EPICOR_MCP_DEV_IDENTITY outranks the session
            # inside resolve_identity; in production they are identical).
            target = _ta.resolve_identity(session_email=(email or "").strip())
            if not (target or "").strip():
                return

            async def _prime_scope() -> None:
                try:
                    await _ta.scope_for(target)
                except Exception:  # noqa: BLE001 — the prime must never surface
                    logger.exception("table-scope prime failed for %s", target)

            task = asyncio.create_task(_prime_scope())
            _scope_prime_tasks.add(task)
            task.add_done_callback(_scope_prime_tasks.discard)
        except Exception:  # noqa: BLE001 — scheduling must never fail the request
            logger.exception("table-scope prime could not be scheduled for %s", email)

    async def _provision_user(validated: Any) -> Any:
        """Auto-provision an unknown user's informational profile.

        Both identity sources use the operator's department map. BAQ-write
        rights are resolved separately from the existing authoring groups;
        department labels do not grant table or save access. Explicit profiles
        are checked by the middleware before this function is called.
        """
        if authz_client is not None:
            try:
                identity = await authz_client.fetch_user(validated.user_id)
            except Exception:
                logger.exception("authz fetch_user failed for %s", validated.user_id)
                identity = None
            if identity is not None and not getattr(identity, "disabled", False):
                groups = getattr(identity, "groups", ())
                depts = _departments_from_groups(groups, user_map.epicor_group_to_department)
                if depts:
                    claims = dict(validated.claims)
                    claims["_epicor_departments"] = depts
                    claims["_can_write_baqs"] = _can_write_baqs_from_groups(groups)
                    claims["name"] = getattr(identity, "name", "") or validated.user_id
                    claims["preferred_username"] = (
                        getattr(identity, "email", "") or validated.user_id
                    )
                    return user_map.get_or_create_user_from_claims(
                        validated.user_id, claims
                    )

        # Legacy fallback (EpicorUserResolver is deprecated under menu-authz).
        epicor_info = await epicor_resolver.resolve_user(validated.user_id)
        if epicor_info and epicor_info["departments"]:
            claims = dict(validated.claims)
            claims["_epicor_departments"] = epicor_info["departments"]
            claims["_can_write_baqs"] = epicor_info.get("can_write_baqs", False)
            claims["name"] = epicor_info["name"]
            claims["preferred_username"] = epicor_info["email"]
            return user_map.get_or_create_user_from_claims(validated.user_id, claims)
        return None

    # -----------------------------------------------------------------------
    # Authentication middleware
    # -----------------------------------------------------------------------
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next: Any) -> Response:
        """Validate Bearer tokens and set the session context for MCP tools."""
        # Skip auth for public/admin endpoints (admin endpoints validate their own tokens)
        if request.url.path.startswith(
            (
                "/health",
                "/.well-known/",
                "/oauth/",
                "/admin/",
                "/response-files/",
            )
        ):
            return await call_next(request)

        async def _serve_as_dev_user():
            """The dev-mode session path, or None if dev mode cannot supply one.

            Shared by the no-token and the unvalidatable-token branches — see the
            note in the exception handler for why those two must agree.
            """
            if not (settings.dev_mode and user_map):
                return None
            first_user = user_map.get_first_user()
            if not first_user:
                return None
            dev_session = MCPSession(
                session_id="dev-session",
                user_id=first_user.user_id,
                department=first_user.department,
                access_level=first_user.access_level.value,
                environment=first_user.environment,
            )
            # Prime the menu-authz snapshot for the dev user (fail-open;
            # per-tool checks fail closed on their own).
            await _ensure_snapshot_safe(first_user.user_id)
            ctx_token = set_current_session(dev_session)
            try:
                return await call_next(request)
            finally:
                clear_current_session(ctx_token)

        # Extract Bearer token
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            # Dev mode: create a default session from the first user in users.json
            dev_response = await _serve_as_dev_user()
            if dev_response is not None:
                return dev_response

            # Production mode: return 401 so mcp-remote initiates OAuth
            if request.url.path.startswith("/mcp"):
                return Response(
                    content=json.dumps({"error": "Authentication required"}),
                    status_code=401,
                    media_type="application/json",
                    headers={
                        "WWW-Authenticate": 'Bearer resource_metadata="/.well-known/oauth-protected-resource"',
                    },
                )
            return await call_next(request)

        token = auth_header[7:]

        try:
            validated = await token_validator.validate_token(token)
        except (JWTError, RuntimeError) as exc:
            logger.warning("Token validation failed: %s", exc)
            return Response(
                content=json.dumps({"error": "Invalid or expired token"}),
                status_code=401,
                media_type="application/json",
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="/.well-known/oauth-protected-resource"',
                },
            )

        # Look up user — first in users.json, then auto-provision from Epicor.
        user_profile = user_map.get_user(validated.user_id) if user_map else None
        if user_profile is None and user_map:
            user_profile = await _provision_user(validated)
        if user_profile is None:
            logger.warning("No configured profile or mapped Epicor department for %s", validated.user_id)
            return Response(
                content=json.dumps({"error": "No configured user profile or mapped Epicor department was found. Contact your administrator."}),
                status_code=403,
                media_type="application/json",
            )

        session_id = request.headers.get("mcp-session-id", validated.user_id)
        session = session_store.get_session(session_id)
        if session is not None and (
            str(session.user_id or "").lower()
            != str(validated.user_id or "").lower()
        ):
            # A session id is a lookup key, never an identity: the header is
            # caller-supplied and the default key is the bare e-mail, so
            # honoring a foreign session here would let ANY valid bearer run
            # every session-scoped decision — including the table-authz gate,
            # which reads `session.user_id` — as whoever's session id it
            # guessed. Fall back to the caller's own key instead; their next
            # request simply recreates their session, and the foreign session
            # is left untouched for its real owner.
            logger.warning(
                "mcp-session-id %r belongs to %s but the bearer token validated "
                "as %s — refusing the foreign session",
                session_id, session.user_id, validated.user_id,
            )
            session_id = validated.user_id
            session = session_store.get_session(session_id)
        if session is None:
            session = session_store.create_session(
                session_id=session_id,
                validated_token=validated,
                user_config={
                    "department": user_profile.department,
                    "access_level": user_profile.access_level.value,
                },
                environment=user_profile.environment,  # type: ignore[arg-type]
            )

        # Sync session with live user profile (picks up hot-swapped access levels)
        session.access_level = user_profile.access_level.value
        session.department = user_profile.department

        # Prime the menu-authz snapshot for this user (fail-open; per-tool
        # checks fail closed on their own).
        await _ensure_snapshot_safe(validated.user_id)
        # Start the table-scope lookup in the background; it is never
        # awaited — by the model's first tool call the scope is pinned
        # (SecurityMgr) or warming (scoped user, shared via single-flight).
        _prime_table_scope(validated.user_id)

        ctx_token = set_current_session(session)
        try:
            response = await call_next(request)
        finally:
            clear_current_session(ctx_token)

        return response

    # -----------------------------------------------------------------------
    # Public routes
    # -----------------------------------------------------------------------
    @app.get("/health")
    async def health_check() -> dict[str, Any]:
        # `table_authz_mode` is the mode as the authorizer NORMALISED it (an
        # invalid env value falls back to gate, not boost); "unwired" means no
        # authorizer was constructed at all — under the public surface that is a
        # fail-CLOSED state for ad-hoc SQL, not an open one.
        _ta = getattr(app.state, "table_authorizer", None)
        return {
            "status": "healthy",
            "server": "epicor-mcp-server",
            "version": __version__,
            "environment": settings.environment,
            "active_sessions": session_store.active_count,
            "departments": credential_manager.departments,
            "index_loaded": service_index is not None,
            "table_authz_mode": _ta.mode if _ta is not None else "unwired",
        }

    # Serve offloaded tool-response files.  Filenames carry 128 bits of
    # entropy, so knowledge of the URL is the access capability.  Files
    # are purged by retention at startup.  The endpoint is auth-skipped
    # (see middleware below) so a separate sandbox can curl the URL.
    _RESPONSE_FILE_RE = re.compile(
        r"^\d{8}T\d{6}Z-[0-9a-f]{32}\.json$"
    )

    @app.get("/response-files/{filename}")
    async def get_response_file(filename: str) -> Response:
        if not _RESPONSE_FILE_RE.match(filename):
            return Response(
                content=json.dumps({"error": "Invalid filename"}),
                status_code=404,
                media_type="application/json",
            )
        offload_dir = settings.response_offload_dir
        if not offload_dir or not str(offload_dir).strip():
            return Response(
                content=json.dumps({"error": "Offload disabled"}),
                status_code=404,
                media_type="application/json",
            )
        file_path = Path(offload_dir) / filename
        try:
            resolved = file_path.resolve(strict=True)
            resolved.relative_to(Path(offload_dir).resolve())
        except (OSError, ValueError):
            return Response(
                content=json.dumps({"error": "Not found"}),
                status_code=404,
                media_type="application/json",
            )
        return Response(
            content=resolved.read_bytes(),
            media_type="application/json",
            headers={"Cache-Control": "private, max-age=0, no-store"},
        )

    # ------------------------------------------------------------------
    # Admin endpoints — hot-swap user access without restarting
    # ------------------------------------------------------------------

    # Admin emails that can use /admin/ endpoints via OAuth token.
    _ADMIN_EMAILS = {email.strip().lower() for email in settings.azure_admin_emails.split(",") if email.strip()}

    async def _check_admin(request: Request) -> str | None:
        """Validate the request is from an admin. Returns user_id or None.

        Supports two auth methods:
        1. X-Admin-Secret header matching EPICOR_MCP_ADMIN_SECRET env var
        2. Bearer token from an admin email in _ADMIN_EMAILS
        """
        # Method 1: shared secret (simplest for curl from the server)
        admin_secret = request.headers.get("x-admin-secret", "")
        if admin_secret and settings.admin_secret and admin_secret == settings.admin_secret:
            return "admin-via-secret"

        # Method 2: OAuth token from an admin user
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:]
        try:
            validated = await token_validator.validate_token(token)
            if validated.user_id.lower() in _ADMIN_EMAILS:
                return validated.user_id.lower()
        except Exception:
            pass
        return None

    @app.get("/admin/users")
    async def admin_list_users(request: Request) -> Response:
        """List all cached user profiles and their current access levels."""
        admin = await _check_admin(request)
        if not admin:
            return Response(
                content=json.dumps({"error": "Admin access required"}),
                status_code=403,
                media_type="application/json",
            )

        users = user_map.list_cached_users()
        result = []
        for u in users:
            all_depts = [u.department] + (u.extra_departments or [])
            result.append({
                "user_id": u.user_id,
                "display_name": u.display_name,
                "epicor_username": u.epicor_username,
                "departments": all_depts,
                "access_level": u.access_level.value,
                "can_write_baqs": u.can_write_baqs,
                "environment": u.environment,
            })

        return Response(
            content=json.dumps({"users": result, "count": len(result)}, indent=2),
            status_code=200,
            media_type="application/json",
        )

    @app.post("/admin/users/{email}/access")
    async def admin_update_access(email: str, request: Request) -> Response:
        """Hot-swap a user's access_level and/or can_write_baqs.

        JSON body (all fields optional):
            {"access_level": "read_write", "can_write_baqs": true}
        """
        admin = await _check_admin(request)
        if not admin:
            return Response(
                content=json.dumps({"error": "Admin access required"}),
                status_code=403,
                media_type="application/json",
            )

        body = await request.json()
        access_level_str = body.get("access_level")
        can_write_baqs = body.get("can_write_baqs")

        from epicor_mcp.rbac.enforcer import AccessLevel
        access_level = None
        if access_level_str:
            try:
                access_level = AccessLevel(access_level_str)
            except ValueError:
                return Response(
                    content=json.dumps({
                        "error": f"Invalid access_level: {access_level_str}. Use 'read_only' or 'read_write'."
                    }),
                    status_code=400,
                    media_type="application/json",
                )

        updated = user_map.update_user_access(
            email.lower(),
            access_level=access_level,
            can_write_baqs=can_write_baqs,
        )

        if updated is None:
            return Response(
                content=json.dumps({
                    "error": f"User '{email}' not found in cache. They need to authenticate first."
                }),
                status_code=404,
                media_type="application/json",
            )

        all_depts = [updated.department] + (updated.extra_departments or [])
        logger.info(
            "Admin %s changed %s: access_level=%s can_write_baqs=%s",
            admin, email, updated.access_level.value, updated.can_write_baqs,
        )
        return Response(
            content=json.dumps({
                "user_id": updated.user_id,
                "display_name": updated.display_name,
                "access_level": updated.access_level.value,
                "can_write_baqs": updated.can_write_baqs,
                "departments": all_depts,
                "note": "Change is immediate and in-memory. Survives until server restart or /admin/reload.",
            }),
            status_code=200,
            media_type="application/json",
        )

    @app.post("/admin/reload")
    async def admin_reload(request: Request) -> Response:
        """Reload users.json and department keys from disk. Clears cached auto-resolved users."""
        admin = await _check_admin(request)
        if not admin:
            return Response(
                content=json.dumps({"error": "Admin access required"}),
                status_code=403,
                media_type="application/json",
            )

        try:
            user_map.reload()
        except (ValueError, OSError) as exc:
            return JSONResponse(
                {"error": "invalid_configuration", "message": str(exc)},
                status_code=400,
            )
        epicor_resolver.set_group_map(user_map.epicor_group_to_department)
        # Also drop every cached menu-authz snapshot so access re-derives from
        # Epicor on the next request (menus/security refetch on next TTL miss).
        if menu_authorizer is not None:
            menu_authorizer.evict_all()
        # Pinned table scopes have no TTL, so every
        # menu-snapshot eviction must take the table scopes with it — a scope
        # derived from an evicted snapshot would otherwise outlive it forever.
        _ta = getattr(request.app.state, "table_authorizer", None)
        if _ta is not None:
            _ta.evict_all()
        logger.info("Admin %s triggered config reload", admin)
        return Response(
            content=json.dumps({
                "status": "reloaded",
                "note": "users.json and department keys reloaded. Auto-resolved users, menu-authz snapshots and pinned table scopes cleared — they will re-resolve on next request.",
            }),
            status_code=200,
            media_type="application/json",
        )

    @app.post("/admin/users/{email}/evict")
    async def admin_evict_user(email: str, request: Request) -> Response:
        """Remove a single user from the in-memory cache, forcing re-resolution on next request."""
        admin = await _check_admin(request)
        if not admin:
            return Response(
                content=json.dumps({"error": "Admin access required"}),
                status_code=403,
                media_type="application/json",
            )

        key = email.lower()
        user = user_map.get_user(key)
        if user is None:
            return Response(
                content=json.dumps({"error": f"User '{email}' not in cache."}),
                status_code=404,
                media_type="application/json",
            )

        user_map._users.pop(key, None)
        # Force the menu-authz snapshot to recompute too (instant propagation
        # of an Epicor menu/group change ahead of the TTL window).
        if menu_authorizer is not None:
            menu_authorizer.evict(key)
        # And the session-pinned table scope, which has no TTL at all.
        _ta = getattr(request.app.state, "table_authorizer", None)
        if _ta is not None:
            _ta.evict(key)
        logger.info("Admin %s evicted %s from cache", admin, email)
        return Response(
            content=json.dumps({
                "status": "evicted",
                "user_id": key,
                "note": "User will be re-resolved from Epicor (profile + menu-authz snapshot + table scope) on their next request.",
            }),
            status_code=200,
            media_type="application/json",
        )

    # ------------------------------------------------------------------
    # Menu-derived RBAC admin endpoints
    # ------------------------------------------------------------------

    @app.post("/admin/authz/evict/{email}")
    async def admin_authz_evict(email: str, request: Request) -> Response:
        """Drop a single user's cached menu-authz snapshot (instant recompute)."""
        admin = await _check_admin(request)
        if not admin:
            return Response(
                content=json.dumps({"error": "Admin access required"}),
                status_code=403,
                media_type="application/json",
            )
        if menu_authorizer is None:
            return Response(
                content=json.dumps({"error": "Menu-derived RBAC is not enabled."}),
                status_code=409,
                media_type="application/json",
            )
        key = email.lower()
        menu_authorizer.evict(key)
        # The pinned table scope rides on the snapshot and has NO TTL — this
        # evict + /admin/reload + a restart are its only refresh paths.
        _ta = getattr(request.app.state, "table_authorizer", None)
        if _ta is not None:
            _ta.evict(key)
        logger.info("Admin %s evicted menu-authz snapshot for %s", admin, key)
        return Response(
            content=json.dumps({
                "status": "evicted",
                "user": key,
                "note": "Menu-authz snapshot and pinned table scope will recompute from Epicor on the next request.",
            }),
            status_code=200,
            media_type="application/json",
        )

    @app.post("/admin/authz/refresh")
    async def admin_authz_refresh(request: Request) -> Response:
        """Refetch the tenant menus/security and drop all cached snapshots."""
        admin = await _check_admin(request)
        if not admin:
            return Response(
                content=json.dumps({"error": "Admin access required"}),
                status_code=403,
                media_type="application/json",
            )
        if menu_authorizer is None:
            return Response(
                content=json.dumps({"error": "Menu-derived RBAC is not enabled."}),
                status_code=409,
                media_type="application/json",
            )
        try:
            await menu_authorizer.refresh_tenant()
            menu_authorizer.evict_all()
            # Every pinned table scope was projected off the snapshots that
            # were just dropped — they go with them (no TTL of their own).
            _ta = getattr(request.app.state, "table_authorizer", None)
            if _ta is not None:
                _ta.evict_all()
        except Exception:
            logger.exception("Menu-authz tenant refresh failed")
            return Response(
                content=json.dumps({"error": "Tenant refresh failed; see server logs."}),
                status_code=502,
                media_type="application/json",
            )
        logger.info("Admin %s refreshed menu-authz tenant data", admin)
        return Response(
            content=json.dumps({
                "status": "refreshed",
                "note": "Tenant menus/security refetched; all snapshots and pinned table scopes evicted.",
            }),
            status_code=200,
            media_type="application/json",
        )

    @app.get("/admin/authz/shadow-report")
    async def admin_authz_shadow_report(request: Request) -> Response:
        """Return recent shadow-mode legacy-vs-menu divergences."""
        admin = await _check_admin(request)
        if not admin:
            return Response(
                content=json.dumps({"error": "Admin access required"}),
                status_code=403,
                media_type="application/json",
            )
        if audit_logger is None:
            return Response(
                content=json.dumps({"error": "Audit log is not available."}),
                status_code=409,
                media_type="application/json",
            )
        rows = audit_logger.shadow_report()
        return Response(
            content=json.dumps({
                "mode": settings.menu_authz_mode,
                "count": len(rows),
                "divergences": rows,
            }, indent=2),
            status_code=200,
            media_type="application/json",
        )

    @app.get("/admin/authz/{email}")
    async def admin_authz_inspect(email: str, request: Request) -> Response:
        """Dump a user's menu-authz snapshot: grants, staleness, ages."""
        admin = await _check_admin(request)
        if not admin:
            return Response(
                content=json.dumps({"error": "Admin access required"}),
                status_code=403,
                media_type="application/json",
            )
        if menu_authorizer is None:
            return Response(
                content=json.dumps({"error": "Menu-derived RBAC is not enabled."}),
                status_code=409,
                media_type="application/json",
            )
        key = email.lower()
        try:
            # Prime a snapshot if none is cached, then explain it.
            if menu_authorizer.get_snapshot(key) is None:
                await menu_authorizer.ensure_snapshot(key)
            report = menu_authorizer.explain(key)
            # Report the table scope beside the menu
            # chain that feeds it. Computed on demand, same as the snapshot
            # priming above — scope_for pins any successful answer, which is
            # exactly what the user's next tool call would have done anyway;
            # UNAVAILABLE is never pinned, so `pinned` can be False under a
            # real state.
            _ta = getattr(request.app.state, "table_authorizer", None)
            if _ta is None:
                report["table_scope"] = {
                    "mode": "unwired",
                    "state": "not_computed",
                    "tables": None,
                    "pinned": False,
                }
            else:
                _scope = await _ta.scope_for(key)
                report["table_scope"] = {
                    "mode": _ta.mode,
                    "state": _scope.state.value,
                    "tables": (
                        len(_scope.tables) if _scope.tables is not None else None
                    ),
                    # The private read is deliberate: the authorizer exposes no
                    # peek (evict() is destructive) and pinned-ness cannot be
                    # inferred from the state — mode=off answers UNLIMITED
                    # without caching, UNAVAILABLE is never cached.
                    "pinned": key in getattr(_ta, "_cache", {}),
                    "reason": _scope.reason,
                }
        except Exception:
            logger.exception("Menu-authz inspect failed for %s", key)
            return Response(
                content=json.dumps({"error": "Failed to inspect snapshot; see server logs."}),
                status_code=502,
                media_type="application/json",
            )
        return Response(
            content=json.dumps(report, indent=2, default=str),
            status_code=200,
            media_type="application/json",
        )

    # ------------------------------------------------------------------
    # OAuth proxy — our server IS the authorization server from
    # mcp-remote's perspective. We proxy to Azure AD behind the scenes.
    # This avoids all Azure AD redirect_uri / response_mode issues.
    # ------------------------------------------------------------------

    # Pending auth requests (state -> client redirect_uri + PKCE) — in-memory, short-lived
    _pending_auth: dict[str, dict[str, str]] = {}

    # Short-lived cache of recently-rotated refresh tokens.
    # When a refresh succeeds the old proxy token is deleted; if the client
    # sends a duplicate request with the same old token (race condition),
    # we return the cached response instead of 400.
    import time as _time_mod
    _REFRESH_CACHE_TTL = 30  # seconds
    _refresh_cache: dict[str, tuple[float, str]] = {}  # old_token -> (timestamp, json_response)

    # Persistent refresh token store — survives server restarts
    import sqlite3 as _sqlite3
    _refresh_db_path = _resolve_data_path(Path("data/refresh_tokens.db"))
    _refresh_db = _sqlite3.connect(str(_refresh_db_path))
    _refresh_db.execute(
        "CREATE TABLE IF NOT EXISTS tokens "
        "(our_token TEXT PRIMARY KEY, azure_token TEXT, user_id TEXT, created_at REAL)"
    )
    _refresh_db.commit()

    def _store_refresh(our_token: str, azure_token: str, user_id: str = "") -> None:
        import time as _t
        _refresh_db.execute(
            "INSERT OR REPLACE INTO tokens VALUES (?, ?, ?, ?)",
            (our_token, azure_token, user_id, _t.time()),
        )
        _refresh_db.commit()

    def _get_refresh(our_token: str) -> str | None:
        row = _refresh_db.execute(
            "SELECT azure_token FROM tokens WHERE our_token = ?", (our_token,)
        ).fetchone()
        return row[0] if row else None

    def _delete_refresh(our_token: str) -> None:
        _refresh_db.execute("DELETE FROM tokens WHERE our_token = ?", (our_token,))
        _refresh_db.commit()

    def _oauth_unavailable_in_dev_mode() -> Response | None:
        """Suppress OAuth metadata for a legacy dev configuration without Azure.

        Supported Settings reject dev mode, but adapters may still exercise
        this compatibility guard. An incomplete tenant/client configuration
        cannot finish OAuth. Returning the 404 through the application keeps
        its CORS headers available to browser clients."""
        if not getattr(settings, "dev_mode", False):
            return None
        if getattr(settings, "azure_tenant_id", "") and getattr(
            settings, "azure_client_id", ""
        ):
            return None
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "detail": (
                    "This MCP instance runs in dev mode and requires no OAuth. "
                    "Connect directly to /mcp with no Authorization header."
                ),
            },
        )

    @app.get("/.well-known/oauth-protected-resource")
    async def oauth_protected_resource() -> Any:
        unavailable = _oauth_unavailable_in_dev_mode()
        if unavailable is not None:
            return unavailable
        return {
            "resource": settings.response_public_base_url.rstrip("/") + '/mcp',
            "authorization_servers": [settings.response_public_base_url.rstrip("/")],
        }

    @app.get("/.well-known/oauth-authorization-server")
    async def oauth_metadata() -> Any:
        unavailable = _oauth_unavailable_in_dev_mode()
        if unavailable is not None:
            return unavailable
        return {
            "issuer": settings.response_public_base_url.rstrip("/"),
            "authorization_endpoint": settings.response_public_base_url.rstrip("/") + '/oauth/authorize',
            "token_endpoint": settings.response_public_base_url.rstrip("/") + '/oauth/token',
            "registration_endpoint": settings.response_public_base_url.rstrip("/") + '/oauth/register',
            "jwks_uri": settings.azure_jwks_url,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "scopes_supported": ["openid", "profile", "email"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        }

    @app.post("/oauth/register")
    async def oauth_register(request: Request) -> Response:
        """Dynamic client registration — issue a client_id to mcp-remote."""
        import uuid
        body = await request.json()
        client_id = str(uuid.uuid4())
        return Response(
            content=json.dumps({
                "client_id": client_id,
                "client_name": body.get("client_name", "mcp-remote"),
                "redirect_uris": body.get("redirect_uris", []),
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }),
            status_code=201,
            media_type="application/json",
        )

    @app.get("/oauth/authorize")
    async def oauth_authorize(request: Request) -> Response:
        """Authorization endpoint — redirect to Azure AD, remember the client's callback."""
        from urllib.parse import urlencode, quote
        import uuid

        params = dict(request.query_params)
        client_redirect = params.get("redirect_uri", "")
        client_state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "S256")

        # Generate our own state to track this request
        our_state = str(uuid.uuid4())
        _pending_auth[our_state] = {
            "client_redirect": client_redirect,
            "client_state": client_state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        }

        # Redirect to Azure AD — OUR server receives the callback
        azure_base = settings.azure_authority
        azure_params = urlencode({
            "response_type": "code",
            "client_id": settings.azure_client_id,
            "redirect_uri": settings.response_public_base_url.rstrip("/") + '/oauth/callback',
            "scope": "openid profile email offline_access",
            "state": our_state,
            "response_mode": "query",
        })
        return Response(
            status_code=302,
            headers={"Location": f"{azure_base}/oauth2/v2.0/authorize?{azure_params}"},
        )

    @app.get("/oauth/callback")
    async def oauth_callback(request: Request) -> Response:
        """Receive callback from Azure AD, exchange code for token, redirect to mcp-remote."""
        from urllib.parse import urlencode
        import httpx as httpx_lib

        params = dict(request.query_params)
        our_state = params.get("state", "")
        code = params.get("code", "")
        error = params.get("error", "")

        if error:
            return Response(
                content=f"Azure AD error: {error} - {params.get('error_description', '')}",
                status_code=400,
            )

        pending = _pending_auth.pop(our_state, None)
        if not pending:
            return Response(content="Invalid or expired auth state", status_code=400)

        # Exchange code for token with Azure AD
        azure_base = settings.azure_authority
        async with httpx_lib.AsyncClient() as http:
            token_resp = await http.post(
                f"{azure_base}/oauth2/v2.0/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.azure_client_id,
                    "client_secret": settings.azure_client_secret,
                    "code": code,
                    "redirect_uri": settings.response_public_base_url.rstrip("/") + '/oauth/callback',
                    "scope": "openid profile email offline_access",
                },
            )

        if token_resp.status_code != 200:
            logger.error("Azure AD token exchange failed with HTTP %s", token_resp.status_code)
            return Response(
                content="Token exchange failed. Review the app registration and server configuration.",
                status_code=400,
            )

        token_data = token_resp.json()

        # Store the token keyed by a new auth code we'll give to mcp-remote.
        # Include refresh_token so mcp-remote can silently refresh later.
        import uuid
        our_code = str(uuid.uuid4())
        our_refresh_token = str(uuid.uuid4())
        _pending_auth[f"code:{our_code}"] = {
            "access_token": token_data.get("id_token", token_data.get("access_token", "")),
            "token_type": "Bearer",
            "expires_in": token_data.get("expires_in", 3600),
            "id_token": token_data.get("id_token", ""),
            "refresh_token": our_refresh_token,
        }
        # Persist the Azure AD refresh token to SQLite (survives server restarts)
        azure_refresh = token_data.get("refresh_token", "")
        if azure_refresh:
            _store_refresh(our_refresh_token, azure_refresh)

        # Redirect to mcp-remote's callback via GET with our code
        client_redirect = pending["client_redirect"]
        redirect_params = urlencode({
            "code": our_code,
            "state": pending["client_state"],
        })
        return Response(
            status_code=302,
            headers={"Location": f"{client_redirect}?{redirect_params}"},
        )

    @app.post("/oauth/token")
    async def oauth_token(request: Request) -> Response:
        """Token endpoint — mcp-remote exchanges our code for the Azure AD token."""
        form = await request.form()
        grant_type = form.get("grant_type", "")

        if grant_type == "authorization_code":
            code = form.get("code", "")
            stored = _pending_auth.pop(f"code:{code}", None)
            if not stored:
                return Response(
                    content=json.dumps({"error": "invalid_grant", "error_description": "Invalid or expired code"}),
                    status_code=400,
                    media_type="application/json",
                )
            return Response(
                content=json.dumps(stored),
                status_code=200,
                media_type="application/json",
            )

        if grant_type == "refresh_token":
            import uuid as _uuid
            import httpx as httpx_lib
            refresh_token = form.get("refresh_token", "")

            # Evict expired entries from the refresh cache
            now = _time_mod.time()
            expired = [k for k, (ts, _) in _refresh_cache.items() if now - ts > _REFRESH_CACHE_TTL]
            for k in expired:
                del _refresh_cache[k]

            azure_refresh = _get_refresh(refresh_token)
            if not azure_refresh:
                # Check if this token was recently rotated — return the cached
                # response so duplicate/concurrent refresh requests succeed.
                cached = _refresh_cache.get(refresh_token)
                if cached:
                    logger.info("Returning cached refresh response for duplicate request")
                    return Response(
                        content=cached[1],
                        status_code=200,
                        media_type="application/json",
                    )
                return Response(
                    content=json.dumps({"error": "invalid_grant", "error_description": "Invalid or expired refresh token"}),
                    status_code=400,
                    media_type="application/json",
                )

            # Exchange Azure AD refresh token for new tokens
            azure_base = settings.azure_authority
            async with httpx_lib.AsyncClient() as http:
                token_resp = await http.post(
                    f"{azure_base}/oauth2/v2.0/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": settings.azure_client_id,
                        "client_secret": settings.azure_client_secret,
                        "refresh_token": azure_refresh,
                        "scope": "openid profile email offline_access",
                    },
                )

            if token_resp.status_code != 200:
                # Remove stale refresh token
                _delete_refresh(refresh_token)
                logger.warning("Azure AD refresh failed with HTTP %s", token_resp.status_code)
                return Response(
                    content=json.dumps({"error": "invalid_grant", "error_description": "Refresh token expired. Re-authenticate."}),
                    status_code=400,
                    media_type="application/json",
                )

            new_data = token_resp.json()
            new_refresh_token = str(_uuid.uuid4())

            # Update the stored Azure refresh token
            _delete_refresh(refresh_token)
            new_azure_refresh = new_data.get("refresh_token", azure_refresh)
            _store_refresh(new_refresh_token, new_azure_refresh)

            response_body = json.dumps({
                "access_token": new_data.get("id_token", new_data.get("access_token", "")),
                "token_type": "Bearer",
                "expires_in": new_data.get("expires_in", 3600),
                "id_token": new_data.get("id_token", ""),
                "refresh_token": new_refresh_token,
            })

            # Cache the response so duplicate requests with the old token
            # get the same successful response instead of 400.
            _refresh_cache[refresh_token] = (_time_mod.time(), response_body)

            logger.info("Token refreshed successfully")
            return Response(
                content=response_body,
                status_code=200,
                media_type="application/json",
            )

        return Response(
            content=json.dumps({"error": "unsupported_grant_type"}),
            status_code=400,
            media_type="application/json",
        )

    # -----------------------------------------------------------------------
    # MCP transport route — FastMCP app handles /mcp internally
    # -----------------------------------------------------------------------
    from starlette.routing import Mount
    app.routes.append(Mount("", app=mcp_asgi_app))

    return app


# ---------------------------------------------------------------------------
# stdio transport (development mode)
# ---------------------------------------------------------------------------

async def _run_stdio() -> None:
    await _run_readonly_stdio(get_settings())


def main() -> None:
    """Parse CLI arguments and start the Epicor MCP Server."""
    parser = argparse.ArgumentParser(description="Epicor Kinetic MCP Server")
    parser.add_argument("--stdio", action="store_true", help="Run in stdio mode")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default 8015)")
    parser.add_argument("--host", type=str, default=None, help="Host to bind (default 127.0.0.1)")
    parser.add_argument("--ssl-cert", type=str, default=None, help="Path to SSL certificate file")
    parser.add_argument("--ssl-key", type=str, default=None, help="Path to SSL key file")
    args = parser.parse_args()

    if args.stdio:
        anyio.run(_run_stdio)
    else:
        settings = get_settings()
        port = args.port or settings.port
        host = args.host or settings.host

        app = create_app(settings)

        ssl_kwargs = {}
        if args.ssl_cert and args.ssl_key:
            ssl_kwargs["ssl_certfile"] = args.ssl_cert
            ssl_kwargs["ssl_keyfile"] = args.ssl_key

        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=settings.log_level.lower(),
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            **ssl_kwargs,
        )


if __name__ == "__main__":
    main()

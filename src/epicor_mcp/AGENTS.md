# MCP runtime

Purpose: serve the five-tool Epicor read surface with portable configuration.

## Entry points and module router
- `server.py`: `create_app`, `main`; HTTP/stdio and optional OAuth/admin routes.
- `config.py`: `Settings`, `validate_runtime`; environment/.env configuration.
- `Settings.plants`: validated `dict[str, str]` from `EPICOR_MCP_PLANTS`.
- `wedge_server.py`: `WedgeRuntime`; query dispatch and session attribution.
- `context.py`, `audit.py`: request identity and the local audit log (both modes).
- [auth](auth/AGENTS.md): credentials, token validation, sessions.
- [rbac](rbac/AGENTS.md): whitelist and per-user menu authorization.
- [sql](sql/AGENTS.md): SELECT validation, policy, limits, and execution.
- [baq_ops](baq_ops/AGENTS.md): saved BAQs, dashboards, and save permissions.
- [discovery](discovery/AGENTS.md): authorized table/field discovery.
- [index](index/AGENTS.md): metadata, documents, optional embeddings.
- [epicor_client](epicor_client/AGENTS.md): REST client and tablesets.
- [response](response/AGENTS.md): formatting and size limits.
- [tools](tools/AGENTS.md): retained libraries outside the public surface.

## Invariants
- Default `auth_mode=none` registers read-only tools and no OAuth/admin routes.
- Exactly five tools: query, tables, fields, help, dashboards, each prefixed `epicor_`.
- Every data path enforces its table/column policy; authorization failures deny access.
- Business writes are unreachable; BAQ saves require SSO and explicit permission.
- Both modes install the argument guard and, unless `audit_log_enabled=false`, the
  audit hook outside it; without SSO the audited principal is `shared-read-only`.
- Credentials, hostnames, departments, and data paths come from operator configuration.
- Missing/blank/`{}` site maps are empty; invalid JSON, non-objects, non-string or
  blank codes/names fail settings validation. Each Settings instance owns its map.
- HTTP and stdio initialization instructions snapshot operator-provided site hints
  for that app only. Hints grant no access and never rewrite submitted SQL.
- Close clients/indexes at shutdown; do not resolve caller permissions at startup.
- No automatic model downloads, imports, or private filesystem dependencies.

## Gotchas
- Run from the configured working directory; Epicor URL is the complete company base.
- Tool and parameter help must reflect actual capabilities.
- Recreate the app and reconnect clients to refresh configured site hints.
- [Public design](../../docs/design.md) explains boundaries and regression coverage.

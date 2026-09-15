# epicor_client/

Purpose: HTTP client for Epicor REST v2 data operations, with service-account Basic
auth, a per-request API key, OData helpers, and Epicor error normalization.

## Public interface
- `EpicorClient(username, password, company_id="", timeout=30, base_url="")`:
  `await get(...)`, `await post(...)`, `await call_method(...)`, `await odata_query(...)`,
  `await close()`. Every public method takes the API key as a parameter; the client stores none.
- `ODataBuilder`: `$filter` / `$select` / `$top` string construction.
- `DatasetHandler`: tableset (`ds`) round-trips used by the legacy multi-step tools.
- `EpicorError`, `ErrorHandler`: Epicor error payloads normalized to one exception type.

## Invariants
- One shared `httpx.AsyncClient` per `EpicorClient`, created lazily; call `close()` on shutdown.
- Headers are `Authorization: Basic`, `X-API-Key` and `Company`; nothing else identifies
  the caller. Microsoft identities never become Epicor tokens; the service account
  performs every call.
- `base_url` is the full company OData base from `Settings`; the client never rewrites
  or inserts a company ID.
- BAQ parse/execute reads use POST because that is Epicor's API shape, not because they
  write. Business-record write methods are unreachable from the public surface.
- Per-request `timeout` applies to connect/read/write; pool waits are bounded at 300 s.

## Gotchas
- SSO menu/security requests use a separate [EpicorAuthzClient](../rbac/epicor_authz.py)
  with service-account token handling; follow the [RBAC guide](../rbac/AGENTS.md) for that path.
- Epicor errors arrive as HTTP 4xx/5xx with a JSON body; `ErrorHandler` extracts the
  message. A 401/403 usually means the API key's access scope, not the password.
- Use reserved example domains for documentation hosts; never name a real installation.

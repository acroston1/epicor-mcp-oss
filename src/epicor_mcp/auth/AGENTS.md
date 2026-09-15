# auth/

Purpose: Epicor service-account credentials, Azure AD bearer-token validation
(SSO mode only), and per-connection MCP session records.

## Public interface
- `CredentialManager(settings)`: `load()`, `service_username`, `service_password`,
  `get_admin_key()`, `get_baq_key()`, `get_api_key(department, baq=False)`,
  `build_headers(department, baq=False)`, `get_base_url(environment=None)`.
- `AzureADTokenValidator` (oauth.py) returns `ValidatedToken`; constructed only when
  `auth_mode=azure_ad`.
- `MCPSession`, `SessionStore` (session.py). The active session travels through
  `epicor_mcp.context.set_current_session` / `get_current_session_or_none`.
- `create_default_department_keys_file(path)`: placeholder template, on explicit operator request only.
Everything else is private.

## Invariants
- Service-account username/password use settings values, then the INI at
  `EPICOR_MCP_CREDENTIALS_PATH`.
- Shared API keys use the corresponding `EPICOR_MCP_EPICOR_*` setting, then INI,
  then `admin_keys` in the configured department-keys file (default `data/department_keys.json`).
- `get_api_key(department, baq=...)` and `build_headers` prefer a non-empty department
  `read_key` or `baq_key` over shared keys, including shared settings/environment values.
  Without that department override, they fall back to the shared read or BAQ key.
  INI keys: `username`, `password`, `api_key`, optional `baq_api_key` (`.epicor_credentials.example`).
- No file under `$HOME` is ever opened automatically; a blank `credentials_path` reads nothing.
- Secrets are `Field(repr=False)` in `Settings` and never logged.
- Token validation is complete: RSA signature via the tenant JWKS, issuer, audience
  (`azure_client_id`), `exp`/`nbf` with 60 s leeway. There is no unverified-claims path.
  The only accommodation is an RSA JWK with no `alg`, which gets its `alg` filled in.
- `azure_ad` requires tenant id, client id, client secret and an `https://` public base URL
  at startup (`Settings.validate_runtime`); commercial and US Government clouds are supported.

## Gotchas
- `get_baq_key()` falls back to the admin key; a missing BAQ key is not an error.
- Sessions and OAuth handshake state are in memory: run one server process with SSO on.
- `build_headers` raises `RuntimeError` when the service account is unset and `get_api_key`
  raises `KeyError` when no key resolves; `server.py` checks both at startup.

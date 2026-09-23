# Optional Microsoft SSO

Microsoft SSO is disabled unless `EPICOR_MCP_AUTH_MODE=azure_ad`. The default
`none` mode needs no Microsoft tenant, registration, login, or menu-security
index. It uses the read-only table whitelist described in the root README.

SSO enables individual Microsoft identities and the retained Epicor menu-derived
authorization engine. Use an Entra tenant and an Epicor service account belonging
to your organization. Commercial and US Government cloud endpoints are supported.
An Epicor account's email should match the user's Microsoft email/UPN.

## Register the application

Create a **single-tenant** web application registration in your Entra tenant.
Register the exact web redirect URI:

```text
https://mcp.example.com/oauth/callback
```

Replace the hostname with the public origin that hosts this server. Create a
client secret and store it in the server's private `.env`; never put it in the
connector. Allow the OIDC `openid`, `profile`, `email`, and `offline_access`
scopes used by the authorization flow. Configure email/name claims as needed
for your tenant. User records in the JSON below avoid reliance on group claims.

Configure `.env` alongside the Epicor settings from the root README:

```dotenv
EPICOR_MCP_AUTH_MODE=azure_ad
EPICOR_MCP_AZURE_CLOUD=commercial
EPICOR_MCP_AZURE_TENANT_ID=your-tenant-id
EPICOR_MCP_AZURE_CLIENT_ID=your-application-client-id
EPICOR_MCP_AZURE_CLIENT_SECRET=your-secret
EPICOR_MCP_RESPONSE_PUBLIC_BASE_URL=https://mcp.example.com
EPICOR_MCP_USERS_CONFIG_PATH=data/users.json
EPICOR_MCP_DEPARTMENT_KEYS_PATH=data/department_keys.json
EPICOR_MCP_MENU_AUTHZ_MODE=enforce
EPICOR_MCP_TABLE_AUTHZ_MODE=gate
EPICOR_MCP_MENU_MAP_DB_PATH=data/menu_security.db
EPICOR_MCP_SERVICE_INDEX_PATH=data/service_index.db
EPICOR_MCP_AZURE_ADMIN_EMAILS=admin@example.com
```

For US Government tenants, use `EPICOR_MCP_AZURE_CLOUD=government`.
`AZURE_ADMIN_EMAILS` is a comma-separated list of Microsoft identities allowed
to inspect/refresh authorization through admin endpoints. Leave it empty to
allow no admins. The shared `SERVER_TOKEN` setting belongs to `none` mode; it
does not replace Microsoft authentication when `azure_ad` is enabled.

## Import service and menu metadata

Build your service index from your own Swagger exports first:

```bash
python scripts/bootstrap_schema.py --swagger-dir /path/to/swagger-json --output-dir data
python scripts/build_menu_map.py
```

The second command explicitly reads Epicor menu/application metadata and writes
local files. It needs read access to `Ice.BO.MenuSvc/GetRows`,
`Ice.LIB.MetaFXSvc/GetApplications`, and `Ice.LIB.MetaFXSvc/ExportApp`.
Runtime authorization also reads `Ice.BO.UserFileSvc`, `Ice.BO.SecuritySvc`,
and `Ice.BO.MenuSvc`. Configure the regular `EPICOR_MCP_EPICOR_API_KEY` for
these operations and the BAQ key for query operations. The menu builder and
runtime authorization use the configured **live** URL; set
`EPICOR_MCP_EPICOR_LIVE_URL` even when data reads use pilot. An optional
`EPICOR_MCP_MENU_AUTHZ_LIVE_URL` overrides the runtime authorization URL.

The builder reports unmapped menus and refuses low-coverage replacements.
Review that report and your installation's applications before using the server.
`menu_authz_mode=enforce` refuses startup without the required local menu map.
A failed user/menu lookup denies affected queries until authorization recovers.

## Configure user profiles

Create private `data/users.json`:

```json
{
  "users": {
    "reader@example.com": {
      "epicor_username": "reader",
      "department": "Operations",
      "access_level": "read_only",
      "environment": "live",
      "display_name": "Example Reader",
      "extra_departments": [],
      "can_write_baqs": false
    }
  },
  "department_defaults": {
    "Operations": {"access_level": "read_only", "environment": "live"},
    "Planning": {"access_level": "read_only", "environment": "live"}
  },
  "epicor_group_to_department": {
    "EXAMPLE_REPORT_READERS": ["Operations"],
    "EXAMPLE_PLANNERS": ["Operations", "Planning"]
  },
  "azure_ad_group_map": {}
}
```

Emails are matched case-insensitively. An explicit entry in `users` takes
precedence: its department, extra departments, and permissions are retained
without inferring a replacement from Epicor groups.

To allow automatic profile creation, replace the example group codes above
with codes from your own Epicor installation. `epicor_group_to_department`
is used by both the menu-authorization identity lookup and the legacy user
lookup. No group-to-department assignments are bundled with the server.

- The mapping must be a JSON object whose keys are exact, case-sensitive Epicor
  group codes and whose values are lists of department names. Names must be
  non-empty strings without leading or trailing whitespace.
- An absent or empty mapping infers no departments. Unknown groups and entries
  with empty lists add nothing. `null`, strings in place of lists, and other
  malformed values fail configuration loading with the file and setting named.
- An unconfigured user needs an Epicor identity with at least one mapped group
  to acquire a profile. If none resolves, the server returns 403. Explicit
  profiles continue to work with an empty mapping.
- Inferred department names are combined with applicable verified Azure claim
  mappings and sorted. The first is primary; the others become extra departments.
  The primary department's configured defaults supply access level and environment.
  Without those defaults, a new profile is `read_only` with environment `pilot`.

Department labels do not grant menu/table access or BAQ-save permission. The
existing BAQ-authoring checks remain separate: auto-created profiles can receive
`can_write_baqs` from membership in `ExtBAQDesigner`, `BAQ`, `BAMP`, or `BAMS`.
Explicit profiles instead keep their configured permission flags. Merely adding
another group to the department mapping does not grant BAQ-write access.

The service account performs Epicor API calls. Microsoft identities supply the
caller's authorization scope and audit attribution, not delegated Epicor tokens.

Create private `data/department_keys.json` (keys can stay in `.env`):

```json
{
  "admin_keys": {},
  "departments": {
    "Operations": {}
  },
  "environments": {
    "live": {"odata_base": "https://epicor.example.com/YourInstance/api/v2/odata/YOUR_COMPANY/"},
    "pilot": {"odata_base": "https://pilot.example.com/YourInstance/api/v2/odata/YOUR_COMPANY/"}
  }
}
```

A department may contain `read_key` and `baq_key` strings to override the shared
keys. Keep this file private even if its initial version contains placeholders.

## Policy differences and connection

- In `none` mode, every caller shares the exact configured whitelist and cannot
  save BAQs. An empty whitelist grants nothing.
- In `azure_ad` mode, ad-hoc SQL and discovery use the authenticated user's Epicor
  menu-derived table scope — default deny: only tables reachable from menus the
  user can launch, plus `_UD` mirror inheritance. Tables no menu maps are
  reachable only by SecurityMgr users. The `none`-mode whitelist does
  **not** add a second restriction to SSO mode.
- Saved BAQs in SSO mode retain Epicor's saved-BAQ grant model. Their parsed
  definitions are checked against the sensitive table/column denylist, while
  per-user table scope applies to ad-hoc SQL. The table whitelist does apply to
  saved BAQs in `none` mode.
- Keep profiles `read_only` with `can_write_baqs=false` to prevent BAQ saving.
  Granting `read_write` or `can_write_baqs=true` enables the existing BAQ-save
  permission in SSO mode; review those grants deliberately. The OSS public
  surface does not expose business-record write tools.

Restart the server after imports. For changes to `users.json` and department
keys, either restart or use the authenticated admin endpoint `POST /admin/reload`.
A successful reload applies the mapping to both identity lookup paths and clears
derived profiles, legacy user resolutions, menu snapshots, and pinned table scopes.
Malformed `users.json` syntax or an invalid Epicor group mapping returns 400
and leaves the prior configuration active.

Use the HTTP server,
HTTPS reverse proxy, and connector with `MCP_AUTH_MODE=oauth` and
`MCP_SERVER_URL=https://mcp.example.com/mcp`. Direct server `--stdio` supports
only `none` mode. The browser opens during the connector's login flow.

Run a single server process: the retained OAuth handshake state is in memory.
Keep `data/refresh_tokens.db` and all user/menu files private and writable only
to the server account. No tokens or authentication databases belong in Git.
This extraction's deterministic tests verify configuration and token validation;
a complete Entra/Epicor sign-in requires your own tenant and configured instance.

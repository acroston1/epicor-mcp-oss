# Epicor MCP OSS

Read-only MCP server for Epicor Kinetic, Apache-2.0, not an Epicor product. The public
surface is exactly five tools: `epicor_query`, `epicor_tables`, `epicor_fields`,
`epicor_help`, `epicor_dashboards`. Operator setup lives in README.md; optional
Microsoft SSO in docs/microsoft-sso.md; the desktop connector in bridge/README.md.

## Run and test
- Install: `python -m pip install -e '.[dev]'` (Python 3.11+). Configure via `.env`
  (`.env.example`) and `table_whitelist.txt` (`table_whitelist.example.txt`).
- Serve: `epicor-mcp` (HTTP, 127.0.0.1:8015, endpoint `/mcp`, `/health`) or
  `epicor-mcp --stdio` (local, `auth_mode=none` only).
- Gate: `.claude/e2e-gate.sh` runs `pytest tests -q`. Offline, deterministic, never
  touches a live Epicor. Run it before every commit.

## Helping a user set this up
- Follow README.md sections 1-5 in order; run every command from the repo root.
- Ask the user for the Epicor REST v2 OData company URL, company ID, a dedicated service
  account, and an API key whose access scope covers README section 2's services. Creating
  those needs an Epicor administrator; README section 2 has the checklist to hand them.
- Verify Epicor with the one-table schema build in README section 3 first; `/health` does
  not log in to Epicor.
- Connect the user's agent per [Connect your coding agent](README.md#connect-your-coding-agent).
  Prefer the HTTP endpoint: stdio clients start the server outside the repo root.
- `epicor_unreachable` / `epicor_auth_error` mean configuration, not SQL.

## Module index
- [Runtime](src/epicor_mcp/AGENTS.md): server/config and integration-package router.
- [Scripts](scripts/AGENTS.md): schema, document, and optional SSO imports.
- [Tests](tests/AGENTS.md): deterministic behavior and documentation checks.
- [Bridge](bridge/AGENTS.md): desktop connector and executable builds.
- [Documentation](docs/AGENTS.md): public design, optional SSO, verification scope.
- [Deployment examples](deploy/AGENTS.md): operator-owned reverse proxy configuration.
- [Local data](data/AGENTS.md): private inputs, generated indexes, and ignore rules.

## Invariants
- `auth_mode=none` is read-only: no BAQ saves, no business writes, admin/OAuth routes 404.
- Table restrictions apply on every path: discovery, fields, ad-hoc SQL including joins and
  subqueries, saved-BAQ definitions, diagnostic probes. Empty whitelist = no tables; a
  missing or malformed configured file fails startup; only a blank setting opts out.
- The built-in denylist in `src/epicor_mcp/sql/denylist.py` beats every grant, including SecurityMgr.
- Nothing installation-specific is bundled: no schema corpus, vectors, documents, site
  maps, hosts or credentials. Operators supply them via `.env`, `data/`, and the whitelist.
- Credentials come from `.env` or `EPICOR_MCP_CREDENTIALS_PATH`; never a `$HOME` file.
- Tests are the contract. Never weaken, skip, or delete a test to turn the gate green.
- Fixtures stay synthetic: no real hosts, tenants, people, customers, or phone numbers.
- A change to a package's public interface or invariants updates that package's
  `CLAUDE.md` and `AGENTS.md` (kept identical) in the same change.

## Gotchas
- `.env` and relative `data/` paths resolve from the working directory: run from repo root.
- The Epicor URL is the full company OData base; the server never inserts a company ID.
- `Settings.plants` reads `EPICOR_MCP_PLANTS`; optional MCP hints grant no table access.
- `public_surface` must stay `True`; `dev_mode`/`dev_identity` bypasses are rejected by `Settings`.
- Architecture and rationale live in [docs/design.md](docs/design.md); cite current
  functions or regression tests instead of external project notes.

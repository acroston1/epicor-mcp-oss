# Epicor MCP OSS

An Apache-2.0 **read-only** MCP server for Epicor Kinetic. It lets MCP-compatible
assistants discover tables and fields, run bounded SELECT queries and saved BAQs,
resolve dashboards, and search documents you import. It never writes business
records.

**You need a dedicated Epicor service account** (username and password) and an
Epicor REST API key for this server to work; it authenticates to Epicor as that
account on behalf of every client. This project is independently developed and
is **not an official Epicor product and not endorsed by Epicor**.

Microsoft SSO is **disabled by default**. Administrators can expose an explicit
list of read-only tables, optionally require a shared server token, and enable
Microsoft SSO later. Every tool call is recorded in a local audit log. No Epicor table/field database, vector index, vendor
manuals, tenant configuration, credentials, or business records are included.

## Tool surface

| Tool | Purpose |
|---|---|
| `epicor_query` | Execute a bounded SELECT or read an existing saved BAQ |
| `epicor_tables` | Find tables in your imported metadata (substring, or optional semantic ranking) |
| `epicor_fields` | Find fields and descriptions for an allowed table (substring, or optional semantic ranking) |
| `epicor_help` | Search your imported documents; optional semantic search |
| `epicor_dashboards` | Resolve dashboard names to saved BAQ IDs |

In SSO-disabled mode, all clients share the same table policy and service
account. Business-record writes, BAQ saving, and admin endpoints are disabled.
A table whitelist restricts data access; it does **not** authenticate a caller.
For hosted use, set a shared server token or place the service behind your own
access-controlled network gateway.

## 1. Install

Use Python 3.11 or newer, an Epicor Kinetic instance with REST API v2 enabled,
a dedicated Epicor service account, and an API key with the necessary access
scope. No GPU or embedding service is required for the default configuration.

From the repository root on Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
cp table_whitelist.example.txt table_whitelist.txt
```

On Windows, use `python -m venv .venv`, activate
`.venv\Scripts\Activate.ps1`, and use `Copy-Item` instead of `cp`.
Keep `.env`, imported documents, and all generated files private; they are
excluded from version control.

## 2. Configure Epicor and read-only access

Edit `.env`. These are the required connection settings:

```dotenv
EPICOR_MCP_AUTH_MODE=none
EPICOR_MCP_ENVIRONMENT=live
EPICOR_MCP_EPICOR_LIVE_URL=https://epicor.example.com/YourInstance/api/v2/odata/YOUR_COMPANY/
EPICOR_MCP_EPICOR_COMPANY_ID=YOUR_COMPANY
EPICOR_MCP_EPICOR_USERNAME=your-service-account
EPICOR_MCP_EPICOR_PASSWORD=your-password
EPICOR_MCP_EPICOR_API_KEY=your-epicor-api-key
EPICOR_MCP_TABLE_WHITELIST_PATH=table_whitelist.txt
```

The three credential values can instead live in a private INI file: copy
`.epicor_credentials.example` to `.epicor_credentials`, fill it in, and set
`EPICOR_MCP_CREDENTIALS_PATH=.epicor_credentials`. Values set in `.env` take
precedence over the file.

The URL is the **full company OData base**, including your installation's path,
`api/v2/odata`, and company ID. The server does not substitute a company ID into
the URL. To use pilot, set `EPICOR_MCP_ENVIRONMENT=pilot` and configure
`EPICOR_MCP_EPICOR_PILOT_URL`. Settings and relative data paths resolve from the
working directory; run commands from the repository root.

Query execution uses Epicor's BAQ parsing/execution APIs, which use HTTP POST
even for reads. Configure your API access scope for the operations you intend
to use, including:

- `Ice.BO.BAQDesignerSvc`: `ParseFromSQL`, `GetTableList`, and `GetFieldList` for parsing and schema inspection.
- `Ice.BO.DynamicQuerySvc`: `Execute`, `Analyze`, and `GetByID` for query execution, diagnostics, and saved BAQ definition inspection.
- `BaqSvc`: execution of permitted saved BAQs.
- `Ice.BO.DashBoardSvc`: read access if dashboard lookup is needed.

Do not grant broad write access just because read methods use POST. Epicor
service-account permissions and API access scopes apply in addition to this
server's table policy. `EPICOR_MCP_EPICOR_BAQ_API_KEY` optionally selects a
separate BAQ key; otherwise it uses `EPICOR_MCP_EPICOR_API_KEY`. The default
mode never creates or overwrites saved BAQs, even if the API key could do so.

**Checklist to hand your Epicor administrator.** Creating these needs Epicor
security rights. Screen names are from Epicor Kinetic and may be labelled
slightly differently in your version:

1. A dedicated service user, created in **User Account Security Maintenance**,
   with access to the company you will query.
2. A REST API key from **API Key Maintenance**. Epicor shows the key only once;
   put it in `.env` or `.epicor_credentials`.
3. An access scope from **Access Scope Maintenance**, assigned to that key and
   allowing only the services listed above.
4. The company's REST v2 OData base URL,
   `https://<server>/<instance>/api/v2/odata/<company>/`, and the company ID.

Edit `table_whitelist.txt` to list only tables you intend to expose:

```text
# Example: part master and warehouse balances
Erp.Part
Erp.PartWhse
```

Names are case-insensitive. Schema-qualified entries are exact; a bare `Part`
entry means `Erp.Part`. `Erp.Part` does not grant `Ice.Part`.

The whitelist applies to table discovery, field discovery, ad-hoc SQL, joined
and nested query tables, and the definitions behind saved BAQs. Listing a base
table does not automatically allow its `_UD` mirror. Wildcards are rejected.
For ad-hoc SQL the check runs on Epicor's parsed statement: the SQL text goes to
`ParseFromSQL`, which reads no business data, and a non-whitelisted table is
refused before anything executes.
The built-in sensitive-table/column denylist always takes precedence. Add
further denials in an optional `table_blacklist.txt`.

An empty whitelist denies all table access. A missing or malformed configured
file stops startup. Restart after editing either list. To **explicitly opt out**
of the whitelist, set `EPICOR_MCP_TABLE_WHITELIST_PATH=`; the remaining limits
are the built-in denylist and your Epicor credentials' permissions.

For a hosted endpoint without Microsoft SSO, also set:

```dotenv
EPICOR_MCP_SERVER_TOKEN=replace-with-a-long-random-secret
EPICOR_MCP_RESPONSE_PUBLIC_BASE_URL=https://mcp.example.com
```

You can generate a shared token with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.
This token is separate from your Epicor API key. Clients receive only the
server token; the Epicor credentials stay on the server.

## 3. Build local table/field metadata

The server starts without a bundled schema corpus; table/field search is empty
until you import metadata. For accurate SQL discovery, read physical table and
field definitions from **your own Epicor instance**:

```bash
python scripts/build_schema_catalogue.py --output-dir data
```

This explicit command calls `GetTableList` and `GetFieldList` using `.env`
credentials. It changes no Epicor records and saves no BAQ. It writes
`data/schema_catalogue.json`, `data/baq_schema.db`, and
`data/discovery_index/`, using metadata and substring search without vectors.
Local SQL column validation also reads `data/schema_catalogue.json` automatically,
including field types, so no separate validator cache is needed.
Use `--tables Part,PartWhse` to build a selected subset; that output replaces the
previous catalogue with the selected subset. Rebuild after schema changes.

**This is also your first live check of the Epicor connection**, because `/health`
does not log in to Epicor. Start with one table:

```bash
python scripts/build_schema_catalogue.py --output-dir data --tables Part
```

Success proves the URL, service account, API key, and the key's access scope for
`Ice.BO.BAQDesignerSvc`. A name-resolution or connection error means the URL or
network; HTTP 401/403 means the credentials or access scope (see section 2). Then
rebuild with the tables in your whitelist, or omit `--tables` for the full
catalogue; a full build reads metadata for every table, so it takes longer.

If you already have your own Epicor Swagger/OpenAPI JSON exports, you can build
a local service index without network access:

```bash
python scripts/bootstrap_schema.py --swagger-dir /path/to/swagger-json --output-dir data
```

Use JSON files exported for your Epicor services, with `info`, `paths`, and
OpenAPI `components.schemas` or Swagger `definitions`. **BO/REST projections
can contain fields that do not exist in SQL.** This fallback labels discovery
as unverified; the physical-catalogue command above is the recommended source
for SQL. To use an existing authoritative physical catalogue alongside Swagger:

```bash
python scripts/bootstrap_schema.py --swagger-dir /path/to/swagger-json \
  --catalogue /path/to/schema_catalogue.json --output-dir data
```

Missing, malformed, or Swagger-only metadata cannot prove that a physical SQL
column is absent, so local column validation abstains. Query parsing and table
access checks still run. For a physical catalogue outside `data/`, set the process
environment variable `EPICOR_MCP_SCHEMA_CATALOGUE` before starting the server.
`EPICOR_MCP_COLUMN_CATALOGUE` optionally overrides only column preflight with a
list of catalogue paths, separated by `:` on Linux/macOS or `;` on Windows.
These two advanced overrides are read from the process environment, not `.env`;
the default commands above need neither. Discovery still uses the generated
discovery index configured for the server.

Optional candidate-key metadata can improve join/fan-out diagnostics for tables
beyond the small core map. Set `EPICOR_MCP_TABLE_KEYS_PATH=data/table_keys.json`
and supply your own definitions, for example:

```json
{"tables": {"Part": [["Company", "PartNum"]]}}
```

Each nested list is a complete candidate key, not an arbitrary list of useful
columns. Key metadata informs diagnostics; it never grants table access.

Optionally set a JSON map of your site (Plant) codes to names in `.env` or the
process environment. The server includes these operator-provided hints in MCP
initialization instructions so the model can use the correct codes in a
`Plant` filter. The server ships no site map:

```dotenv
EPICOR_MCP_PLANTS={"10": "Main Plant", "20": "North Plant"}
```

Omit the setting, leave it blank, or use `{}` to provide no hints. Codes and
names must be non-empty strings; invalid maps fail configuration validation.
The map does not grant table access or rewrite SQL. Restart the server and
reconnect clients after changing it.

Only import schema exports and documentation you are entitled to use. None of
these generated files belong in a public repository.

### Optional: semantic search for tables and fields

Skip this if substring search over names, labels and descriptions is enough.
To rank tables and fields by meaning ("suppliers we sent orders to" finds
`POHeader` before `OrderHed`), rebuild the discovery index with vectors from
an embedding model you choose. Two providers are supported, and the index
manifest records which one built it.

A local Sentence Transformers model (the same interface as document search):

```bash
python -m pip install -e '.[semantic]'
python scripts/build_discovery_index.py --model /path/to/your-embedding-model \
  --local-files-only
```

An OpenAI-compatible `/v1/embeddings` server you operate yourself (vLLM,
Ollama, llama.cpp and similar; nothing is contacted unless you pass this):

```bash
python -m pip install -e '.[vectors]'
python scripts/build_discovery_index.py \
  --endpoint http://localhost:8000/v1/embeddings --model your-model --dim 1024
```

Then configure the server with the **same** provider, model and dimension and
turn the switch on:

```dotenv
EPICOR_MCP_VECTOR_SEARCH_ENABLED=true
# Local model:
EPICOR_MCP_EMBEDDING_MODEL=/path/to/your-embedding-model
# Or endpoint:
EPICOR_MCP_EMBED_ENDPOINT=http://localhost:8000/v1/embeddings
EPICOR_MCP_EMBED_MODEL_NAME=your-model
EPICOR_MCP_EMBED_DIM=1024
```

Restart the server. Every `epicor_tables` and `epicor_fields` response states
its `search_mode`. If the switch is off, the configured model or dimension
differs from the build, the arrays are missing or corrupt, or the provider is
unreachable, discovery falls back to substring search and says why under
`notes`. Rebuild after schema or model changes; a rebuild without `--model`
removes the vectors again. The endpoint provider embeds each query over the
network at request time; the local provider loads model files only.

## 4. Optional: add documentation

Skip this entire section if you only want Epicor data tools. `epicor_help` will
explain that no documents have been imported; the other tools remain available.

Put your documents in a private directory. Supported inputs are UTF-8 `.txt`,
`.md`, `.rst`, and JSON records containing `title` and `content` (optional
`source_type`, `section`, `url`). For text-based PDFs, first install the extra:

```bash
python -m pip install -e '.[documents]'
```

Import and chunk the documents:

```bash
python scripts/build_docs_index.py --input-dir /path/to/your-documents \
  --output data/epicor_docs.db
```

Restart the server. **No embedding model is needed:** help search uses
case-insensitive literal substring matching. Search for distinctive words or
phrases that occur in your documents. PDF page numbers are retained; scanned
image-only PDFs require OCR before importing. All clients who can use this MCP
can search the imported documents; the table whitelist does not partition docs.
Re-running ingestion replaces the document database.

### Optional: semantic search with your chosen embedding model

This additional step is optional. The supported model interface is
[Sentence Transformers](https://sbert.net/docs/package_reference/sentence_transformer/model.html):
choose a compatible model identifier or a local model directory. The repository ships no model and prescribes no vendor.

```bash
python -m pip install -e '.[semantic]'
python scripts/build_document_vectors.py \
  --database data/epicor_docs.db --output data/document_vectors \
  --model /path/to/your-embedding-model --device cpu --local-files-only
```

Alternatively, pass a model identifier and omit `--local-files-only` to allow
that explicit build to download it. `--device cuda` may be used with a suitable
PyTorch/CUDA installation. Configure the server with the **same** identifier or
model path used when building:

```dotenv
EPICOR_MCP_VECTOR_SEARCH_ENABLED=true
EPICOR_MCP_EMBEDDING_MODEL=/path/to/your-embedding-model
EPICOR_MCP_DOCUMENT_VECTORS_PATH=data/document_vectors
```

Restart the server. The index manifest records the model, embedding dimension,
and document fingerprint. Query-time loading uses an existing local/cached
model; it does not automatically download one. If vectors/model are absent,
incompatible, or stale, help falls back to substring search and reports the
fallback. Rebuild vectors after changing documents or models. Set
`EPICOR_MCP_VECTOR_SEARCH_ENABLED=false` to use substring search exclusively.
Table/field discovery has its own optional vector build (see section 3); the
same switch enables both, but each index is built and verified separately.

## 5. Run and connect

```bash
epicor-mcp
# Equivalent: python -m epicor_mcp.server
curl http://localhost:8015/health
```

The HTTP MCP endpoint is `http://localhost:8015/mcp`. `/health` reports the
active authentication and table-policy modes; it does not verify Epicor login.
The server binds to loopback by default. For remote use, put an HTTPS reverse
proxy in front of port 8015 and set `EPICOR_MCP_RESPONSE_PUBLIC_BASE_URL` to your
public origin. A dedicated hostname avoids path-prefix configuration. A sample
nginx configuration is in [deploy/nginx.conf.example](deploy/nginx.conf.example).
The sample is documentation; no services or system configuration are installed.

Every tool call is appended to a local SQLite audit log, `data/audit.db` by
default (`EPICOR_MCP_AUDIT_LOG_PATH`): timestamp, tool name, arguments,
outcome, duration and the caller. Without Microsoft SSO there is no
per-person identity, so the caller is recorded as `shared-read-only`; a shared
server token identifies the deployment, not a user. Set
`EPICOR_MCP_AUDIT_LOG_ENABLED=false` to turn the log off; `/health` reports
`audit_log`. The file is private operator data and is gitignored.

For a compatible client with direct Streamable HTTP support, use the full
`https://mcp.example.com/mcp` endpoint and, when configured, send
`Authorization: Bearer YOUR_SHARED_SERVER_TOKEN`.

### Connect your coding agent

Start the server from the repository root with `epicor-mcp`, then point your
client at `http://127.0.0.1:8015/mcp` (or your public `/mcp` URL). Prefer this
HTTP endpoint: the server reads `.env` and `data/` from its working directory,
and clients that launch stdio servers start them somewhere else. If you set
`EPICOR_MCP_SERVER_TOKEN`, export the same value in the client's environment and
keep the header lines below; without a server token, drop them.

**Claude Code**

```bash
claude mcp add --transport http epicor http://127.0.0.1:8015/mcp \
  --header "Authorization: Bearer $EPICOR_MCP_SERVER_TOKEN"
claude mcp list
```

The default scope stores the entry in `~/.claude.json`. With `--scope project`
it goes to `.mcp.json` in your project; write the header there as
`"Bearer ${EPICOR_MCP_SERVER_TOKEN}"` so the token itself is not committed.

**Codex CLI** (`config.toml` in the `.codex` folder of your home directory)

```toml
[mcp_servers.epicor]
url = "http://127.0.0.1:8015/mcp"
bearer_token_env_var = "EPICOR_MCP_SERVER_TOKEN"
```

Or run `codex mcp add epicor --url http://127.0.0.1:8015/mcp --bearer-token-env-var EPICOR_MCP_SERVER_TOKEN`,
then check with `codex mcp list`.

**Opencode** (`opencode.json`)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "epicor": {
      "type": "remote",
      "url": "http://127.0.0.1:8015/mcp",
      "enabled": true,
      "headers": {"Authorization": "Bearer {env:EPICOR_MCP_SERVER_TOKEN}"}
    }
  }
}
```

Check with `opencode mcp list`.

**Claude Desktop** cannot use a `localhost` URL as a custom connector, because
those connections come from Anthropic's cloud rather than your machine, and its
local-server configuration has no working-directory setting. Use the desktop
connector in [bridge/README.md](bridge/README.md): add it to
`claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`,
Windows `%APPDATA%\Claude\`), then fully quit and reopen the app.

**Stdio instead of HTTP.** Codex (`cwd`) and Opencode (`cwd`) can start the
server in the repository root: use the absolute path
`/path/to/epicor-mcp-oss/.venv/bin/epicor-mcp` with the argument `--stdio`. For
Claude Code, wrap it:
`claude mcp add --transport stdio epicor -- sh -c 'cd /path/to/epicor-mcp-oss && exec .venv/bin/epicor-mcp --stdio'`.

**First success check:** ask the agent to call `epicor_tables` for "part". After
the section 3 metadata build it lists matching whitelisted tables; before it,
the result is empty and explains why.

For desktop clients that need a local executable, see
[bridge/README.md](bridge/README.md) for complete build and configuration steps.
Build **on Windows** with Python and PyInstaller:

```powershell
cd bridge
python -m venv .buildvenv
.\.buildvenv\Scripts\Activate.ps1
.\build_exe.ps1
```

Distribute `bridge/dist/epicor_mcp_oss_bridge.exe`. Users configure
`MCP_SERVER_URL`, `MCP_AUTH_MODE=none` or `token`, and optionally
`MCP_SERVER_TOKEN`. The executable does not contain your server credentials
and does not need rebuilding when its server URL changes. Linux/macOS builds
use `bridge/build_exe.sh` and produce a native executable for that OS.

Direct local stdio is also available with `epicor-mcp --stdio`; it uses the
same SSO-disabled table policy and local server configuration. Microsoft SSO,
when enabled, requires the HTTP server and OAuth-capable connector. See
[docs/microsoft-sso.md](docs/microsoft-sso.md).

## Troubleshooting and development

- Startup names missing settings: fill in `.env` and check the working directory.
- Startup rejects the whitelist: copy the example, use one table name per line,
  and remove wildcard expressions. A blank file intentionally grants nothing.
- Tables/fields are empty: build your local metadata and restart; ensure the
  desired tables are allowed.
- Epicor returns 401/403: check service-account permissions, company URL, and
  the API key's method scope. A shared MCP token does not replace Epicor auth.
- A query returns `epicor_unreachable` or `epicor_auth_error`: the server could
  not reach Epicor or Epicor rejected its credentials. Fix the configuration and
  restart; rewriting the SQL will not help.
- Help has no results: import documents and try a literal phrase from the text.
- Remote client gets a host/origin error: match the public URL setting to your
  proxy hostname; configure `EPICOR_MCP_MCP_CLIENT_ORIGINS` for your client.
- Bridge cannot start: run `--check` with its environment variables set. Windows
  executable builds must be performed on Windows.

Run the deterministic suite with no live Epicor access:

```bash
python -m pip install -e '.[dev]'
.claude/e2e-gate.sh
# Windows: python -m pytest tests -q
```

The repository keeps the established engine modules even where they are not
registered as public tools. The supported public surface is the five tools
listed above. Each package carries a short `CLAUDE.md`/`AGENTS.md` stating its
public interface and invariants for contributors and coding agents.

Start with the [repository guide](AGENTS.md) for the directory map and
[public design](docs/design.md) for the architecture and authorization boundaries.
The offline gate also runs `python scripts/check_repo_docs.py` to check local
Markdown links, quoted repository file references in prose and Python comments/
docstrings, required guide pairs, their 60-line limit, and retired references.
Generated data paths in setup examples are operator inputs/outputs, not bundled files.

## License

The framework is licensed under the [Apache License 2.0](LICENSE).
See [NOTICE](NOTICE). Imported Epicor documentation, schema exports, embedding
models, and third-party dependencies retain their own terms; they are not
relicensed by this repository. Epicor MCP OSS is independently developed and is
not an official Epicor product, nor endorsed by Epicor.

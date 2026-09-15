# Architecture and behavior

This document describes the code shipped in this repository. Installation,
imports and launch commands are in the [README](../README.md); optional identity
setup is in [Microsoft SSO](microsoft-sso.md). No private design notes, benchmark
corpus or earlier checkout is required to use these contracts.

## Public surface

The [server](../src/epicor_mcp/server.py) exposes exactly five tools:

| Tool | Responsibility |
|---|---|
| `epicor_query` | Execute one supported SELECT or an existing saved BAQ; optionally save an executed SELECT when SSO and permission allow it. |
| `epicor_tables` | Find tables in operator-imported metadata, applying the configured table policy. |
| `epicor_fields` | Find physical columns and useful keys for requested tables; distinguish unverified OData projections. |
| `epicor_help` | Search operator-imported documents, with optional local semantic retrieval. |
| `epicor_dashboards` | Resolve dashboard names to BAQ ids; return them to the query tool for authorized execution. |

The modules under [tools](../src/epicor_mcp/tools/AGENTS.md) retain legacy engine
helpers. They are not additional public tools. Public configuration fixes
`public_surface=True` and rejects development identity bypasses.

## Configuration and identity

[Settings](../src/epicor_mcp/config.py) reads `EPICOR_MCP_*` configuration from the
environment and optional `.env`. Relative paths resolve from the working
directory. The configured Epicor URL is the complete company OData base; the
server does not insert a company id.

`EPICOR_MCP_PLANTS` supplies optional site-code/name hints through `.env` or the
process environment. Each server includes its own validated map in MCP
initialization instructions. Missing or blank settings provide no hints; the
map never grants table access or rewrites submitted SQL.

[CredentialManager](../src/epicor_mcp/auth/credentials.py) loads explicitly
configured credentials. A blank credentials-file path opens no home-directory
file. Hosts, API keys, schema exports, documents, vectors and site maps are
operator inputs, not package data.

With `auth_mode=none`, the server uses its Epicor service account and table
whitelist. It registers no OAuth or admin routes and refuses BAQ saving before
any Epicor execution. The shared server token can protect the HTTP endpoint;
local stdio is available only in this mode. Both modes install the argument
guard and, unless `EPICOR_MCP_AUDIT_LOG_ENABLED=false`, the audit hook outside
it; without SSO the recorded principal is the shared read-only session.

With `auth_mode=azure_ad`, Microsoft token validation establishes the caller's
identity. User profiles and Epicor menu access supply authorization. The
operator's optional `epicor_group_to_department` map assigns department names;
missing or empty mappings infer none, and malformed mappings fail clearly.
Mapping a department does not grant tables or BAQ-save rights. Explicit profiles
retain their configured attributes. The [RBAC guide](../src/epicor_mcp/rbac/AGENTS.md)
and [SSO setup](microsoft-sso.md) describe configuration and missing-user behavior.

Microsoft identity is used for authorization and attribution inside this server.
The [Epicor client](../src/epicor_mcp/epicor_client/AGENTS.md) performs REST calls
with the configured service account and the selected API key; it does not turn
a Microsoft token into an Epicor token. SSO sessions and handshake state are
in memory, so SSO operation uses one server process.

## Authorization boundaries

The [denylist](../src/epicor_mcp/sql/denylist.py) overrides every grant, including
SecurityMgr. The operator blacklist can add denials. Denied tables and columns
must also be hidden from discovery, local column-error suggestions and generated
diagnostic queries; a helpful error must not expose a forbidden schema.

In SSO-disabled mode, [TableWhitelist](../src/epicor_mcp/rbac/table_whitelist.py)
applies exact, case-insensitive `Schema.Table` grants. A missing or malformed
configured file fails startup. An empty file grants no tables; only an explicitly
blank path disables this whitelist. The built-in denylist still applies.

In SSO mode, menu-derived scope applies to ad-hoc SQL and discovery. Identity,
menu and table-mapping failures deny affected access. Saved BAQ reads and
dashboard resolution deliberately follow Epicor's existing grant model rather
than this separate menu-derived scope. Saved definitions still pass the hard
denylist; SSO-disabled saved reads also pass the whitelist.

Table authorization consumes Epicor's parsed `QueryTable` rows, including joins
and subqueries, rather than guessing from SQL text. Derived-table aliases are
not physical table grants. The [scope gate](../src/epicor_mcp/sql/scope_gate.py)
uses the same resolved-table extraction as the denylist.

## Ad-hoc SQL pipeline

[WedgeRuntime](../src/epicor_mcp/wedge_server.py) validates the seven query
parameters and dispatches to SQL execution, saved-BAQ execution or run-and-save.
The [SQL pipeline](../src/epicor_mcp/sql/adhoc.py) then proceeds in this order:

1. Transpile the supported SELECT subset and check page reachability.
2. Validate column names against deny-filtered physical metadata when available.
3. Parse with Epicor's `BAQDesignerSvc/ParseFromSQL`.
4. Apply the denylist and injected caller/table scope to the resolved dataset.
5. Lint that dataset and apply the cost governor.
6. Execute with `DynamicQuerySvc/Execute` under runtime limits.
7. Attach applicable grain, domain and zero-row diagnostic guidance.

The denylist precedes lint because lint can return column suggestions. Local
column checking reads the physical catalogue generated by the documented import
commands from `data/schema_catalogue.json` in the working directory. Field types
are retained; missing, malformed, and unverified Swagger metadata cannot prove
absence and therefore make this check abstain. It never replaces the later
authorization check. The transpiler likewise is a compatibility
transformer, not a security boundary.

A successful HTTP status alone does not establish a correct result. Parse and
execution error payloads are checked. Unsupported shapes are refused when a
rewrite could change the answer: examples include unsafe DISTINCT/row-bound
combinations and set-operation ordering. Applied rewrites are announced.

The SQL package does not persist definitions. It compiles and executes in-memory
tablesets; save operations live in the separate `baq_ops` package. The endpoint
boundary is enforced by [the no-write-methods test](../tests/test_query_no_write_methods.py).

## Bounds and response semantics

The [governor](../src/epicor_mcp/sql/governor.py) applies shape checks to ad-hoc
SQL and maintains a shared concurrency limit and per-session time budget.
Default settings allow two inflight executions, a 25-second execution timeout,
and 120 seconds of query time per session budget window. Stopping the client's
wait does not establish that Epicor cancelled its server-side work.

`TOP` bounds the whole query result. `PageSize` and `PageNum` select a window
inside that bound. A server-injected bound equals page size, so a later page
cannot discover additional source rows. Such a request is refused; keyset
paging is the supported continuation. No signed paging cursor is issued.
Full pages and size-truncated results do not claim completeness.

Query rows use TSV: the first line is the header and an empty field means null.
Response channels have distinct responsibilities:

| Channel | Meaning |
|---|---|
| `assumptions` | SQL rewrites, injected bounds and other server transformations. |
| `notes` | Advisory findings about returned rows. |
| `grain_checks` | Suggested queries to measure possible join fan-out; never run automatically. |
| `diagnosis` | Bounded investigation of a successful zero-row first page. |
| `next_step` | Actionable guidance assembled from the applicable channels. |
| `saved` | Outcome of an explicitly requested BAQ save and verification. |

Zero-row diagnostics run only after successful first-page execution and repeat
the query's authorization checks. A measured domain outranks a dated snapshot;
static information cannot silently become authoritative. A likely mistake can
make the result nonterminal, but diagnostics preserve its rows and success state.

## Saved BAQs and optional saving

The [saved runner](../src/epicor_mcp/baq_ops/saved_run.py) reads a definition before
executing it. An unreadable definition or one with no physical database table
is refused. Unresolved parameter or cross-subquery references are tolerated on
this path, while known forbidden tables/columns remain denied.

Saved BAQs omit the ad-hoc shape-based cost check because parameter filters need
not appear as literals in their definitions. Runtime time and concurrency limits
still apply. Public saved-query paging beyond page 1 is unsupported.

Saving requires SSO, an installed [save-permission callback](../src/epicor_mcp/baq_ops/gate.py),
and the caller's request-time permission. Tool and parameter help advertise
saving only when that server capability exists; listing tools does not evaluate
a caller's permission. The right is `read_write` access or an explicit
`can_write_baqs` profile grant. No department assignment creates that right.

The [writer](../src/epicor_mcp/baq_ops/save.py) accepts only `AUTO-` identifiers.
It runs the SQL first, checks existence and the new definition before deletion,
keeps a restoration snapshot, then writes and verifies the saved BAQ. Reusing a
name overwrites that BAQ in the configured Epicor environment. Save failures
preserve the already-returned query rows and report the failed save separately.

## Metadata and document retrieval

Operators import their own physical schema catalogue and documents. Swagger
projections are useful BO metadata but must not be advertised as verified SQL
columns. [Discovery](../src/epicor_mcp/discovery/AGENTS.md) supports metadata-only
operation without vectors or a model server and returns actionable empty results
before import.

Discovery vectors are an opt-in build (`--model` for a local Sentence
Transformers model, or `--endpoint` with `--model` for an OpenAI-compatible
server the operator runs). The index manifest records provider, model and
dimension; the server verifies the arrays at load and uses them only when
`vector_search_enabled` is on and the configured provider, model and dimension
match. Every other case ranks by substring, reports `search_mode: "substring"`
and names the reason under `notes`.

[Document retrieval](../src/epicor_mcp/index/AGENTS.md) defaults to deterministic
case-insensitive substring matching. Semantic retrieval is optional. Its model,
vector dimensions and corpus hashes must agree, and failures fall back to
substring retrieval. Runtime loading uses local model files only; explicit
operator indexing is the step that may download a selected model.

## Verification

Run [.claude/e2e-gate.sh](../.claude/e2e-gate.sh) from the repository root.
The offline suite covers authorization boundaries, registered schemas, SQL
refusals and transformations, saved-query behavior, retrieval and the connector.
[Verification notes](verification.md) distinguish completed local checks from
live Epicor, Windows execution and production-model validation. These tests
protect the shipped behavior without requiring access to an original deployment.

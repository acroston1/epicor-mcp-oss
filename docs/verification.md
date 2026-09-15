# Verification scope

Latest offline gate: **2,418 passed, 0 failed, 0 skipped**, exit 0.
Verified 2026-09-15 after classifying Epicor connection and credential failures
separately from SQL errors and adding per-client connection instructions.
Two dependency deprecation warnings remain; neither is a test failure.

The release is verified with synthetic fixtures and an unreachable dummy Epicor
URL. No live Epicor tenant was queried or modified during extraction.

- The deterministic gate covers query validation and execution routing,
  allowlist enforcement, SSO-disabled saving refusal, capability-aware saving help, authorization,
  substring retrieval and optional embedding failure paths.
- Semantic discovery regressions build the index with a deterministic stand-in
  encoder and a recording endpoint stub: manifest provider/model/dimension are
  recorded, verified at load, used only when the configuration matches, and every
  fallback (switch off, model mismatch, corrupt arrays, provider outage, wrong
  query dimension) ranks by substring and names the reason under `notes`.
- The SSO-disabled server is exercised through the real tool manager and HTTP
  transport: every call, including a refused argument shape, lands in the audit
  log attributed to the shared principal; disabling the log is reported by `/health`.
- Operator SSO mapping tests cover both identity paths, empty/unmapped groups,
  invalid configuration, explicit-profile precedence, and reload/cache behavior.
- Documentation validation checks local links, heading targets, retired references,
  quoted source-file references in Python comments/docstrings and Markdown prose,
  and 18 identical guide pairs, each no longer than 60 lines. The extracted source
  archive passes the same check, including the guides under `data/`.
- Actual schema import outputs feed default column validation from the working
  directory. Regression tests preserve field types, refuse invented columns,
  separate schemas, and abstain on missing, malformed or Swagger-only metadata.
- Generic SQL diagnostic regressions exercise wildcard and free-text predicates
  with synthetic inputs. They require useful suggestions without embedded
  installation anecdotes, unrelated quoted identifiers or measured record counts.
- Site-map regressions cover `.env` and process configuration, blank/default
  values, invalid mappings, MCP initialization hints and isolation between server
  instances. Initialization is exercised without SSO, with SSO, and with SSO
  before a service index exists. Site hints do not change the table policy or
  submitted SQL.
- Content review includes runtime messages, fixtures and source comments, not
  just organization-name searches. Known installation identifiers and observations
  are removed or replaced with synthetic examples. A passing functional suite
  alone does not establish that every source string is suitable for publication.
- The scoped post-fix review found no remaining issues in these changes.
  Source and release-archive scans found no known private identifiers, private
  installation paths, non-example email addresses or embedded credential signatures.
- A fresh Python environment installed the base package and development
  dependencies successfully without Torch, FAISS or Sentence Transformers.
- Both the source distribution and wheel built successfully. Archive inspection
  found no runtime databases, vectors, real configuration or credentials.
- PyInstaller produced a Linux connector executable. The compiled connector
  completed an actual stdio → HTTP exchange against the server: initialization,
  five-tool listing, document search, metadata discovery, table refusal and
  save refusal, with shared-token authentication.
- After removing the obsolete connector email/file resolver, both the Python
  connector and a rebuilt Linux executable passed local MCP initialization,
  tool listing and tool invocation with authentication disabled and with a
  shared token. Tool arguments are forwarded without reading client-side files.
- The optional document vector pipeline ran with the actual SentenceTransformer
  API and a small locally constructed test encoder. Building, loading, searching
  and falling back after a model mismatch passed without model downloads. This
  verifies integration, not the retrieval quality of any production model.

Windows executable builds and interactive Microsoft SSO have not been exercised
on a Windows desktop or a newly configured Microsoft tenant. The repository
includes Windows build scripts and configuration instructions. Operators must
validate their Epicor API version, account permissions, metadata and chosen
embedding model against their own installation.

Schema and retrieval checks use small synthetic inputs. They do not measure
discovery quality over an operator's full corpus or chosen production model.
See the [fixture policy](../tests/fixtures/README.md) for the scope of that coverage.

## Publication disclosure review

Reviewed 2026-09-15 10:15 AM CST after a final cleanup of source comments,
fixtures, and diagnostic wording. No confirmed secret or proprietary business-data
finding remains in the files eligible for Git publication.

- Reviewed all 341 publishable files, including hidden configuration examples,
  source, tests, fixtures, documentation, and build/deployment scripts.
- Removed a historical pay-rate/count anecdote and installation-specific
  catalogue sizes, payload measurements, crawl timings, and record-count anecdotes.
  Synthetic fixture values and test assertions are unchanged; two wildcard-query
  refusal messages now explain response-size risk without installation measurements.
- Gitleaks 8.30.1 with its default rules, full redaction, and no local suppressions
  found no secrets in the publication files or working tree. Additional checks
  inspected credential assignments, emails, hosts, paths, identifiers, and business
  examples; an independent semantic review rechecked the corrected findings.
- The only organization-name reference is the copyright attribution in `NOTICE`.
  Configuration examples contain placeholders, and fixture identities use reserved
  example domains. No real customer/vendor records, transaction amounts, private
  endpoints, or credentials were identified.
- Git has no commits, refs, stored objects, or remotes; there is no inherited
  history to scrub. The ignored local token database contains zero token rows.
- Fresh wheel and source-archive inventories contain only the expected source and
  package metadata. No databases, logs, bytecode, private configuration, documents,
  or generated indexes are included. Packaged source matches the reviewed files.
- Post-cleanup gate: **2,418 passed, 0 failed, 0 skipped**. No Epicor connection,
  commit, push, or publication was performed.

This review applies to the current source and rebuilt packages. It combines
pattern scans with contextual inspection; it does not establish the original
provenance of every fixture or guarantee detection of every possible secret.

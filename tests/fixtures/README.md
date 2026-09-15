# Synthetic regression fixtures

The parsed_ds fixtures contain minimized parser dataset shapes for SQL engine
regressions. Identifier UUIDs and test users are synthetic; they contain no
business rows, database dump, document corpus, or vector data.

The private benchmark corpus and full physical schema export are excluded.
The static card tests retain bounded width, known invalid-column exclusions,
and deterministic output checks; they do not claim validation against a
licensed physical-schema corpus. Operators should validate their imported
metadata against their own Epicor version.

Offline coverage uses the minimal generated `synthetic_catalogue.py` fixture
for column types, schema-qualified names, recovery-list limits and deny filtering.
The star-query parser fixtures contain three example columns, not a physical
schema export. User identities, plant names and business numbers are invented.

The removed private discovery-quality benchmark depended on a proprietary
schema corpus and one particular hosted embedding model. It cannot describe
an operator-selected model and is not part of this release gate. The replacement
checks build and search an actual small local substring index. Optional document
vector tests exercise the real build/query adapters with a deterministic encoder
substitute; they cover model selection, normalization, vector dimensions, corpus
changes and fallback behavior, not the retrieval quality of a downloaded model.

Legacy Microsoft-authenticated save/RBAC tests select `azure_ad` explicitly.
SSO-disabled tests separately verify read-only operation, optional shared-token
HTTP access, disabled admin/OAuth routes, exact table grants, saved-query table
inspection, joins/subqueries and diagnostic probes. No tests are skipped because
private data is absent. External DNS and network connections are prohibited by
the test fixtures. Live Epicor writes/reads are never invoked by the gate.

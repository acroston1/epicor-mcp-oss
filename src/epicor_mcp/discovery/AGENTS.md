# Table and field discovery

Purpose: serve `epicor_tables` and `epicor_fields` from imported metadata.

Public interfaces: `DiscoveryIndex.load/empty`, table resolution/search methods,
`register_discovery_tools`, `TableAuthorizer` (Microsoft menu scope), and
`embeddings.embedder_for_index(settings, manifest)` with its two providers,
`LocalEmbedder` (sentence-transformers) and `EndpointEmbedder` (OpenAI-style).

Invariants:
- Register both tools even before metadata import; return actionable empty data.
- A metadata-only index needs no vector arrays, model server, or numpy import.
- Substring search covers names, labels and descriptions; ties are deterministic.
- Vectors are optional: `scripts/build_discovery_index.py --model` writes them and
  records provider, model and dimension in the manifest. `DiscoveryIndex` verifies
  the arrays at load and ignores a query vector of another dimension.
- Semantic ranking runs only when `vector_search_enabled` is on AND the configured
  provider, model and dimension match the build. Every other case ranks by
  substring, reports `search_mode: "substring"` and says why under `notes`.
- Runtime model loads are local-files-only; only the build script may download.
- Server-injected authorization applies to primary results, UD mirrors and
  cross-table suggestions. Table/column denial callbacks fail closed.
- Qualified table requests must match the imported schema; never resolve an
  `Ice.X` request to `Erp.X`. Duplicate unqualified names are refused at import.
- Imported Swagger projections must never be presented as verified SQL fields.

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
- A comma/semicolon/newline `query` is ranked PER TERM (`rank.split_terms` ->
  `DiscoveryIndex.search_fields_terms`, merged round-robin, `limit` raised to one
  slot per term) in BOTH modes: semantic uses `fuse(per_term=True)`, substring uses
  `_lex_term_fields`. Both add `term_name_match`, an exact-name bonus and the
  `LOCALE_PREFIXES` penalty. A query with no separator takes the original path
  byte-for-byte (pinned); a slash is NOT a separator (`site/plant` is one term).
- Field entries are compact: `name`, `type`, `label`, plus `description` only when
  it says more than the label (cut at 100 chars). No per-field `primary_key` or
  `required`. `also_named_on_other_tables` fires only for terms nothing served answers.
- Runtime model loads are local-files-only; only the build script may download.
- Server-injected authorization applies to primary results, UD mirrors and
  cross-table suggestions. Table/column denial callbacks fail closed.
- Qualified table requests must match the imported schema; never resolve an
  `Ice.X` request to `Erp.X`. Duplicate unqualified names are refused at import.
- Imported Swagger projections must never be presented as verified SQL fields.

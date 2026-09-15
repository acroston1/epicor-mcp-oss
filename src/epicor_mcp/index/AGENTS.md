# Retrieval indexes

Purpose: import and search administrator-owned schema/document metadata.

Public interfaces: `ServiceIndex`, `BAQSchemaIndex`, `DocsIndex`,
`local_retrieval.register_local_retrieval(mcp, settings, table_authorizer)` and
`local_retrieval.discovery_embedding(settings, index)`, which yields the discovery
tools' `embed_query`/`search_note` pair. The registration hook returns resources
(indexes, embedder) the server must close on shutdown.

Invariants:
- Do not bundle customer schema, vendor documents, vectors, or derived indexes.
- Base document retrieval is deterministic casefold substring matching and has
  no numpy, FAISS, torch, sentence-transformers, network, or model-download step.
- Semantic documents and discovery vectors are opt-in behind `vector_search_enabled`.
  Manifest provider, model and dimension (plus corpus hash for documents) must agree
  with the configuration; failures fall back to substring search and are announced.
- Runtime model loads use `local_files_only=True`; the indexing CLI is the only
  path permitted to download the explicitly selected embedding model.
- Physical SQL metadata comes from BAQDesigner read APIs or an administrator
  catalogue. Swagger-only metadata is labeled as an unverified BO projection.
- Credential access belongs to `auth/credentials.py`; builders never inspect
  a user's home directory for credentials or private external scripts.

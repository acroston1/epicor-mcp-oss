# Operator scripts

Purpose: import operator-owned schema, documents, and optional SSO metadata.

## Entry points
- `build_schema_catalogue.py`: physical metadata via Epicor read APIs.
- `bootstrap_schema.py`: local Swagger import, optionally with a physical catalogue.
- `build_index.py`: service-index-only wrapper; `build_baq_index.py`: JSON dictionary to SQLite.
- `build_discovery_index.py`: catalogue to table/field metadata; `--model` (local) or
  `--endpoint --model` (OpenAI-compatible server) add optional vectors.
- `build_docs_index.py`: text/JSON and optional PDF chunk ingestion.
- `build_document_vectors.py`: optional embeddings with an operator-selected model.
- `build_menu_map.py`: optional SSO menu/application import.
- `check_repo_docs.py`: offline guide, local-link, quoted source-path, and retired-reference validation.
- Run from the repository root; see [setup](../README.md) for formats and commands.

## Invariants
- Network imports use configured read methods and never save Epicor records.
- Credential handling belongs to `epicor_mcp.auth.credentials`.
- Inputs and generated databases, vectors, reports, and caches stay private.
- Base ingestion needs no model or GPU; only explicit vector builds may download models.
- Preserve the distinction between physical SQL metadata and Swagger projections.

## Gotchas
- Rebuilding replaces indexes; restart the server and rebuild stale vectors afterward.
- Guide pairs stay identical and <=60 lines; the documentation checker uses the standard library.

# Local operator data

This directory is empty in the source distribution except for documentation and
synthetic configuration examples. Setup scripts generate metadata databases,
document chunks, and optionally document vectors here. They are gitignored. The
server also writes its audit log here (`audit.db` by default) unless
`EPICOR_MCP_AUDIT_LOG_ENABLED=false`.

- [build_schema_catalogue.py](../scripts/build_schema_catalogue.py): physical SQL catalogue, BAQ metadata, and substring
  discovery from your own Epicor REST metadata endpoints.
- [build_discovery_index.py](../scripts/build_discovery_index.py): table/field metadata;
  `--model` or `--endpoint` add optional vectors to the index directory.
- [bootstrap_schema.py](../scripts/bootstrap_schema.py): service metadata from your own Swagger exports, with an
  optional authoritative physical catalogue.
- [build_docs_index.py](../scripts/build_docs_index.py): optional local documentation import.
- [build_document_vectors.py](../scripts/build_document_vectors.py): optional embeddings for imported documents.

Do not publish data, audit logs, OAuth stores, secrets or vendor documents.
Rebuild indexes after changing input data; restart the server after rebuilding.
See the root README for exact commands and formats.

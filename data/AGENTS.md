# Local operator data

Purpose: hold private configuration, imported metadata, and generated retrieval files.

## Entry points
- [README.md](README.md): data lifecycle.
- [Setup](../README.md): commands and operator inputs.
- [Scripts](../scripts/AGENTS.md): builders that create files here.

## Invariants
- Never distribute credentials, tenant maps, schema exports, documents, or runtime records.
- Databases, vectors, caches, audit records, and OAuth stores are generated locally.
- Root and directory ignore files protect private contents.
- Exceptions permit guides, README, and explicitly synthetic examples only.
- Never link another installation's private/live data into this directory.

## Gotchas
- Relative paths resolve from the server working directory.
- Rebuild indexes from operator inputs, and vectors after corpus/model changes.
- Restart the server after rebuilding; guide pairs remain identical and <=60 lines.

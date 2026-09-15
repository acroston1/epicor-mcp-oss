# Deterministic tests

Purpose: verify runtime, authorization, retrieval, and documentation without a live tenant.

## Entry points
- [The gate](../.claude/e2e-gate.sh) runs documentation checks and pytest after installing `.[dev]`.
- `python -m pytest tests -q` runs tests directly; target a file to diagnose failures.
- `conftest.py`: environment and network isolation.
- `fixtures/`: synthetic catalogues, parser datasets, and adapters; see [policy](fixtures/README.md).
- `harness/sql_lint.py`: independent SQL scoring helper.
- `test_oss_*.py`: portable setup, read-only policy, documents, HTTP, and bridge.

## Invariants
- Never delete, weaken, or skip assertions to pass the gate.
- Use synthetic hosts, identities, keys, records, and metadata.
- Mock external services; never use live credentials or network access.
- Test denials before execution as well as successful allowed reads.
- Test exposed tool descriptions through real registration and serialization.
- Keep expected results independent of the implementation under test.
- Contract changes need matching tests and documentation.

## Gotchas
- Deterministic encoder tests verify adapters and fallback, not model retrieval quality.
- Restore shared configuration between tests; keep guide pairs identical and <=60 lines.

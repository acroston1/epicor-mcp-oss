# response/

Purpose: shrink and size-guard tool results before they reach the model.

## Public interface
- `format_response(result, *, records_key="records", format="json", strip_only=False, settings=None) -> str`
- `strip_bloat`, `truncate_and_summarize`, `rows_to_csv`, `compute_stats`
- `offload_response`, `purge_stale_offloads(offload_dir, retention_hours)`
- `BLOAT_KEYS`, `BLOAT_PREFIXES`: the stripped OData/system noise.
Everything else is private.

## Invariants
- Output is always a JSON string; non-dict results pass through unshrunk.
- `response_max_bytes` (100 000) triggers offloading or truncation for normal dict
  responses; it is not an unconditional output ceiling. Non-dicts and `strip_only=True`
  bypass the size guard.
- Truncation starts at `response_truncate_keep` with `compute_stats` summaries
  (`response_stats_top_k`) and progressively reduces rows. The final fallback retains
  statistics and diagnostics, which can themselves exceed the budget.
- Offloading is off by default (`response_offload_enabled=false`). When on, files go to
  `response_offload_dir`, are served under `response_public_base_url`, and expire after
  `response_offload_retention_hours`. Nothing is offloaded silently.
- Bloat stripping (`response_aggressive_strip`, `response_drop_empty`) removes keys, never
  rewrites values; `response_max_string_chars` truncates long strings and says so.

## Gotchas
- `sql/adhoc.run_sql` enforces its own `max_bytes` on TSV rows; check both places when
  changing a size limit.

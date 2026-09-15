# tools/

Purpose: the legacy intent tools (`epicor_read`, `epicor_baq`, `epicor_act`, ...)
retained as engine libraries. The public surface is the five tools registered
elsewhere; nothing here is listed or callable on it.

## Public interface
- `register_tools(server, index, rbac, client, dataset_handler, ...)`: registers the legacy
  surface. Only the SSO (`azure_ad`) app path calls it, and `public_surface=True` (forced by
  `Settings`) filters those tools from `tools/list` and blocks calls with `tool_not_available`.
- `_argguard.install_validation_guard(mcp)`: argument screening installed by `server.py`.
- `_baq_helpers`: designer-tableset helpers imported by `baq_ops/save.py`.
- `_tenant.PLANTS`, `match_plant`, `plant_lines`, `commercial_brand_lines`: private
  compatibility helpers. `PLANTS` stays empty; public hints use per-app `Settings.plants`.
Everything else is private to this package.

## Invariants
- No site codes, customer codes, field semantics, hosts or people are hardcoded anywhere
  in this package. `_tenant.py` never reads environment or `.env` at import and is
  never populated from an app's site map; shared globals must not leak app settings.
- The `none`-mode app never calls `register_tools`; importing the package for
  `_argguard` or `_baq_helpers` registers nothing.
- Write-capable modules (`act.py`, `workflow.py`, `run_method.py`, `baq_create.py`,
  `baq_delete.py`) are never registered in `none` mode and stay behind RBAC in SSO mode.
- `_EMPTY_AT_TENANT` in `read.py` is intentionally empty: no tenant-specific
  empty-table assertions are bundled.

## Gotchas
- `read.py`, `_partviews.py` and `run_baq.py` bind `PLANTS` at import; tests monkeypatch
  all three plus `_tenant` (see `tests/test_partviews.py::configured_plants`).
- Query and authorization boundaries are documented in [design](../../../docs/design.md).
  Keep compatibility explanations inline; do not cite unavailable private notes.

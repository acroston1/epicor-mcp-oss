# rbac/

Purpose: decide which tables a caller may read. `auth_mode=none` uses the
operator's table whitelist; `azure_ad` uses per-user Epicor menu-derived scope.

## Public interface
- `table_whitelist.TableWhitelist.from_file(path)`, `.allows(name)`, `.active`,
  `.is_unlimited`, `.state`, `.note()`; `normalize_table(value)`.
- `table_whitelist.NoneTableAuthorizer(whitelist)`: the authorizer contract
  (`resolve_identity`, `scope_for(email)`, `evict`, `evict_all`) consumed by
  `sql/scope_gate.check_table_scope` and `discovery/`.
- SSO mode: `RBACEnforcer`, `AccessLevel`, `AccessCheckResult`; `UserMap`, `UserProfile`;
  `MenuAuthorizer`, `UserAuthzSnapshot`, `MenuMapStore`, `EpicorAuthzClient`.
- `UserMap.epicor_group_to_department` returns a defensive copy of the operator map;
  `UserMap.reload()` validates replacement configuration before adopting it.
- `epicor_user_resolver.validate_group_map(value)` validates exact group-to-list mappings;
  `EpicorUserResolver(..., group_map=...)` and `.set_group_map(map)` copy that mapping.
- Offline builder: `menu_map_builder.MenuMapBuilder`, `write_menu_map_db`
  (used by `scripts/build_menu_map.py`).

## Invariants
- Whitelist entries are exact `Schema.Table`; a bare name means `Erp.`; case-insensitive;
  `Erp`/`Ice` only; wildcards rejected. `Part` never grants `Part_UD`; `Erp.X` never grants `Ice.X`.
- `from_file("")` (the explicit blank setting) is unlimited. A missing file raises; a
  malformed line raises with `file:line`; an empty file denies every table.
- `allows()` checks `sql/denylist.is_denied_table` first: a denied table is refused even when listed.
- Every authorizer failure (exception, unavailable scope, empty identity in gate mode)
  fails closed with zero Epicor calls.
- Menu-derived scope adds `discovery/baseline.BASELINE_TABLES` and `_UD` mirror
  inheritance; the whitelist adds neither.
- `UserMap` admin keys come only from `EPICOR_MCP_CREDENTIALS_PATH` (`api_key`,
  `baq_api_key`, `write_api_key`) or `department_keys.json`; never from `$HOME`.
- Epicor group mappings default to empty. Keys and department names are non-empty,
  case-sensitive strings without surrounding whitespace; values must be lists.
  Missing/unmapped groups add no departments. Explicit profiles take precedence.
- Department mapping does not grant table access or BAQ-save rights. Existing BAQ
  authoring-group checks and explicit profile permission flags remain separate.
- `/admin/reload` updates both SSO resolution paths and clears their derived caches;
  an invalid Epicor group mapping returns 400 and leaves prior configuration active.

## Gotchas
- `menu_authz_mode=enforce` refuses startup without `data/menu_security.db`;
  `shadow` only records disagreements in the audit log.
- In SSO mode, saved-BAQ and dashboard paths are deliberately not scope-gated
  (Epicor's own grant model). In `none` mode the whitelist does gate saved-BAQ definitions.
- `table_authz_mode` is always `gate` in `none` mode; `boost` never consults the authorizer.

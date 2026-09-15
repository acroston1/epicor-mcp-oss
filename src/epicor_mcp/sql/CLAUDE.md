# sql/

Purpose: run bounded, authorized SELECTs through Epicor's BAQ engine without persistence.

## Public interface
```python
from epicor_mcp.sql import transpile, TableSchema, Policy, WEDGE_POLICY, Outcome
from epicor_mcp.sql.adhoc import run_sql, rows_to_tsv, make_probe_runner
from epicor_mcp.sql.lint import lint_parsed, Severity, qualify_tables
from epicor_mcp.sql.governor import check_cost, CostGovernor, GovernorPolicy
from epicor_mcp.sql.denylist import check_parsed_ds, is_denied_table, is_denied_column, \
    db_tables_read, denial_envelope, install_table_blacklist_from_file
from epicor_mcp.sql.scope_gate import check_table_scope
from epicor_mcp.sql.card import card_text, CARD_TABLES, CARD_COLUMNS
from epicor_mcp.sql.tool import register_query_tool, registration_decision, tool_description
from epicor_mcp.sql.envelope import error_envelope, is_envelope
from epicor_mcp.sql.validate_columns import validate_columns, load_catalogue, load_ud_mirrors
from epicor_mcp.sql.diagnose_empty import diagnose_empty
from epicor_mcp.sql.domains import ground
from epicor_mcp.sql.grain import analyse_grain
from epicor_mcp.sql.next_step import annotate_next_step
```
`run_sql(sql, *, client, api_key, base_url, page_size, page_num, governor, session_id,
max_bytes, ..., table_scope=None)` returns a result dict or an error envelope. It never
raises and never returns a bare string. `table_scope=None` is ungated and byte-identical
to pre-gate behaviour. Everything else is private.

## Invariants
1. Nothing persists. The only Epicor endpoints reachable from `sql/` are `ParseFromSQL`,
   `Execute` and `Analyze`; `tests/test_query_no_write_methods.py` scans `sql/*.py` and
   `wedge_server.py` to prove it. Saving lives in `baq_ops/`.
2. Gate order is fixed: transpile, paging, validate_columns, parse, denylist, authz
   (scope gate), lint, governor, execute. A refusal names its gate in `detail.stage`.
3. Deny beats everything, including SecurityMgr. For ad-hoc SQL an unattributable column
   reference denies (`check_parsed_ds(..., unattributed_denies=True)`).
4. One channel per claim: `assumptions` (what the server rewrote), `notes` (advisory),
   `grain_checks` (offered, never auto-run), `diagnosis` (zero-row page 1 only),
   `next_step` (derived from the others; only `WedgeRuntime._run_and_save` may overwrite it),
   `saved` (owned by `baq_ops`). A refusal is an error envelope with `retry_with`.
5. The dialect requires `select top N`; there is no ORDER BY peel. Paging refusals key on
   the transpiler's `row_bound`.
6. `load_catalogue()` reads CWD `data/schema_catalogue.json`: rich fields/types or legacy lists.
   Process env `EPICOR_MCP_COLUMN_CATALOGUE` path list wins over `EPICOR_MCP_SCHEMA_CATALOGUE`.
   Missing/malformed/Swagger metadata abstains; only Erp metadata judges Erp columns.
   `_c` recovery joins `<Table>_UD` on `SysRowID = ForeignSysRowID`; metadata verifies both keys.
7. `denylist.DENIED_TABLE_PATTERNS` and `DENIED_COLUMNS_*` may widen; they never narrow
   silently. The operator blacklist (`table_blacklist.txt`) only adds denials.
8. `domains.PLANTS`, `TABLE_ROWS` and related observations ship empty; they are operator
   input, never bundled facts. Diagnostic examples never contain installation observations.
9. `tool_description(..., saving_available=False)` and parameter help default to no saving.
   Registration advertises saving only for Azure SSO and a bound runtime with callable
   `can_save`; that callback still checks the caller's BAQ-write right per request.

## Gotchas
- `check_cost` runs on ad-hoc SQL only; saved BAQs skip it (parameters are not literals).
- `sql_execute_timeout_s` (25 s), `sql_max_inflight` (2) and `sql_session_budget_s`
  (120 s) bound every execution regardless of gate outcome.
- `validate_columns` returns `ok=False` only for a provably absent column; CTE outputs,
  unresolved aliases and uncatalogued tables report `skipped`, not failure.
- Epicor status 0/408 is terminal `epicor_unreachable`, 401/403 `epicor_auth_error`: never SQL errors.

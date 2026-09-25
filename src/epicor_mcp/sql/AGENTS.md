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
`run_sql(sql, *, client, api_key, base_url, page_size, page_num, governor, session_id, ...,
table_scope=None)` returns a result dict or an error envelope, never raises. `table_scope=None`
is ungated and byte-identical to pre-gate behaviour. Everything else is private.

## Invariants
1. Nothing persists: `sql/` reaches only `ParseFromSQL`, `Execute`, `Analyze` (proved by
   `tests/test_query_no_write_methods.py` over `sql/*.py` + `wedge_server.py`). Saving is `baq_ops/`.
2. Gate order is fixed: transpile, paging, validate_columns, parse, denylist, authz
   (scope gate), sort_key, lint, governor, execute; a refusal names it in `detail.stage`.
   `sort_key`: an ORDER BY key > 125 rendered chars fails at Execute ("column name is missing
   or empty", CASE or not), so it is wrapped in a CTE once and the whole pipe re-entered.
3. Deny beats everything, including SecurityMgr. For ad-hoc SQL an unattributable column
   reference denies (`check_parsed_ds(..., unattributed_denies=True)`).
4. One channel per claim: `assumptions` (what the server rewrote), `notes` (advisory),
   `grain_checks` (offered, never auto-run), `diagnosis` (zero-row page 1 only),
   `next_step` (derived from the others; only `WedgeRuntime._run_and_save` may overwrite it),
   `saved` (owned by `baq_ops`). A refusal is an error envelope with `retry_with`.
5. The dialect requires `select top N`; no ORDER BY peel. Paging refusals key on `row_bound`.
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
- `check_cost` is ad-hoc only (saved BAQs skip it); 25 s timeout, 2 in flight, 120 s budget bound every run.
- Epicor runs `x in (select …)` as "subquery returned ANY row". Uncorrelated → CTE join; key-correlated kept; rest refused.
- `validate_columns` returns `ok=False` only for a provably absent column; CTE outputs,
  unresolved aliases and uncatalogued tables report `skipped`, not failure.
- ON conjuncts are filed under their LEFT table; under an earlier-joined table they fail
  ("could not be bound"). Transpiler: swap sides, else WHERE (inner only), else refuse.
- Epicor status 0/408 is terminal `epicor_unreachable`, 401/403 `epicor_auth_error`: never SQL errors.

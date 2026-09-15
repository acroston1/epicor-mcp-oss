# baq_ops/

Purpose: run a saved BAQ by id, resolve a dashboard to its BAQ ids, and (SSO mode
with an explicit grant only) save an executed statement as an `AUTO-` BAQ. It is a
separate package so `sql/`'s "nothing persists" invariant stays a scanned test.

## Public interface
```python
from epicor_mcp.baq_ops.gate import SaveRight, make_can_save, save_gate_envelope, save_unavailable_envelope
from epicor_mcp.baq_ops.save import sql_for_save, sanitize_baq_name, save_query_as_baq, delete_saved_baq, AUTO_PREFIX
from epicor_mcp.baq_ops.saved_run import run_saved_baq, describe_saved_baq, coerce_baq_params, paging_unsupported_envelope
from epicor_mcp.baq_ops.dashboards import register_dashboards_tool, dashboards_description
```
`run_saved_baq(...)` returns the ad-hoc success shape or an error envelope.
`save_query_as_baq(...)` returns the `saved` response channel and never raises.
`delete_saved_baq` is never registered as a tool. Everything else is private.

## Invariants
1. `auth_mode=none` never saves: `Settings` rejects `enable_baq_create`, `WedgeRuntime`
   refuses `save_as` before any Epicor call, and the writer is self-gating as well.
2. Run first, save second. Only SQL that already parsed and executed is persisted, and
   what is saved is the transpiled `sql_executed`.
3. Save order is the safety property: existence probe (`GetByID`), `ParseFromSQL`,
   unresolved-field check, `DeleteByID` only if the probe found something, `Update`
   with restore-on-failure from a `BAQDesignerSvc/GetByID` snapshot, verification run.
   `replaced_existing` is the probe's answer, never "the delete did not throw".
4. Only `AUTO-` ids are ever written or deleted; `sanitize_baq_name` strips and the
   server re-prefixes. Re-using a `save_as` overwrites that BAQ in place.
5. A save reports success only if the BAQ both saved and ran (`verified`). A save-side
   failure returns the rows with `saved.saved: false`; rows are never discarded.
6. A saved BAQ's `GetByID` definition passes the table/column denylist before execution,
   with `unattributed_denies=False` (parameters and runtime tokens are ordinary there).
   A tableset with no DB-typed `QueryTable` row is refused, not run.
7. In `none` mode the table whitelist is applied to the saved-BAQ definition before
   execution. In SSO mode saved BAQs and dashboards follow Epicor's own grant model and
   are deliberately not scope-gated; do not add a scope check "for symmetry".
8. Dashboards resolve, never execute: ids go back to `epicor_query(saved_baq=...)`, which
   has the gate. No rung of the dashboard lookup resolves an ambiguous hit;
   `dashboard_ambiguous` is distinct from `dashboard_not_found`.

## Gotchas
- `check_cost` is not enforced on saved BAQs; the runtime budget and 25 s wall still are.
- Saved-BAQ paging is unsupported unless `allow_paging=True`; the envelope says so.
- A failed `DeleteByID` does not abort a save (SaaS nodes propagate asynchronously); it
  is reported as `saved.pre_delete_failed`.

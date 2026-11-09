# Configurable shared table reads

Add operator-managed shared table reads to the existing optional SSO menu
authorization without broadening the strict no-SSO whitelist contract.

1. Add a portable local `table_read_allowlist.txt` loader and validator command,
   configurable path, comments/exact identifiers, empty/missing no shared
   grants and malformed/unreadable startup refusal. Load once per authorizer.
   Resolve relative defaults consistently with documented installation/config
   roots and include default policy in supported distributions/container builds.
2. In SSO mode, union shared reads only after successful identity/menu/index
   resolution. Preserve unknown/disabled/failure default denial, built-in
   denials, schema boundaries and existing save rights. No new public tools.
3. Preserve no-SSO `table_whitelist_path` as the strict operator ceiling across
   discovery, fields, queries, joins/subqueries, saved definitions and probes.
   The shared SSO policy never widens it. Include approved tables in the
   editable example/policy and document which file applies in each mode.
4. Seed the shared policy with exactly these generic Epicor tables:
   Erp.PartDtl, Erp.PartMtl, Erp.SugPOChg, Erp.PegDmdMst, Erp.PegSupMst,
   Erp.PegLink, Erp.PartOpr, Erp.PartOpDtl, Erp.ARBalance, Erp.InspPlan,
   Erp.InspResults, Erp.PartBOM, Erp.PartOprInsp. PartBOM is a temporary
   definition and a grant does not imply stored/queryable data exists.
5. Add tests first for parsing, runtime/config wiring, provenance, removal,
   instance isolation, empty/malformed files, success/denial, mode/schema
   separation, strict no-SSO ceiling and packaging. Keep fixtures synthetic
   and independent of the operator's editable policy. No live ERP calls.
6. Maintain paired module/project guides and README/setup guidance. Review
   correctness against this plan, run focused tests and the existing offline
   release gate. Local source update only; no public push or release.

| Caller / request | Result |
|---|---|
| No SSO | existing exact operator whitelist and built-in denylist |
| Admitted SSO user | menu grants plus loaded shared reads, sensitive denials retained |
| Unknown/disabled/unresolved SSO identity | deny |
| Wrong schema or unlisted table outside menu scope | deny |
| Client attempt to edit policy | no tool/route exists |

Site names remain operator hints under this repository's existing contract;
do not claim that importing a table whitelist installs row-level site filtering.
MCP impact: authorization for the same five tools changes; schemas stay stable.

# Shared read policy verification

Recorded: 2026-11-09 11:54:11 AM CST. Scope: PLAN.md configurable shared reads #1–6.
All fixtures, identities, URLs, and records are synthetic. No ERP calls,
credentials, business writes, source publishing, or cloud deployment occurred.

| # | Feature | Tests | Result |
|---|---------|-------|--------|
| 1 | Exact local configuration and validation | `tests/test_oss_shared_reads.py::test_parser_normalizes_schema_case_comments_and_duplicates`, `::test_parser_rejects_invalid_policy_atomically`, `::test_invalid_utf8_never_returns_partial_shared_grants` | PASS |
| 1 | Empty/missing/unreadable, CWD paths and CLI | `tests/test_oss_shared_reads.py::test_missing_empty_and_unreadable_policy_are_distinct`, `::test_relative_policy_uses_operator_working_directory`, `::test_validator_cli_checks_local_file_without_modification` | PASS |
| 2 | SSO shared grants, provenance and failure denial | `tests/test_oss_shared_reads.py::test_successful_sso_scope_adds_exact_shared_grant_with_provenance`, `::test_shared_policy_never_masks_sso_authorization_failure` | PASS |
| 2 | Exact schemas and no implicit shared UD expansion | `tests/test_oss_shared_reads.py::test_ice_and_explicit_ud_shared_grants_do_not_expand_schema_or_parent`, `::test_real_discovery_preserves_shared_schema_and_ud_boundaries` | PASS |
| 2 | Removal and instance isolation | `tests/test_oss_shared_reads.py::test_removed_empty_missing_and_instance_policy_have_no_hidden_fallback` | PASS |
| 3 | Strict no-SSO whitelist remains the ceiling | `tests/test_oss_shared_reads.py::test_no_sso_operator_ceiling_is_unchanged_even_when_shared_file_grants_more`; existing OSS contract suite | PASS |
| 2–3 | Sensitive deny precedence | `tests/test_oss_shared_reads.py::test_sensitive_denylist_still_beats_shared_table_scope` | PASS; denied before Execute |
| 4–5 | Runtime wiring and portable policy configuration | `tests/test_oss_shared_reads.py::test_real_sso_server_receives_operator_policy`, `::test_invalid_shared_file_refuses_sso_startup`, `::test_policy_is_in_wheel_and_source_distribution_configuration` | PASS |
| 5 | Future operator edits independent of fixture permissions | `tests/test_oss_shared_reads.py::test_editable_operator_policy_can_change_without_hardcoded_test_grants`; isolated policy fixture | PASS |

Tests-first baseline: **33 failed / 2 passed in 2.24s** on the absent feature.
The final feature file contains **36 cases**, including an additional UTF-8
negative and actual SQL deny-before-Execute coverage.

Focused implemented group: **125 passed in 2.97s** (new policy, existing strict
OSS contracts, table scopes, UD inheritance, and real server wiring). Scoped
correctness audit cleared the implementation after correcting denial wording
to describe configured authorization rather than menu access alone.

Release gate: `PYTHON=<test interpreter> bash .claude/e2e-gate.sh`, **exit 0**;
documentation checks passed and **2,543 tests passed in 19.90s**, no skips or
failures. Log: `/tmp/oss-shared-reads-release-gate.log`. External network is
rejected by the existing fixture; no live tests were enabled.

Packaging follow-through: an actual `dist/epicor_mcp_oss-0.1.2-py3-none-any.whl`
was built with an isolated temporary Hatchling installation. Wheel ZIP inspection
confirmed both `shared_reads.py` and the packaged 13-entry
`table_read_allowlist.example.txt`; no public release was performed. No UI or
row-level site-filtering claims are made; operator site names remain hints in
this server.

MCP impact: updated authorization for the same five tools through the shared
SSO scope; no schema changes, additional tools, or provider enrollment.

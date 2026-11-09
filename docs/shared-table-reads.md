# Shared table read policy

There are two separate operator-controlled files:

| Mode | File | Meaning |
|---|---|---|
| `auth_mode=none` | `table_whitelist.txt` | Exact ceiling for all callers and all data paths. |
| `auth_mode=azure_ad` | `table_read_allowlist.txt` | Exact shared reads added to a successfully resolved user's menu grants. |

The SSO shared-read file never widens or disables the no-SSO ceiling. A blank
`EPICOR_MCP_TABLE_WHITELIST_PATH` remains the only explicit no-SSO opt-out.
Unknown/disabled users or failed SSO menu/index resolution receive no shared
grants. Built-in sensitive-table/column denials and `table_blacklist.txt` take
precedence over either policy. Existing BAQ-save permissions do not change.

## Edit and validate

The tracked shared policy contains thirteen generic Epicor table names for
planning, engineering, inspection and receivables. Review them before enabling
shared SSO reads, especially `ARBalance` and engineering cost visibility.
Choose a different file with `EPICOR_MCP_TABLE_READ_ALLOWLIST_PATH`.

Use one exact `Schema.Table` name per line. A bare name means `Erp.Table`;
`Erp` and `Ice` are the supported schemas. Names are case-insensitive, `#`
starts a comment, and blank lines are ignored. Wildcards, paths, SQL and
bracketed names are rejected. A shared parent does not grant its `_UD` mirror;
list a mirror explicitly if it should be shared. Existing menu-derived mirror
inheritance remains independent of these exact shared grants.

From the configured working directory:

```bash
python -m epicor_mcp.discovery.shared_reads --check
# Or validate a specific local file:
python -m epicor_mcp.discovery.shared_reads --check --path ./my-shared-tables.txt
```

The command reports canonical names and count, makes no ERP calls, and changes
no files. It validates identifiers, not the existence or contents of a table.
`PartBOM` is a temporary definition in some Epicor versions: whitelisting it
does not create stored data or guarantee that a SELECT will return rows.

Relative paths use the working directory, like `.env` and other OSS settings.
A missing or empty shared file adds no grants, and a blank setting disables
shared reads. Invalid or unreadable files refuse gated SSO startup. There is
no hidden fallback list. The source checkout carries the operator-editable
root file; wheels include its contents as
`epicor_mcp/discovery/table_read_allowlist.example.txt` for explicit copying,
not automatic use when an operator file is absent.

## Activate or remove a grant

1. Edit the appropriate file for the active authentication mode.
2. Validate the shared file, and run `.claude/e2e-gate.sh` from the source tree.
3. Restart the server and reconnect clients. Files are read once per authorizer;
   cache eviction alone does not reload shared policy.
4. Verify the intended table/field discovery and query, plus an unlisted table
   denial, using the deployment's ordinary authentication.

Remove a line and restart to withdraw the shared grant. The table can remain
available through a user's menu grants; use the blacklist for an absolute
denial. The bundled example for the no-SSO whitelist includes the same table
names, but operators copy only entries they intend to expose.

This feature adds table authorization only. Operator site-name hints are not
row-level filters. This repository does not install tenant-specific site
maps, and shared reads do not imply that site isolation has been configured.

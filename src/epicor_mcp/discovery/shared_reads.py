"""Validate local, exact shared-read grants for optional Microsoft SSO."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from epicor_mcp.rbac.table_whitelist import normalize_table


def read_allowlist(path: str | Path | None = "table_read_allowlist.txt") -> frozenset[str]:
    """Read canonical Schema.Table grants; relative paths use the working directory.

    None, a blank setting, and missing/empty files add no shared reads. An
    unreadable or malformed file raises before any partial set can be returned.
    """
    if path is None or not str(path).strip():
        return frozenset()
    source = Path(path)
    try:
        contents = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    entries: set[str] = set()
    for number, line in enumerate(contents.splitlines(), 1):
        entry = line.partition("#")[0].strip()
        if not entry:
            continue
        try:
            entries.add(normalize_table(entry))
        except ValueError:
            # A mistaken path might point at private content: never echo a line.
            raise ValueError(f"Invalid shared table identifier at {source}:{number}") from None
    return frozenset(entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate and print the configured shared reads")
    parser.add_argument("--path", type=Path, help="Use this local policy file instead of the configured path")
    args = parser.parse_args()
    try:
        path = args.path
        if path is None:
            from epicor_mcp.config import get_settings

            path = get_settings().table_read_allowlist_path
        entries = read_allowlist(path)
    except (OSError, ValueError) as exc:
        print(f"Shared table read validation failed: {exc}", file=sys.stderr)
        return 2
    print(f"Valid shared table reads: {len(entries)} entries")
    for entry in sorted(entries):
        print(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

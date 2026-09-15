"""Exact, read-only table allowlist; no inferred grants or custom-table expansion."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

_TABLE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*\Z")


def normalize_table(value: str) -> str:
    value = str(value).strip()
    if not _TABLE.fullmatch(value):
        raise ValueError(f"Invalid table whitelist entry: {value!r}; use Table or Schema.Table, without wildcards")
    # A bare table means Erp.Table. Explicit qualifiers never broaden a grant.
    schema, table = value.split(".") if "." in value else ("Erp", value)
    if schema.casefold() not in {"erp", "ice"}:
        raise ValueError("Table whitelist schema must be Erp or Ice")
    return f"{schema}.{table}".casefold()


@dataclass(frozen=True)
class TableWhitelist:
    tables: frozenset[str] | None
    email: str = "shared-read-only"
    reason: str = "administrator-configured table whitelist"
    service_count: int = 0
    security_mgr: bool = False
    is_unavailable: bool = False

    @classmethod
    def from_file(cls, path: str | Path | None) -> "TableWhitelist":
        # ONLY an explicit empty setting opts out. A missing file is never opt-out.
        if path is None or (isinstance(path, str) and not path.strip()):
            return cls(None, reason="table whitelist explicitly disabled")
        source = Path(path)
        tables: set[str] = set()
        for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            entry = line.split("#", 1)[0].strip()
            if entry:
                try:
                    tables.add(normalize_table(entry))
                except ValueError as exc:
                    raise ValueError(f"{source}:{lineno}: {exc}") from exc
        return cls(frozenset(tables))

    @property
    def active(self) -> bool:
        return self.tables is not None

    @property
    def is_unlimited(self) -> bool:
        return self.tables is None

    @property
    def state(self):
        from epicor_mcp.discovery.authz import ScopeState
        return ScopeState.SCOPED if self.active else ScopeState.UNLIMITED

    def allows(self, name: str) -> bool:
        from epicor_mcp.sql.denylist import is_denied_table
        try:
            normalized = normalize_table(name)
        except ValueError:
            return False
        if is_denied_table(name):
            return False
        return self.tables is None or normalized in self.tables

    def note(self, mode: str = "gate") -> str:
        if self.tables is None:
            return "Table whitelist explicitly disabled; built-in table/column denials still apply."
        return f"Only the {len(self.tables)} explicitly whitelisted tables may be read. Custom _UD tables require their own entry."


class NoneTableAuthorizer:
    """Adapter for the existing query and discovery scope contracts."""
    mode = "gate"

    def __init__(self, whitelist: TableWhitelist):
        self.whitelist = whitelist

    def resolve_identity(self, *, session_email: str = "", **_: Any) -> str:
        return session_email or "shared-read-only"

    async def scope_for(self, email: str = "") -> TableWhitelist:
        return self.whitelist

    def evict(self, email: str) -> bool:
        return False

    def evict_all(self) -> int:
        return 0

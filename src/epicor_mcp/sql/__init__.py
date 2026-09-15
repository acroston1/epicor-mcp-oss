"""Deterministic plain-T-SQL -> Epicor BAQ SQL translation.

See ``CLAUDE.md`` in this directory for the contract. Every rewrite is based on
Epicor's documented and observed BAQ behavior.
"""

from epicor_mcp.sql.transpile import (
    DIALECT,
    Advisory,
    Outcome,
    Policy,
    RowBound,
    TableSchema,
    Transformation,
    TranspileResult,
    WEDGE_POLICY,
    transpile,
)

__all__ = [
    "DIALECT",
    "Advisory",
    "Outcome",
    "Policy",
    "RowBound",
    "TableSchema",
    "Transformation",
    "TranspileResult",
    "WEDGE_POLICY",
    "transpile",
]

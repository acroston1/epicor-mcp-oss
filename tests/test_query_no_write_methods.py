"""Feature E1 / S8 — nothing in the SQL surface can persist or write.

The whole non-persistence claim rests on ONE property: the pipe calls
``ParseFromSQL`` (which compiles in memory) and ``Execute`` (which takes the
tableset in memory), and **never** ``BAQDesignerSvc/Update``,
``DynamicQuerySvc/Update``, ``UpdateByID``, ``FieldUpdate``, ``RunCustomAction``
or ``DeleteByID``. A grep is a crude test and it is exactly the right one here:
the failure it guards against is a future edit adding a call site, not a subtle
logic error.

The scan runs over the module with **docstrings and comments stripped**, so the
prose above — which names the forbidden methods in order to forbid them — cannot
make the test pass or fail. The assertions inspect executable calls so a
comment claiming read-only behavior cannot substitute for enforcement.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parents[1] / "src" / "epicor_mcp" / "sql"
WEDGE_SERVER = Path(__file__).resolve().parents[1] / "src" / "epicor_mcp" / "wedge_server.py"

FORBIDDEN = (
    "BAQDesignerSvc/Update",
    "BAQDesignerSvc/DeleteByID",
    "DynamicQuerySvc/Update",
    "DynamicQuerySvc/UpdateByID",
    "DynamicQuerySvc/FieldUpdate",
    "DynamicQuerySvc/RunCustomAction",
    "DeleteByID",
    "UpdateExt",
)

ALLOWED_ENDPOINTS = {
    "Ice.BO.BAQDesignerSvc/ParseFromSQL",
    "Ice.BO.DynamicQuerySvc/Execute",
    "Ice.BO.DynamicQuerySvc/Analyze",
}


def _sources() -> list[Path]:
    return sorted(SQL_DIR.glob("*.py")) + [WEDGE_SERVER]


def _code_only(path: Path) -> str:
    """The module's executable source: no comments, no docstrings."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
    return ast.unparse(tree)


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_write_or_persist_method_appears_in_executable_code(path: Path):
    code = _code_only(path)
    for needle in FORBIDDEN:
        assert needle not in code, f"{path.name} references {needle}"


def test_only_the_read_endpoints_are_reachable():
    pattern = re.compile(r"(?:Ice|Erp)\.BO\.[A-Za-z]+Svc/[A-Za-z]+")
    found: set[str] = set()
    for path in _sources():
        found |= set(pattern.findall(_code_only(path)))
    assert found == ALLOWED_ENDPOINTS, f"endpoint set changed: {sorted(found)}"


def test_the_stripper_actually_strips_or_this_whole_file_is_theatre():
    """If _code_only silently returned the raw text, every assertion above would
    pass for the wrong reason."""
    code = _code_only(SQL_DIR / "adhoc.py")
    raw = (SQL_DIR / "adhoc.py").read_text()
    assert "DeleteByID" in raw  # the module docstring names it, to forbid it
    assert "DeleteByID" not in code


def test_the_parse_row_is_marked_added_not_updated():
    """`RowMod: "A"` is an ADD to an in-memory tableset. `"U"` would be an
    update against a persisted query."""
    from epicor_mcp.sql.adhoc import _PARSE_BODY

    row = _PARSE_BODY["ds"]["DynamicQueryDesigner"][0]
    assert row["RowMod"] == "A"
    assert row["QueryID"] == "AdHocV3"


def test_the_authorization_gate_never_imports_the_transpiler():
    """`tables_referenced` is diagnostics only. The gate reads
    Epicor's own resolved QueryTable rows, and a hand-rolled parser must never
    become the authority."""
    code = _code_only(SQL_DIR / "denylist.py")
    assert "transpile" not in code
    assert "sqlglot" not in code

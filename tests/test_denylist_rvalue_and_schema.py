"""Two authorization holes found by adversarial review, both closed.

Neither was caught by the existing suite, and neither could have been: the
first had **zero** fixtures putting a denied column in the channel it used, and
the second was invisible because every existing test spells the table the way the
deny-list already blocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from epicor_mcp.sql.denylist import check_parsed_ds, is_denied_table

FIXTURES = Path(__file__).parent / "fixtures" / "parsed_ds"


# --------------------------------------------------------------------------- #
# Hole 1 — QueryWhereItem.RValue was never read
# --------------------------------------------------------------------------- #
def _where_ds(field_name: str, rvalue: str, table: str = "LaborDtl", alias: str = "LD"):
    """Regression coverage:  where ds."""
    return {
        "QueryTable": [
            {
                "TableID": alias,
                "TableType": "DB",
                "DBSchemaName": "Erp",
                "DBTableName": table,
            }
        ],
        "QueryWhereItem": [
            {"TableID": "", "FieldName": field_name, "RValue": rvalue, "CondSign": "<"}
        ],
        "QueryField": [],
        "QuerySortBy": [],
        "QuerySubQuery": [],
    }


@pytest.mark.parametrize(
    "rvalue, expected",
    [
        ("((LD.LaborRate * 1))", "Erp.LaborDtl.LaborRate"),
        ("(LD.ChargeRate)", "Erp.LaborDtl.ChargeRate"),
        ("((LD.LaborRate + LD.LaborHrs))", "Erp.LaborDtl.LaborRate"),
    ],
)
def test_a_denied_column_hiding_in_rvalue_is_denied(rvalue, expected):
    """Denied columns must be checked on both sides of an expression predicate.

    An unchecked ``RValue`` can let repeated predicates infer protected pay
    rates even when direct selection and filtering of the column are refused.
    Expression handling must preserve the denylist check on both operands.
    """
    ds = _where_ds("(((LD.LaborHrs * 0) + 60))", rvalue)
    verdict = check_parsed_ds(ds)
    assert verdict, f"{rvalue} must be denied"
    assert expected in verdict.denied_columns


def test_rvalue_scanning_does_not_break_the_ordinary_shapes():
    """``RValue`` legitimately holds literals, BETWEEN pairs and subquery ids.
    None of them may manufacture an anomaly or a denial."""
    for rvalue in ("100", "'ACTIVE'", "10 AND 20", "'LD.LaborRate'", ""):
        ds = _where_ds("(((LD.LaborHrs * 0) + 60))", rvalue)
        verdict = check_parsed_ds(ds)
        assert not verdict, f"RValue={rvalue!r} must not deny"
        assert not verdict.anomalies, f"RValue={rvalue!r} produced {verdict.anomalies}"


def test_a_clean_column_comparison_in_rvalue_still_runs():
    """The fix must not block comparing two ordinary columns."""
    ds = _where_ds("((LD.LaborHrs * 1))", "((LD.BurdenHrs * 1))")
    verdict = check_parsed_ds(ds)
    assert not verdict
    assert not verdict.denied_columns


# --------------------------------------------------------------------------- #
# Hole 2 — the deny-list spelled two tables with the wrong schema
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table", ["UserFile", "UserComp"])
def test_user_tables_are_denied_under_their_CANONICAL_schema(table):
    """Regression coverage: test user tables are denied under their CANONICAL schema."""
    assert is_denied_table(f"Erp.{table}"), f"Erp.{table} is the CANONICAL name"
    assert is_denied_table(f"Ice.{table}"), "the Ice spelling must stay denied too"
    assert is_denied_table(table), "the bare name must stay denied"


def test_the_denied_table_is_refused_through_the_parsed_ds():
    ds = {
        "QueryTable": [
            {
                "TableID": "U",
                "TableType": "DB",
                "DBSchemaName": "Erp",
                "DBTableName": "UserFile",
            }
        ],
        "QueryField": [],
        "QueryWhereItem": [],
        "QuerySortBy": [],
        "QuerySubQuery": [],
    }
    verdict = check_parsed_ds(ds)
    assert verdict
    assert "Erp.UserFile" in verdict.denied_tables


def test_no_deny_pattern_misses_its_own_canonical_table_name(tmp_path):
    """Systematic sweep — the check that would have caught hole 2 up front.

    For every deny pattern, find the real tables it names in Epicor's catalogue
    and assert ``is_denied_table`` refuses their CANONICAL ``Schema.Table``.
    Skips when the catalogue is absent (it is gitignored).
    """
    from tests.fixtures.synthetic_catalogue import build
    _, catalogue = build(tmp_path)
    from epicor_mcp.sql.denylist import DENIED_TABLE_PATTERNS

    tables = json.loads(catalogue.read_text())["tables"]
    misses = []
    for pattern in DENIED_TABLE_PATTERNS:
        if "." not in pattern:
            continue
        tail = pattern.split(".", 1)[1]
        star = tail.endswith("*")
        stem = (tail[:-1] if star else tail).lower()
        for name, meta in tables.items():
            low = name.lower()
            if (low.startswith(stem) if star else low == stem):
                full = meta["full_name"]
                if not is_denied_table(full):
                    misses.append((pattern, full))
    assert not misses, (
        "deny patterns that do NOT refuse their own canonical table name: "
        f"{sorted(set(misses))}"
    )


# --------------------------------------------------------------------------- #
# Hole 2b — a RECOVERY must never name a denied table
# --------------------------------------------------------------------------- #
def test_suggestions_are_filtered_not_just_inputs():
    """``_suggestion_allowed`` gated the name the caller WROTE; the names being
    SERVED went out unfiltered, so a one-character typo handed back the reachable
    spelling of a hard-denied table."""
    from epicor_mcp.sql.lint import _allowed_suggestions

    assert _allowed_suggestions(["Erp.UserFile"]) == []
    assert _allowed_suggestions(["Ice.UserFile"]) == []
    assert _allowed_suggestions(["Erp.JobHead"]) == ["Erp.JobHead"]
    assert _allowed_suggestions(["Erp.UserFile", "Erp.JobHead"]) == ["Erp.JobHead"]

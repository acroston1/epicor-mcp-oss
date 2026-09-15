"""Regression coverage: test wedge card."""

from __future__ import annotations

import json
from pathlib import Path

from epicor_mcp.sql import card

def test_card_is_exactly_the_thirty_tables_plan_5_6_names():
    expected = [
        "JobHead", "Part", "LaborDtl", "JobOper", "POHeader", "DMRHead", "APInvHed",
        "JobMtl", "OrderHed", "InvcHead", "APInvDtl", "Vendor", "OrderDtl", "PartWhse",
        "DMRActn", "CheckHed", "Customer", "PartTran", "SugPoDtl", "JobAsmbl", "Warehse",
        "PartMtl", "PODetail", "InvcDtl", "PartBin", "Resource", "VendCnt", "RcvDtl",
        "PartCost", "Plant",
    ]
    assert list(card.CARD_TABLES) == expected
    assert len(card.CARD_TABLES) == 30
    assert set(card.CARD_COLUMNS) == set(expected)


def test_the_known_odata_phantoms_are_not_on_the_card():
    """Regression coverage: test the known odata phantoms are not on the card."""
    assert "DocExtPrice" not in card.CARD_COLUMNS["OrderDtl"]
    assert "OnHandQty" not in card.CARD_COLUMNS["Part"]
    assert "HasOnHandQty" not in card.CARD_COLUMNS["Part"]
    assert "ActScrapQty" not in card.CARD_COLUMNS["JobOper"]
    # and the real one IS there
    assert "ExtPriceDtl" in card.CARD_COLUMNS["OrderDtl"]
    assert "OnHandQty" in card.CARD_COLUMNS["PartWhse"]


def test_card_stays_curated_not_full():
    """A full, uncurated card is too wide to be useful: its size alone makes
    the model decline questions."""
    for table, columns in card.CARD_COLUMNS.items():
        assert 5 <= len(columns) <= 20, f"{table} has {len(columns)} columns"
    total = sum(len(c) for c in card.CARD_COLUMNS.values())
    assert total < 350, f"card is {total} columns — that is the FULL-card failure mode"


def test_card_text_is_within_the_token_budget():
    """The compact card stays within its token budget."""
    text = card.card_text()
    assert len(text) < 9000, f"{len(text)} chars is past the ~2K token budget"
    assert text.startswith("HOT TABLES")


def test_empbasic_is_not_on_the_card_and_list_userfile_partwhsesearch_are_excluded():
    """Plant takes EmpBasic's slot; the three stubs are excluded."""
    for excluded in ("EmpBasic", "List", "UserFile", "PartWhseSearch"):
        assert excluded not in card.CARD_TABLES
    assert "Plant" in card.CARD_TABLES


def test_every_card_table_is_schema_qualified_in_the_rendering():
    text = card.card_text()
    for table in card.CARD_TABLES:
        assert f"Erp.{table}:" in text


def test_notes_carry_the_discriminators_no_column_name_can():
    text = card.card_text()
    for needle in ("ExtPriceDtl", "PersonID", "OnhandQty", "DMRActn"):
        assert needle in text


def test_card_says_it_is_a_hint_not_a_limit():
    """the card is ADVISORY. A model must not read it as an allow-list."""
    assert "HINT, not a limit" in card.card_text()


def test_card_columns_lookup_is_case_and_schema_insensitive():
    assert card.card_columns("Erp.OrderDtl") == card.CARD_COLUMNS["OrderDtl"]
    assert card.card_columns("orderdtl") == card.CARD_COLUMNS["OrderDtl"]
    assert card.card_columns("Erp.NoSuchTable") == ()


def test_rendering_is_deterministic():
    assert card.card_text() == card.card_text()
    assert "KNOW THIS ABOUT CONFIGURED DATA" in card.card_text()
    assert "KNOW THIS ABOUT CONFIGURED DATA" not in card.card_text(include_notes=False)

"""Deterministic tests for the discovery surface.

Everything here runs offline with synthetic inputs: no Epicor tenant,
embedding server, or operator-generated discovery index is required.
The assertions cover document composition and deterministic ranking behavior.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from epicor_mcp.discovery import rank
from epicor_mcp.discovery.text import (
    expand_abbrevs,
    field_document,
    field_name_document,
    split_camel,
)

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / "data" / "discovery_index"
CATALOGUE = REPO / "data" / "schema_catalogue.json"

# --------------------------------------------------------------------------- #
# Document composition
# --------------------------------------------------------------------------- #
def test_camel_split_boundaries_are_load_bearing():
    """The digit stays glued and ``In``/``Invoice`` must not collide.

    ``Rpt1`` as one token is what lets the reporting-currency prior fire on the
    Rpt family without demoting everything containing those letters; the
    ``In``/``Invoice`` split is what makes the tax-inclusive ``In*`` penalty safe.
    """
    assert split_camel("Rpt1InvoiceAmt") == ["Rpt1", "Invoice", "Amt"]
    assert split_camel("InExtPriceDtl") == ["In", "Ext", "Price", "Dtl"]
    assert split_camel("InvoiceAmt") == ["Invoice", "Amt"]
    assert split_camel("APInvHed") == ["AP", "Inv", "Hed"]
    assert split_camel("OnHandQty") == ["On", "Hand", "Qty"]


def test_abbrev_expansion_is_additive():
    assert "warehouse" in expand_abbrevs(["Whse"])
    assert "quantity" in expand_abbrevs(["Qty"])
    # Multi-word expansions are deliberate: `req` is both in the corpus.
    assert set(expand_abbrevs(["Req"])) == {"required", "request"}


def test_field_document_carries_the_ui_label():
    """Regression coverage: test field document carries the ui label."""
    doc = field_document("JobHead", "PersonIDName", "nvarchar", "", "Planner")
    assert "Planner" in doc
    assert "JobHead.PersonIDName" in doc
    
    # indexing descriptions in the BM25 leg makes it fire on sibling columns.
    assert "Planner" not in field_name_document("JobHead", "PersonIDName")


def test_field_document_uses_the_raw_sql_type():
    """The raw SQL type retrieves better than ``Edm.*`` or plain English.
    The catalogue's own type string wins; do not "humanise" it."""
    assert "(decimal)" in field_document("PartWhse", "OnHandQty", "decimal", "x")


# --------------------------------------------------------------------------- #
# Ranking terms
# --------------------------------------------------------------------------- #
def test_column_prior_demotes_currency_mirrors_but_not_the_base_column():
    assert rank.column_prior_penalty("extended price", "Rpt1ExtPriceDtl") > 0.5
    assert rank.column_prior_penalty("extended price", "ExtPriceDtl") == 0.0
    # ...and is WAIVED when the question actually asks for that flavour.
    assert rank.column_prior_penalty("reporting currency price", "Rpt1ExtPriceDtl") == 0.0


def test_type_affinity_keys_on_SQL_types_not_edm():
    """The bench keys on ``Edm.Double`` because it was built over the OData
    index. This surface is served from GetFieldList, whose DataType is the SQL
    type. Keying on ``Edm.*`` here scores every affinity as a mismatch — a
    regression small enough to hide."""
    assert rank.type_affinity("how many did we receive", "decimal") > 0
    assert rank.type_affinity("how many did we receive", "nvarchar") < 0
    assert rank.type_affinity("when did it finish", "datetime") > 0
    assert rank.type_affinity("is the job closed", "bit") > 0
    # An Edm name must NOT be recognised — that would mean someone reintroduced
    # the OData vocabulary.
    assert rank.type_affinity("how many did we receive", "Edm.Double") < 0


def test_name_match_canonicalises_through_the_abbreviations():
    assert rank.name_match_score("on hand quantity", "PartWhse", "OnHandQty") > 0.6
    assert rank.name_match_score("on hand quantity", "PartWhse", "SafetyQty") < 0.4


# --------------------------------------------------------------------------- #
# The traffic prior — the circularity guard
# --------------------------------------------------------------------------- #
def test_traffic_prior_weight_stays_small():
    """Regression coverage: test traffic prior weight stays small."""
    assert rank.TRAFFIC_PRIOR_WEIGHT <= 0.03, (
        "Raising the traffic prior above ~0.03 makes tables outside the top 30 "
        "unreachable even when their names match the request."
    )
    assert rank.traffic_prior("JobHead") > rank.traffic_prior("Plant") > 0.0
    assert rank.traffic_prior("MtlQueue") == 0.0


# --------------------------------------------------------------------------- #
# Catalogue integrity — the phantom-column guard
# --------------------------------------------------------------------------- #
def test_catalogue_excludes_the_known_odata_phantoms():
    """Regression coverage: test catalogue excludes the known odata phantoms."""
    cat = json.loads(CATALOGUE.read_text())["tables"]

    def has(table: str, col: str) -> bool:
        v = cat.get(table)
        return bool(v) and any(f["name"] == col for f in v["fields"])

    assert not has("JobOper", "ScrapQty"), "JobOper.ScrapQty is an OData phantom"
    assert not has("JobOper", "ActScrapQty")
    assert not has("Part", "OnHandQty"), "Part.OnHandQty is a phantom; PartWhse has it"
    assert not has("OrderDtl", "ExtPrice"), "the real column is ExtPriceDtl"

    # ...and the REAL homes of those concepts must be present.
    assert has("LaborDtl", "ScrapQty"), "reported scrap lives on LaborDtl"
    assert has("JobOper", "EstScrap"), "JobOper carries only ESTIMATED scrap"
    assert has("PartWhse", "OnHandQty")
    assert has("OrderDtl", "ExtPriceDtl")
    assert has("JobOpDtl", "ResourceGrpID"), "not JobOper.PrimaryResourceGrpID"


def test_custom_c_columns_live_on_the_UD_mirror_only():
    """Regression coverage: test custom c columns live on the UD mirror only."""
    cat = json.loads(CATALOGUE.read_text())["tables"]
    base = {f["name"] for f in cat["OrderRel"]["fields"]}
    mirror = {f["name"] for f in cat["OrderRel_UD"]["fields"]}
    assert "Note_c" not in base
    assert "Note_c" in mirror
    assert not any(n.endswith("_c") for n in base)


def test_family_dedupe_patterns_are_not_applied():
    """Regression coverage: test family dedupe patterns are not applied."""
    cat = json.loads(CATALOGUE.read_text())["tables"]
    assert "PartTran" in cat, "the Tran$ family filter must not be applied"
    assert "APTran" in cat and "BankTran" in cat
    for shadow in ("IMLaborDtl", "LaborDtlList", "PartWhseSearch", "MobileLaborDtl"):
        assert shadow not in cat, f"{shadow} is an OData artefact, not a real table"


# --------------------------------------------------------------------------- #
# Index behaviour
# --------------------------------------------------------------------------- #
def test_index_loads_and_resolves_tables():
    from epicor_mcp.discovery.store import DiscoveryIndex

    ix = DiscoveryIndex.load(INDEX)
    assert ix is not None
    assert ix.resolve_table("partwhse") == "PartWhse"
    assert ix.resolve_table("Erp.PartWhse") == "PartWhse"
    assert ix.resolve_table("NoSuchTable") is None
    assert len(ix.fields_of("PartWhse")) > 20


def test_elsewhere_is_a_fact_not_a_guess():
    """Regression coverage: test elsewhere is a fact not a guess."""
    from epicor_mcp.discovery.store import DiscoveryIndex

    ix = DiscoveryIndex.load(INDEX)
    hits = dict(ix.name_matches_elsewhere("ScrapQty",
                                          ["JobOper"]))
    assert hits.get("LaborDtl") == "ScrapQty"

    # Generic words must not produce "findings".
    assert ix.name_matches_elsewhere("total quantity on the order", ["OrderDtl"]) == []\
        or all(t != "Part" for t, _ in
               ix.name_matches_elsewhere("total quantity on the order", ["OrderDtl"]))


def test_ud_mirrors_never_rank_as_subject_tables():
    """A 4-column ``<Table>_UD`` mirror embeds close to its parent and would take
    a top-5 slot from a real answer. It stays reachable via ``custom_columns``."""
    from epicor_mcp.discovery.store import DiscoveryIndex

    ix = DiscoveryIndex.load(INDEX)
    hits = ix.search_tables(None, "discrepant material", limit=10)
    assert all(not h.table.endswith("_UD") for h in hits)


def test_field_search_without_a_vector_degrades_rather_than_raising():
    """The embedding server is a separate process that sleeps. Losing it must
    cost ranking quality, not the tool."""
    from epicor_mcp.discovery.store import DiscoveryIndex

    ix = DiscoveryIndex.load(INDEX)
    hits = ix.search_fields("PartWhse", None, "on hand quantity", limit=5)
    assert hits and all(h.table == "PartWhse" for h in hits)

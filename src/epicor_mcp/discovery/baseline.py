'The curated baseline allow-list — the GAP POLICY behind the table-authz gate.'

from __future__ import annotations

from epicor_mcp.discovery.rank import HOT_TABLES
from epicor_mcp.sql.card import CARD_TABLES
from epicor_mcp.sql.denylist import is_denied_table

__all__ = ["BASELINE_TABLES"]

#: The gap additions plus judgement calls. CamelCase on purpose — the
#: deny-filter below needs the canonical casing (see module docstring).
_ADDITIONS: tuple[str, ...] = (
    # -- unreachable through any menu chain, needed by built-in recognizers ---
    "PartMtl",   # already on the card; listed so the reason survives a card edit
    "PartOpr",
    "SugPOChg",
    # -- the engineering-BOM family PartMtl/PartOpr belong to ----------------
    # PartRev is the PARENT of both (PartOpr's description: "Child of PartRev
    # file"); a BOM question without the revision table is a dead end one join
    # away from a table the baseline already grants.
    "PartRev",
    "PartOpDtl",  # "the manufacturing details for the PartOpr"
    # -- physical homes this repo's own docs name as load-bearing ------------
    # discovery/tools.py's header: JobOper.PrimaryResourceGrpID is an OData
    # phantom whose real home is JobOpDtl.ResourceGrpID.
    "JobOpDtl",
    # -- parent/sibling completion of card tables ----------------------------
    # The card carries the child/detail; refusing the parent makes the granted
    # table unjoinable: LaborDtl -> LaborHed, RcvDtl -> RcvHead. VendCnt is on
    # the card while its customer twin is not — CustCnt is also the home of
    # the legacy tools' contacts recognizer (customer contacts are NOT on CustomerSvc).
    "LaborHed",
    "RcvHead",
    "CustCnt",
    # -- commonly asked query subjects ----------------------------------------
    # "packing slips we sent customers" is epicor_tables' own canonical example
    # subject; specific quote numbers are values rather than schema subjects.
    "ShipHead",
    "ShipDtl",
    "QuoteHed",
    "QuoteDtl",
)


def _allowed(name: str) -> bool:
    """Deny-list check on BOTH spellings a curated entry can be matched under."""
    return not (is_denied_table(name) or is_denied_table(f"Erp.{name}"))


#: Bare lowercase table names, unioned into every SCOPED authz scope.
BASELINE_TABLES: frozenset[str] = frozenset(
    name.lower()
    for name in (*CARD_TABLES, *HOT_TABLES, *_ADDITIONS)
    if _allowed(name)
)

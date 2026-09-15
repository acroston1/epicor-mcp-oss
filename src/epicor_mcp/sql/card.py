"""A compact reference card of common Epicor tables and selected columns."""

from __future__ import annotations

#: Stable presentation order for the 30 tables in the compact reference card.
CARD_TABLES: tuple[str, ...] = (
    "JobHead", "Part", "LaborDtl", "JobOper", "POHeader", "DMRHead", "APInvHed",
    "JobMtl", "OrderHed", "InvcHead", "APInvDtl", "Vendor", "OrderDtl", "PartWhse",
    "DMRActn", "CheckHed", "Customer", "PartTran", "SugPoDtl", "JobAsmbl", "Warehse",
    "PartMtl", "PODetail", "InvcDtl", "PartBin", "Resource", "VendCnt", "RcvDtl",
    "PartCost", "Plant",
)

#: Every card table is an ``Erp`` table. Kept as an explicit map rather than a
#: default so an ``Ice`` addition cannot silently inherit the wrong prefix.
CARD_SCHEMA: dict[str, str] = {t: "Erp" for t in CARD_TABLES}

#: table -> curated columns. 5–17 per table, never the full width.
#: This is a small built-in reference, not a complete schema export. Import
#: physical metadata with ``scripts/build_schema_catalogue.py`` for your version.
#: ``tests/test_wedge_card.py`` checks the card's shape and known exclusions.
CARD_COLUMNS: dict[str, tuple[str, ...]] = {
    "JobHead": (
        "Company", "JobNum", "PartNum", "PartDescription", "ProdQty", "QtyCompleted",
        "JobClosed", "JobComplete", "JobReleased", "StartDate", "DueDate", "CreateDate",
        "JobCompletionDate", "Plant", "PersonID", "PersonIDName", "PlanUserID",
    ),
    "Part": (
        "Company", "PartNum", "PartDescription", "ClassID", "ProdCode", "TypeCode",
        "IUM", "PUM", "NonStock", "InActive", "UnitPrice", "PricePerCode",
    ),
    "LaborDtl": (
        "Company", "JobNum", "AssemblySeq", "OprSeq", "EmployeeNum", "LaborType",
        "LaborQty", "ScrapQty", "ScrapReasonCode", "DiscrepQty", "LaborHrs",
        "BurdenHrs", "ClockInDate", "OpCode",
    ),
    "JobOper": (
        "Company", "JobNum", "AssemblySeq", "OprSeq", "OpCode", "OpDesc",
        "QtyCompleted", "RunQty", "OpComplete", "JobComplete", "EstScrap",
        "EstScrapType",
    ),
    "POHeader": (
        "Company", "PONum", "VendorNum", "OrderDate", "BuyerID", "EntryPerson",
        "OpenOrder", "ApprovalStatus", "OrderHeld", "PurPoint",
    ),
    "DMRHead": (
        "Company", "DMRNum", "PartNum", "PartDescription", "JobNum", "PONum",
        "VendorNum", "TotRejectedQty", "TotDiscrepantQty", "TotAcceptedQty",
        "RevisionNum",
    ),
    "APInvHed": (
        "Company", "VendorNum", "InvoiceNum", "InvoiceDate", "InvoiceAmt", "GroupID",
        "OpenPayable", "DueDate", "Posted", "Description",
    ),
    "JobMtl": (
        "Company", "JobNum", "AssemblySeq", "MtlSeq", "PartNum", "Description",
        "QtyPer", "RequiredQty", "IssuedQty", "IssuedComplete",
    ),
    "OrderHed": (
        "Company", "OrderNum", "CustNum", "OrderDate", "RequestDate", "NeedByDate",
        "OpenOrder", "OrderHeld", "EntryPerson", "PONum",
    ),
    "InvcHead": (
        "Company", "InvoiceNum", "CustNum", "InvoiceDate", "InvoiceAmt", "OpenInvoice",
        "InvoiceType", "DueDate", "Posted", "OrderNum",
    ),
    "APInvDtl": (
        "Company", "VendorNum", "InvoiceNum", "InvoiceLine", "PartNum", "Description",
        "ExtCost", "VendorQty", "PONum", "POLine",
    ),
    "Vendor": (
        "Company", "VendorNum", "VendorID", "Name", "City", "State", "TermsCode",
        "GroupCode", "CurrencyCode", "Approved", "Inactive",
    ),
    "OrderDtl": (
        "Company", "OrderNum", "OrderLine", "PartNum", "LineDesc", "OrderQty",
        "SellingQuantity", "UnitPrice", "ExtPriceDtl", "RequestDate", "NeedByDate",
        "OpenLine", "VoidLine",
    ),
    "PartWhse": (
        "Company", "PartNum", "WarehouseCode", "OnHandQty", "NonNettableQty",
        "MinimumQty", "SafetyQty",
    ),
    "DMRActn": (
        "Company", "DMRNum", "ActionNum", "ActionDate", "ActionType",
        "DestinationType", "JobNum", "DMRSeqNum",
    ),
    "CheckHed": (
        "Company", "CheckNum", "CheckDate", "CheckAmt", "VendorNum", "Name",
        "BankAcctID", "Voided", "Posted", "ClearedCheck", "GroupID",
    ),
    "Customer": (
        "Company", "CustNum", "CustID", "Name", "TerritoryID", "CustomerType",
        "Inactive",
    ),
    "PartTran": (
        "Company", "PartNum", "TranDate", "TranType", "TranQty", "TranNum", "JobNum",
        "WareHouseCode", "ExtCost", "CostMethod",
    ),
    "SugPoDtl": (
        "Company", "SugNum", "SugType", "PartNum", "LineDesc", "VendorNum", "VendorID",
        "Name", "BuyerID", "DueDate", "RelQty", "XRelQty", "UnitPrice", "JobNum",
        "PONUM", "Plant",
    ),
    "JobAsmbl": (
        "Company", "JobNum", "AssemblySeq", "PartNum", "Description", "RequiredQty",
        "QtyPer", "IssuedQty", "DueDate", "TotalCost", "BomLevel",
    ),
    "Warehse": (
        "Company", "WarehouseCode", "Description", "Plant", "Inactive", "WarehouseType",
    ),
    "PartMtl": (
        "Company", "PartNum", "MtlSeq", "MtlPartNum", "QtyPer", "RevisionNum",
    ),
    "PODetail": (
        "Company", "PONUM", "POLine", "PartNum", "LineDesc", "OrderQty", "UnitCost",
        "DueDate", "OpenLine", "VoidLine", "IUM",
    ),
    "InvcDtl": (
        "Company", "InvoiceNum", "InvoiceLine", "PartNum", "LineDesc", "ExtPrice",
        "UnitPrice", "OurShipQty", "SellingShipQty", "OrderNum", "OrderLine", "JobNum",
    ),
    "PartBin": (
        "Company", "PartNum", "WarehouseCode", "BinNum", "OnhandQty", "LotNum",
        "AllocatedQty", "DimCode",
    ),
    "Resource": (
        "Company", "ResourceID", "Description", "ResourceGrpID", "Inactive",
        "ResourceType",
    ),
    "VendCnt": (
        "Company", "VendorNum", "ConNum", "Name", "PhoneNum", "EmailAddress",
        "ContactTitle", "Inactive",
    ),
    "RcvDtl": (
        "Company", "PackSlip", "PackLine", "PartNum", "PartDescription", "VendorNum",
        "PONum", "POLine", "OurQty", "OurUnitCost", "JobNum", "ReceiptDate",
    ),
    "PartCost": (
        "Company", "PartNum", "StdMaterialCost", "StdLaborCost", "StdBurdenCost",
        "StdSubContCost", "AvgMaterialCost", "AvgLaborCost", "AvgBurdenCost",
        "AvgSubContCost", "LastMaterialCost", "LastLaborCost", "LastBurdenCost",
        "LastSubContCost",
    ),
    "Plant": (
        "Company", "Plant", "Name", "City", "State", "PlantCostID", "CalendarID",
    ),
}

#: Discriminators the column NAMES alone do not carry; each one prevents a known
#: wrong-answer pattern. Business-value guidance is stated here because
#: business-domain resolution stays unsolved, and a short business glossary is
#: one of the most effective levers for query accuracy.
CARD_NOTES: tuple[str, ...] = (
    "Sales revenue is OrderDtl.ExtPriceDtl. There is NO DocExtPrice column at the SQL "
    "layer -- it is an OData-only phantom and selecting it FAILS at run time with "
    "'Bad SQL statement.'",
    "The job planner is JobHead.PersonID (a code like 'Plan16', name in PersonIDName), "
    "NOT PlanUserID (the login that last ran planning, usually blank).",
    "Reported scrap lives ONLY on LaborDtl.ScrapQty / JobOper. Yield = "
    "JobHead.QtyCompleted (good) vs the summed scrap.",
    "PartBin's on-hand column is spelled OnhandQty (lower-case h). PartWhse's is "
    "OnHandQty. Part has NO on-hand quantity at all.",
    "Quality/DMR questions usually need DMRHead + DMRActn together, joined on "
    "Company + DMRNum.",
    "PODetail and SugPoDtl spell the PO number PONUM; POHeader spells it PONum.",
    "A/P invoices are APInvHed/APInvDtl (supplier bills); A/R invoices are "
    "InvcHead/InvcDtl (customer bills). A bare 'invoice' is A/R.",
    "This card is a HINT, not a limit -- any Erp/Ice table you can name may be queried, "
    "and any real column of these tables may be selected.",
)


def card_text(*, include_notes: bool = True, include_domains: bool = True) -> str:
    """Render the static table card deterministically.
    
    ``include_domains`` adds the operator-supplied value hints from
    ``domains.domain_block``. A column name alone does not establish stored codes:
    a type may use ``M`` instead of ``Manufactured``. Domain hints are separately
    switchable from general notes so operators can choose the amount of context.
    """
    lines = [
        "HOT TABLES (the 30 tables that carry 89% of real questions naming a table).",
        "Format: Schema.Table: column, column, ...   Not exhaustive -- these are the",
        "columns that answer most questions; the tables have more.",
        "",
    ]
    for table in CARD_TABLES:
        cols = ", ".join(CARD_COLUMNS[table])
        lines.append(f"  {CARD_SCHEMA[table]}.{table}: {cols}")
    if include_notes:
        lines.append("")
        lines.append("KNOW THIS ABOUT CONFIGURED DATA")
        for note in CARD_NOTES:
            lines.append(f"  - {note}")
    if include_domains:
        from epicor_mcp.sql.domains import domain_block

        lines.append("")
        lines.append(domain_block())
    return "\n".join(lines)


def card_columns(table: str) -> tuple[str, ...]:
    """Curated columns for *table* (bare or ``Erp.``-qualified); ``()`` if off-card."""
    name = table.rsplit(".", 1)[-1]
    for known in CARD_TABLES:
        if known.lower() == name.lower():
            return CARD_COLUMNS[known]
    return ()

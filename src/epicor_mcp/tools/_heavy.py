"""Compatibility routing for services with unreliable direct OData reads."""

from __future__ import annotations

# Distinct Erp.BO.*Svc services known to return "unexpected internal problem"
# 500s on direct OData reads. Kept sorted for readable diffs.
HEAVY_SERVICES: set[str] = {
    "Erp.BO.APInvGrpSvc",
    "Erp.BO.APInvoiceSvc",
    "Erp.BO.ARInvSearchSvc",
    "Erp.BO.ARInvoiceSvc",
    "Erp.BO.CashHeadSearchSvc",
    "Erp.BO.CashRecSvc",
    "Erp.BO.CustShipSvc",
    "Erp.BO.CustomerSvc",
    "Erp.BO.DemandEntrySvc",
    "Erp.BO.GLAccountSvc",
    "Erp.BO.GLCntrlSvc",
    "Erp.BO.GLJournalEntrySvc",
    "Erp.BO.GLJrnDtlSvc",
    "Erp.BO.JobEntrySvc",
    "Erp.BO.LaborSvc",
    "Erp.BO.POSvc",
    "Erp.BO.PartSvc",
    "Erp.BO.PaymentEntrySvc",
    "Erp.BO.PurAgentSvc",
    "Erp.BO.ReceiptSvc",
    "Erp.BO.SOPOLinkSvc",
    "Erp.BO.SalesOrderSvc",
    "Erp.BO.SalesRepSvc",
    "Erp.BO.ShipDtlSearchSvc",
    "Erp.BO.VendPartSvc",
    "Erp.BO.VendorSvc",
    "Erp.BO.VoidPaymentSvc",
}


def is_heavy(service: str) -> bool:
    """Return True if *service* must auto-route through GetRows/BAQ (INV-2)."""
    return service in HEAVY_SERVICES

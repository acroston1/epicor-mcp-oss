"""The unbounded-rollup guard: refuse up front instead of scanning for 10 min.

The slow tail of `group_by`/`aggregate` calls is ONE shape — a rollup with no
bounding conjunct over a transaction table::

    target=OrderDtl, group_by=PartNum, aggregate="sum(DocExtPrice) as revenue"

which can run for around ten minutes. The scan still stops at the 20-page
ceiling, so the totals that eventually come back are TRUNCATED: maximum cost
for an answer we already know is incomplete. That is
exactly the trade `order_scan_unbounded` already refuses on the join sort path.

These tests pin BOTH halves of the contract: the refusal fires on a big
unbounded rollup, and it stays completely out of the way of every rollup that
is already bounded, small, or curated.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import types

import pytest

from epicor_mcp.tools import read as read_mod
from epicor_mcp.tools._aggregate import (
    BIG_ROLLUP_TABLES,
    is_big_rollup_scan,
    unbounded_rollup_refusal,
)
from epicor_mcp.tools._inline_schema import best_date_column

TODAY = datetime.date(2026, 7, 27)


def _refusal(**kw):
    base = dict(
        target="Erp.BO.SalesOrderSvc/OrderHed",
        scanned="OrderHed",
        group_by="CustNum",
        aggregate="sum(DocOrderAmt) as revenue",
        where="",
        bounded=False,
        date_columns=["OrderDate", "ChangeDate", "NeedByDate"],
        today=TODAY,
    )
    base.update(kw)
    return unbounded_rollup_refusal(**base)


# --------------------------------------------------------------------------- #
# The three trigger conditions — all must hold, or the rollup runs untouched.
# --------------------------------------------------------------------------- #

def test_unbounded_rollup_over_a_transaction_table_is_refused():
    env = _refusal()
    assert env is not None
    assert env["error"] == "rollup_scan_unbounded"


def test_a_bounded_rollup_is_never_touched():
    """The common, legitimate case. A `where` means the scan is cheap."""
    assert _refusal(where="OrderDate >= '2026-01-01'", bounded=True) is None


def test_a_plain_read_is_not_a_rollup():
    assert _refusal(group_by="", aggregate="") is None


def test_a_small_or_curated_table_is_unaffected():
    """`is_heavy()` is deliberately NOT the signal — it carries the Customer,
    Vendor and Part masters, whose rollups finish in one page."""
    for table in ("Customer", "Vendor", "Part", "Plant", "PartClass"):
        assert _refusal(scanned=table) is None, table


def test_big_table_membership_is_case_insensitive():
    assert is_big_rollup_scan("orderhed")
    assert is_big_rollup_scan("  OrderHed ")
    assert not is_big_rollup_scan("Customer")
    assert not is_big_rollup_scan("")


def test_the_listed_slow_tables_are_all_covered():
    """Large transaction tables require a bounded rollup scan."""
    for t in ("orderhed", "orderdtl", "parttran", "joboper", "labordtl",
              "apinvhed", "poheader", "podetail", "invcdtl"):
        assert t in BIG_ROLLUP_TABLES, t


# --------------------------------------------------------------------------- #
# INV-1: retry_with must be a RUNNABLE call, not a shrug.
# --------------------------------------------------------------------------- #

def test_retry_with_is_a_complete_runnable_call():
    env = _refusal()
    retry = env["retry_with"]
    assert retry["target"] == "Erp.BO.SalesOrderSvc/OrderHed"
    # The caller's own rollup rides back verbatim — nothing to re-derive.
    assert retry["group_by"] == "CustNum"
    assert retry["aggregate"] == "sum(DocOrderAmt) as revenue"
    # A concrete window on a REAL date column, not "<add a filter>".
    assert retry["where"] == "OrderDate >= '2025-07-27'"


def test_the_offered_retry_no_longer_trips_the_guard():
    """The recovery must actually terminate — one hop, not a loop."""
    env = _refusal()
    assert unbounded_rollup_refusal(
        target=env["retry_with"]["target"],
        scanned="OrderHed",
        group_by=env["retry_with"]["group_by"],
        aggregate=env["retry_with"]["aggregate"],
        where=env["retry_with"]["where"],
        bounded=True,
        date_columns=["OrderDate"],
        today=TODAY,
    ) is None


def test_an_existing_child_only_where_is_preserved_not_replaced():
    """A child-side filter does NOT bound a parent scan (same rule as
    `order_scan_unbounded`'s empty `p_parts`) — but throwing it away would
    silently widen the retry."""
    env = _refusal(where="PartNum = '12345-6789-0001'")
    assert env["retry_with"]["where"] == (
        "PartNum = '12345-6789-0001' and OrderDate >= '2025-07-27'")


def test_without_a_real_date_column_it_names_a_placeholder_not_a_guess():
    env = _refusal(date_columns=[])
    assert env["retry_with"]["where"] == "<a OrderHed condition>"
    assert "any condition on OrderHed" in env["message"]


def test_valid_serves_every_bounding_date_column():
    env = _refusal()
    assert env["valid"]["bounding_date_columns"] == [
        "ChangeDate", "NeedByDate", "OrderDate"]


def test_message_names_all_three_escape_paths():
    msg = _refusal()["message"]
    assert "(1)" in msg and "(2)" in msg and "(3)" in msg
    assert "epicor_baq" in msg
    assert "TRUNCATED" in msg


def test_server_side_alternative_is_a_composable_baq_create():
    env = _refusal(baq_tables="Erp.OrderHed,Erp.OrderDtl")
    alt = env["server_side_alternative"]
    assert alt["tool"] == "epicor_baq" and alt["action"] == "create"
    assert alt["tables"] == "Erp.OrderHed,Erp.OrderDtl"
    assert alt["fields"] == "CustNum, sum(DocOrderAmt) as revenue"


def test_a_date_bucketed_rollup_never_promises_a_baq():
    """The BAQ composer groups by RAW columns only — no month()/quarter().
    Offering a server-side GROUP BY that cannot be composed just costs another
    failed hop."""
    env = _refusal(group_by="month(OrderDate), CustNum")
    assert "server_side_alternative" not in env
    assert "epicor_baq" not in env["message"]
    # Still refuses, and still hands back a bounded retry.
    assert env["error"] == "rollup_scan_unbounded"
    assert env["retry_with"]["group_by"] == "month(OrderDate), CustNum"


def test_a_grand_total_count_is_sent_to_count_only_not_to_a_date_window():
    """`aggregate="count(*)"` with no group_by is not a rollup question at all
    — it is a $count. Answering it with "narrow to 12 months" would answer a
    DIFFERENT question; letting it run would report the page ceiling as the
    row count."""
    env = _refusal(group_by="", aggregate="count(*)")
    assert env["error"] == "rollup_scan_unbounded"
    assert env["retry_with"] == {
        "target": "Erp.BO.SalesOrderSvc/OrderHed", "count_only": True}
    assert "count_only=true" in env["message"]
    assert "OrderDate" not in env["message"]


def test_a_grand_total_count_keeps_a_caller_where_in_the_retry():
    env = _refusal(group_by="", aggregate="count(*)", where="OpenOrder = true")
    assert env["retry_with"]["where"] == "OpenOrder = true"
    assert env["retry_with"]["count_only"] is True


def test_a_grouped_count_is_still_a_real_rollup():
    """count(*) BY something is a legitimate rollup — it must get the normal
    bounding advice, not the $count redirect."""
    env = _refusal(group_by="CustNum", aggregate="count(*)")
    assert "count_only" not in env["retry_with"]
    assert env["retry_with"]["group_by"] == "CustNum"


def test_a_sum_is_never_mistaken_for_a_count():
    env = _refusal(group_by="", aggregate="sum(DocOrderAmt) as revenue")
    assert "count_only" not in env["retry_with"]


def test_an_arithmetic_measure_never_promises_a_baq_either():
    """`sum(A * B)` only the in-process evaluator understands — the BAQ
    composer takes `fn([Table].[Col])`."""
    env = _refusal(aggregate="sum(OrderQty * UnitPrice) as V")
    assert "server_side_alternative" not in env
    assert "epicor_baq" not in env["message"]


def test_best_date_column_ranks_transaction_dates_first():
    assert best_date_column(["ChangeDate", "OrderDate", "DueDate"]) == "OrderDate"
    assert best_date_column(["ChangeDate", "DueDate"]) == "DueDate"
    assert best_date_column(["SysRevID", "Company"]) == ""
    assert best_date_column([]) == ""
    # Real casing is preserved — the suggested `where` has to be executable.
    assert best_date_column(["invoicedate"]) == "invoicedate"


# =========================================================================== #
# End to end through the REGISTERED epicor_read, with a mock client.
# =========================================================================== #

APINVHED_FIELDS = [
    {"field_name": "InvoiceNum", "field_type": "Edm.String"},
    {"field_name": "VendorNum", "field_type": "Edm.Int32"},
    {"field_name": "GroupID", "field_type": "Edm.String"},
    {"field_name": "DocInvoiceAmt", "field_type": "Edm.Decimal"},
    {"field_name": "InvoiceDate", "field_type": "Edm.DateTimeOffset"},
    # A '%Date' column that is NOT an Edm date — the guard must not offer it.
    {"field_name": "DueDate", "field_type": "Edm.String"},
]

CUSTOMER_FIELDS = [
    {"field_name": "CustNum", "field_type": "Edm.Int32"},
    {"field_name": "CustID", "field_type": "Edm.String"},
    {"field_name": "Territory", "field_type": "Edm.String"},
    {"field_name": "CreditLimit", "field_type": "Edm.Decimal"},
]

ORDERHED_FIELDS = [
    {"field_name": "OrderNum", "field_type": "Edm.Int32"},
    {"field_name": "CustNum", "field_type": "Edm.Int32"},
    {"field_name": "OrderDate", "field_type": "Edm.DateTimeOffset"},
]

ORDERDTL_FIELDS = [
    {"field_name": "OrderNum", "field_type": "Edm.Int32"},
    {"field_name": "PartNum", "field_type": "Edm.String"},
    {"field_name": "DocExtPrice", "field_type": "Edm.Decimal"},
]

_FIELDS = {
    ("Erp.BO.APInvoiceSvc", "APInvHed"): APINVHED_FIELDS,
    ("Erp.BO.CustomerSvc", "Customer"): CUSTOMER_FIELDS,
    ("Erp.BO.SalesOrderSvc", "OrderHed"): ORDERHED_FIELDS,
    ("Erp.BO.SalesOrderSvc", "OrderDtl"): ORDERDTL_FIELDS,
}
_SETS = {
    "Erp.BO.APInvoiceSvc": ["APInvHed", "APInvHeds"],
    "Erp.BO.CustomerSvc": ["Customer", "Customers"],
    "Erp.BO.SalesOrderSvc": ["OrderHed", "OrderHeds", "OrderDtl"],
}
_HOSTS = {
    "apinvhed": [{"service_id": "Erp.BO.APInvoiceSvc",
                  "entity_set_name": "APInvHed"}],
    "customer": [{"service_id": "Erp.BO.CustomerSvc",
                  "entity_set_name": "Customer"}],
    "orderhed": [{"service_id": "Erp.BO.SalesOrderSvc",
                  "entity_set_name": "OrderHed"}],
    "orderdtl": [{"service_id": "Erp.BO.SalesOrderSvc",
                  "entity_set_name": "OrderDtl"}],
}


class _Idx:
    def get_fields(self, service, entity_set):
        return _FIELDS.get((service, entity_set), [])

    def get_field_types(self, service, entity_set):
        return {r["field_name"]: r.get("field_type") or ""
                for r in self.get_fields(service, entity_set)}

    def get_entity_sets(self, service):
        return _SETS.get(service, [])

    def services_for_entity(self, entity_set):
        return _HOSTS.get(entity_set.strip().lower(), [])

    def search_services(self, raw, limit=5):
        return []

    def find_field_owners(self, name, limit=6):
        return []


class _RBAC:
    def check_access(self, user_id, service_id):
        return (True, "")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key="K")


class _Client:
    def __init__(self):
        self.gets: list = []
        self.posts: list = []

    async def get(self, url, api_key, params=None):
        self.gets.append((url, dict(params or {})))
        return {"value": []}

    async def post(self, url, api_key, json_body=None):
        self.posts.append((url, dict(json_body or {})))
        return {"returnObj": {}}

    @property
    def calls(self) -> int:
        return len(self.gets) + len(self.posts)


class _Srv:
    def __init__(self):
        self.fn = None

    def tool(self, **kwargs):
        def deco(fn):
            self.fn = fn
            return fn
        return deco


@pytest.fixture()
def read_tool(monkeypatch):
    """The REGISTERED epicor_read plus the mock client it talks to."""
    from epicor_mcp.tools import query_with_children as qwc_mod
    from epicor_mcp.tools.query import _DATE_COLS_CACHE
    from epicor_mcp.tools.read import _SEARCH_SVC_CACHE
    _DATE_COLS_CACHE.clear()
    _SEARCH_SVC_CACHE.clear()
    session = lambda: types.SimpleNamespace(user_id="tester")  # noqa: E731
    monkeypatch.setattr(read_mod, "get_current_session", session)
    # The join engine reads the session from its OWN module.
    monkeypatch.setattr(qwc_mod, "get_current_session", session)
    srv, client = _Srv(), _Client()
    read_mod.register(srv, _Idx(), _RBAC(), client)

    def call(**kw):
        return client, json.loads(asyncio.run(srv.fn(**kw)))

    return call


def test_e2e_unbounded_rollup_refuses_before_touching_the_wire(read_tool):
    client, out = read_tool(
        target="APInvHed", group_by="VendorNum",
        aggregate="sum(DocInvoiceAmt) as Total")

    assert out["error"] == "rollup_scan_unbounded"
    # The whole point: ~300ms, zero HTTP, no 20-page scan.
    assert client.calls == 0, "the guard let the scan start anyway"
    # A real Edm date column, never the Edm.String 'DueDate' lookalike.
    assert out["valid"]["bounding_date_columns"] == ["InvoiceDate"]
    assert out["retry_with"]["where"].startswith("InvoiceDate >= '")


def test_e2e_the_same_rollup_with_a_bounding_where_still_runs(read_tool):
    client, out = read_tool(
        target="APInvHed", where="GroupID = 'BATCH-100'",
        group_by="VendorNum", aggregate="sum(DocInvoiceAmt) as Total")

    assert "error" not in out, out
    assert client.calls > 0, "a bounded rollup must still reach Epicor"


def test_e2e_a_small_entity_rollup_is_untouched(read_tool):
    """Customers-by-territory is one page and must stay a one-call answer."""
    client, out = read_tool(
        target="Customer", group_by="Territory", aggregate="count(*) as n")

    assert "error" not in out, out
    assert client.calls > 0


def test_e2e_the_refusals_own_retry_runs(read_tool):
    """INV-1 end to end: replay retry_with verbatim and the call succeeds."""
    client, refused = read_tool(
        target="APInvHed", group_by="VendorNum",
        aggregate="sum(DocInvoiceAmt) as Total")
    assert refused["error"] == "rollup_scan_unbounded"

    retry = refused["retry_with"]
    client2, out = read_tool(**retry)
    assert "error" not in out, out
    assert client2.calls > 0


def test_e2e_the_slow_rollup_shape_is_refused_on_the_join_path(read_tool):
    """The canonical slow rollup. `OrderDtl` auto-joins to `OrderHed`, so the
    scanned table is the HEADER — that is what the guard must measure."""
    client, out = read_tool(
        target="OrderDtl", group_by="PartNum",
        aggregate="sum(DocExtPrice) as revenue", limit=1000)

    assert out["error"] == "rollup_scan_unbounded"
    assert client.calls == 0
    assert "OrderHed" in out["message"]
    assert out["retry_with"]["where"].startswith("OrderDate >= '")
    assert out["server_side_alternative"]["tables"] == "Erp.OrderHed,Erp.OrderDtl"


def test_e2e_a_child_only_where_does_not_count_as_bounding(read_tool):
    """`PartNum = 'X'` lands on OrderDtl; every OrderHed page is still paged."""
    client, out = read_tool(
        target="OrderDtl", where="PartNum = '12345-6789-0001'",
        group_by="PartNum", aggregate="sum(DocExtPrice) as revenue")

    assert out["error"] == "rollup_scan_unbounded"
    assert client.calls == 0
    assert out["retry_with"]["where"] == (
        "PartNum = '12345-6789-0001' and "
        + out["retry_with"]["where"].split(" and ", 1)[1])
    assert "OrderDate >= '" in out["retry_with"]["where"]


def test_e2e_a_parent_bounded_join_rollup_still_runs(read_tool):
    client, out = read_tool(
        target="OrderDtl", where="OrderDate >= '2026-01-01'",
        group_by="PartNum", aggregate="sum(DocExtPrice) as revenue")

    assert out.get("error") != "rollup_scan_unbounded", out
    assert client.calls > 0


def test_e2e_a_grand_total_count_redirect_is_runnable(read_tool):
    client, refused = read_tool(target="APInvHed", aggregate="count(*)")
    assert refused["error"] == "rollup_scan_unbounded"
    assert client.calls == 0

    client2, out = read_tool(**refused["retry_with"])
    assert "error" not in out, out
    assert client2.calls > 0


def test_e2e_a_plain_bounded_listing_never_sees_the_guard(read_tool):
    """No group_by/aggregate at all — the guard must be invisible."""
    client, out = read_tool(target="APInvHed", limit=5)
    assert "error" not in out, out
    assert client.calls > 0

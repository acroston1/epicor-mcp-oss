"""`epicor_fields` multi-concept ranking + compact output.

Callers send a comma list of concepts in one ``query`` ("invoice number, legal
number, customer, date, sales order"). Ranked as ONE string, every concept blurs
into the others and short canonical columns (``InvoiceNum``, ``Name``,
``TranDate``) fall out of the top ``limit``. The fix: split into terms, rank
each on its own, merge round-robin — in the substring-only mode AND the
semantic mode.

The invariant this file pins hardest: a query with NO separator takes the
original single-query path unchanged.
"""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import numpy as np
import pytest

from epicor_mcp.discovery import rank
from epicor_mcp.discovery.store import DiscoveryIndex, FieldHit
from epicor_mcp.discovery.tools import register_discovery_tools

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# split_terms
# --------------------------------------------------------------------------- #
def test_a_query_without_separators_is_one_term_returned_verbatim():
    for q in ("on hand quantity", "  vendor and order date ", "site/plant", ""):
        assert rank.split_terms(q) == [q]


def test_commas_semicolons_and_newlines_separate_terms():
    assert rank.split_terms("PO line, part number; due date\njob") == [
        "PO line", "part number", "due date", "job",
    ]


def test_a_slash_joins_synonyms_into_ONE_term():
    assert rank.split_terms("warehouse, plant/site") == ["warehouse", "plant/site"]


def test_terms_drop_empties_leading_conjunctions_and_duplicates():
    assert rank.split_terms("unit cost, , and due date, Unit Cost") == [
        "unit cost", "due date",
    ]


def test_terms_are_capped():
    q = ", ".join(f"term{i}" for i in range(50))
    assert len(rank.split_terms(q)) == rank.MAX_TERMS


# --------------------------------------------------------------------------- #
# per-term name terms
# --------------------------------------------------------------------------- #
def test_term_name_match_keeps_the_tables_word_when_it_is_part_of_the_column():
    """Stripping "invoice" from "invoice number" on InvcHead would leave
    {number}, which scores InvoiceNum and LegalNumber the same."""
    assert rank.term_name_match("invoice number", "InvcHead", "InvoiceNum") == 1.0
    assert rank.term_name_match("invoice number", "InvcHead", "LegalNumber") < 1.0
    assert rank.exact_name_match("check date", "CheckHed", "CheckDate")
    assert rank.exact_name_match("transaction date", "PartTran", "TranDate")
    assert rank.exact_name_match("part number", "PartTran", "PartNum")


def test_term_name_match_strips_the_tables_word_when_the_column_does_not_carry_it():
    assert rank.exact_name_match("vendor name", "Vendor", "Name")
    assert rank.exact_name_match("vendor id", "Vendor", "VendorID")


def test_a_term_that_only_names_the_table_asks_for_its_identity_columns():
    assert rank.term_name_match("vendor", "Vendor", "Name") == 1.0
    assert rank.term_name_match("vendor", "Vendor", "VendorID") == 1.0
    assert rank.term_name_match("customer", "Customer", "CustID") == 1.0
    # the key is already in `primary_key`; it is not the identity answer
    assert rank.term_name_match("vendor", "Vendor", "VendorNum") < 1.0
    assert rank.term_name_match("vendor", "Vendor", "EInvoice") == 0.0


def test_locale_families_are_penalised_unless_asked_for():
    for field in ("CPayInvoiceBal", "CColOrderNum", "GlbVendorNum", "TWGUIRegNumBuyer",
                  "THRefVendorNum", "PEAPPayNum", "AGLegalNumber", "EInvExternalID",
                  "CarbonExtCost", "DEOrgType", "OTSName"):
        assert rank.locale_penalty("invoice balance", field) == 1.0, field
    for field in ("InvoiceBal", "PartNum", "PONum", "Plant", "DueDate", "TranDate",
                  "CheckNum", "PackSlip", "ExtCost"):
        assert rank.locale_penalty("invoice balance", field) == 0.0, field
    assert rank.locale_penalty("central payment balance", "CPayInvoiceBal") == 0.0
    assert rank.locale_penalty("co2 cost", "CarbonExtCost") == 0.0


def test_the_default_fusion_is_the_original_formula_exactly():
    """per_term=False must be byte-identical to the original formula, so a
    single-term query ranks exactly as before."""
    random.seed(7)
    fields = ["OnHandQty", "Rpt1OnHandQty", "PartNum", "InvoiceNum", "CPayInvoiceBal",
              "Name", "VendorID", "SysRevID", "DueDate"]
    types = ["decimal", "decimal", "nvarchar", "int", "decimal", "nvarchar",
             "nvarchar", "bigint", "date"]
    meta = {f"T.{f}": ("Vendor", f, t) for f, t in zip(fields, types)}
    for q in ("on hand quantity", "vendor name", "invoice balance due date"):
        dense = [(k, random.random()) for k in meta]
        lex = [(k, 1.0) for k in random.sample(list(meta), 4)]
        lex_credit = {k: 10.0 / (10 + i) for i, (k, _) in enumerate(lex)}
        want = sorted(
            (
                (k, s
                 + 0.05 * rank.name_match_score(q, "Vendor", meta[k][1], scoped=True)
                 + 0.01 * lex_credit.get(k, 0.0)
                 + 0.02 * rank.type_affinity(q, meta[k][2])
                 - 0.10 * rank.column_prior_penalty(q, meta[k][1]))
                for k, s in dense
            ),
            key=lambda kv: -kv[1],
        )
        assert rank.fuse(dense, lex, q, meta) == want


# --------------------------------------------------------------------------- #
# DiscoveryIndex.search_fields_terms (the merge)
# --------------------------------------------------------------------------- #
def _hit(name: str) -> FieldHit:
    return FieldHit(table="T", field=name, sql_type="int", description="", label="", score=0.0)


class _Ranked:
    """A stand-in `self` whose search_fields returns a canned list per term."""

    def __init__(self, per_term: dict[str, list[str]]) -> None:
        self.per_term = per_term
        self.calls: list[tuple] = []

    def search_fields(self, table, vec, query, *, limit=15, exclude=(), per_term=False):
        self.calls.append((table, vec, query, limit, tuple(exclude), per_term))
        return [_hit(n) for n in self.per_term.get(query, [])][:limit]


def test_one_term_is_exactly_the_single_query_call():
    me = _Ranked({"on hand quantity": ["OnHandQty", "Qty"]})
    out = DiscoveryIndex.search_fields_terms(
        me, "PartWhse", [("on hand quantity", "VEC")], limit=7
    )
    assert [h.field for h in out] == ["OnHandQty", "Qty"]
    assert me.calls == [("PartWhse", "VEC", "on hand quantity", 7, (), False)]


def test_several_terms_merge_round_robin_deduplicated_to_the_limit():
    me = _Ranked({
        "a": ["A1", "Shared", "A3"],
        "b": ["B1", "B2"],
        "c": ["Shared", "C2", "C3"],
    })
    out = DiscoveryIndex.search_fields_terms(
        me, "T", [("a", 1), ("b", 2), ("c", 3)], limit=6
    )
    assert [h.field for h in out] == ["A1", "B1", "Shared", "B2", "C2", "A3"]
    assert all(c[5] is True for c in me.calls), "each term must rank with per_term=True"


# --------------------------------------------------------------------------- #
# the registered tool
# --------------------------------------------------------------------------- #
class _Index:
    manifest = {"table_count": 1, "field_count": 3}

    def __init__(self) -> None:
        self.single: list[tuple] = []
        self.multi: list[tuple] = []

    def resolve_table(self, name):
        return "Vendor" if str(name).lower() in ("vendor", "erp.vendor") else None

    def table_info(self, canon):
        return {"full_name": "Erp.Vendor", "field_count": 3, "description": ""}

    def fields_of(self, table):
        return []

    def find_column_elsewhere(self, core, **kw):
        return [], 0

    def name_matches_elsewhere(self, q, here, **kw):
        self.elsewhere_q = q
        return [("APInvHed", "TWGUIRegNumBuyer")]

    def _hits(self):
        return [
            FieldHit("Vendor", "Name", "nvarchar", "Name", "Name", 0.9, required=True),
            FieldHit("Vendor", "VendorID", "nvarchar",
                     "User-assigned supplier identifier. " * 10, "Supplier ID", 0.8),
            FieldHit("Vendor", "PhoneNum", "nvarchar", "", "Phone", 0.7),
        ]

    def search_fields(self, table, vec, query, limit=15):
        self.single.append((table, vec, query, limit))
        return self._hits()[:limit]

    def search_fields_terms(self, table, terms, *, limit=15):
        self.multi.append((table, list(terms), limit))
        return self._hits()[:limit]


def _tool(index):
    tools: dict = {}

    class _MCP:
        def tool(self, *, name, description):
            def deco(fn):
                tools[name] = fn
                return fn
            return deco

    embedded: list[str] = []

    async def embed(text, prefix):
        embedded.append(text)
        return f"vec:{text}"

    register_discovery_tools(_MCP(), index, embed_query=embed)
    return tools["epicor_fields"], embedded


async def test_a_single_term_query_takes_the_original_path():
    ix = _Index()
    fields, embedded = _tool(ix)
    await fields(table="Vendor", query="vendor name and phone", limit=5)
    assert ix.multi == []
    assert ix.single == [("Vendor", "vec:vendor name and phone", "vendor name and phone", 30)]
    assert embedded == ["vendor name and phone"]


async def test_a_comma_list_is_embedded_per_term_and_ranked_per_term():
    ix = _Index()
    fields, embedded = _tool(ix)
    await fields(table="Vendor", query="vendor id, name, phone", limit=15)
    assert ix.single == []
    assert sorted(embedded) == ["name", "phone", "vendor id"]
    (table, terms, limit), = ix.multi
    assert terms == [("vendor id", "vec:vendor id"), ("name", "vec:name"),
                     ("phone", "vec:phone")]


async def test_the_limit_is_raised_to_one_slot_per_term():
    ix = _Index()
    fields, _ = _tool(ix)
    await fields(table="Vendor", query="a, b, c, d, e", limit=2)
    assert ix.multi[0][2] == 5 + 25


async def test_field_entries_are_compact():
    ix = _Index()
    fields, _ = _tool(ix)
    resp = await fields(table="Vendor", query="vendor id, name, phone")
    by = {f["name"]: f for f in resp["tables"][0]["fields"]}
    for f in by.values():
        assert "primary_key" not in f and "required" not in f
        assert set(f) <= {"name", "type", "label", "description"}
    assert "description" not in by["Name"], "a description that restates the name is noise"
    assert "description" not in by["PhoneNum"]
    assert len(by["VendorID"]["description"]) <= 100
    assert by["VendorID"]["description"].endswith("…")


async def test_other_table_name_matches_only_for_terms_nothing_here_answers():
    ix = _Index()
    fields, _ = _tool(ix)
    resp = await fields(table="Vendor", query="vendor id, name, phone")
    assert "elsewhere" not in resp, "every term was answered by a column served here"

    resp = await fields(table="Vendor", query="vendor id, buyer registration")
    assert ix.elsewhere_q == "buyer registration"
    assert "APInvHed.TWGUIRegNumBuyer" in repr(resp["elsewhere"])


# --------------------------------------------------------------------------- #
# End to end on a synthetic index — substring-only AND semantic
# --------------------------------------------------------------------------- #
def _f(name, type_, label="", description=""):
    return {"name": name, "type": type_, "label": label, "description": description}


#: Synthetic, Epicor-shaped columns. The distractors are what made the single
#: blurred ranking miss: localisation copies of the canonical column, and
#: descriptions that mention every concept in the list.
CATALOGUE = {"tables": {
    "Vendor": {"schema": "Erp", "full_name": "Erp.Vendor", "description": "Supplier master",
               "fields": [
                   _f("VendorNum", "int", "Supplier Number", "Internal supplier number"),
                   _f("VendorID", "nvarchar", "Supplier ID", "User-assigned supplier id"),
                   _f("Name", "nvarchar", "", ""),
                   _f("EInvExternalID", "nvarchar", "", "Electronic invoice id for the supplier name"),
                   _f("GlbVendorNum", "int", "", "Global supplier number"),
                   _f("PhoneNum", "nvarchar", "Phone", ""),
                   _f("TermsCode", "nvarchar", "Terms", "Payment terms used on invoices and PO"),
               ]},
    "InvcHead": {"schema": "Erp", "full_name": "Erp.InvcHead", "description": "AR invoice header",
                 "fields": [
                     _f("InvoiceNum", "int", "Invoice", "Invoice number"),
                     _f("LegalNumber", "nvarchar", "Legal Number", "Legal invoice number"),
                     _f("CColLegalNumber", "nvarchar", "Parent Legal Number", "Legal number of the parent invoice"),
                     _f("CColOrderNum", "int", "Child Sales Order", "Child sales order number"),
                     _f("OrderNum", "int", "Order", "Sales order number"),
                     _f("CustNum", "int", "Customer", "Customer number"),
                     _f("InvoiceDate", "date", "Inv Date", "Invoice date"),
                     _f("AGLegalNumber", "nvarchar", "Legal Number", "Argentina legal invoice number"),
                 ]},
    "PartTran": {"schema": "Erp", "full_name": "Erp.PartTran", "description": "Part transaction history",
                 "fields": [
                     _f("TranNum", "int", "Tran Num", "Transaction number"),
                     _f("TranDate", "date", "Date", "Transaction date"),
                     _f("TranType", "nvarchar", "Type", "Transaction type"),
                     _f("PartNum", "nvarchar", "Part", "Part number"),
                     _f("Plant", "nvarchar", "Site", "Site"),
                     _f("WareHouseCode", "nvarchar", "Whse", "Warehouse"),
                     _f("PlantTranNum", "int", "SiteTranNum", "Site transaction number of the part transaction"),
                     _f("JobSeqType", "nvarchar", "Type", "Transaction type of the job sequence part"),
                     _f("BinType", "nvarchar", "Bin Type", "Warehouse bin type for the transaction"),
                 ]},
}}

SINGLE_TERM_ONLY = [
    ("Vendor", "vendor id name, phone, terms", {"VendorID", "Name"}),
    ("InvcHead", "invoice number, legal number, customer, date, sales order",
     {"InvoiceNum", "LegalNumber", "CustNum", "OrderNum"}),
    ("PartTran", "transaction date, transaction type, part number, plant/site, warehouse",
     {"TranDate", "TranType", "PartNum", "Plant"}),
]


def _builder():
    spec = importlib.util.spec_from_file_location(
        "discovery_builder", REPO / "scripts/build_discovery_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VOCAB = ["invoice", "legal", "customer", "order", "date", "transaction", "type", "part",
         "site", "warehouse", "supplier", "vendor", "name", "number", "phone", "terms"]


def _vec(text: str) -> np.ndarray:
    low = text.lower()
    raw = np.asarray([float(w in low) for w in VOCAB] + [0.3], dtype=np.float32)
    return raw / np.linalg.norm(raw)


class _Embedder:
    provider, model = "local", "synthetic-model"

    def encode(self, texts, **kwargs):
        return np.stack([_vec(t) for t in texts])


def _index(tmp_path, *, vectors: bool) -> DiscoveryIndex:
    out = tmp_path / ("sem" if vectors else "lex")
    _builder().build_discovery_index(CATALOGUE, out, _Embedder() if vectors else None)
    return DiscoveryIndex.load(out)


@pytest.mark.parametrize("vectors", [False, True], ids=["substring", "semantic"])
@pytest.mark.parametrize("table, query, must", SINGLE_TERM_ONLY)
def test_every_named_concept_gets_its_canonical_column(tmp_path, vectors, table, query, must):
    ix = _index(tmp_path, vectors=vectors)
    terms = rank.split_terms(query)
    vec_terms = [(t, _vec(t) if vectors else None) for t in terms]
    got = [h.field for h in ix.search_fields_terms(table, vec_terms, limit=len(terms) * 2)]
    assert must <= set(got), f"missing {must - set(got)} from {got}"
    assert not {"CColLegalNumber", "CColOrderNum", "AGLegalNumber", "EInvExternalID",
                "GlbVendorNum"} & set(got[: len(must)]), got


def test_substring_mode_single_term_is_unchanged(tmp_path):
    """A one-term query in substring mode is still the plain substring ranking."""
    ix = _index(tmp_path, vectors=False)
    for table, query in (("PartTran", "transaction date"), ("Vendor", "supplier")):
        want = [k.split(".", 1)[1] for k, _ in ix._lex_fields(table, query, 100)]
        got = [h.field for h in ix.search_fields_terms(table, [(query, None)], limit=100)]
        assert got == want
